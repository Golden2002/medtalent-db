# -*- coding: utf-8 -*-
"""
code/demo/portal_admin.py —— 开发者模式里的两个"操作界面"

  ① `/occupation`  **新增职业**（把一个职业写进职业树）
  ② `/import`      **批量导入个体**（粘贴 CSV/TSV，或指定本机表格文件）

为什么单独一个模块
--------------------------------------------------------------------------
`portal_dev.py` 是"开发者模式"（本机、可写、绝不暴露公网，见它的文件头）。
这两个界面都是**写操作**，按项目纪律：写动作只在 GET 渲染、POST 才执行
（防预取/爬虫误触发），并且每一步都留审计。

设计原则（和透视界面一致）
--------------------------------------------------------------------------
· **先给证据、再写库**：导入默认**只做计划**（plan），把"会写什么、缺什么、
  哪些列不认识"摆出来，人看清楚再点"确认导入"。
· **不发明内容**：职业的层级/族/父节点都从库里的实际数据里选，不让人手打 id
  猜错了才报错。
· **错误要能看懂**：把数据库的原话（约束名、拒绝原因）直接展示，
  而不是包装成"操作失败"。
"""
from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys
import tempfile

import psycopg

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "ops"))

import psycopg                                       # noqa: E402
from psycopg.rows import dict_row                    # noqa: E402
import portal as P                                   # noqa: E402

# ⚠ 一开始我凭印象 import 了 `_portal_shared` 并想用它的 esc/page/dict_row，
# 还想用 portal_dev.DSN —— 实测这三样都不存在（portal_dev 只有 PORT，
# 它自己用 P.DSN 连库）。**别猜 API**：按同目录 portal_mockreg.py 的写法来。
esc = P.esc

# 审计里的"谁"：门户在建会话时用 set_config('mt.actor', ...) 写进会话变量，
# 所以取会话变量而不是 session_user（后者永远是连接用的那个超级用户，
# 那样每条审计都会写成 postgres，等于没记谁干的）。
ACTOR = "coalesce(nullif(current_setting('mt.actor', true), ''), session_user)"


def _page(title, body, msg="", kind="info", subtitle="", nav=None):
    import portal_mockreg as MR
    return P.page(title, body, msg=msg, kind=kind, subtitle=subtitle,
                  nav=nav if nav is not None else MR.DEV_NAV())


# ---------------------------------------------------------------------------
# ① 新增职业
# ---------------------------------------------------------------------------
def _occ_form(c, msg="", kind="info", done=None):
    with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
        parents = cc.execute(
            "SELECT occupation_id, label_zh, level, family FROM mt.occupation "
            "WHERE status='active' ORDER BY level, occupation_id").fetchall()
        fams = [r["family"] for r in cc.execute(
            "SELECT DISTINCT family FROM mt.occupation WHERE family IS NOT NULL "
            "ORDER BY 1")]

    n1 = [r for r in parents if r["level"] == 1]
    n2 = [r for r in parents if r["level"] == 2]
    n3 = [r for r in parents if r["level"] == 3]

    def opt(rows, indent=0):
        return "".join(
            '<option value="%s">%s%s（%s · %s）</option>'
            % (esc(r["occupation_id"]), "&nbsp;" * indent, esc(r["label_zh"]),
               esc(r["occupation_id"]), esc(r["family"] or "无族"))
            for r in rows)

    body = (
        '<div class="card"><h2>新增职业（写进职业树）</h2>'
        '<form method="post" action="/occupation">'
        '<input type="hidden" name="act" value="add">'
        '<div class="row">'
        '<div style="flex:1 1 220px"><label>职业编号 occupation_id '
        '<span class="muted">（必填，全局唯一）</span></label>'
        '<input name="oid" placeholder="例如 OCC-F02-10" required></div>'
        '<div style="flex:1 1 200px"><label>中文名 label_zh（必填）</label>'
        '<input name="label" required></div>'
        '<div style="flex:1 1 200px"><label>英文名 label_en</label>'
        '<input name="label_en"></div>'
        '</div><div class="row">'
        '<div style="flex:0 0 120px"><label>层级 level（1/2/3）</label>'
        '<select name="level"><option value="3">3（具体职业）</option>'
        '<option value="2">2（方向）</option><option value="1">1（大类）</option>'
        '</select></div>'
        '<div style="flex:1 1 240px"><label>父节点 parent_id</label>'
        '<select name="parent"><option value="">（无父节点＝顶层）</option>'
        '<optgroup label="层级 1">%s</optgroup>'
        '<optgroup label="层级 2">%s</optgroup>'
        '<optgroup label="层级 3">%s</optgroup></select></div>'
        '<div style="flex:1 1 200px"><label>岗位族 family</label>'
        '<select name="family"><option value="">（无）</option>%s</select></div>'
        '</div><div class="row">'
        '<div style="flex:0 0 170px"><label>医疗依赖 0-5</label>'
        '<input name="med" type="number" min="0" max="5" value="3"></div>'
        '<div style="flex:0 0 170px"><label>转换难度 0-5</label>'
        '<input name="tran" type="number" min="0" max="5" value="3"></div>'
        '<div style="flex:0 0 190px"><label>码状态 code_status</label>'
        '<select name="cs"><option value="N">N 自有</option>'
        '<option value="E">E 外部码待核</option><option value="V">V 已核验</option>'
        '</select></div>'
        '<div style="flex:1 1 320px"><label>说明 description</label>'
        '<input name="desc"></div>'
        '</div>'
        '<div class="row"><div style="flex:1 1 100%%">'
        '<label>为什么加它（会写进审计，必填）</label>'
        '<input name="note" required placeholder="例如：岗位数据里出现 42 条该职业的描述，'
        '但职业树里没有对应节点"></div></div>'
        '<p class="muted">数据库会强制：编号必填且唯一、层级为 1/2/3、'
        '医疗依赖与转换难度取 0–5、code_status 取 V/E/N。'
        '写库同时会留一条审计（谁、什么时候、为什么加了它）。</p>'
        '<button type="submit">写入职业树</button></form></div>' % (
            opt(n1), opt(n2, 2), opt(n3, 4),
            "".join('<option value="%s">%s</option>' % (esc(f), esc(f)) for f in fams)))

    if done:
        # ⚠ done 是 **dict**，必须 .items()。第一版写成 `for k, v in done`，
        # 迭代出的是键名（字符串），拆成两个变量时长度>2 直接抛异常 →
        # 服务端**没发响应就断开连接**，客户端看到 RemoteDisconnected，
        # 而数据库里其实已经写成功了 —— 这种"写成功了但看起来失败"
        # 最容易被误判成"写入失败"，然后人再点一次（好在编号唯一会拦住）。
        rows_html = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v))
                            for k, v in (done.items() if hasattr(done, "items")
                                         else done))
        body = ('<div class="card"><h2>已写入</h2><table>%s</table>'
                '<p><a class="btnlink" href="/tree">到职业树看它</a> '
                '<a class="btnlink sec" href="/occupation">再加一个</a></p></div>'
                % rows_html) + body
    return _page("新增职业", body, msg=msg, kind=kind,
                 subtitle="写进职业树并留审计", nav=None)


def do_occupation(c, qs) -> tuple:
    g = lambda k, d="": (qs.get(k, [d])[0] or d).strip()   # noqa: E731
    oid, label = g("oid"), g("label")
    if not oid or not label:
        return "职业编号与中文名都是必填", "err"
    try:
        level = int(g("level", "3"))
        med = int(g("med", "3"))
        tran = int(g("tran", "3"))
    except ValueError:
        return "层级/医疗依赖/转换难度必须是整数", "err"
    if level not in (1, 2, 3):
        return "层级只能是 1 / 2 / 3", "err"
    if not (0 <= med <= 5) or not (0 <= tran <= 5):
        return "医疗依赖与转换难度只能在 0–5 之间", "err"
    fam, parent = g("family") or None, g("parent") or None
    cs = g("cs", "N")
    if cs not in ("V", "E", "N"):
        return "code_status 只能是 V / E / N", "err"
    note = g("note") or "（未填写理由）"

    with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
        # **先检查再写**：把数据库会报的错提前变成人能看懂的话
        if cc.execute("SELECT 1 FROM mt.occupation WHERE occupation_id=%s",
                      (oid,)).fetchone():
            return "职业编号 %s 已存在 —— 编号必须全局唯一" % oid, "err"
        if parent and not cc.execute("SELECT 1 FROM mt.occupation WHERE occupation_id=%s",
                                     (parent,)).fetchone():
            return "父节点 %s 不存在（不能挂到不存在的节点上）" % parent, "err"
        try:
            cc.execute(
                """INSERT INTO mt.occupation
                     (occupation_id, label_zh, label_en, level, parent_id, family,
                      medical_reliance, transition_ease, description,
                      code_status, code_source, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'web_admin','active')""",
                (oid, label, g("label_en") or None, level, parent, fam, med, tran,
                 g("desc") or None, cs))
            # 直接插入**不会**自动留痕，所以显式写审计（与指南第 5 节同一纪律）。
            # ⚠ 参数必须显式 `::text` 转型：`jsonb_build_object` 的形参是 "any"，
            # psycopg 发出的参数是 unknown 类型，PostgreSQL 推不出来就报
            # `could not determine data type of parameter $2`（实测踩过）。
            # 这个错会让**整条事务回滚**，把职业的 INSERT 一起撤销 ——
            # 那次回滚其实是**正确的**（不允许"没有审计的职业"），
            # 但错误信息完全看不出是参数类型问题，所以在此写明。
            cc.execute(
                """INSERT INTO mt.change_log (actor, object_type, object_name,
                          change_type, detail)
                   VALUES (coalesce(nullif(current_setting('mt.actor', true), ''),
                                   session_user), 'occupation', %s, 'add',
                           jsonb_build_object('op','INSERT','table','occupation',
                             'row', %s::text, 'note', %s::text,
                             'via','portal_admin'))""",
                (oid, "occupation_id=" + oid, note))
            cc.commit()
        except psycopg.Error as e:
            cc.rollback()
            return "数据库拒绝：%s" % str(e).splitlines()[0], "err"
        row = cc.execute("SELECT occupation_id, label_zh, level, parent_id, family, "
                         "status FROM mt.occupation WHERE occupation_id=%s",
                         (oid,)).fetchone()
    return ("职业 %s「%s」已写入（层级 %s、父节点 %s、族 %s）。"
            "它现在就在职业树里了。" % (oid, label, row["level"],
                                        row["parent_id"] or "无", row["family"] or "无"),
            "info", dict(row))


def view_occupation(c, qs, msg="", kind="info", done=None):
    return _occ_form(c, msg, kind, done)


# ---------------------------------------------------------------------------
# ② 批量导入个体
# ---------------------------------------------------------------------------
IMPORT_SAMPLE_HEAD = ["外部编号", "同意个人分析", "当前阶段", "学历", "专业",
                      "意向城市", "意向职业", "海外经历", "备注"]
IMPORT_SAMPLE_ROWS = [
    ["u101", "是", "ST1", "DG3", "MA01", "REG_11", "occ_1", "Y", "正常一行"],
    ["u102", "是", "ST1", "DG2", "", "REG_12", "", "N", "专业空着"],
    ["u103", "否", "ST2", "DG3", "MA02", "REG_11", "occ_2", "Y", "**未授权** → 会被授权门拒绝"],
    ["u104", "是", "", "", "MA03", "", "", "", "几乎全缺"],
    ["u105", "是", "ST1", "DG4", "MA04", "REG_13", "occ_1", "未填写", "写了「未填写」"],
]


def _importer():
    return os.path.join(BASE, "ops", "import_persons.py")


def view_import(c, qs, msg="", kind="info", out=""):
    fields = []
    try:
        with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
            fields = cc.execute(
                "SELECT field_id, title FROM mt.field_catalog "
                "WHERE entity_id='person' AND status='active' ORDER BY field_id"
            ).fetchall()
            n_xls = cc.execute("SELECT count(*) AS n FROM mt.external_identity "
                               "WHERE source_system='xlsimport'").fetchone()["n"]
    except psycopg.Error:
        n_xls = 0
    sample = ",".join(IMPORT_SAMPLE_HEAD) + "\n" + "\n".join(
        ",".join(r) for r in IMPORT_SAMPLE_ROWS)
    known = "、".join("<code>%s</code>" % esc(f["title"]) for f in fields[:14])
    body = (
        '<div class="card"><h2>批量导入个体（表格 → 数据库）</h2>'
        '<p class="muted">两种输入方式：**粘贴表格内容**（从 Excel 直接复制就是'
        '制表符分隔，也能用逗号），或**填本机文件路径**（支持 .xlsx / .csv）。'
        '导入走的是**小程序注册同一条路**（交换包 → bridge），'
        '所以身份、版本门、授权门、幂等这些规则全部生效。</p>'
        '<form method="post" action="/import">'
        '<input type="hidden" name="act" value="run">'
        '<div class="row">'
        '<div style="flex:1 1 320px"><label>本机文件路径（.xlsx / .csv，可空）</label>'
        '<input name="path" placeholder="例如 D:\\data\\注册导出.xlsx"></div>'
        '<div style="flex:0 0 150px"><label>模式</label>'
        '<select name="mode"><option value="plan">只看计划（不写库）</option>'
        '<option value="apply">确认导入（写库）</option></select></div>'
        '</div>'
        '<label>或者：把表格内容粘贴到这里（首行是表头）</label>'
        '<textarea name="text" rows="9" style="width:100%%;font-family:monospace;'
        'font-size:12px">%s</textarea>'
        '<p class="muted">列怎么认：按**中文表头**或**字段 id** 匹配。'
        '能认的字段包括：%s …（完整清单见 '
        '<a href="/portal" target="_blank">数据目录</a>）。'
        '不认识的列**不会进库，但会在报告里列出来**。</p>'
        '<div class="row"><div style="flex:1 1 100%%">'
        '<label>这一批是哪来的（写进导入记录，便于以后精确清理）</label>'
        '<input name="note" placeholder="例如：7 月小程序注册导出"></div></div>'
        '<button type="submit">→ 运行</button></form>'
        '<p class="muted">当前本来源已导入 <b>%d</b> 条身份。'
        '清理用命令行：<code>python ops\\import_persons.py --cleanup --yes</code>'
        '（按来源精确匹配，不用名字前缀）。</p></div>'
        % (esc(sample), known, n_xls))
    if out:
        body = ('<div class="card"><h2>导入报告</h2><pre style="white-space:pre-wrap;'
                'font-size:12px;line-height:1.5">%s</pre></div>' % esc(out)) + body
    return _page("批量导入", body, msg=msg, kind=kind,
                 subtitle="表格 → bridge → 落库（含字段缺失处理）")


def do_import(c, qs) -> tuple:
    g = lambda k, d="": (qs.get(k, [d])[0] or d).strip()   # noqa: E731
    path, text, mode = g("path"), qs.get("text", [""])[0], g("mode", "plan")
    note = g("note")
    tmp = None
    try:
        if path:
            if not os.path.isfile(path):
                return "找不到文件：%s" % path, "err", ""
            src = path
        elif text.strip():
            # 粘贴的内容：Excel 复制是制表符分隔，也容忍逗号。
            # 用嗅探而不是猜：看首行哪个分隔符出现得多。
            first = text.strip().split("\n")[0]
            sep = "\t" if first.count("\t") >= first.count(",") else ","
            fd, tmp = tempfile.mkstemp(suffix=".csv", dir=os.path.join(BASE, ".tools"))
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(text)
            src = tmp
        else:
            return "请填文件路径，或粘贴表格内容", "err", ""

        args = [sys.executable, _importer(), "--file", src]
        if mode == "apply":
            args.append("--apply")
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           cwd=BASE, timeout=600)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        if mode == "apply" and note:
            with psycopg.connect(P.DSN, row_factory=dict_row) as cc:
                # 审计留痕。参数同样要 `::text`（见 do_occupation 里的说明）。
                # 注意：这里在**子进程已经提交之后**才写审计 ——
                # 所以审计写失败不会回滚导入本身。这是刻意的：
                # 导入的数据比"审计没写上"更重要，不该因为审计失败而丢数据；
                # 但也因此**审计失败必须显式暴露**（异常会被下面 except 捕获并回显）。
                cc.execute("""INSERT INTO mt.change_log (actor, object_type, object_name,
                                change_type, detail)
                              VALUES (coalesce(nullif(current_setting('mt.actor', true), ''),
                                              session_user), 'person', 'xlsimport', 'add',
                                      jsonb_build_object('op','BULK_IMPORT',
                                        'source','xlsimport','note',%s::text,
                                        'via','portal_admin'))""", (note,))
                cc.commit()
        kind = "info" if r.returncode == 0 else "err"
        head = ("导入完成（模式：%s）" % ("确认导入" if mode == "apply" else "仅计划"))
        return head, kind, out
    except subprocess.TimeoutExpired:
        return "导入超时（超过 10 分钟）—— 建议拆成小批", "err", ""
    except Exception as e:                                  # noqa: BLE001
        return "运行出错：%s" % str(e).splitlines()[0], "err", ""
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def handle(c, qs):
    """POST 分派：返回渲染好的页面（与 mockreg.handle 同一签名）。"""
    act = (qs.get("act", [""])[0] or "").strip()
    if act == "add":
        res = do_occupation(c, qs)
        if len(res) == 3:
            msg, kind, done = res
            return view_occupation(c, qs, msg, kind, done)
        return view_occupation(c, qs, res[0], res[1])
    if act == "run":
        head, kind, out = do_import(c, qs)
        return view_import(c, qs, head, kind, out)
    return view_occupation(c, qs, "不认识的动作：%s" % act, "err")
