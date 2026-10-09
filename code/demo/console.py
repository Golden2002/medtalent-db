# -*- coding: utf-8 -*-
"""
code/demo/console.py —— 产品演示控制台（动态实测版，需求 b2）

一个零前端依赖的本地站点，每个页面都**真实读写数据库**，不是放录屏：

  /            概览：实时规模数字 + 各演示入口
  /jobs        岗位图谱：17 族筛选、分页、要求三态（must/preferred/unclear）
  /matrix      能力权重矩阵：按职业查看能力重要性与必需性
  /dimension   动态建模：现场加一个维度（零 DDL），立刻可在录入页使用
  /intake      人才录入：每个字段 n 个选项 → 用户选择 → 写入数据库
  /match       匹配演示：met / gap / unknown 三态与排序依据
  /bridge      小程序接入：贴交换包 JSON → 摄入 → 幂等/映射结果
  /backup      数据安全：一键备份、校验、恢复演练、保留策略

启动：python code/demo/console.py --serve          http://127.0.0.1:8081
      python code/demo/console.py --seed 12        # 造演示人才
      python code/demo/console.py --reset          # 清演示人才与演示维度
"""
from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
import random
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
for p in (os.path.join(BASE, "code"), os.path.join(BASE, "code", "bridge"),
          os.path.join(BASE, "ops", "backup")):
    sys.path.insert(0, p)

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import backup as bk  # noqa: E402
import exchange as ex  # noqa: E402
import projection as pj  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

# 控制台自己的连接身份 —— **不要写成 `DSN = ex.DSN`**（原来是那样，已修）。
# 为什么踩过：`exchange.py` 的 DSN 从 postgres 改成最小权限角色 mt_bridge 之后，
# 控制台**跟着改了身份**，于是它的 `reset_live()`（要 DELETE field_value）直接
# permission denied —— 一个文件的安全改动把另一个无关进程弄坏了。
# 根因是"DSN 常量被别的模块导入"：这让"这个进程用什么身份连库"变得说不清楚，
# 而 I4.1 这条指标恰恰要逐个进程回答这个问题。
# 约定：**每个服务入口声明自己的连接串**（带自己的 application_name）。
DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public' "
       "application_name=medtalent_console")
ENTITY = "person"
FORM_ENTITIES = ("person", "person_demographics")
LIVE_PREFIX = "per_live_"
# 控制台示例交换包的 personId 前缀（见 fresh_example_package）——
# 用 bridge 链路摄入的人不在 per_live_ 前缀下，reset 必须按这个前缀单独清理。
# `wx_anon_` 是 exchange.example_package() 自带的**占位 id**，只有演示路径会用到它。
DEMO_EXT_PREFIXES = ("wx_demo_", "wx_anon_")
DEMO_EVT_PREFIX = "evt_demo_"
# 小程序接入测试的来源系统前缀。这也是**测试独占**的命名空间
# （ops/tests/bridge_test.py 用 `bridge_test_<hex>`），所以一并纳入 reset：
# 测试自己会清，但历史版本漏下的残留会让"干净状态"名不副实。
TEST_SRC_PREFIX = "bridge_test_"
PORT = 8081


def db():
    return psycopg.connect(DSN, row_factory=dict_row)


def q(c, sql, p=None):
    with c.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchall()


def q1(c, sql, p=None):
    r = q(c, sql, p)
    return list(r[0].values())[0] if r else None


def qrow(c, sql, p=None):
    r = q(c, sql, p)
    return r[0] if r else {}


# ---------------------------------------------------------------------------
# 表单字段（"每个字段 n 个选项"的数据库来源）
# ---------------------------------------------------------------------------
def form_fields(c) -> list[dict]:
    return q(c, """
        SELECT entity_id, field_id, title, description, data_type, cardinality,
               is_required, form_section, option_count, options, form_order
        FROM v_form_schema
        WHERE entity_id = ANY(%s)
          AND (option_count > 0 OR form_section IS NOT NULL)
        ORDER BY COALESCE(form_section,'zzz'), form_order, field_id""",
        (list(FORM_ENTITIES),))


def option_labels(c) -> dict:
    out = {}
    for r in q(c, "SELECT field_id, title, options FROM v_form_schema "
                  "WHERE entity_id = ANY(%s)", (list(FORM_ENTITIES),)):
        out[r["field_id"]] = (r["title"],
                              {o["code"]: o["label"] for o in (r["options"] or [])})
    return out


def add_dimension(c, field_id, title, options, multi, section="演示维度"):
    opts = [{"code": "O%d" % (i + 1), "label": t, "sort": (i + 1) * 10}
            for i, t in enumerate(options) if t.strip()]
    with c.cursor() as cur:
        cur.execute("""SELECT mt.add_dimension(p_entity=>%s, p_field_id=>%s, p_title=>%s,
                          p_options=>%s::jsonb, p_multi=>%s, p_section=>%s)""",
                    (ENTITY, field_id, title, json.dumps(opts, ensure_ascii=False),
                     multi, section))


def submit_profile(c, form: dict) -> str:
    code = (form.get("F_SUBJECT_CODE") or [""])[0].strip()
    pid = LIVE_PREFIX + secrets.token_hex(4)
    if not code:
        code = "MT-LIVE-" + pid[-6:].upper()
    with c.cursor() as cur:
        cur.execute("""SELECT mt.create_instance(p_entity=>'person', p_id=>%s,
                          p_payload=>%s::jsonb)""",
                    (pid, json.dumps({"person_id": pid, "subject_code": code,
                                      "enroll_channel": "EC1"}, ensure_ascii=False)))
        for f in form_fields(c):
            vals = [v for v in form.get(f["field_id"], []) if v != ""]
            if not vals:
                continue
            ent = f["entity_id"]
            if f["cardinality"] == "array":
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_codes=>%s::text[])",
                            (ent, pid, f["field_id"], vals))
            elif f["data_type"] in ("integer", "number"):
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_num=>%s::numeric)",
                            (ent, pid, f["field_id"], vals[0]))
            elif f["data_type"] == "code":
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_code=>%s)", (ent, pid, f["field_id"], vals[0]))
            else:
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_text=>%s)", (ent, pid, f["field_id"], vals[0]))
    return pid


def _person_fk_children(cur) -> list:
    """person 的全部直接外键子表及其指向 person 的列（从 pg_constraint 动态发现）。

    不写死清单：表结构会变，写死就会在下次加表时悄悄漏删。
    """
    cur.execute("""
        SELECT DISTINCT cl.relname AS child,
               (SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                 ORDER BY k.ord LIMIT 1) AS col
          FROM pg_constraint con
          JOIN pg_class cl ON cl.oid = con.conrelid
          JOIN pg_class rf ON rf.oid = con.confrelid
          JOIN pg_namespace n ON n.oid = cl.relnamespace
         WHERE con.contype = 'f' AND rf.relname = 'person' AND n.nspname = 'mt'
         ORDER BY 1""")
    return [(r["child"], r["col"]) for r in cur.fetchall()]


def reset_live(c) -> dict:
    """清除控制台演示造出的全部数据。

    踩过的坑（问题 #40）：这里原来只删 `per_live_%`，但控制台自己的
    「小程序接入」页面会通过 `exchange.ingest` 造人 —— 那些人的本地 ID 是
    `per_<sha1>`，不在 `per_live_` 前缀下，于是 `--reset` 号称"一键恢复干净状态"
    却每次演示都留下一个人和一条外部身份绑定。现在按**示例包的 personId 前缀
    `wx_demo_`** 把这条链路造出的人也一并清掉。
    """
    with c.cursor() as cur:
        cur.execute("DELETE FROM field_value WHERE subject_id LIKE %s", (LIVE_PREFIX + "%",))
        nv = cur.rowcount
        cur.execute("DELETE FROM observation_window WHERE person_id LIKE %s", (LIVE_PREFIX + "%",))
        cur.execute("DELETE FROM match_result WHERE person_id LIKE %s", (LIVE_PREFIX + "%",))
        cur.execute("DELETE FROM skill_assertion WHERE person_id LIKE %s", (LIVE_PREFIX + "%",))
        cur.execute("DELETE FROM person WHERE person_id LIKE %s", (LIVE_PREFIX + "%",))
        np_ = cur.rowcount

        # ---- bridge 演示摄入的人（含测试残留）----
        cur.execute("""SELECT DISTINCT person_id FROM external_identity
                        WHERE external_person_id LIKE ANY(%s)
                           OR source_system LIKE %s""",
                    ([p + "%" for p in DEMO_EXT_PREFIXES], TEST_SRC_PREFIX + "%"))
        demo = [r["person_id"] for r in cur.fetchall()]
        nd = 0
        if demo:
            for child, col in _person_fk_children(cur):
                cur.execute(sql.SQL("DELETE FROM mt.{} WHERE {} = ANY(%s)")
                            .format(sql.Identifier(child), sql.Identifier(col)), (demo,))
            cur.execute("DELETE FROM person WHERE person_id = ANY(%s)", (demo,))
            nd = cur.rowcount
        # 幂等兜底：身份绑定/同步事件按演示与测试前缀再扫一遍
        cur.execute("""DELETE FROM external_identity
                        WHERE external_person_id LIKE ANY(%s) OR source_system LIKE %s""",
                    ([p + "%" for p in DEMO_EXT_PREFIXES], TEST_SRC_PREFIX + "%"))
        cur.execute("DELETE FROM sync_event WHERE external_event_id LIKE %s",
                    (DEMO_EVT_PREFIX + "%",))

        cur.execute("""DELETE FROM field_value WHERE field_id IN
                       (SELECT field_id FROM field_catalog WHERE field_id LIKE 'F_LIVE%%')""")
        cur.execute("DELETE FROM field_catalog WHERE field_id LIKE 'F_LIVE%%'")
        nf = cur.rowcount
        cur.execute("DELETE FROM code_value WHERE code_table_id LIKE 'CT_F_LIVE%%'")
        cur.execute("DELETE FROM code_table WHERE code_table_id LIKE 'CT_F_LIVE%%'")
    return {"persons": np_, "values": nv, "fields": nf, "bridged": nd}


def seed_live(c, n: int) -> int:
    """造演示人才。
    除了偏好类回答，还会写入若干**自述能力主张**——否则匹配结果会全是 unknown（0 分），
    演示看不出 met/gap 的区分。自述一律按规则落 V1 / confidence 0.5 / 无证据。

    造完人之后**顺手跑一次匹配**（复用 bridge 的 `compute_matches` / `persist_matches`，
    不另写一套）。踩过的坑（问题 #41）：这里原来只造人不跑匹配，于是
    `--reset` 清掉 match_result 之后，"匹配"页就再也生不出数据——
    演示会因为一次清理而永久失去一个页面。演示的**构造能力必须覆盖它能清理的范围**。
    """
    labels = option_labels(c)
    # 先建立"带能力的演示人才"，再统一匹配：compute_matches 依赖岗位侧已解析
    skill_pool = [r["concept_id"] for r in q(c, """
        SELECT DISTINCT jr.concept_id FROM job_requirement jr
        WHERE jr.concept_id IS NOT NULL""")]
    pids = []
    for i in range(n):
        pid = submit_profile(c, {})
        pids.append(pid)
        for fid, (_t, omap) in labels.items():
            if not omap or not fid.startswith(("F_LIVE_", "F_PERSON_")):
                continue
            ks = list(omap.keys())
            with c.cursor() as cur:
                cur.execute("""SELECT cardinality, data_type, entity_id FROM field_catalog
                               WHERE field_id=%s""", (fid,))
                meta = cur.fetchone()
                if not meta:
                    continue
                ent = meta["entity_id"]
                if meta["cardinality"] == "array":
                    cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                                "p_field_id=>%s,p_codes=>%s::text[])",
                                (ent, pid, fid, random.sample(ks, min(2, len(ks)))))
                else:
                    cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                                "p_field_id=>%s,p_code=>%s)",
                                (ent, pid, fid, random.choice(ks)))
        # 自述能力：从真实出现在岗位要求里的概念中随机取 2–4 项
        if skill_pool:
            with c.cursor() as cur:
                for cid in random.sample(skill_pool, min(len(skill_pool),
                                                         random.randint(2, 4))):
                    cur.execute("""INSERT INTO skill_assertion (assertion_id, person_id,
                                      concept_id, level, level_basis, claim_type,
                                      evidence_id, confidence, transferability,
                                      transfer_note, verify_status)
                                   VALUES (%s,%s,%s,3,'LB1','CT1',NULL,0.5,3,
                                           '演示用自述能力', 'V1')
                                   ON CONFLICT (assertion_id) DO NOTHING""",
                                ("skl_" + hashlib.sha1(("%s|%s" % (pid, cid)).encode())
                                 .hexdigest()[:24], pid, cid))
                cur.execute("""INSERT INTO observation_window (window_id, person_id, window_type,
                                  start_date, coverage_note)
                               VALUES (%s,%s,'W1', CURRENT_DATE - 500, '演示窗口：覆盖在校至今')
                               ON CONFLICT DO NOTHING""", ("ow_" + pid, pid))
    # 统一跑一次匹配，让"匹配"页有真实数据（met/gap/unknown 三态都可解释）
    for pid in pids:
        try:
            m = pj.compute_matches(c, pid)
            pj.persist_matches(c, pid, m)
        except Exception as e:                       # noqa: BLE001
            print("  [!] %s 匹配失败（其余人才继续）：%s" % (pid, str(e).splitlines()[0]))
    return n


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
CSS = """
body{font:14px/1.65 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;
     background:#f5f6f8;color:#1f2328}
.wrap{max-width:1120px;margin:0 auto;padding:0 18px 40px}
nav{background:#0f2a5c;padding:0 18px;display:flex;align-items:center;gap:2px;
    flex-wrap:wrap;position:sticky;top:0;z-index:9}
nav .brand{color:#fff;font-weight:700;margin-right:18px;padding:13px 0;font-size:15px}
nav a{color:#c9ddff;text-decoration:none;padding:13px 11px;font-size:13.5px;
      border-bottom:3px solid transparent}
nav a:hover{color:#fff;background:#16376f}
nav a.on{color:#fff;border-bottom-color:#4c9aff}
h1{font-size:21px;margin:22px 0 4px}
h2{font-size:16px;margin:22px 0 10px}
.sub{color:#656d76;font-size:12.5px;margin-bottom:14px}
.card{background:#fff;border:1px solid #d8dee4;border-radius:9px;padding:16px 18px;
      margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.metric{background:#f6f8fa;border:1px solid #d8dee4;border-radius:8px;padding:11px 13px}
.metric .n{font-size:21px;font-weight:700;color:#0f2a5c;line-height:1.2}
.metric .t{font-size:12px;color:#57606a;margin-top:2px}
label{display:block;font-weight:600;margin:11px 0 4px;font-size:13px}
label .req{color:#cf222e;margin-left:3px}
label .hint{font-weight:400;color:#656d76;margin-left:6px;font-size:12px}
select,input[type=text],input[type=number],textarea{width:100%;padding:7px 9px;
  border:1px solid #d0d7de;border-radius:6px;font-size:13px;box-sizing:border-box;background:#fff}
textarea{height:74px;resize:vertical;font-family:Consolas,monospace;font-size:12px}
textarea.tall{height:220px}
.chk{display:inline-flex;align-items:center;margin:3px 14px 3px 0;font-weight:400;font-size:13px}
.chk input{margin-right:5px}
button{background:#1f883d;color:#fff;border:0;border-radius:6px;padding:9px 18px;
       font-size:13px;cursor:pointer;font-weight:600}
button.sec{background:#f6f8fa;color:#24292f;border:1px solid #d0d7de}
button.danger{background:#cf222e}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{border-bottom:1px solid #eaeef2;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600;color:#424a53;position:sticky;top:48px}
code{background:#f6f8fa;border:1px solid #eaeef2;border-radius:4px;padding:1px 5px;
     font-family:Consolas,monospace;font-size:12px}
pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;font-size:12px;
    overflow:auto;font-family:Consolas,monospace;line-height:1.5;margin:8px 0}
.sect{font-size:12px;font-weight:700;color:#0969da;margin:18px 0 2px;
      border-top:1px solid #eaeef2;padding-top:12px}
.sect:first-child{border-top:0;padding-top:0;margin-top:4px}
.note{background:#fff8c5;border:1px solid #d4a72c66;border-radius:6px;padding:10px 12px;
      font-size:12.5px;color:#4d2d00;margin-bottom:14px}
.ok{background:#dafbe1;border:1px solid #1a7f3733;color:#0a3d17}
.err{background:#ffebe9;border:1px solid #cf222e33;color:#82071e}
.pill{display:inline-block;border-radius:10px;padding:1px 8px;font-size:11.5px;margin-right:5px}
.p-met{background:#dafbe1;color:#0a3d17}.p-gap{background:#ffebe9;color:#82071e}
.p-unk{background:#f6f8fa;color:#57606a;border:1px solid #d8dee4}
.bar{height:9px;background:#1f6feb;border-radius:5px;display:inline-block;vertical-align:middle}
.muted{color:#656d76;font-size:12px}
.row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1 1 180px}
.pager a{margin-right:10px;font-size:13px}
"""

NAV = [("/", "概览"), ("/jobs", "岗位图谱"), ("/matrix", "能力矩阵"),
       ("/dimension", "动态维度"), ("/intake", "人才录入"), ("/match", "匹配"),
       ("/bridge", "小程序接入"), ("/backup", "备份与恢复")]


def page(title: str, body: str, msg: str = "", msg_kind: str = "note") -> bytes:
    nav = "".join('<a href="%s" class="%s">%s</a>'
                  % (u, "on" if title == t else "", t) for u, t in NAV)
    m = '<div class="note %s">%s</div>' % (msg_kind, html.escape(msg)) if msg else ""
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>%s · 医学生人才信息库演示</title><style>%s</style></head><body>
<nav><span class="brand">医学生人才信息库 · 演示控制台</span>%s</nav>
<div class="wrap"><h1>%s</h1>%s%s</div></body></html>""" % (
        html.escape(title), CSS, nav, html.escape(title), m, body)
    return doc.encode("utf-8")


def metric(v, t):
    return '<div class="metric"><div class="n">%s</div><div class="t">%s</div></div>' % (v, t)


# ---------------------------------------------------------------------------
# 各页渲染
# ---------------------------------------------------------------------------
def view_home(c, msg="", kind="note") -> bytes:
    n = {
        "occ": q1(c, "SELECT count(*) FROM occupation"),
        "fam": q1(c, "SELECT count(DISTINCT family) FROM occupation WHERE level=1"),
        "job": q1(c, "SELECT count(*) FROM job_posting"),
        "req": q1(c, "SELECT count(*) FROM job_requirement"),
        "con": q1(c, "SELECT count(*) FROM concept"),
        "w": q1(c, "SELECT count(*) FROM job_competency_weight"),
        "fld": q1(c, "SELECT count(*) FROM field_catalog"),
        "code": q1(c, "SELECT count(*) FROM code_value"),
        "tbl": q1(c, "SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema='mt' AND table_type='BASE TABLE'"),
        "live": q1(c, "SELECT count(*) FROM person WHERE person_id LIKE %s", (LIVE_PREFIX + "%",)),
        "bk": q1(c, "SELECT count(*) FROM backup_run WHERE status='ok'"),
    }
    body = """
<div class="sub">下面的每个数字都实时查库。左侧导航每一页都能真实读写数据库——这是可实测的演示，不是录屏。</div>
<div class="cards">%s</div>
<div class="card"><h2>建议的演示动线（约 6 分钟）</h2>
<ol>
<li><b>岗位图谱</b>：先让客户看到「临床只是 17 个岗位族之一」，看医学依赖度与转出容易度。</li>
<li><b>能力矩阵</b>：选一个非临床岗位（如医学科学联络官），看它到底要什么能力、哪些是必需的。</li>
<li><b>动态维度</b>：现场说一个新的画像维度（比如「是否愿意值夜班」），当场加进系统——不重启、不改表。</li>
<li><b>人才录入</b>：刚才加的维度立刻出现在表单里，n 个选项，选完即入库。</li>
<li><b>匹配</b>：看某个人的匹配结果，重点讲 met / gap / <b>unknown 不是不合格</b>。</li>
<li><b>小程序接入</b>：贴一份交换包，看到幂等、禁身份标识、不臆造映射。</li>
<li><b>备份与恢复</b>：一键备份，再演练一次「恢复到新库并逐表核对」。</li>
</ol></div>""" % "".join([
        metric(n["occ"], "职业树节点"), metric(n["fam"], "岗位族"),
        metric(n["job"], "结构化岗位"), metric(n["req"], "要求条目"),
        metric(n["con"], "能力概念"), metric(n["w"], "能力权重"),
        metric(n["fld"], "已登记字段"), metric(n["code"], "代码值"),
        metric(n["tbl"], "基础表"), metric(n["live"], "演示人才"),
        metric(n["bk"], "可用备份"),
    ])
    return page("概览", body, msg, kind)


def view_jobs(c, qs, msg="", kind="note") -> bytes:
    fam = qs.get("family", [""])[0]
    pg = int(qs.get("page", ["1"])[0])
    size = 12
    where = "WHERE (%(f)s::text = '' OR jp.job_family = %(f)s::text)"
    P = {"f": fam, "lim": size, "off": (pg - 1) * size}
    rows = q(c, """SELECT jp.job_id, jp.job_family, jp.title_raw, jp.city, jp.education_req,
                          jp.salary_min, jp.salary_max, jp.salary_period, jp.employer_name_raw
                   FROM job_posting jp %s ORDER BY jp.job_id LIMIT %%(lim)s OFFSET %%(off)s""" % where, P)
    total = q1(c, "SELECT count(*) FROM job_posting jp " + where, P)
    fams = q(c, """SELECT job_family AS family, count(*) AS n FROM job_posting
                   WHERE job_family IS NOT NULL GROUP BY 1 ORDER BY 1""")
    fid = rows[0]["job_id"] if rows else None
    reqs = q(c, """SELECT requirement_kind, requirement_type, raw_text, concept_id
                   FROM job_requirement WHERE job_id=%s ORDER BY requirement_id""", (fid,)) if fid else []
    kind_txt = {"RK1": "must", "RK2": "preferred", "RK3": "preferred"}
    req_html = "".join(
        '<tr><td><span class="pill %s">%s</span></td><td>%s</td><td>%s</td></tr>'
        % ("p-gap" if r["requirement_kind"] == "RK1" else "p-unk",
           "must" if r["requirement_kind"] == "RK1" else
           ("unclear" if not r["concept_id"] else "preferred"),
           html.escape(r["raw_text"]), html.escape(r["concept_id"] or "—"))
        for r in reqs)
    opts = ['<option value="">全部岗位族</option>'] + [
        '<option value="%s" %s>%s（%d 条）</option>'
        % (f["family"], "selected" if f["family"] == fam else "", f["family"], f["n"])
        for f in fams]
    rows_html = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            r["job_family"], html.escape(r["title_raw"]), html.escape(r["employer_name_raw"] or ""),
            html.escape(r["city"] or ""), html.escape(r["education_req"] or ""),
            ("%s–%s" % (int(r["salary_min"]), int(r["salary_max"]))
             if r["salary_min"] else "面议"))
        for r in rows)
    body = """
<div class="sub">共 <b>%d</b> 条岗位。要求按契约拆成 <code>must / preferred / unclear</code>：
映射不到能力的进 unclear，不臆断。</div>
<div class="card"><form method="get" action="/jobs" class="row">
  <div><label>按岗位族筛选</label><select name="family">%s</select></div>
  <div style="flex:0 0 110px"><button type="submit">筛选</button></div>
  <div style="flex:0 0 130px"><a href="/jobs"><button type="button" class="sec">重置</button></a></div>
</form></div>
<div class="card"><table><tr><th>族</th><th>岗位</th><th>雇主</th><th>城市</th><th>学历</th><th>薪资</th></tr>%s</table>
<div class="pager" style="margin-top:12px">%s</div></div>
<div class="card"><h2>第一条岗位的要求三态</h2>
<table><tr><th>三态</th><th>要求原文</th><th>映射到能力</th></tr>%s</table>
<p class="muted">unclear 不代表这条要求不重要，而是说明我们的能力词表还没覆盖它——
系统会把它留在原文里并计数，而不是硬塞一个概念。</p></div>""" % (
        total, "".join(opts), rows_html,
        " ".join('<a href="/jobs?family=%s&page=%d">%d</a>'
                 % (urllib.parse.quote(fam), p, p)
                 for p in range(1, min(6, (total // size) + 2)))
        if total > size else "<span class='muted'>共 1 页</span>", req_html)
    return page("岗位图谱", body, msg, kind)


def view_matrix(c, qs, msg="", kind="note") -> bytes:
    occs = q(c, """SELECT o.occupation_id, o.family, o.label_zh, f.label_zh AS fam_name
                   FROM occupation o
                   LEFT JOIN code_value f ON f.code_table_id='CT_JOB_FAMILY' AND f.code=o.family
                   WHERE o.level=3 AND EXISTS (SELECT 1 FROM job_competency_weight w
                                               WHERE w.occupation_id=o.occupation_id)
                   ORDER BY o.family, o.occupation_id""")
    sel = qs.get("occ", [""])[0] or (occs[0]["occupation_id"] if occs else "")
    ws = q(c, """SELECT c.preferred_label, c.concept_type, w.importance, w.essentiality,
                        w.sample_size
                 FROM job_competency_weight w JOIN concept c ON c.concept_id=w.concept_id
                 WHERE w.occupation_id=%s ORDER BY w.importance DESC""", (sel,))
    bars = "".join(
        '<tr><td>%s</td><td>%s</td><td><span class="bar" style="width:%dpx"></span> %.3f</td>'
        '<td>%s</td><td>%d</td></tr>'
        % (html.escape(w["preferred_label"]),
           {"K1": "技能", "K2": "知识", "K3": "能力"}.get(w["concept_type"], w["concept_type"]),
           int(float(w["importance"]) * 200), float(w["importance"]),
           '<span class="pill p-gap">必需</span>' if w["essentiality"] == "essential"
           else '<span class="pill p-unk">加分</span>', w["sample_size"])
        for w in ws)
    opts = "".join('<option value="%s" %s>%s %s</option>'
                   % (o["occupation_id"], "selected" if o["occupation_id"] == sel else "",
                      o["family"], html.escape(o["label_zh"])) for o in occs)
    body = """
<div class="sub">能力权重矩阵由招聘要求聚合而成：<code>importance = 出现频次 × 硬性系数 ÷ 样本数</code>，
样本不足 30 条的组合直接跳过。</div>
<div class="card"><form method="get" action="/matrix" class="row">
  <div><label>选择职业</label><select name="occ">%s</select></div>
  <div style="flex:0 0 110px"><button type="submit">查看</button></div>
</form></div>
<div class="card"><h2>该职业最看重的能力</h2>
<table><tr><th>能力</th><th>类型</th><th>重要性</th><th>必需性</th><th>样本量</th></tr>%s</table>
<p class="muted">「必需」= 该职业至少有一条硬性要求指向这项能力；「加分」= 只出现在优先/加分项里。</p></div>""" % (
        opts, bars or "<tr><td colspan=5>该职业暂无权重数据</td></tr>")
    return page("能力矩阵", body, msg, kind)


def view_dimension(c, msg="", kind="note") -> bytes:
    dims = q(c, """SELECT field_id, title, cardinality,
                          (SELECT count(*) FROM code_value v
                           WHERE v.code_table_id=f.code_table_id AND NOT v.deprecated) AS n_opt
                   FROM field_catalog f
                   WHERE entity_id='person' AND status='active' AND field_id LIKE 'F_LIVE%%'
                   ORDER BY field_id""")
    rows = "".join("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%d</td></tr>"
                   % (d["field_id"], html.escape(d["title"]),
                      "多选" if d["cardinality"] == "array" else "单选", d["n_opt"])
                   for d in dims)
    body = """
<div class="sub">这是演示「数据库高扩展」的地方：新增一个画像维度<b>不执行任何 DDL</b>，
不重启、不改表结构，刷新即生效。</div>
<div class="card"><h2>现场新增一个维度</h2>
<form method="post" action="/dimension" class="row">
  <div><label>维度标题</label><input type="text" name="title" placeholder="例如：是否愿意值夜班" required></div>
  <div><label>选项（逗号分隔，即 n 个给定内容）</label>
       <input type="text" name="options" placeholder="完全接受,可以商量,不接受" required></div>
  <div style="flex:0 0 110px"><label>类型</label>
       <select name="multi"><option value="0">单选</option><option value="1">多选</option></select></div>
  <div style="flex:0 0 130px"><button type="submit">添加维度</button></div>
</form>
<p class="muted">提交后请到 <a href="/intake">人才录入</a> 看效果——新维度会立刻出现在表单里。</p></div>
<div class="card"><h2>本次演示新增的维度</h2>
<table><tr><th>字段 ID</th><th>标题</th><th>类型</th><th>候选选项数</th></tr>%s</table>
<form method="post" action="/reset-live" style="margin-top:12px">
  <button class="danger" type="submit">清除演示数据</button>
  <span class="muted" style="margin-left:8px">删除演示人才与演示维度（不影响真实数据）</span>
</form></div>""" % (rows or "<tr><td colspan=4>还没有，先在上面加一个</td></tr>")
    return page("动态维度", body, msg, kind)


def view_intake(c, msg="", kind="note") -> bytes:
    fields = form_fields(c)
    sections: dict[str, list] = {}
    for f in fields:
        # 未指定分组的字段（多是性别/户籍这类基础信息）归到「基础信息」而不是「其他」
        sections.setdefault(f["form_section"] or "基础信息", []).append(f)
    parts = ['<div class="card"><form method="post" action="/intake">']
    parts.append('<label for="F_SUBJECT_CODE">对象编号<span class="hint">留空自动生成</span></label>'
                 '<input type="text" name="F_SUBJECT_CODE" placeholder="MT-LIVE-0001">')
    for sec, fs in sections.items():
        parts.append('<div class="sect">%s</div>' % html.escape(sec))
        for f in fs:
            req = '<span class="req">*</span>' if f["is_required"] else ""
            hint = ('<span class="hint">%d 个选项 · %s</span>'
                    % (f["option_count"], "多选" if f["cardinality"] == "array" else "单选")) \
                if f["option_count"] else '<span class="hint">%s</span>' % f["data_type"]
            parts.append('<label>%s%s%s</label>' % (html.escape(f["title"]), req, hint))
            fid = f["field_id"]
            if f["option_count"] and f["cardinality"] == "array":
                for o in f["options"]:
                    parts.append('<span class="chk"><input type="checkbox" name="%s" value="%s">'
                                 '%s</span>' % (fid, o["code"], html.escape(o["label"])))
            elif f["option_count"]:
                parts.append('<select name="%s"><option value="">— 请选择 —</option>' % fid)
                for o in f["options"]:
                    parts.append('<option value="%s">%s</option>' % (o["code"], html.escape(o["label"])))
                parts.append("</select>")
            elif f["data_type"] in ("integer", "number"):
                parts.append('<input type="number" step="any" name="%s">' % fid)
            else:
                parts.append('<input type="text" name="%s">' % fid)
    # "清空"用链接而不是 `<button onclick="location.href=…">`：
    # 业务控制台虽然不在"两个门户站点"的名下，但同一个仓库里出现内联 JS，
    # 就会让"零 JS"这句话变成需要加限定条件的说法 —— 那还不如把这一行改掉。
    parts.append('<div style="margin-top:16px"><button type="submit">提交并写入数据库</button>'
                 ' <a class="btnlink sec" href="/intake">清空</a></div>')
    parts.append("</form></div>")

    people = q(c, """SELECT p.person_id, p.subject_code,
                            mt.profile_json('person', p.person_id) AS prof,
                            mt.profile_json('person_demographics', p.person_id) AS prof2
                     FROM person p WHERE p.person_id LIKE %s
                     ORDER BY p.recorded_at DESC LIMIT 8""", (LIVE_PREFIX + "%",))
    labels = option_labels(c)
    rows = []
    for p in people:
        merged = dict(p["prof2"] or {})
        merged.update(p["prof"] or {})
        chips = []
        for k, v in merged.items():
            fid = v["field_id"]
            title, omap = labels.get(fid, (fid, {}))
            if v["codes"]:
                disp = "、".join(omap.get(x, x) for x in v["codes"])
            elif v["code"]:
                disp = omap.get(v["code"], v["code"])
            elif v["num"] is not None:
                disp = str(v["num"])
            else:
                disp = v["text"] or ""
            chips.append('<span class="pill p-unk">%s：%s</span>'
                         % (html.escape(title), html.escape(str(disp))))
        rows.append("<tr><td>%s</td><td>%s</td></tr>"
                    % (html.escape(p["subject_code"]), "".join(chips) or "—"))
    body = ("".join(parts) +
            '<div class="card"><h2>已入库的演示人才（最近 8 条）</h2>'
            '<table><tr><th>对象编号</th><th>画像</th></tr>%s</table></div>'
            % ("".join(rows) or "<tr><td colspan=2>还没有，先提交一份</td></tr>"))
    return page("人才录入", body, msg, kind)


def view_match(c, qs, msg="", kind="note") -> bytes:
    header = """
<div class="sub">匹配分三态：<span class="pill p-met">met</span> 具备 ·
<span class="pill p-gap">gap</span> 不具备（且落在有效观测窗口内）·
<span class="pill p-unk">unknown</span> 无法判断——<b>unknown 不是不合格</b>。</div>"""
    people = q(c, "SELECT person_id, subject_code FROM person WHERE person_id LIKE %s "
                  "ORDER BY recorded_at DESC LIMIT 30", (LIVE_PREFIX + "%",))
    sel = qs.get("pid", [""])[0] or (people[0]["person_id"] if people else "")
    if not sel:
        return page("匹配", header + '<div class="card">还没有演示人才。请先到 '
                    '<a href="/intake">人才录入</a> 提交一份，或运行 '
                    '<code>python code/demo/console.py --seed 10</code>。</div>', msg, kind)
    with db() as c2:
        m = pj.compute_matches(c2, sel)
    items = m["items"][:6]
    cards = []
    for it in items:
        def chips(lst, cls):
            return "".join('<span class="pill %s">%s</span>' % (cls, html.escape(x.get("conceptId") or x.get("reason", "")))
                           for x in lst) or '<span class="muted">无</span>'
        cards.append("""
<div class="card"><h2 style="margin-top:0">%s <span class="muted">(%s · %s · %s)</span></h2>
<div class="muted">排序分 %.3f ｜ 覆盖度 %.3f ｜ profileVersion %s ｜ jobSourceVersion %s ｜ 规则 %s</div>
<div style="margin-top:8px"><b>命中 met</b>：%s</div>
<div style="margin-top:4px"><b>缺口 gap</b>：%s</div>
<div style="margin-top:4px"><b>未知 unknown</b>：%s</div></div>""" % (
            html.escape(it["jobTitle"]), it["jobFamily"],
            html.escape(str(it.get("employer") or "—")), html.escape(str(it.get("city") or "—")),
            it["rankingScore"], it["coverage"],
            it["profileVersion"], html.escape(str(it["jobSourceVersion"])), it["ruleVersion"],
            chips(it["met"], "p-met"), chips(it["gap"], "p-gap"), chips(it["unknown"], "p-unk")))
    opts = "".join('<option value="%s" %s>%s</option>'
                   % (p["person_id"], "selected" if p["person_id"] == sel else "",
                      html.escape(p["subject_code"])) for p in people)
    body = header + """
<div class="card"><form method="get" action="/match" class="row">
  <div><label>选择人才</label><select name="pid">%s</select></div>
  <div style="flex:0 0 110px"><button type="submit">计算匹配</button></div>
</form>
<p class="muted">排序分只是排序用的分数，不是录用概率；覆盖度说明有多少要求能被现有事实解释。</p></div>
%s""" % (opts, "".join(cards) or '<div class="card">没有可匹配的岗位</div>')
    return page("匹配", body, msg, kind)


# 演示用：记住上一次投递的交换包，用于现场演示"重复投递 → 幂等"
LAST_PKG: dict = {}


def fresh_example_package() -> dict:
    """每次生成一个新的示例包。
    为什么不能用固定包：示例包里的 personId+aggregateVersion 一旦被处理过，
    再投就会正确地被判为 CONFLICT/幂等——演示"首次摄入"就演不出来了。"""
    p = ex.example_package()
    tag = secrets.token_hex(4)
    p["personId"] = "wx_demo_" + tag
    p["eventId"] = "evt_demo_" + tag
    p["submissionId"] = "sub_demo_" + tag
    p["requestId"] = "req_demo_" + tag
    p["aggregateVersion"] = 1
    return p


def view_bridge(c, msg="", kind="note", pkg_text="") -> bytes:
    if not pkg_text:
        pkg_text = json.dumps(ex.example_package(), ensure_ascii=False, indent=2)[:1800]
    recent = q(c, """SELECT event_id, external_event_id, event_type, aggregate_version,
                            status, attempts, created_at
                     FROM sync_event WHERE direction='inbound'
                     ORDER BY created_at DESC LIMIT 8""")
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (html.escape(r["external_event_id"] or ""), r["event_type"],
                      r["aggregate_version"], r["status"], r["attempts"]) for r in recent)
    body = """
<div class="sub">链路：小程序 → CloudBase 云函数 → <b>本服务</b> → 数据库。前端不直连库、不持有密码。
交换包<b>禁含身份标识</b>（memberKey/openid/appid/手机号），深度扫描，发现即整包拒绝。</div>
<div class="card"><h2>投递一个交换包</h2>
<form method="post" action="/bridge">
  <textarea name="pkg" class="tall">%s</textarea>
  <div class="row" style="margin-top:10px">
    <div style="flex:0 0 190px"><button type="submit">摄入（幂等）</button></div>
    <div style="flex:0 0 200px"><button class="sec" type="submit" name="use_example" value="1">① 生成新示例包并摄入</button></div>
    <div style="flex:0 0 210px"><button class="sec" type="submit" name="again" value="1">② 再投一次（验证幂等）</button></div>
    <div style="flex:0 0 230px"><button class="sec" type="submit" name="use_bad" value="1">③ 试一个含 memberKey 的包</button></div>
  </div>
</form>
<p class="muted">演示顺序：先点 ① 得到首次摄入结果，再点 ② 会返回
<code>idempotent: true</code> 且不产生第二个人；点 ③ 会被整包拒绝。</p></div>
<div class="card"><h2>最近的入向事件</h2>
<table><tr><th>对方事件号</th><th>类型</th><th>版本</th><th>状态</th><th>尝试次数</th></tr>
%s</table></div>""" % (html.escape(pkg_text), rows or "<tr><td colspan=5>暂无</td></tr>")
    return page("小程序接入", body, msg, kind)


def view_backup(c, msg="", kind="note") -> bytes:
    h = qrow(c, "SELECT * FROM v_backup_health")
    runs = q(c, """SELECT backup_id, started_at, status, size_bytes, contains_pii,
                          table_counts FROM backup_run ORDER BY started_at DESC LIMIT 8""")
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%.1f MB</td><td>%s</td><td>%d</td></tr>"
                   % (r["backup_id"], r["started_at"].strftime("%m-%d %H:%M"), r["status"],
                      (r["size_bytes"] or 0) / 2**20, "是" if r["contains_pii"] else "否",
                      len(r["table_counts"] or {})) for r in runs)
    restores = q(c, """SELECT restore_id, backup_id, target_db, status, mismatch
                       FROM restore_run ORDER BY started_at DESC LIMIT 5""")
    rrows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (r["restore_id"], r["backup_id"], html.escape(r["target_db"]),
                       r["status"]) for r in restores)
    body = """
<div class="cards" style="margin-bottom:16px">
%s</div>
<div class="card"><h2>执行一次备份</h2>
<form method="post" action="/backup" class="row">
  <div style="flex:0 0 220px"><button type="submit" name="op" value="backup">立即备份</button></div>
  <div style="flex:0 0 220px"><button class="sec" type="submit" name="op" value="verify">校验全部备份</button></div>
  <div style="flex:0 0 240px"><button class="sec" type="submit" name="op" value="prune">清理超期（演练）</button></div>
</form>
<p class="muted">备份 = 全库逻辑备份（pg_dump）+ 用户数据 XLSX 副本（多 sheet）+ 逐文件 sha256 清单。
默认<b>不导出 PII</b>；超期只删文件、记录保留并标记 pruned。</p></div>
<div class="card"><h2>备份记录</h2>
<table><tr><th>备份 ID</th><th>时间</th><th>状态</th><th>大小</th><th>含 PII</th><th>表数</th></tr>
%s</table></div>
<div class="card"><h2>恢复演练记录</h2>
<table><tr><th>恢复 ID</th><th>来源备份</th><th>目标库</th><th>状态</th></tr>
%s</table>
<p class="muted">恢复永远写入<b>新库</b>并逐表核对行数，不覆盖生产库。
可在命令行执行：<code>python ops/backup/backup.py restore --backup &lt;id&gt; --target-db verify</code></p></div>""" % (
        "".join([metric(h["ok_backups"], "可用备份"),
                 metric("%.1f MB" % ((h["total_bytes"] or 0) / 2**20), "备份总量"),
                 metric(h["verified_restores"], "已验证恢复"),
                 metric(h["earliest_expiry"], "最早到期日")]),
        rows or "<tr><td colspan=6>暂无</td></tr>",
        rrows or "<tr><td colspan=4>暂无</td></tr>")
    return page("备份与恢复", body, msg, kind)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MedTalentConsole/0.1"

    def _send(self, body: bytes, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"),
                                     keep_blank_values=True) if n else {}

    def do_GET(self):  # noqa: N802
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        msg = qs.get("msg", [""])[0]
        try:
            with db() as c:
                if p.path == "/":
                    self._send(view_home(c, msg))
                elif p.path == "/jobs":
                    self._send(view_jobs(c, qs, msg))
                elif p.path == "/matrix":
                    self._send(view_matrix(c, qs, msg))
                elif p.path == "/dimension":
                    self._send(view_dimension(c, msg))
                elif p.path == "/intake":
                    self._send(view_intake(c, msg))
                elif p.path == "/match":
                    self._send(view_match(c, qs, msg))
                elif p.path == "/bridge":
                    self._send(view_bridge(c, msg))
                elif p.path == "/backup":
                    self._send(view_backup(c, msg))
                else:
                    self._send(page("未找到", "<div class='card'>页面不存在</div>"), 404)
        except Exception as e:  # noqa: BLE001
            self._send(page("出错", "<div class='card err'>%s: %s</div>"
                            % (type(e).__name__, html.escape(str(e)))), 500)

    def _redirect(self, msg, kind="ok"):
        self.send_response(303)
        self.send_header("Location", "/?" + urllib.parse.urlencode({"msg": msg}))
        self.send_header("X-Msg-Kind", kind)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        f = self._form()
        try:
            if self.path == "/dimension":
                title = (f.get("title") or [""])[0].strip()
                opts = [x.strip() for x in (f.get("options") or [""])[0].split(",") if x.strip()]
                multi = (f.get("multi") or ["0"])[0] == "1"
                if not title or not opts:
                    return self._redirect("标题与选项都不能为空")
                fid = "F_LIVE_" + hashlib.sha1(title.encode()).hexdigest()[:6].upper()
                with db() as c:
                    add_dimension(c, fid, title, opts, multi)
                    c.commit()
                return self._redirect("已新增维度 %s（%d 个选项）——零 DDL，刷新即生效"
                                      % (fid, len(opts)))
            if self.path == "/intake":
                with db() as c:
                    pid = submit_profile(c, f)
                    c.commit()
                return self._redirect("已写入数据库：%s" % pid)
            if self.path == "/bridge":
                use_ex = (f.get("use_example") or [""])[0] == "1"
                use_bad = (f.get("use_bad") or [""])[0] == "1"
                again = (f.get("again") or [""])[0] == "1"
                if use_bad:
                    pkg = fresh_example_package()
                    pkg["memberKey"] = "deadbeef"
                elif again and LAST_PKG.get("pkg"):
                    pkg = LAST_PKG["pkg"]          # 用同一份包验证幂等
                elif use_ex or not (f.get("pkg") or [""])[0].strip():
                    pkg = fresh_example_package()
                    LAST_PKG["pkg"] = pkg
                else:
                    pkg = json.loads((f.get("pkg") or ["{}"])[0])
                    LAST_PKG["pkg"] = pkg
                try:
                    r = ex.ingest(pkg)
                    d = r["data"]
                    return self._redirect(
                        "摄入成功：personId=%s，幂等=%s，facts %d（未映射 %d），"
                        "skillClaims %d（未映射 %d）"
                        % (d["personId"], d.get("idempotent", False), d.get("facts", 0),
                           d.get("unmappedFacts", 0), d.get("skillClaims", 0),
                           d.get("unmappedSkillClaims", 0)))
                except ex.ExchangeError as e:
                    return self._redirect("%s：%s" % (e.code, e.message))
            if self.path == "/backup":
                op = (f.get("op") or ["backup"])[0]
                if op == "backup":
                    rc = bk.do_backup(note="演示控制台触发")
                    return self._redirect("备份完成" if rc == 0 else "备份失败，见控制台输出")
                if op == "verify":
                    rc = bk.do_verify(None, True)
                    return self._redirect("校验通过" if rc == 0 else "校验发现问题，见控制台输出")
                if op == "prune":
                    bk.do_prune(dry_run=True)
                    return self._redirect("已按策略演练清理（未实际删除），详情见控制台输出")
            if self.path == "/reset-live":
                with db() as c:
                    st = reset_live(c)
                    c.commit()
                return self._redirect("已清除演示数据：人才 %d、值 %d、维度 %d"
                                      % (st["persons"], st["values"], st["fields"]))
            self._send(page("未找到", "<div class='card'>未知操作</div>"), 404)
        except Exception as e:  # noqa: BLE001
            self._send(page("出错", "<div class='card err'>%s: %s</div>"
                            % (type(e).__name__, html.escape(str(e)))), 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[console] %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--seed", type=int, metavar="N")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()

    if a.reset:
        with db() as c:
            st = reset_live(c); c.commit()
        print("[✓] 已清除演示数据：人才 %d、值 %d、维度 %d"
              % (st["persons"], st["values"], st["fields"]))
    if a.seed:
        with db() as c:
            if not q(c, "SELECT 1 FROM field_catalog WHERE field_id LIKE %s LIMIT 1",
                     ("F_LIVE%",)):
                add_dimension(c, "F_LIVE_A", "基层服务意愿",
                              ["非常愿意", "愿意", "犹豫", "不愿意"], False)
                add_dimension(c, "F_LIVE_B", "可接受的岗位族",
                              ["临床医疗", "药企医学事务", "CRO/临床研究",
                               "医疗AI/数字健康", "健康险"], True)
                add_dimension(c, "F_LIVE_C", "可接受最低年薪（万）", ["15", "20", "25", "30"],
                              False)
                c.commit()
            n = seed_live(c, a.seed); c.commit()
        print("[✓] 已用 mock 数据生成 %d 条演示人才" % n)
    if a.serve or not (a.seed or a.reset):
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
        print("[✓] 演示控制台： http://127.0.0.1:%d" % a.port)
        print("    所有页面实时读写数据库；Ctrl+C 停止")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[=] 已停止")


if __name__ == "__main__":
    main()
