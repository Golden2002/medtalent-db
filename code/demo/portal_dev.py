# -*- coding: utf-8 -*-
"""
code/demo/portal_dev.py —— 开发者模式（**可写**，独立进程与端口）

为什么单独一个进程，而不是给门户加几个路由：

    门户的价值有一半来自"它证明自己只读"——`ops/tests/portal_test.py` 会用
    审计流水（`change_log` 不增长）和 `SELECT 1 INTO t` 被拒来证明这一点。
    一旦同一个进程里存在能写的路由，那条证明就没了：你得先相信"路由没被误挂"。
    所以读写分离成两个进程、两个端口，门户在任何配置下都写不进去。

这一页做什么（三块，都通过**既有且已被测试覆盖的入口**执行，Web 层不重新实现业务逻辑）：

  ① SQL 控制台  多语句、一个事务、**默认试运行（执行后回滚）**，
                勾选提交才落地；逐条给出影响行数
  ② 动态建模    直接调用 `mt.add_dimension / create_instance / set_value /
                deprecate_dimension / register_entity`，并在操作后**给出证据**：
                基础表数、person 列数（证明 DDL 次数 = 0）、表单 schema 行数
  ③ 运维工具    一键驱动 `code/` 与 `ops/` 下已有的 CLI（重建派生层、发现候选、
                重算版本、质量门、复位基线、备份、跑指定回归套件），原样回显输出

启动：
  python code/demo/portal_dev.py --serve        # http://127.0.0.1:8083
  python code/demo/portal_dev.py --check        # 自检：不开服务，渲染各页 + 验证事务回滚
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
for p in (os.path.join(BASE, "code"), os.path.join(BASE, "code", "demo")):
    sys.path.insert(0, p)

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

# 开发者模式（本进程，:8083）用**特权连接** P.admin_db(readonly=False)：
#   · 它是可写的运维工具：要 CREATE TABLE / 加维度 / 跑迁移，等级角色没有 DDL 权限；
#   · 只读门户 :8082 才是面向用户的进程，它用受限角色 mt_portal + SET LOCAL ROLE。
# 因此本进程**绝不允许暴露到公网**（docs/19 的部署纪律），也仍在 ops/health.py
# 的 I4.1 待办里（写路径需要各自的受限写角色，不能在只读角色上顺手放开）。
import portal as P  # noqa: E402
from _portal_shared import PortalError  # noqa: E402  ← 同一个异常类对象，见该模块说明

sys.stdout.reconfigure(encoding="utf-8")

PORT = 8083
TOOL_TIMEOUT = 900
DEV_NAV = [("/", "概览"), ("/sql", "SQL 控制台"), ("/model", "动态建模"),
           ("/tools", "运维工具"), ("/audit", "审计流水"), ("/migrate", "迁移与结构"),
           ("/portal", "← 只读门户")]

# 一键驱动既有 CLI。Web 层不重新实现业务逻辑——这些脚本都有回归测试兜着。
TOOLS = [
    ("quality", "岗位侧质量门（只读）", ["code/gates/jd_quality_gate.py"], False),
    ("rebuild", "重建派生层：概念映射 + 职业映射 + 能力权重版本",
     ["code/analytics/build_competency.py", "--min-sample", "30"], True),
    ("discover", "发现职业/能力候选（写候选池）", ["code/evolve/discover.py"], True),
    ("recompute", "重算能力权重：新增一个版本 + 漂移", ["code/evolve/recompute.py"], True),
    ("history", "能力权重版本历史（只读）", ["code/evolve/recompute.py", "--history"], False),
    ("drift", "最新版本的漂移明细（只读）", ["code/evolve/recompute.py", "--drift"], False),
    ("baseline", "复位职业树基线（恢复被退役的种子节点）",
     ["code/evolve/restore_baseline.py"], True),
    ("backup", "立即备份（全库 + 用户数据 XLSX）", ["ops/backup/backup.py", "backup"], True),
    ("bklist", "列出备份（只读）", ["ops/backup/backup.py", "list"], False),
    ("cat", "静态校验：数据字典（只读）", ["code/validate_catalog.py"], False),
    ("sch", "静态校验：Schema 结构（只读）", ["code/validate_schema.py"], False),
]

SUITES = [
    ("1", "目录与词表静态校验"), ("2", "Schema 静态校验"),
    ("3", "码值一致性（存码不存标签）"), ("4", "门禁回归 + 完整性不变量"),
    ("5", "Schema 冒烟"), ("6", "动态行列增删"), ("7", "动态表单服务"),
    ("8", "L0 采集与去重"), ("9", "小程序 bridge"), ("10", "备份/保留/恢复"),
    ("11", "控制台端到端"), ("12", "演化层（职业树生长）"), ("13", "数据库门户"),
]


# ---------------------------------------------------------------------------
# 语句切分：尊重单引号与 $$ 块，否则 DO $$ ... $$ 会被从中间切断
# ---------------------------------------------------------------------------
def split_sql(script: str) -> list:
    out, buf, i, n = [], [], 0, len(script)
    quote = None
    while i < n:
        ch = script[i]
        two = script[i:i + 2]
        if quote is None:
            if two == "--":
                j = script.find("\n", i)
                j = n if j < 0 else j
                buf.append(script[i:j])
                i = j
                continue
            if ch == "'":
                quote = "'"
            elif two == "$$":
                quote = "$$"
                buf.append("$$")
                i += 2
                continue
            elif ch == ";":
                out.append("".join(buf).strip())
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
        else:
            if quote == "'" and ch == "'":
                if script[i + 1:i + 2] == "'":      # '' 是转义的单引号
                    buf.append("''")
                    i += 2
                    continue
                quote = None
            elif quote == "$$" and two == "$$":
                buf.append("$$")
                i += 2
                quote = None
                continue
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [s for s in out if s and not _is_comment_only(s)]


def _is_comment_only(s: str) -> bool:
    return all(not ln.strip() or ln.strip().startswith("--") for ln in s.splitlines())


def run_script(script: str, commit: bool) -> dict:
    """一个事务里按顺序执行；逐条记录影响行数与结果集。commit=False 时回滚。"""
    stmts = split_sql(script)
    if not stmts:
        return {"ok": False, "error": "没有可执行的语句", "items": [],
                "committed": False, "n_stmts": 0, "ms": 0}
    c = psycopg.connect(P.DSN, row_factory=dict_row)
    items, t0, err = [], time.time(), None
    committed = False
    try:
        with c.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 30000")
            for s in stmts:
                one = {"sql": s, "rowcount": None, "rows": [], "cols": [],
                       "ms": 0, "error": None}
                t1 = time.time()
                try:
                    cur.execute(s)
                    one["rowcount"] = cur.rowcount
                    if cur.description:
                        one["cols"] = [d.name for d in cur.description]
                        one["rows"] = cur.fetchall()[:200]
                except psycopg.Error as e:
                    one["error"] = str(e).splitlines()[0]
                    err = one["error"]
                one["ms"] = int((time.time() - t1) * 1000)
                items.append(one)
                if one["error"]:
                    break
        if commit and not err:
            c.commit()
            committed = True
        else:
            c.rollback()
    finally:
        c.close()
    return {"ok": err is None, "error": err, "items": items,
            "committed": committed, "ms": int((time.time() - t0) * 1000),
            "n_stmts": len(stmts)}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def db_info(c) -> dict:
    return {
        "version": P.q1(c, "SELECT version()"),
        "db_size": P.q1(c, "SELECT pg_size_pretty(pg_database_size(current_database()))"),
        "schema_size": P.q1(c, "SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) "
                               "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                               "WHERE n.nspname=%s AND c.relkind='r'", (P.SCHEMA,)),
        "conn": P.q1(c, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"),
        "encoding": P.q1(c, "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                            "WHERE datname=current_database()"),
        "extensions": P.q(c, "SELECT extname, extversion FROM pg_extension ORDER BY extname"),
    }


def ddl_counter(c) -> dict:
    """扩展能力的"证据"：基础表数与 person 列数。加维度前后这两个数必须不变。"""
    return {
        "tables": P.q1(c, "SELECT count(*) FROM pg_class cc JOIN pg_namespace n "
                          "ON n.oid=cc.relnamespace WHERE n.nspname=%s AND cc.relkind='r'",
                       (P.SCHEMA,)),
        "person_cols": P.q1(c, "SELECT count(*) FROM pg_attribute a "
                               "WHERE a.attrelid='mt.person'::regclass AND a.attnum>0 "
                               "AND NOT a.attisdropped"),
        "fields": P.q1(c, "SELECT count(*) FROM field_catalog"),
        "attr_defs": P.q1(c, "SELECT count(*) FROM attribute_definition"),
        "form_rows": P.q1(c, "SELECT count(*) FROM v_form_schema"),
        "code_values": P.q1(c, "SELECT count(*) FROM code_value"),
    }


def ddl_evidence(before: dict, after: dict) -> str:
    def cell(k, label):
        b, a = before[k], after[k]
        delta = a - b
        arrow = ('<span class="pill p-t">+%d</span>' % delta if delta > 0 else
                 ('<span class="pill p-n">不变</span>' if delta == 0 else
                  '<span class="pill p-red">%d</span>' % delta))
        return ('<tr><td>%s</td><td class="n">%s</td><td class="n">%s</td><td>%s</td></tr>'
                % (P.esc(label), f"{b:,}", f"{a:,}", arrow))
    return ('<div class="card"><h2>证据：这次操作有没有做 DDL</h2>'
            '<table><tr><th>指标</th><th class="n">操作前</th><th class="n">操作后</th>'
            '<th>变化</th></tr>'
            + cell("tables", "基础表数量") + cell("person_cols", "person 表列数")
            + cell("fields", "已登记字段") + cell("attr_defs", "扩展属性定义")
            + cell("form_rows", "表单 schema 行数") + cell("code_values", "代码值")
            + '</table>'
            '<p class="muted"><b>基础表数量与 person 列数必须不变</b>——'
            '这就是"加字段/加行没有做 DDL"的证据。变的只能是字段目录、属性定义、'
            '表单 schema 与码值。</p></div>')


def tag() -> str:
    return secrets.token_hex(3)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def view_home(c, qs, msg="", kind="info") -> bytes:
    i = db_info(c)
    d = ddl_counter(c)
    ext = "、".join("%s %s" % (x["extname"], x["extversion"]) for x in i["extensions"])
    h = P.q(c, "SELECT * FROM v_evolution_health")[0]
    body = """
<div class="note err"><b>这是可写的开发者模式，独立进程、独立端口。</b>
只监听 127.0.0.1。<u>只读门户（:8082）在任何配置下都写不进去</u>——
读写分离成两个进程，是为了让"门户只读"这件事可以被证明。</div>

<div class="cards">%s</div>

<div class="grid2">
<div class="card"><h2>数据库</h2><table>
<tr><td>版本</td><td><code>%s</code></td></tr>
<tr><td>库大小</td><td>%s（schema <code>mt</code> 的表占 %s）</td></tr>
<tr><td>连接数</td><td>%s</td></tr>
<tr><td>字符集</td><td>%s</td></tr>
<tr><td>扩展</td><td><code>%s</code></td></tr>
<tr><td>schema</td><td><code>%s</code></td></tr>
</table>
<p class="muted">连接串：<code>host=127.0.0.1 port=55432 dbname=medtalent user=postgres</code>
（<code>options='-c search_path=mt,public'</code>；trust 认证，仅回环）。</p></div>
<div class="card"><h2>演化层健康度</h2><table>
<tr><td>在用节点</td><td class="n">%s</td><td>已退役</td><td class="n">%s</td></tr>
<tr><td>迁移记录</td><td class="n">%s</td><td>重算版本</td><td class="n">%s</td></tr>
<tr><td>候选待评审（岗位）</td><td class="n">%s</td><td>（能力）</td><td class="n">%s</td></tr>
</table>
%s</div>
</div>

<div class="card"><h2>开发能力</h2>
<table><tr><th>页面</th><th>能做什么</th><th>写库？</th></tr>
<tr><td><a href="/sql">SQL 控制台</a></td>
    <td>多语句、单事务、<b>默认试运行（执行后回滚）</b>；勾选提交才落地；逐条给出影响行数与结果集</td>
    <td>可写（需勾选）</td></tr>
<tr><td><a href="/model">动态建模</a></td>
    <td>调用 <code>add_dimension</code> / <code>create_instance</code> / <code>set_value</code> /
        <code>deprecate_dimension</code> / <code>register_entity</code>，每次操作后给出
        "基础表数 / person 列数不变"的证据</td>
    <td>可写</td></tr>
<tr><td><a href="/tools">运维工具</a></td>
    <td>一键驱动既有 CLI：重建派生层、发现候选、重算版本、质量门、复位基线、备份、跑指定回归套件</td>
    <td>视工具而定</td></tr>
<tr><td><a href="/audit">审计流水</a></td><td><code>change_log</code> 最近变更 + 按对象统计（append-only）</td>
    <td>只读</td></tr>
<tr><td><a href="/migrate">迁移与结构</a></td><td>12 个迁移文件清单、schema 对象统计、触发器清单</td>
    <td>只读</td></tr>
</table>
<p class="muted">刻意<b>没有</b>做的东西：不做删库/删表，不做 PII 明文导出，不做绕过约束的写入。
开发能力不等于放弃纪律。</p></div>

<div class="card"><h2>当前结构计数</h2>%s</div>
""" % ("".join([
        P.metric(f"{i['db_size']}", "库大小"),
        P.metric(f"{d['tables']}", "基础表"),
        P.metric(f"{d['fields']:,}", "已登记字段"),
        P.metric(f"{d['code_values']:,}", "代码值"),
        P.metric(f"{h['runs']}", "能力权重版本"),
        P.metric(f"{h['active_nodes']}", "职业树节点"),
    ]),
        P.esc(i["version"].split(" on ")[0]),
        P.esc(i["db_size"]), P.esc(i["schema_size"]), P.esc(i["conn"]),
        P.esc(i["encoding"]), P.esc(ext), P.esc(P.SCHEMA),
        f"{h['active_nodes']}", f"{h['retired_nodes']}", f"{h['migrations']}",
        f"{h['runs']}", f"{h['occ_ready']}", f"{h['con_ready']}",
        P.sql_box("SELECT * FROM v_evolution_health;"),
        ddl_evidence(d, d))
    return P.page("开发者模式", body, msg, kind, nav=DEV_NAV,
                  subtitle="可写 · 独立进程 · 只监听回环")


EXAMPLES = [
    ("看基础表数量（加维度前后应不变）",
     "SELECT count(*) AS tables FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace\n"
     " WHERE n.nspname='mt' AND c.relkind='r';"),
    ("看一张表的列（确认没有 ALTER TABLE）",
     "SELECT count(*) AS person_cols FROM pg_attribute\n"
     " WHERE attrelid='mt.person'::regclass AND attnum>0 AND NOT attisdropped;"),
    ("当前表单 schema（前 10 行）",
     "SELECT * FROM v_form_schema ORDER BY entity_id, form_order LIMIT 10;"),
    ("代码表与取值数",
     "SELECT t.code_table_id, count(v.code) AS vals\n"
     "  FROM code_table t LEFT JOIN code_value v USING (code_table_id)\n"
     " GROUP BY 1 ORDER BY 2 DESC LIMIT 10;"),
    ("事务语义演示：建临时表再查（试运行模式下会被回滚）",
     "CREATE TEMP TABLE _demo_tmp(x int);\nINSERT INTO _demo_tmp VALUES (1),(2),(3);\n"
     "SELECT count(*) AS inserted FROM _demo_tmp;"),
    ("故意写错，看错误怎么呈现", "SELECT * FROM no_such_table;"),
]


def view_sql(c, qs, msg="", kind="info", execute=False) -> bytes:
    """execute=False（GET）：只把 SQL 填进表单，不执行——避免链接预取触发写库。
    execute=True（POST）：真的执行。"""
    script = qs.get("q", [""])[0]
    commit = qs.get("commit", [""])[0] == "1"
    result = ""
    if script and execute:
        r = run_script(script, commit)
        parts = []
        for k, it in enumerate(r["items"], 1):
            if it["error"]:
                parts.append('<div class="note err">第 %d 条失败：%s</div><pre>%s</pre>'
                             % (k, P.esc(it["error"]), P.esc(it["sql"])))
                continue
            rc = it["rowcount"]
            rc_txt = ("影响 %s 行" % f"{rc:,}") if rc is not None and rc >= 0 else "OK"
            tbl = (P.render_rows(None, it["cols"], it["rows"], maxlen=60)
                   if it["cols"] else "")
            parts.append('<div class="note ok">第 %d 条：%s · %d ms</div><pre>%s</pre>%s'
                         % (k, P.esc(rc_txt), it["ms"],
                            P.esc(it["sql"] if len(it["sql"]) < 1200
                                  else it["sql"][:1200] + "…"), tbl))
        banner = ('<div class="note %s">%s 共 %d 条语句，用时 %d ms。%s</div>'
                  % ("ok" if r["committed"] else "info",
                     "已<b>提交</b>。" if r["committed"] else "已<b>回滚</b>（试运行）。",
                     r["n_stmts"], r["ms"],
                     "" if r["committed"] else
                     "数据库状态没有改变。确认影响行数无误后，勾选「提交」再执行一次。"))
        result = banner + "".join(parts)

    ex = "".join('<div class="note info"><a href="/sql?q=%s">%s</a>'
                 '<span class="muted">（点它只填入表单，执行请按按钮）</span></div>'
                 % (urllib.parse.quote(s), P.esc(t)) for t, s in EXAMPLES)
    body = """
<div class="note err"><b>可写。</b>默认<b>试运行</b>：语句在一个事务里按顺序执行，
执行完<b>回滚</b>，让你先看到每条语句的影响行数与结果集；确认无误后勾选「提交」再执行一次。
语句超时 30 秒。语句切分尊重单引号与 <code>$$</code> 块。</div>
<div class="card"><form method="post" action="/sql">
<label>SQL（可多语句，用分号分隔）</label>
<textarea name="q" rows="8" spellcheck="false">%s</textarea>
<div class="row" style="margin-top:8px">
  <div style="flex:0 0 300px">
    <label style="font-weight:400"><input type="checkbox" name="commit" value="1" %s
      style="width:auto"> 提交（不勾选则执行后回滚）</label></div>
  <div style="flex:0 0 110px"><button type="submit">执行</button></div>
  <div style="flex:0 0 110px"><a href="/sql"><button type="button" class="sec">清空</button></a></div>
</div></form>
<p class="muted">写操作只走 <code>POST</code>：<code>GET /sql</code> 只渲染表单，
所以链接预取、爬虫或被误点的示例链接都不会改到数据库。</p></div>
%s
<div class="card"><h2>随手可试</h2>%s</div>
""" % (P.esc(script), "checked" if commit else "", result, ex)
    return P.page("SQL 控制台", body, msg, kind, nav=DEV_NAV,
                  subtitle="多语句 · 单事务 · 默认试运行后回滚 · 写走 POST")


def view_model(c, qs, msg="", kind="info", evidence="") -> bytes:
    d = ddl_counter(c)
    fields = P.q(c, """SELECT entity_id, field_id, title, data_type, cardinality,
                              is_required, option_count, form_section
                         FROM v_form_schema ORDER BY entity_id, form_order LIMIT 200""")
    ents = P.q(c, "SELECT entity_id, table_name, domain, kind, status "
                  "FROM entity_catalog ORDER BY entity_id LIMIT 100")
    attrs = P.q(c, "SELECT attr_key, entity_id, title, data_type, code_table_id, status, "
                  "promoted_to_field, created_at FROM attribute_definition "
                  "ORDER BY created_at DESC LIMIT 30")
    fv = P.q(c, """SELECT value_id, field_id, subject_type, subject_id,
                          value_code, value_num, value_text, recorded_at
                     FROM field_value ORDER BY recorded_at DESC LIMIT 30""")

    body = """
<div class="note err"><b>可写。</b>这些操作直接调用数据库里的 PL/pgSQL 接口，
每一次都在一个事务里执行并提交。操作后立刻给出"有没有做 DDL"的证据。</div>
%s
<div class="cards">%s</div>

<div class="grid2">
<div class="card"><h2>① 新增一个画像维度（加「列」）</h2>
<form method="post" action="/model">
<input type="hidden" name="act" value="add_dim">
<div><label>实体</label><input name="entity" value="person"></div>
<div><label>字段 ID</label><input name="field_id" value="F_DEV_%s"></div>
<div><label>标题</label><input name="title" value="是否愿意值夜班"></div>
<div><label>选项（每行一个 <code>码=标签</code>）</label>
<textarea name="options" rows="3">A1=非常愿意
A2=可以接受
A3=不愿意</textarea></div>
<button type="submit">调用 add_dimension()</button>
</form>
<p class="muted">等价于 <code>SELECT mt.add_dimension(entity, field_id, title, options)</code>。
选项会写进 <code>code_table</code> + <code>code_value</code>。</p></div>

<div class="card"><h2>② 建一条记录（加「行」）</h2>
<form method="post" action="/model">
<input type="hidden" name="act" value="make_row">
<div><label>实体</label><input name="entity" value="person"></div>
<div><label>记录 ID</label><input name="subject_id" value="per_dev_%s"></div>
<button type="submit">调用 create_instance()</button>
</form>
<p class="muted">等价于 <code>SELECT mt.create_instance('person', id, payload)</code>。
未登记的键会被数据库直接拒绝——这是「字段必须登记」纪律的执行者。</p></div>
</div>

<div class="grid2">
<div class="card"><h2>③ 给记录写一个值</h2>
<form method="post" action="/model">
<input type="hidden" name="act" value="set_val">
<div><label>实体</label><input name="entity" value="person"></div>
<div><label>记录 ID</label><input name="subject_id" value="per_dev_%s"></div>
<div><label>字段 ID</label><input name="field_id" value=""></div>
<div><label>选项码（可空）</label><input name="code" value=""></div>
<button type="submit">调用 set_value()</button>
</form>
<p class="muted">写入 <code>field_value</code> 统一值表；
未在词表里定义的选项会被 <code>assert_option</code> 拒绝。</p></div>

<div class="card"><h2>④ 废弃一个维度（软删）</h2>
<form method="post" action="/model">
<input type="hidden" name="act" value="deprecate">
<div><label>字段 ID</label><input name="field_id" value=""></div>
<div><label>原因</label><input name="reason" value="演示：改用新维度"></div>
<button type="submit">调用 deprecate_dimension()</button>
</form>
<p class="muted"><b>软删</b>：维度从表单里消失，但已写入的值<b>一条不丢</b>。</p></div>
</div>

<div class="card"><h2>⑤ 登记一个新实体</h2>
<form method="post" action="/model" class="row">
<input type="hidden" name="act" value="reg_entity">
<div><label>实体 ID</label><input name="entity_id" value="lab_project_%s"></div>
<div><label>域</label><input name="domain" value="talent"></div>
<div><label>类型</label><input name="kind" value="EK2"></div>
<div style="flex:0 0 170px"><button type="submit">register_entity()</button></div>
</form>
<p class="muted">登记后该实体就能承载 <code>field_catalog</code> 里的字段与
<code>field_value</code> 的值。</p></div>

<div class="card"><h2>当前表单 schema（<code>v_form_schema</code>）
<span class="muted">· %d 行</span></h2>%s</div>
<div class="grid2">
<div class="card"><h2>扩展属性定义 <span class="muted">· %d 行</span></h2>%s</div>
<div class="card"><h2>统一值表 <code>field_value</code> 最近 30 行
<span class="muted">· 表内共 %s 行</span></h2>%s</div>
</div>
<div class="card"><h2>已登记实体 <span class="muted">· %d 个</span></h2>%s</div>
%s
""" % (evidence,
       "".join([P.metric(f"{d['tables']}", "基础表"),
                P.metric(f"{d['person_cols']}", "person 列数"),
                P.metric(f"{d['fields']:,}", "已登记字段"),
                P.metric(f"{d['attr_defs']:,}", "属性定义"),
                P.metric(f"{d['form_rows']:,}", "表单 schema 行"),
                P.metric(f"{d['code_values']:,}", "代码值")]),
       P.esc(tag()), P.esc(tag()), P.esc(tag()), P.esc(tag()),
       len(fields),
       P.render_rows(None, list(fields[0].keys()), fields[:12], maxlen=34) if fields
       else '<p class="muted">（空）</p>',
       len(attrs),
       P.render_rows(None, list(attrs[0].keys()), attrs, maxlen=30) if attrs
       else '<p class="muted">（空）——机制 B 目前没有被使用过。</p>',
       f"{P.q1(c, 'SELECT count(*) FROM field_value'):,}",
       P.render_rows(None, list(fv[0].keys()), fv, maxlen=28) if fv
       else '<p class="muted">（空）——还没有通过动态建模写入过值。</p>',
       len(ents),
       P.render_rows(None, list(ents[0].keys()), ents[:12], maxlen=30) if ents else "",
       P.sql_box("""SELECT mt.add_dimension('person','F_X','标题',
       '[{"code":"A1","label":"选项"}]'::jsonb);
SELECT mt.create_instance('person','per_x',
       '{"person_id":"per_x","subject_code":"MT-1"}'::jsonb);
SELECT mt.set_value('person','per_x','F_X','A1');
SELECT mt.deprecate_dimension('F_X','原因');
SELECT * FROM v_form_schema WHERE entity_id='person';"""))
    return P.page("动态建模", body, msg, kind, nav=DEV_NAV,
                  subtitle="加列 / 建行 / 写值 / 废弃 —— 全部零 DDL")


def do_model(c, qs) -> tuple:
    """执行一个动态建模操作，返回 (消息, 类型, 证据 HTML)。"""
    act = qs.get("act", [""])[0]
    before = ddl_counter(c)
    entity = (qs.get("entity", ["person"])[0] or "person").strip()
    msg, kind = "", "info"
    try:
        with c.cursor() as cur:
            if act == "add_dim":
                fid = (qs.get("field_id", [""])[0] or "").strip()
                title = (qs.get("title", [""])[0] or "").strip()
                opts = []
                for line in (qs.get("options", [""])[0] or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    code, _sep, label = line.partition("=")
                    opts.append({"code": code.strip(), "label": (label or code).strip()})
                if not fid or not title:
                    raise PortalError(400, "字段 ID 与标题都必填")
                cur.execute("SELECT mt.add_dimension(%s, %s, %s, %s::jsonb)",
                            (entity, fid, title, json.dumps(opts, ensure_ascii=False)))
                msg = "已新增维度 %s（%d 个选项）——刷新表单即可看到它" % (fid, len(opts))
            elif act == "make_row":
                sid = (qs.get("subject_id", [""])[0] or "").strip()
                if not sid:
                    raise PortalError(400, "记录 ID 必填")
                payload = ({"person_id": sid, "subject_code": "MT-DEV-" + sid[-6:]}
                           if entity == "person" else {"project_id": sid})
                cur.execute("SELECT mt.create_instance(%s, %s, %s::jsonb)",
                            (entity, sid, json.dumps(payload, ensure_ascii=False)))
                msg = "已创建记录 %s（实体 %s）" % (sid, entity)
            elif act == "set_val":
                sid = (qs.get("subject_id", [""])[0] or "").strip()
                fid = (qs.get("field_id", [""])[0] or "").strip()
                code = (qs.get("code", [""])[0] or "").strip() or None
                if not sid or not fid:
                    raise PortalError(400, "记录 ID 与字段 ID 都必填")
                cur.execute("SELECT mt.set_value(%s, %s, %s, %s)", (entity, sid, fid, code))
                msg = "已写入 %s.%s = %s" % (sid, fid, code or "（空）")
            elif act == "deprecate":
                fid = (qs.get("field_id", [""])[0] or "").strip()
                if not fid:
                    raise PortalError(400, "字段 ID 必填")
                cur.execute("SELECT mt.deprecate_dimension(%s, %s)",
                            (fid, qs.get("reason", [""])[0] or None))
                msg = "已废弃维度 %s（数据未删除，只是从表单里消失）" % fid
            elif act == "reg_entity":
                eid = (qs.get("entity_id", [""])[0] or "").strip()
                if not eid:
                    raise PortalError(400, "实体 ID 必填")
                cur.execute("SELECT mt.register_entity(%s, %s, %s)",
                            (eid, qs.get("domain", ["talent"])[0],
                             qs.get("kind", ["EK2"])[0]))
                msg = "已登记实体 %s" % eid
            else:
                return "", "info", ""
        c.commit()
        kind = "ok"
    except PortalError as e:
        c.rollback()
        msg, kind = e.message, "err"
    except psycopg.Error as e:
        c.rollback()
        msg, kind = "数据库拒绝：" + str(e).splitlines()[0], "err"
    return msg, kind, ddl_evidence(before, ddl_counter(c))


def view_tools(c, qs, out="", msg="", kind="info") -> bytes:
    rows = "".join(
        '<tr><td><b>%s</b></td><td><code>%s</code></td><td>%s</td>'
        '<td><a href="/tools?run=%s">运行</a></td></tr>'
        % (P.esc(title), P.esc(" ".join(cmd)),
           '<span class="pill p-red">写库</span>' if wr else '<span class="pill p-t">只读</span>',
           k)
        for k, title, cmd, wr in TOOLS)
    suite_opts = "".join('<option value="%s">%s · %s</option>' % (n, n, P.esc(t))
                         for n, t in SUITES)
    body = """
<div class="note err"><b>可写。</b>这些按钮会真的改动数据库。Web 层<b>不重新实现</b>业务逻辑——
它只驱动 <code>code/</code> 与 <code>ops/</code> 下那些已经被回归测试覆盖的 CLI。
输出原样回显，不做美化。</div>

<div class="card"><h2>运维工具</h2>
<table><tr><th>工具</th><th>命令</th><th>写库</th><th></th></tr>%s</table></div>

<div class="card"><h2>跑指定回归套件</h2>
<form method="post" action="/tools" class="row">
<input type="hidden" name="run" value="suite">
<div><label>套件</label><select name="suite">%s</select></div>
<div style="flex:0 0 120px"><button type="submit">运行</button></div>
</form>
<p class="muted">等价于 <code>python ops\\tests\\run_all.py --only N</code>。
完整回归（13 套 + 2 个收尾）在命令行跑：<code>python ops\\tests\\run_all.py</code>。
注意：套件 12（演化层）会拆分真实种子节点，跑完请点一次「复位职业树基线」。</p></div>

%s
""" % (rows, suite_opts, out)
    return P.page("运维工具", body, msg, kind, nav=DEV_NAV,
                  subtitle="驱动既有 CLI · 输出原样回显")


def run_tool(key: str, extra=None) -> tuple:
    if key == "suite":
        n = (extra or {}).get("suite", ["1"])[0]
        cmd = [sys.executable, os.path.join(BASE, "ops", "tests", "run_all.py"),
               "--only", n]
        label = "回归套件 %s" % n
    else:
        hit = [t for t in TOOLS if t[0] == key]
        if not hit:
            return '<div class="note err">未知工具：%s</div>' % P.esc(key), "err"
        _k, label, argv, _w = hit[0]
        cmd = [sys.executable] + [os.path.join(BASE, a.replace("/", os.sep)) for a in argv]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=BASE, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=TOOL_TIMEOUT)
        out = r.stdout.decode("utf-8", "replace")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = "（超时 %d 秒）" % TOOL_TIMEOUT, -1
    cli = " ".join(('"%s"' % x if " " in x else x) for x in cmd)
    head = ('<div class="card"><h2>%s <span class="muted">· 退出码 %s · %.1f 秒</span></h2>'
            '<details open><summary>命令：<code>%s</code></summary><pre>%s</pre></details></div>'
            % (P.esc(label), code, time.time() - t0, P.esc(cli), P.esc(out)))
    return head, ("ok" if code == 0 else "err")


def view_audit(c, qs) -> bytes:
    n = P.q1(c, "SELECT count(*) FROM change_log")
    recent = P.q(c, """SELECT at, actor, object_type, object_name, change_type, detail
                         FROM change_log ORDER BY at DESC LIMIT 60""")
    byobj = P.q(c, """SELECT object_type AS "对象类型", count(*) AS "变更次数",
                             count(DISTINCT object_name) AS "涉及对象数", max(at) AS "最近"
                        FROM change_log GROUP BY 1 ORDER BY 2 DESC LIMIT 20""")
    body = """
<div class="sub">这个库的审计纪律是<b>触发器强制</b>的：除了一小份跳过清单，
每张表的每次写入都会自动落一行 <code>change_log</code>，只增不改。
这也是「门户只读」能被证明的原因——遍历门户所有页面后，这里的行数不变。</div>
<div class="cards">%s</div>
<div class="card"><h2>变更流水 <span class="muted">· 最近 60 条</span></h2>%s</div>
<div class="card"><h2>按对象类型统计</h2>%s</div>
%s
""" % ("".join([P.metric(f"{n:,}", "审计流水行数"),
                P.metric(f"{P.q1(c, 'SELECT count(DISTINCT object_name) FROM change_log'):,}",
                         "涉及对象数"),
                P.metric(f"{P.q1(c, 'SELECT count(DISTINCT object_type) FROM change_log'):,}",
                         "对象类型")]),
       P.render_rows("change_log", list(recent[0].keys()), recent, maxlen=48) if recent
       else '<p class="muted">（空）</p>',
       P.render_rows(None, list(byobj[0].keys()), byobj, maxlen=30) if byobj else "",
       P.sql_box("SELECT * FROM change_log ORDER BY at DESC LIMIT 60;"))
    return P.page("审计流水", body, nav=DEV_NAV, subtitle="append-only · 触发器强制")


def view_migrate(c, qs) -> bytes:
    d = os.path.join(BASE, "schema", "sql")
    files = sorted(f for f in os.listdir(d) if f.endswith(".sql")) if os.path.isdir(d) else []
    m = P.meta()
    frows = []
    for f in files:
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            text = fh.read()
        creates = re.findall(
            r"CREATE\s+(?:TABLE|VIEW|MATERIALIZED VIEW)\s+(?:IF NOT EXISTS\s+)?([a-z_]+)",
            text, re.I)
        applied = "—"
        if creates:
            applied = ("<span class='pill p-t'>已应用</span>" if creates[0] in m["by_name"]
                       else "<span class='pill p-e'>未见对象</span>")
        frows.append({"文件": f, "行数": len(text.splitlines()),
                      "CREATE 对象数": len(creates),
                      "首个对象": creates[0] if creates else "",
                      "是否已应用": applied})
    cols_lvl = P.q(c, """SELECT c.relname AS "表", count(a.attnum) AS "列数",
                                (SELECT count(*) FROM pg_constraint x
                                  WHERE x.conrelid=c.oid) AS "约束",
                                (SELECT count(*) FROM pg_trigger t
                                  WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) AS "触发器"
                           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                           LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0
                                 AND NOT a.attisdropped
                          WHERE n.nspname=%s AND c.relkind='r'
                          GROUP BY c.oid, c.relname ORDER BY 4 DESC, 3 DESC LIMIT 20""",
                       (P.SCHEMA,))
    body = """
<div class="note info">迁移文件是<b>唯一</b>的结构变更入口：每个文件自带
<code>BEGIN; … COMMIT;</code> 与 <code>SET search_path TO mt, public;</code>，
约束用 <code>DROP CONSTRAINT IF EXISTS</code> + <code>ADD</code> 写成可重放。
应用命令：<code>python ops\\pg.py apply</code>。
<code>004_vector.sql</code> 需要 pgvector，本机不可用，<b>按设计跳过</b>。</div>
<div class="card"><h2>迁移文件（%d 个）</h2>%s</div>
<div class="card"><h2>结构最复杂的表（按触发器与约束排）</h2>%s</div>
<div class="card"><h2>schema 对象统计</h2><table>
<tr><td>基础表</td><td class="n">%d</td><td>视图</td><td class="n">%d</td></tr>
<tr><td>列</td><td class="n">%d</td><td>外键</td><td class="n">%d</td></tr>
<tr><td>索引</td><td class="n">%d</td><td>触发器</td><td class="n">%d</td></tr>
<tr><td>函数</td><td class="n">%d</td><td>自定义域</td><td class="n">%d</td></tr>
</table>
<p class="muted">注意：静态校验器报 <b>79</b> 张表，实际库只有 <b>78</b> 张 ——
差额是 <code>004_vector.sql</code> 里的 <code>embedding</code>。
静态计数与运行态不一致时，<b>以运行态为准</b>。</p></div>
%s
""" % (len(files),
       P.render_rows(None, list(frows[0].keys()), frows, maxlen=40) if frows else "",
       P.render_rows(None, list(cols_lvl[0].keys()), cols_lvl, maxlen=30) if cols_lvl else "",
       len([r for r in m["rels"] if r["kind"] == "table"]),
       len([r for r in m["rels"] if r["kind"] != "table"]),
       sum(len(v) for v in m["cols"].values()),
       sum(1 for t in m["cons"] for x in m["cons"][t] if x["kind"] == "f"),
       sum(len(v) for v in m["idx"].values()),
       sum(len(v) for v in m["trg"].values()),
       P.q1(c, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
               "WHERE n.nspname=%s", (P.SCHEMA,)),
       P.q1(c, "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
               "WHERE n.nspname=%s AND t.typtype='d'", (P.SCHEMA,)),
       P.sql_box("SELECT c.relname, count(a.attnum) FROM pg_class c "
                 "JOIN pg_namespace n ON n.oid=c.relnamespace "
                 "LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 "
                 "WHERE n.nspname='mt' AND c.relkind='r' GROUP BY 1;"))
    return P.page("迁移与结构", body, nav=DEV_NAV, subtitle="结构变更的唯一入口")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class DevHandler(BaseHTTPRequestHandler):
    server_version = "MedTalentDevMode/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(body)

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return urllib.parse.parse_qs(raw)

    def _dispatch(self, c, path, qs, write: bool):
        """write=True 时才允许执行写动作；GET 一律只渲染。"""
        if path == "/sql":
            return self._send(200, view_sql(c, qs, execute=write))
        if path == "/model":
            if write and qs.get("act", [""])[0]:
                msg, kind, ev = do_model(c, qs)
                return self._send(200, view_model(c, qs, msg, kind, ev))
            return self._send(200, view_model(c, qs))
        if path == "/tools":
            if write and qs.get("run", [""])[0]:
                out, kind = run_tool(qs["run"][0], qs)
                return self._send(200, view_tools(c, qs, out, "", kind))
            return self._send(200, view_tools(c, qs))
        if path == "/mockreg":
            # mock 小程序注册窗口：**真实写库**，走 bridge 而不是直写 person。
            # GET 只渲染表单；POST 才执行（防预取/爬虫误触发写库，与 /sql 同一纪律）。
            import portal_mockreg as MR
            if write and qs.get("act", [""])[0]:
                return self._send(200, MR.handle(c, qs))
            return self._send(200, MR.view(c, qs))
        raise PortalError(404, "没有这个页面：%s" % path)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/portal":
                return self._send(200, P.page(
                    "只读门户在另一个端口",
                    '<div class="note info">只读门户在 <b>http://127.0.0.1:8082</b>'
                    '（用 <code>python code\\demo\\portal.py --serve</code> 启动）。<br>'
                    '开发者模式是<b>另一个进程</b>，因为它能写库——'
                    '分开才能证明门户在任何配置下都写不进去。</div>',
                    nav=DEV_NAV, subtitle="信任边界"))
            with P.admin_db(readonly=False) as c:
                if path == "/":
                    return self._send(200, view_home(c, qs))
                if path in ("/sql", "/model", "/tools"):
                    # GET 只渲染表单（或把 SQL 填进去），绝不执行 —— 防预取/爬虫误触发写库
                    return self._dispatch(c, path, qs, write=False)
                if path == "/audit":
                    return self._send(200, view_audit(c, qs))
                if path == "/migrate":
                    return self._send(200, view_migrate(c, qs))
                return self._send(404, P.page(
                    "未找到", "<div class='note err'>没有这个页面</div>", nav=DEV_NAV))
        except PortalError as e:
            return self._send(e.status, P.page(
                "出错了", "<div class='note err'>%s</div>" % P.esc(e.message), nav=DEV_NAV))
        except psycopg.Error as e:
            return self._send(500, P.page(
                "数据库错误", "<div class='note err'>%s</div>"
                % P.esc(str(e).splitlines()[0]), nav=DEV_NAV))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = self._form()
        try:
            with P.admin_db(readonly=False) as c:
                return self._dispatch(c, path, qs, write=True)
        except PortalError as e:
            return self._send(e.status, P.page(
                "出错了", "<div class='note err'>%s</div>" % P.esc(e.message), nav=DEV_NAV))
        except psycopg.Error as e:
            return self._send(500, P.page(
                "数据库错误", "<div class='note err'>%s</div>"
                % P.esc(str(e).splitlines()[0]), nav=DEV_NAV))


def serve(port=PORT):
    P.meta()
    srv = ThreadingHTTPServer(("127.0.0.1", port), DevHandler)
    print("[!] 开发者模式（可写）已启动：http://127.0.0.1:%d" % port)
    print("    只监听回环地址。所有写操作走事务；SQL 控制台默认试运行后回滚。")
    print("    只读门户请另起：python code\\demo\\portal.py --serve  (:8082)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[✓] 已停止")
    finally:
        srv.server_close()


def cmd_check():
    fails = []
    cases = [("SELECT 1; SELECT 2;", 2),
             ("SELECT 'a;b' AS x; SELECT 2;", 2),
             ("DO $$ BEGIN RAISE NOTICE 'x;y'; END $$; SELECT 1;", 2),
             ("-- 注释; 里也有分号\nSELECT 1;", 1),
             ("SELECT 'it''s ok';", 1)]
    n_bad = 0
    for script, want in cases:
        got = len(split_sql(script))
        if got != want:
            n_bad += 1
            fails.append("split_sql 期望 %d 条、得到 %d 条：%r" % (want, got, script))
    print("  [%s] 语句切分：单引号 / $$ 块 / 注释 / 转义引号（%d 个用例）"
          % ("PASS" if not n_bad else "FAIL", len(cases)))

    run_script("CREATE TABLE IF NOT EXISTS _dev_check_tmp(x int);", commit=False)
    with P.admin_db(readonly=False) as c:
        exists = P.q1(c, "SELECT count(*) FROM information_schema.tables "
                         "WHERE table_schema='mt' AND table_name='_dev_check_tmp'")
    if exists:
        fails.append("试运行（commit=False）竟然把表建出来了")
    print("  [%s] 试运行回滚不落地" % ("PASS" if not exists else "FAIL"))

    run_script("CREATE TABLE IF NOT EXISTS _dev_check_tmp(x int);\n"
               "INSERT INTO _dev_check_tmp VALUES (1);", commit=True)
    with P.admin_db(readonly=False) as c:
        n = P.q1(c, "SELECT count(*) FROM mt._dev_check_tmp")
    run_script("DROP TABLE IF EXISTS _dev_check_tmp;", commit=True)
    with P.admin_db(readonly=False) as c:
        gone = P.q1(c, "SELECT count(*) FROM information_schema.tables "
                       "WHERE table_schema='mt' AND table_name='_dev_check_tmp'")
    if n != 1 or gone:
        fails.append("提交模式异常（插入 %s 行，清理后残留 %s）" % (n, gone))
    print("  [%s] 提交模式落地并已清理" % ("PASS" if (n == 1 and not gone) else "FAIL"))

    with P.admin_db(readonly=False) as c:
        for fn, args in ((view_home, (c, {})), (view_sql, (c, {})),
                         (view_model, (c, {})), (view_tools, (c, {})),
                         (view_audit, (c, {})), (view_migrate, (c, {}))):
            try:
                fn(*args)
            except Exception as e:                       # noqa: BLE001
                fails.append("%s 渲染失败：%s" % (fn.__name__, e))
    print("  [%s] 6 个页面全部可渲染" % ("PASS" if not fails else "FAIL"))

    if fails:
        print("\n[X] 开发者模式自检失败 %d 项：" % len(fails))
        for f in fails:
            print("    - %s" % f)
        return 1
    print("\n[✓] 开发者模式自检全部通过")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return cmd_check()
    if a.serve:
        serve(a.port)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
