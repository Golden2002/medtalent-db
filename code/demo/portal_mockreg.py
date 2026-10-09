# -*- coding: utf-8 -*-
"""
mock 小程序注册窗口（开发者模式进程 :8083 的可写页面）

需求原文：「建立一个 mock 窗口，模仿小程序的用户注册页面，我要测试数据能够真实地加入和删除」

## 这一页要证明什么（以及不证明什么）
用户要的是"**真的**加进去了、**真的**删掉了"。所以这一页的价值不在于表单长什么样，
而在于**每次操作都给出可独立复核的证据**：
  L1 回显   —— 包里发了什么（最弱：只证明请求构造对了）
  L2 读回   —— 用 mt.profile_json() 把落库结果读回来，与提交内容逐项比对
  L3 独立复核 —— **另开一条连接**（不是处理请求的那条）做精确 count(*)、
                孤儿行检测、以及"无关的表有没有被误动"
只有 L3 能回答"真的进去了吗"，因为它是**换一个视角**去看同一个库。

## 写路径：走 bridge，不直写 person
`exchange.ingest()` 是我们和小程序之间的契约实现，它包含六条规则：
身份扫描（整包拒绝带 openid/手机号）、幂等（同一 eventId 重放不产生第二个人）、
版本门（aggregateVersion 不得倒退）、墓碑（删过的人不接受迟到写入）、
授权门（没有个人分析授权不得写分析产物）、crosswalk（映射不上不臆造概念）。
**绕过它直写 person 表就等于绕过了全部六条** —— 那样测试通过也说明不了小程序那条路是通的。

## 身份与清理的命名空间
  · 来源系统固定为 `regmock`；
  · 外部 personId 形如 `regmock_xxxxxx`；
  · 清理**按 source_system 精确匹配**，绝不用 `LIKE '前缀%'` 去猜 ——
    实测教训：`'per_mockreg_x' LIKE 'per_mock_%'` 为 **true**（`_` 是单字符通配符），
    用 LIKE 清理合成档案会连带删掉别人的数据。

## 诚实的边界
  · 这是**模拟**小程序：真正的微信登录、手机号验证不在本机，也不该在本机（本库整包拒绝这些标识）。
  · 删除分三档（见页面），其中"注销"是**异步受理**：受理 ≠ 完成。
    这是合规要求的真实形状（下游系统可能还没确认），不能为了好看把它写成"已删除"。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, os.path.join(BASE, "code"), os.path.join(BASE, "code", "bridge")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg                                       # noqa: E402
from psycopg.rows import dict_row                    # noqa: E402

import portal as P                                   # noqa: E402
import exchange as EX                                # noqa: E402

SRC = "regmock"          # 来源系统：清理按它精确匹配
EXT_PREFIX = "regmock_"  # 外部 personId 前缀
NOTICE_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# 表单选项：一律来自固定字典（项目纪律：前端选项必须来自字典，不能写死中文）
# ---------------------------------------------------------------------------
def opts(c, code_table_id, limit=None):
    rows = P.q(c, """SELECT code, label_zh FROM mt.code_value
                      WHERE code_table_id=%s AND NOT deprecated ORDER BY sort_order""",
               (code_table_id,))
    return rows[:limit] if limit else rows


def occupation_opts(c, limit=15):
    """目标职业：只列**有岗位数据**的职业，否则选了也匹配不出东西。
    列名用 label_zh（不是 title）—— 本库的职业表存的是 cn_occode/isco08_code/label_zh。"""
    return P.q(c, """SELECT o.occupation_id AS code, o.label_zh
                       FROM mt.occupation o
                      WHERE EXISTS (SELECT 1 FROM mt.job_posting j
                                     WHERE j.occupation_id = o.occupation_id)
                      ORDER BY o.occupation_id LIMIT %s""", (limit,))


def sel(name, rows, selected="", blank="（不填）"):
    o = ['<option value="">%s</option>' % P.esc(blank)]
    for r in rows:
        o.append('<option value="%s"%s>%s</option>'
                 % (P.esc(r["code"]), " selected" if r["code"] == selected else "",
                    P.esc("%s · %s" % (r["code"], r["label_zh"]))))
    return '<select name="%s">%s</select>' % (name, "".join(o))


# ---------------------------------------------------------------------------
# 独立复核（L3）：另开连接的精确计数 + 孤儿检测
# ---------------------------------------------------------------------------
COUNTED = ["person", "external_identity", "tombstone", "talent_deletion_request",
           "field_value", "consent_record", "experience_episode", "response_session", "answer"]


def snapshot():
    """用**独立连接**取一份精确计数（不是处理请求的那条连接）。"""
    with psycopg.connect(P.DSN, row_factory=dict_row) as c:
        out = {t: P.q1(c, "SELECT count(*) FROM mt.%s" % t) for t in COUNTED}
    return out


def orphan_check():
    """孤儿行检测 —— 这是"真删"与"假删"最有力的区分判据。

    两条都要查：
      ① 动态发现所有指向 person 的外键子表，逐个 LEFT JOIN 反查；
      ② **field_value.subject_id 没有外键**（实测确认）—— 直接 DELETE person 会成功，
         而值原样留着、不报任何错。所以它必须单独查，不能指望外键保护。
    """
    with psycopg.connect(P.DSN, row_factory=dict_row) as c:
        kids = P.q(c, """
            SELECT cl.relname AS t, a.attname AS col
              FROM pg_constraint con
              JOIN pg_class cl ON cl.oid = con.conrelid
              JOIN pg_namespace n ON n.oid = cl.relnamespace
              JOIN pg_class cl2 ON cl2.oid = con.confrelid
              JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
             WHERE con.contype='f' AND n.nspname='mt' AND cl2.relname='person'""")
        bad = []
        for k in kids:
            n = P.q1(c, 'SELECT count(*) FROM mt.%s ch LEFT JOIN mt.person p '
                        'ON p.person_id = ch.%s WHERE ch.%s IS NOT NULL '
                        'AND p.person_id IS NULL' % (k["t"], k["col"], k["col"]))
            if n:
                bad.append("%s.%s 悬空 %d 行" % (k["t"], k["col"], n))
        # ② 无外键的那根裸 TEXT
        nfv = P.q1(c, """SELECT count(*) FROM mt.field_value fv
                          WHERE fv.subject_id LIKE 'per_%'
                            AND NOT EXISTS (SELECT 1 FROM mt.person p
                                             WHERE p.person_id = fv.subject_id)""")
        if nfv:
            bad.append("field_value.subject_id 悬空 %d 行（该列没有外键）" % nfv)
    return {"n_fk_children": len(kids), "problems": bad}


# ---------------------------------------------------------------------------
# 构造交换包
# ---------------------------------------------------------------------------
def build_package(c, qs):
    def one(k, d=""):
        return (qs.get(k, [""])[0] or d).strip()

    ext = one("ext_id")
    if not ext:
        ext = EXT_PREFIX + hashlib.sha1(
            ("%f" % time.time()).encode()).hexdigest()[:8]
    if not ext.startswith(EXT_PREFIX):        # 命名空间纪律：不允许外人乱填前缀
        ext = EXT_PREFIX + ext

    # 版本：默认取该人已处理版本 +1，让"版本门"自然生效而不是靠用户猜
    with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
        row = cc.execute("""SELECT last_seen_version FROM mt.external_identity
                             WHERE source_system=%s AND external_person_id=%s""",
                         (SRC, ext)).fetchone()
    auto_ver = (row["last_seen_version"] if row else 0) + 1
    ver = int(one("version") or auto_ver)

    facts = []
    stage, degree = one("stage"), one("degree")
    city = one("city")
    if stage:
        facts.append({"fieldId": "current_stage", "status": "answered",
                      "valueCodes": [stage]})
    if degree:
        facts.append({"fieldId": "education.degree_level", "instanceId": "education/edu_1",
                      "status": "answered", "valueCodes": [degree]})
    if city:
        facts.append({"fieldId": "current_city", "status": "answered", "valueCodes": [city]})
    if not facts:
        facts.append({"fieldId": "target_direction", "status": "prefer_not_to_say"})

    exp = []
    if one("org") or one("role"):
        exp.append({"instanceId": "experience/exp_%d" % int(time.time()),
                    "episodeType": one("ep_type", "work"),
                    "organization": one("org"), "roleTitle": one("role"),
                    "startYm": one("start_ym"), "endYm": one("end_ym"),
                    "isCurrent": one("is_current") == "1"})

    skills = [{"sourceQuestionId": "q_" + s, "label": lab}
              for s, lab in (("lit", "文献检索与证据分级"), ("team", "团队协作"),
                             ("data", "数据分析"), ("comm", "医患沟通"))
              if one("skill_" + s) == "1"]

    occupations = [x for x in qs.get("occupation", []) if x.strip()]

    pkg = {
        "schemaVersion": EX.SCHEMA_VERSION,
        "sourceSystem": SRC,
        "personId": ext,
        "eventId": "evt_" + hashlib.sha1(
            ("%s|%f" % (ext, time.time())).encode()).hexdigest()[:20],
        "eventType": "profile_upsert",
        "submissionId": "sub_" + hashlib.sha1(
            ("%s|%f" % (ext, time.time())).encode()).hexdigest()[:20],
        "requestId": "req_regmock_%d" % int(time.time()),
        "questionnaireId": "mock_onboarding",
        "questionnaireVersion": "1.0.0",
        "catalogId": "mock_1.0.0",
        "mappingVersion": "xw_1.0.0",
        "aggregateVersion": ver,
        "facts": facts,
        "education": [],
        "experience": exp,
        "skillClaims": skills,
        "preferences": {"targetOccupations": occupations, "industries": [],
                        "workModes": []},
        # 授权：**默认不勾**。不勾时包里显式声明 False，
        # 于是 bridge 的授权门会拒绝写入分析产物并报 CONSENT_REQUIRED ——
        # 那是"门在工作"的证据，不是 bug（页面会这么解释）。
        "consents": {
            "noticeVersion": NOTICE_VERSION,
            "personalAnalysis": one("consent_cp1") == "1",
            "opportunityNotifications": one("consent_cp2") == "1",
        },
    }
    if one("major"):
        pkg["facts"].append({"fieldId": "education.major_name",
                             "instanceId": "education/edu_1", "status": "answered",
                             "valueText": one("major")})
    return pkg, ext, ver


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def _readback(c, person_id):
    """L2：把落库结果读回来（用门户自己的 profile_json 口径，不另写一份查询）。"""
    try:
        return P.q1(c, "SELECT mt.profile_json('person', %s)", (person_id,))
    except psycopg.Error as e:
        return {"error": str(e).splitlines()[0]}


def _evidence_block(pkg, res, before, after, orph, person_id, c):
    """三层证据一起摆出来。**没有 L3 的"成功"不算成功。**"""
    rows = []
    for t in COUNTED:
        b, a = before.get(t), after.get(t)
        if b != a:
            rows.append('<tr><td><code>%s</code></td><td class="n">%s</td>'
                        '<td class="n">%s</td><td class="n">%+d</td></tr>'
                        % (t, f"{b:,}", f"{a:,}", a - b))
    delta = ("<table><thead><tr><th>表</th><th class='n'>写前</th><th class='n'>写后</th>"
             "<th class='n'>变化</th></tr></thead><tbody>%s</tbody></table>"
             % "".join(rows)) if rows else '<p class="muted">（没有表发生变化）</p>'
    same = all(before.get(t) == after.get(t) for t in COUNTED if t not in
               ("person", "external_identity", "response_session", "answer",
                "experience_episode", "consent_record"))
    rl = _readback(c, person_id) if person_id else {}
    return """
<div class="card"><h2>L3 独立复核 <span class="muted">· 另开一条连接做的精确计数（这才是"真的进去了吗"的答案）</span></h2>
%s
<p class="muted">逻辑删除：<code>field_value</code>／<code>tombstone</code> 等无关表
<b>%s</b>（这证明没有误伤别的数据）。</p>
<p>孤儿行检测：检查了 <b>%d</b> 张指向 person 的外键子表 + <code>field_value</code>
那根**没有外键**的裸 TEXT 列 → %s</p></div>
<div class="card"><h2>L2 读回 <span class="muted">· 用 mt.profile_json 读落库结果</span></h2>
<pre class="sqlbox">%s</pre></div>
<div class="card"><h2>L1 发出去的包 <span class="muted">· 最弱的一层，只证明请求构造对了</span></h2>
<pre class="sqlbox">%s</pre></div>
""" % (delta, "没有变化" if same else "<b>有变化</b>（需要检查是否误伤）",
       orph["n_fk_children"],
       ("全部干净" if not orph["problems"]
        else "<b>发现问题：%s</b>" % P.esc("；".join(orph["problems"]))),
       P.esc(json.dumps(rl, ensure_ascii=False, indent=2)[:2000]),
       P.esc(json.dumps(pkg, ensure_ascii=False, indent=2)[:2000]))


def view(c, qs, msg="", kind="info", result=None):
    deg = opts(c, "CT_DEGREE_LEVEL")
    city = opts(c, "CT_CITY", limit=24)
    occ = occupation_opts(c)
    with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
        regs = cc.execute("""SELECT ei.external_person_id, ei.person_id, ei.status,
                                    ei.last_seen_version, ei.linked_at,
                                    tb.deleted_through_version AS tomb,
                                    (SELECT count(*) FROM mt.consent_record cr
                                      WHERE cr.person_id = ei.person_id
                                        AND cr.purpose='CP1' AND cr.revoked_at IS NULL) AS cp1,
                                    (SELECT count(*) FROM mt.talent_deletion_request r
                                      WHERE r.person_id = ei.person_id) AS delreq
                               FROM mt.external_identity ei
                               LEFT JOIN mt.tombstone tb
                                      ON tb.person_id = ei.person_id AND tb.source_system = ei.source_system
                              WHERE ei.source_system = %s
                              ORDER BY ei.linked_at DESC LIMIT 12""", (SRC,)).fetchall()

    reg_html = "".join(
        '<tr><td><code>%s</code></td><td><code>%s</code></td><td>%s</td>'
        '<td class="n">%s</td><td class="n">%s</td><td>%s</td><td>%s</td></tr>'
        % (P.esc(r["external_person_id"]), P.esc(r["person_id"]), P.esc(r["status"]),
           r["last_seen_version"],
           ("是（至版本 %s）" % r["tomb"]) if r["tomb"] is not None else "否",
           "有" if r["cp1"] else "<b>无</b>",
           ("已受理 %d 条" % r["delreq"]) if r["delreq"] else "—")
        for r in regs) or '<tr><td colspan="7" class="muted">（本来源还没有注册记录）</td></tr>'

    body = """
<div class="sub">这是**模拟**小程序注册页：字段与选项来自本库的固定字典（<code>code_value</code>），
提交走<b>小程序接入适配器</b>（<code>exchange.ingest</code>）而不是直写 <code>person</code> 表 ——
直写就等于绕过它的六条契约规则（身份扫描／幂等／版本门／墓碑／授权门／crosswalk），
那样测试通过也说明不了小程序那条路是通的。</div>
%s
<div class="card"><h2>① 身份与学历 <span class="muted">· 编号类字段是公开等级（T0）</span></h2>
<form method="post" action="/mockreg">
<input type="hidden" name="act" value="submit">
<div class="row">
  <div style="flex:2 1 260px"><label>外部编号（可留空，自动生成 <code>%s</code> 开头）</label>
    <input name="ext_id" placeholder="regmock_ 开头"></div>
  <div style="flex:1 1 120px"><label>提交版本</label>
    <input name="version" placeholder="留空=自动+1"></div>
  <div style="flex:2 1 220px"><label>当前学历层次</label>%s</div>
  <div style="flex:2 1 220px"><label>专业名称</label>
    <input name="major" placeholder="如：临床医学"></div>
  <div style="flex:2 1 220px"><label>当前城市</label>%s</div>
</div>
<div class="card" style="margin:12px 0 0"><h2>② 一段经历 <span class="muted">· 可留空</span></h2>
<div class="row">
  <div style="flex:2 1 240px"><label>机构</label><input name="org"></div>
  <div style="flex:2 1 200px"><label>角色</label><input name="role"></div>
  <div style="flex:1 1 110px"><label>开始（YYYY-MM）</label><input name="start_ym" placeholder="2022-09"></div>
  <div style="flex:1 1 110px"><label>结束</label><input name="end_ym" placeholder="2025-06"></div>
  <div style="flex:0 0 90px"><label>在任</label><input type="checkbox" name="is_current" value="1"></div>
</div></div>
<div class="card" style="margin:12px 0 0"><h2>③ 求职期望</h2>
<div class="row">
  <div style="flex:2 1 300px"><label>目标职业（可多选，只列有岗位数据的）</label>
    <select name="occupation" multiple size="4">%s</select></div>
  <div style="flex:2 1 300px"><label>自述技能（未核验，本库记为"自述"而不是"具备"）</label>
    <label><input type="checkbox" name="skill_lit" value="1"> 文献检索与证据分级</label>
    <label><input type="checkbox" name="skill_team" value="1"> 团队协作</label>
    <label><input type="checkbox" name="skill_data" value="1"> 数据分析</label>
    <label><input type="checkbox" name="skill_comm" value="1"> 医患沟通</label></div>
</div></div>
<div class="card" style="margin:12px 0 0"><h2>④ 授权 <span class="muted">· 单独一步，默认不勾</span></h2>
<p>这一步是**真的门**，不是装饰：不勾 <b>个人分析授权</b> 而包里带分析事实时，
接入适配器会拒绝并返回 <code>CONSENT_REQUIRED</code>。页面会把它解释成
"门在工作"，而不是报错 —— 这正是我们要看到的行为。</p>
<label><input type="checkbox" name="consent_cp1" value="1">
  <b>CP1 个人分析</b>：允许为本人做画像与岗位匹配</label><br>
<label><input type="checkbox" name="consent_cp2" value="1">
  CP2 机会通知：允许在出现匹配岗位时通知我</label>
<p class="muted">授权与撤回都会写进 <code>consent_record</code>（含时间戳），
撤回后不再允许写入分析产物 —— 这是"目的限制"的可执行形式。</p></div>
<button type="submit">提交注册（真实写库）</button>
</form></div>
%s
<div class="card"><h2>本来源的注册记录 <span class="muted">· source_system = <code>%s</code></span></h2>
<table><thead><tr><th>外部编号</th><th>本库 person_id</th><th>状态</th>
<th class="n">版本</th><th>墓碑</th><th>CP1 授权</th><th>注销受理</th></tr></thead>
<tbody>%s</tbody></table>
<p class="muted">清理**按 <code>source_system</code> 精确匹配**，绝不用 <code>LIKE '前缀%%'</code> 去猜：
实测 <code>'per_mockreg_x' LIKE 'per_mock_%%'</code> 为 <b>true</b>（<code>_</code> 是单字符通配符），
用 LIKE 清理会连带删掉别人的数据。</p></div>
""" % (('<div class="note %s">%s</div>' % (kind, P.esc(msg))) if msg else "",
       EXT_PREFIX, sel("degree", deg), sel("city", city),
       "".join('<option value="%s">%s · %s</option>'
               % (P.esc(o["code"]), P.esc(o["code"]), P.esc(o["label_zh"])) for o in occ),
       result or "", SRC, reg_html)
    return P.page("mock 小程序注册窗口", body, nav=DEV_NAV(),
                  subtitle="真实写入 · 真实删除 · 三层证据", here="mock 注册窗口")


def DEV_NAV():
    import portal_dev as D
    return D.DEV_NAV


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
def handle(c, qs):
    act = (qs.get("act", ["submit"])[0] or "submit").strip()
    if act == "submit":
        return _do_submit(c, qs)
    if act == "revoke":
        return _do_revoke(qs)
    if act == "deregister":
        return _do_deregister(qs)
    if act == "hardclean":
        return _do_hardclean(qs)
    return view(c, qs, "不认识的动作：%s" % act, "err")


def _do_submit(c, qs):
    pkg, ext, ver = build_package(c, qs)
    before = snapshot()
    try:
        res = EX.ingest(pkg)                 # 自己开连接、自己提交（不吞进页面的事务）
        err, kind = "", "ok"
    except EX.ExchangeError as e:
        res, err, kind = None, "接入适配器拒绝了这次写入：<b>%s</b> —— %s" % (
            P.esc(e.code), P.esc(e.message)), "warn"
    after = snapshot()
    orph = orphan_check()

    with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
        pid = P.q1(cc, """SELECT person_id FROM mt.external_identity
                           WHERE source_system=%s AND external_person_id=%s""", (SRC, ext))

    if res:
        summary = ("<div class='note ok'><b>写入成功</b>：person_id=<code>%s</code>，"
                   "版本 %d，事实 %d 条（映射上 %d），自述技能 %d 条（映射上 %d）。"
                   "<br>外部编号 <code>%s</code></div>"
                   % (P.esc(res["data"]["personId"]), ver, res["data"]["facts"],
                      res["data"]["mappedFacts"], res["data"]["skillClaims"],
                      res["data"]["mappedSkillClaims"], P.esc(ext)))
    else:
        summary = ("<div class='note %s'>%s</div>"
                   % (kind, err if err else "写入被拒"))
    extra = summary + _evidence_block(pkg, res, before, after, orph, pid, c)
    return view(c, qs, "", "info", result=extra)


def _do_revoke(qs):
    """D-A 撤回授权：用户自助。撤回是一条**新记录**（append-only），不是删掉旧的。"""
    ext = (qs.get("ext_id", [""])[0] or "").strip()
    with psycopg.connect(P.DSN, row_factory=dict_row) as c:
        pid = P.q1(c, """SELECT person_id FROM mt.external_identity
                          WHERE source_system=%s AND external_person_id=%s""", (SRC, ext))
        if not pid:
            return _plain("撤回授权", "找不到这个外部编号：%s" % P.esc(ext), "err")
        cid = "cns_rev_" + hashlib.sha1(
            ("%s|%f" % (pid, time.time())).encode()).hexdigest()[:18]
        c.execute("""INSERT INTO consent_record (consent_id, person_id, purpose, scope,
                        granted_at, revoked_at, channel)
                     VALUES (%s,%s,'CP1','miniprogram onboarding', now(), now(), 'mock_regmock')""",
                  (cid, pid))
        c.commit()
        return _plain("撤回授权（D-A）", (
            "<p>已写入一条<b>撤回</b>记录：<code>%s</code>（person_id=<code>%s</code>）。</p>"
            "<p>为什么是「新写一条」而不是「改掉旧记录」：授权历史必须 append-only —— "
            "否则事后无法回答「他什么时候授权过、什么时候撤回的」。</p>"
            "<p>下一步可以再提交一次注册包，会看到适配器返回 "
            "<code>CONSENT_REQUIRED</code> —— 那是授权门在工作。</p>"
            % (P.esc(cid), P.esc(pid))), "ok")


def _do_deregister(qs):
    """D-B 注销：用户自助，**异步受理**。受理 ≠ 完成。"""
    ext = (qs.get("ext_id", [""])[0] or "").strip()
    with psycopg.connect(P.DSN, row_factory=dict_row) as c:
        row = c.execute("""SELECT person_id, last_seen_version FROM mt.external_identity
                            WHERE source_system=%s AND external_person_id=%s""",
                        (SRC, ext)).fetchone()
        if not row:
            return _plain("注销（D-B）", "找不到这个外部编号：%s" % P.esc(ext), "err")
        pid, ver = row["person_id"], row["last_seen_version"]
        c.execute("""INSERT INTO mt.tombstone (tombstone_id, person_id, source_system,
                        deleted_through_version, reason)
                     VALUES (%s,%s,%s,%s,'用户自助注销（mock 窗口）')
                     ON CONFLICT (person_id, source_system) DO UPDATE
                        SET deleted_through_version = GREATEST(
                              mt.tombstone.deleted_through_version,
                              EXCLUDED.deleted_through_version)""",
                  ("tmb_" + hashlib.sha1(("%s|%s" % (pid, SRC)).encode()).hexdigest()[:20],
                   pid, SRC, ver))
        rid = "del_" + hashlib.sha1(("%s|%f" % (pid, time.time())).encode()).hexdigest()[:18]
        c.execute("""INSERT INTO mt.talent_deletion_request
                        (local_request_id, request_id, person_id, source_system,
                         requested_at, status, downstream_confirmed, completed_at, note)
                     VALUES (%s,%s,%s,%s, now(), 'pending', false, NULL,
                             '受理后由下游确认；受理 ≠ 完成')""",
                  (rid, "REQ-" + rid[-8:].upper(), pid, SRC))
        c.execute("""UPDATE mt.external_identity SET status='deleted'
                      WHERE source_system=%s AND external_person_id=%s""", (SRC, ext))
        c.commit()
    return _plain("注销（D-B）· 异步受理", _dereg_body(ext))


def _dereg_body(ext):
    with psycopg.connect(P.DSN, row_factory=dict_row) as c:
        r = c.execute("""SELECT ei.person_id, ei.status,
                                tb.deleted_through_version,
                                (SELECT local_request_id FROM mt.talent_deletion_request
                                  WHERE person_id = ei.person_id
                                  ORDER BY requested_at DESC LIMIT 1) AS rid
                           FROM mt.external_identity ei
                           LEFT JOIN mt.tombstone tb ON tb.person_id = ei.person_id
                                AND tb.source_system = ei.source_system
                          WHERE ei.source_system=%s AND ei.external_person_id=%s""",
                      (SRC, ext)).fetchone()
    return """
<p>已受理（<b>受理 ≠ 完成</b>）：</p>
<ul>
<li>本库 person_id：<code>%s</code>，外部身份状态：<b>%s</b></li>
<li>墓碑：<code>deleted_through_version = %s</code> —— 此后<b>版本 ≤ %s 的迟到写入一律被拒</b>。
这正是"删除后小程序重试把人复活"这个真实故障的防线。</li>
<li>注销请求：<code>%s</code>，状态 <b>pending</b>，<code>completed_at = NULL</code></li>
</ul>
<p><b>为什么显示"受理"而不是"已完成"</b>：删除在下游（对方小程序、备份、日志）可能还没执行完，
把它们假装成"已完成"是自欺；合规上也要求区分受理与完成。真正的完成需要下游回执 ——
那一步不在这台机器上。</p>
<p>现在可以验证一件事：再用<b>同一个外部编号</b>提交一次注册包，
应该被墓碑拦下（适配器返回 <code>PROFILE_DELETING</code>）。</p>
""" % (P.esc(r["person_id"]), P.esc(r["status"]),
       r["deleted_through_version"], r["deleted_through_version"], P.esc(r["rid"]))


def _do_hardclean(qs):
    """D-C 硬清理：**仅测试/运维**，范围严格限定在本来源。"""
    ext = (qs.get("ext_id", [""])[0] or "").strip()
    if ext and not ext.startswith(EXT_PREFIX):
        return _plain("硬清理（D-C）",
                      "只允许清理 <code>%s</code> 开头的测试数据，拒绝：%s"
                      % (EXT_PREFIX, P.esc(ext)), "err")
    before = snapshot()
    with psycopg.connect(P.DSN, row_factory=dict_row) as c:
        pids = [r["person_id"] for r in P.q(c, """
            SELECT person_id FROM mt.external_identity
             WHERE source_system=%s %s""" % ("%s", "AND external_person_id=%s" if ext else ""),
            ((SRC, ext) if ext else (SRC,)))]
        if not pids:
            return _plain("硬清理（D-C）", "没有可清理的本来源数据（source_system=%s）" % SRC, "info")
        # 子表按外键顺序删；person 的 29 张子表全是 NO ACTION（实测），所以必须严格按序
        kids = P.q(c, """
            SELECT cl.relname AS t, a.attname AS col
              FROM pg_constraint con
              JOIN pg_class cl ON cl.oid = con.conrelid
              JOIN pg_namespace n ON n.oid = cl.relnamespace
              JOIN pg_class cl2 ON cl2.oid = con.confrelid
              JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
             WHERE con.contype='f' AND n.nspname='mt' AND cl2.relname='person'""")
        n_del = {}
        for p in pids:
            for k in kids:
                r = c.execute('DELETE FROM mt.%s WHERE %s = %%s' % (k["t"], k["col"]),
                              (p,)).rowcount
                if r:
                    n_del[k["t"]] = n_del.get(k["t"], 0) + r
            # 无外键的那根裸 TEXT 必须单独删（否则留孤儿）
            r = c.execute("DELETE FROM mt.field_value WHERE subject_id=%s", (p,)).rowcount
            if r:
                n_del["field_value"] = n_del.get("field_value", 0) + r
            for t in ("experience_episode", "consent_record", "tombstone",
                      "talent_deletion_request", "response_session", "answer",
                      "external_identity"):
                r = c.execute("DELETE FROM mt.%s WHERE person_id=%%s" % t, (p,)).rowcount
                if r:
                    n_del[t] = n_del.get(t, 0) + r
            r = c.execute("DELETE FROM mt.person WHERE person_id=%s", (p,)).rowcount
            if r:
                n_del["person"] = n_del.get("person", 0) + r
        c.commit()
    after = snapshot()
    orph = orphan_check()
    rows = "".join('<tr><td><code>%s</code></td><td class="n">%d</td></tr>' % (k, v)
                   for k, v in sorted(n_del.items())) or '<tr><td colspan="2">（无变化）</td></tr>'
    return _plain("硬清理（D-C）· 仅测试", """
<p>清理了 <b>%d</b> 个本来源的 person（source_system=<code>%s</code>）。</p>
<table><thead><tr><th>表</th><th class="n">删除行数</th></tr></thead><tbody>%s</tbody></table>
<div class="card"><h2>清理之后必须再查一次孤儿行</h2>
<p>检查了 <b>%d</b> 张外键子表 + <code>field_value</code> 的无外键列 → <b>%s</b></p>
<p class="muted">为什么清理完还要查：<code>field_value.subject_id</code> <b>没有外键</b>，
直接删 person 会成功而值原样留下、且不报任何错（实测）。所以"删了"不等于"没孤儿"，
必须显式验证。</p></div>
""" % (len(pids), SRC, rows, orph["n_fk_children"],
       "全部干净" if not orph["problems"] else "发现问题：%s" % P.esc("；".join(orph["problems"]))))


def _plain(title, html, kind="info"):
    """结果页。kind 由调用方决定语义，这里只负责套门户外壳与导航。"""
    return P.page(title, html, nav=DEV_NAV(), subtitle="mock 注册窗口")
