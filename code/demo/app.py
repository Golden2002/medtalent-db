# -*- coding: utf-8 -*-
"""
code/demo/app.py —— 动态表单演示（零额外依赖：stdlib http.server + psycopg）

演示目标（对应用户提出的前端倾向）：
  · **每一个字段都有 n 个给定内容作为选项**，前端从数据库读取这些选项渲染成
    下拉框 / 单选 / 多选，用户选择后写入数据库；
  · 页面上的「新增一个维度」按钮可现场添加一个新的画像维度，
    刷新后表单**立刻多出这个字段**——整个过程不执行任何 DDL。

用法：
  python code/demo/app.py --seed 30      # 造 30 条 mock 档案
  python code/demo/app.py --reset        # 清除演示数据（维度与档案）
  python code/demo/app.py --serve        # 启动演示站点 http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")

ENTITY = "person"
# 一个用户可见表单往往横跨多个实体：身份类字段在 person，人口学字段在 person_demographics。
# 每个字段按自己所属实体落库（set_value 的 p_entity 取该字段的 entity_id）。
FORM_ENTITIES = ("person", "person_demographics")
DEMO_FIELDS = ["F_DEMO_A", "F_DEMO_B", "F_DEMO_C", "F_DEMO_D"]
PORT = 8080


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


# ---------------------------------------------------------------------------
# 数据访问
# ---------------------------------------------------------------------------
def get_form_fields(c) -> list[dict]:
    """表单字段 = 有候选选项的字段 + 显式归入某个分组的字段。
    这正是"每个字段 n 个选项"的数据库来源（v_form_schema → code_value）。
    返回结果带 entity_id —— 提交时按它决定值写到哪个实体。"""
    with c.cursor() as cur:
        cur.execute("""
            SELECT entity_id, field_id, title, description, data_type, cardinality,
                   is_required, form_section, option_count, options, form_order
            FROM v_form_schema
            WHERE entity_id = ANY(%s)
              AND (option_count > 0 OR form_section IS NOT NULL)
            ORDER BY COALESCE(form_section,'zzz'), form_order, field_id
        """, (list(FORM_ENTITIES),))
        return cur.fetchall()


def list_profiles(c, limit=12) -> list[dict]:
    with c.cursor() as cur:
        cur.execute("""
            SELECT p.person_id, p.subject_code, p.recorded_at,
                   mt.profile_json('person', p.person_id) AS profile,
                   mt.profile_json('person_demographics', p.person_id) AS demo_profile
            FROM person p
            WHERE p.person_id LIKE %s
            ORDER BY p.recorded_at DESC, p.person_id DESC
            LIMIT %s
        """, ("per_demo%", limit))
        out = []
        for r in cur.fetchall():
            merged = dict(r["demo_profile"] or {})
            merged.update(r["profile"] or {})
            r["profile"] = merged
            out.append(r)
        return out


def count_profiles(c) -> int:
    with c.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM person WHERE person_id LIKE %s",
                    ("per_demo%",))
        return cur.fetchone()["n"]


def field_labels(c) -> dict:
    """field_id -> (title, {code: label})，用于把存进去的码翻译成人看得懂的选项名。"""
    with c.cursor() as cur:
        cur.execute("""
            SELECT field_id, title, options FROM v_form_schema WHERE entity_id = ANY(%s)
        """, (list(FORM_ENTITIES),))
        out = {}
        for r in cur.fetchall():
            out[r["field_id"]] = (r["title"],
                                  {o["code"]: o["label"] for o in (r["options"] or [])})
        return out


# ---------------------------------------------------------------------------
# 业务动作
# ---------------------------------------------------------------------------
def add_dimension(c, field_id: str, title: str, options: list[str], multi: bool,
                  section: str = "自定义维度") -> None:
    opts = [{"code": "O%d" % (i + 1), "label": t, "sort": (i + 1) * 10}
            for i, t in enumerate(options) if t.strip()]
    if not opts:
        raise ValueError("至少需要一个选项")
    with c.cursor() as cur:
        cur.execute("""
            SELECT mt.add_dimension(
                p_entity   => %s,
                p_field_id => %s,
                p_title    => %s,
                p_options  => %s::jsonb,
                p_multi    => %s,
                p_section  => %s)
        """, (ENTITY, field_id, title, json.dumps(opts, ensure_ascii=False), multi, section))


def submit_profile(c, form: dict[str, list[str]]) -> str:
    subject_code = (form.get("F_SUBJECT_CODE") or [""])[0].strip()
    pid = "per_demo_%06d" % random.randint(1, 999999)
    if not subject_code:
        subject_code = "MT-DEMO-" + pid[-6:]

    with c.cursor() as cur:
        # 1) 建实例：只传身份列，其余列走默认值
        cur.execute("""
            SELECT mt.create_instance(p_entity => 'person', p_id => %s, p_payload => %s::jsonb)
        """, (pid, json.dumps({"person_id": pid, "subject_code": subject_code,
                               "enroll_channel": "EC1"}, ensure_ascii=False)))
        # 2) 每个字段的选择结果写入库（单选→p_code，多选→p_codes，数值→p_num，文本→p_text）
        #    注意按字段自己的 entity_id 落库 —— 表单可以横跨多个实体
        fields = {f["field_id"]: f for f in get_form_fields(c)}
        for fid, f in fields.items():
            vals = [v for v in form.get(fid, []) if v != ""]
            if not vals:
                continue
            ent = f["entity_id"]
            # 注意：psycopg 会把 Python float 推断为 double precision，
            # 而函数签名是 numeric，必须显式 ::numeric / ::text[] 转换
            if f["cardinality"] == "array":
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_codes=>%s::text[])", (ent, pid, fid, vals))
            elif f["data_type"] in ("integer", "number"):
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_num=>%s::numeric)", (ent, pid, fid, vals[0]))
            elif f["data_type"] == "code":
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_code=>%s)", (ent, pid, fid, vals[0]))
            else:
                cur.execute("SELECT mt.set_value(p_entity=>%s,p_subject_id=>%s,"
                            "p_field_id=>%s,p_text=>%s)", (ent, pid, fid, vals[0]))
    return pid


def reset_demo(c) -> dict:
    """清除演示数据：档案、值、演示维度与页面新增的自定义维度，以及它们各自的词表。

    约定：LIKE 的通配符一律**作为参数传入**（('F_DEMO%',)），不要写进 SQL 文本——
    psycopg3 在带参数时会拒绝裸 '%'（要求写成 '%%'），参数化写法没有这个歧义。
    """
    P_PER = "per_demo%"
    P_F, P_C = "F_DEMO%", "F_CUSTOM%"
    P_CTF, P_CTC = "CT_F_DEMO%", "CT_F_CUSTOM%"

    with c.cursor() as cur:
        cur.execute("DELETE FROM field_value WHERE subject_id LIKE %s", (P_PER,))
        n_val = cur.rowcount
        cur.execute("DELETE FROM person WHERE person_id LIKE %s", (P_PER,))
        n_per = cur.rowcount

        # 先删值再删维度（field_value.field_id 有外键指向 field_catalog）
        cur.execute("""
            DELETE FROM field_value WHERE field_id IN (
              SELECT field_id FROM field_catalog
              WHERE entity_id = %s AND (field_id LIKE %s OR field_id LIKE %s))""",
            (ENTITY, P_F, P_C))
        cur.execute("""
            DELETE FROM field_catalog
            WHERE entity_id = %s AND (field_id LIKE %s OR field_id LIKE %s)""",
            (ENTITY, P_F, P_C))
        n_fld = cur.rowcount

        cur.execute("""
            DELETE FROM code_value WHERE code_table_id IN (
              SELECT code_table_id FROM code_table
              WHERE code_table_id LIKE %s OR code_table_id LIKE %s)""", (P_CTF, P_CTC))
        cur.execute("""
            DELETE FROM code_table
            WHERE code_table_id LIKE %s OR code_table_id LIKE %s""", (P_CTF, P_CTC))
        n_ct = cur.rowcount
    return {"values": n_val, "persons": n_per, "fields": n_fld, "code_tables": n_ct}


DEMO_DEFS = [
    ("F_DEMO_A", "基层服务意愿（单选）", ["非常愿意", "愿意", "犹豫", "不愿意"], False,
     "就业偏好"),
    ("F_DEMO_B", "可接受的岗位族（多选）",
     ["临床医疗", "药企医学事务", "CRO/临床研究", "医疗AI/数字健康", "健康险"], True,
     "就业偏好"),
    ("F_DEMO_C", "可接受最低年薪（万）", None, False, "薪酬期望"),
    ("F_DEMO_D", "最看重的工作特征（单选）",
     ["稳定性", "收入", "成长空间", "工作强度", "社会价值"], False, "就业偏好"),
]


def seed(c, n: int) -> int:
    existing = {f["field_id"] for f in get_form_fields(c)}
    for fid, title, opts, multi, section in DEMO_DEFS:
        if fid in existing:
            continue
        if opts:
            add_dimension(c, fid, title, opts, multi, section)
        else:
            with c.cursor() as cur:
                cur.execute("SELECT mt.add_dimension(p_entity=>%s,p_field_id=>%s,"
                            "p_title=>%s,p_data_type=>'number',p_section=>%s)",
                            (ENTITY, fid, title, section))

    labels = field_labels(c)
    made = 0
    for i in range(n):
        form: dict[str, list[str]] = {"F_SUBJECT_CODE": ["MT-DEMO-%04d" % (i + 1)]}
        for fid in ("F_PERSON_SEX", "F_PERSON_HUKOU_TYPE", "F_PERSON_POLITICAL"):
            if fid in labels and labels[fid][1]:
                form[fid] = [random.choice(list(labels[fid][1].keys()))]
        if "F_DEMO_A" in labels:
            form["F_DEMO_A"] = [random.choice(list(labels["F_DEMO_A"][1].keys()))]
        if "F_DEMO_B" in labels:
            ks = list(labels["F_DEMO_B"][1].keys())
            form["F_DEMO_B"] = random.sample(ks, random.randint(1, 2))
        if "F_DEMO_C" in labels:
            form["F_DEMO_C"] = [str(random.choice([12, 15, 18, 20, 25, 30, 35]))]
        if "F_DEMO_D" in labels:
            form["F_DEMO_D"] = [random.choice(list(labels["F_DEMO_D"][1].keys()))]
        submit_profile(c, form)
        made += 1
    return made


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
CSS = """
body{font:14px/1.65 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
     margin:0;background:#f5f6f8;color:#1f2328}
.wrap{max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 10px;color:#424a53}
.sub{color:#656d76;font-size:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:18px;margin-bottom:18px}
label{display:block;font-weight:600;margin:12px 0 4px;font-size:13px}
label .req{color:#cf222e;margin-left:3px}
label .hint{font-weight:400;color:#656d76;margin-left:6px;font-size:12px}
select,input[type=text],input[type=number],textarea{
  width:100%;padding:7px 9px;border:1px solid #d0d7de;border-radius:6px;
  font-size:13px;box-sizing:border-box;background:#fff}
textarea{height:56px;resize:vertical}
.chk{display:inline-flex;align-items:center;margin:3px 14px 3px 0;font-weight:400}
.chk input{margin-right:5px}
button{background:#1f883d;color:#fff;border:0;border-radius:6px;padding:9px 18px;
       font-size:13px;cursor:pointer;font-weight:600}
button.sec{background:#f6f8fa;color:#24292f;border:1px solid #d0d7de}
button.danger{background:#cf222e}
.row{display:flex;gap:10px;align-items:flex-end}
.row>div{flex:1}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{border-bottom:1px solid #eaeef2;padding:7px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600;color:#424a53}
.opt-code{display:inline-block;background:#ddf4ff;color:#0969da;border-radius:4px;
          padding:1px 6px;margin:1px 3px 1px 0;font-size:12px}
.tag{display:inline-block;background:#f6f8fa;border:1px solid #d0d7de;border-radius:10px;
     padding:0 8px;font-size:11px;color:#424a53;margin-right:6px}
.sect{font-size:12px;font-weight:700;color:#0969da;margin:20px 0 2px;
      border-top:1px solid #eaeef2;padding-top:12px}
.sect:first-child{border-top:0;padding-top:0;margin-top:6px}
.note{background:#fff8c5;border:1px solid #d4a72c66;border-radius:6px;padding:10px 12px;
      font-size:12.5px;color:#4d2d00;margin-bottom:14px}
"""


def render_page(c, msg: str = "") -> bytes:
    fields = get_form_fields(c)
    profiles = list_profiles(c)
    labels = field_labels(c)

    # 表单按分组渲染
    sections: dict[str, list[dict]] = {}
    for f in fields:
        sections.setdefault(f["form_section"] or "其他", []).append(f)

    parts = []
    for sec, fs in sections.items():
        parts.append('<div class="sect">%s</div>' % html.escape(sec))
        for f in fs:
            req = '<span class="req">*</span>' if f["is_required"] else ""
            n = f["option_count"]
            hint = ('<span class="hint">%d 个选项 · %s</span>'
                    % (n, "多选" if f["cardinality"] == "array" else "单选")) if n else \
                   ('<span class="hint">%s</span>' % f["data_type"])
            parts.append('<label for="%s">%s%s%s</label>'
                         % (f["field_id"], html.escape(f["title"]), req, hint))
            fid = f["field_id"]
            if n and f["cardinality"] == "array":
                for o in f["options"]:
                    parts.append(
                        '<span class="chk"><input type="checkbox" name="%s" value="%s">'
                        '%s <span class="opt-code">%s</span></span>'
                        % (fid, o["code"], html.escape(o["label"]), o["code"]))
            elif n:
                parts.append('<select name="%s">' % fid)
                parts.append('<option value="">— 请选择 —</option>')
                for o in f["options"]:
                    parts.append('<option value="%s">%s（%s）</option>'
                                 % (o["code"], html.escape(o["label"]), o["code"]))
                parts.append("</select>")
            elif f["data_type"] in ("integer", "number"):
                parts.append('<input type="number" step="any" name="%s">' % fid)
            elif f["data_type"] == "boolean":
                parts.append('<span class="chk"><input type="checkbox" name="%s" value="1">是</span>'
                             % fid)
            else:
                parts.append('<textarea name="%s"></textarea>' % fid)

    # 已入库档案
    prows = []
    for p in profiles:
        items = []
        for k, v in (p["profile"] or {}).items():
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
            items.append('<span class="tag">%s：%s</span>' % (html.escape(title), html.escape(str(disp))))
        prows.append("<tr><td>%s</td><td>%s</td></tr>"
                     % (html.escape(p["subject_code"]), "".join(items) or "—"))

    dim_rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (html.escape(f["field_id"]), html.escape(f["title"]),
           "多选" if f["cardinality"] == "array" else ("单选" if f["option_count"] else f["data_type"]),
           "、".join("%s=%s" % (o["code"], html.escape(o["label"])) for o in (f["options"] or [])) or "—")
        for f in fields)

    msg_html = ('<div class="note">%s</div>' % html.escape(msg)) if msg else ""

    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>医学生人才信息库 · 动态表单演示</title><style>%s</style></head><body><div class="wrap">
<h1>医学生人才信息库 · 动态表单演示</h1>
<div class="sub">每个字段的候选选项全部来自数据库（<code>code_value</code>）；
新增维度不执行任何 DDL，刷新即出现在表单中。当前档案数：<b>%d</b></div>
%s
<div class="card">
<h2 style="margin-top:0">① 填写一份画像（所有选项来自数据库）</h2>
<form method="post" action="/submit">
  %s
  <div style="margin-top:18px"><button type="submit">提交并写入数据库</button></div>
</form>
</div>

<div class="card">
<h2 style="margin-top:0">② 现场新增一个维度（列）——零 DDL</h2>
<form method="post" action="/dimension" class="row">
  <div><label>维度标题</label><input type="text" name="title" placeholder="例如：是否愿意异地工作" required></div>
  <div><label>选项（用逗号分隔，即 n 个给定内容）</label>
       <input type="text" name="options" placeholder="非常愿意,可以考虑,不愿意" required></div>
  <div style="flex:0 0 110px"><label>类型</label>
       <select name="multi"><option value="0">单选</option><option value="1">多选</option></select></div>
  <div style="flex:0 0 120px"><button type="submit">添加维度</button></div>
</form>
<div class="sub" style="margin:10px 0 0">提交后本页刷新，表单里就会立刻多出这个字段。</div>
</div>

<div class="card">
<h2 style="margin-top:0">③ 当前表单的字段与候选选项（数据库视角）</h2>
<table><tr><th>field_id</th><th>标题</th><th>类型</th><th>候选选项（n 个）</th></tr>%s</table>
</div>

<div class="card">
<h2 style="margin-top:0">④ 已入库的画像（最近 %d 条）</h2>
<table><tr><th>对象编号</th><th>画像维度（含动态新增的列）</th></tr>%s</table>
</div>

<div class="card">
<h2 style="margin-top:0">维护</h2>
<form method="post" action="/reset" onsubmit="return confirm('确认清除全部演示数据？')">
  <button class="danger" type="submit">清除演示数据</button>
  <span class="sub" style="margin-left:10px">删除 per_demo_* 档案、演示维度及其词表</span>
</form>
</div>
</div></body></html>""" % (CSS, count_profiles(c), msg_html, "".join(parts),
                           dim_rows, len(profiles), "".join(prows) or
                           "<tr><td colspan=2>暂无数据，可先运行 python code/demo/app.py --seed 30</td></tr>")
    return doc.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "MedTalentDemo/1.0"

    def _send(self, body: bytes, code=200, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, msg: str):
        self.send_response(303)
        self.send_header("Location", "/?msg=" + urllib.parse.quote(msg))
        self.end_headers()

    def _read_form(self) -> dict[str, list[str]]:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def do_GET(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        try:
            with conn() as c:
                if path == "/api/form-schema":
                    with c.cursor() as cur:
                        cur.execute("SELECT mt.form_schema(%s) AS s", (ENTITY,))
                        data = cur.fetchone()["s"]
                    self._send(json.dumps(data, ensure_ascii=False, default=str)
                               .encode("utf-8"), ctype="application/json; charset=utf-8")
                elif path in ("/", "/index.html"):
                    self._send(render_page(c, q.get("msg", [""])[0]))
                else:
                    self._send(b"not found", 404, "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(("错误：%s" % e).encode("utf-8"), 500)

    def do_POST(self):  # noqa: N802
        try:
            form = self._read_form()
            with conn() as c:
                if self.path == "/submit":
                    pid = submit_profile(c, form)
                    c.commit()
                    self._redirect("已写入数据库：%s" % pid)
                elif self.path == "/dimension":
                    title = (form.get("title") or [""])[0].strip()
                    opts = [x.strip() for x in (form.get("options") or [""])[0].split(",") if x.strip()]
                    multi = (form.get("multi") or ["0"])[0] == "1"
                    if not title or not opts:
                        self._redirect("标题与选项都不能为空")
                        return
                    fid = "F_CUSTOM_" + (
                        "".join(ch for ch in title if ch.isascii() and ch.isalnum())[:8].upper()
                        or "%04d" % random.randint(1000, 9999))
                    try:
                        add_dimension(c, fid, title, opts, multi)
                        c.commit()
                        self._redirect("已新增维度 %s（%d 个选项）——无需任何 DDL" % (fid, len(opts)))
                    except psycopg.errors.UniqueViolation:
                        c.rollback()
                        self._redirect("维度 %s 已存在，请换个标题" % fid)
                elif self.path == "/reset":
                    st = reset_demo(c)
                    c.commit()
                    self._redirect("已清除：档案 %d、值 %d、维度 %d、词表 %d"
                                   % (st["persons"], st["values"], st["fields"], st["code_tables"]))
                else:
                    self._send(b"not found", 404, "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send(("错误：%s" % e).encode("utf-8"), 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, metavar="N")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()

    if a.reset:
        with conn() as c:
            st = reset_demo(c)
            c.commit()
        print("[✓] 已清除演示数据：档案 %d、值 %d、维度 %d、词表 %d"
              % (st["persons"], st["values"], st["fields"], st["code_tables"]))
    if a.seed:
        with conn() as c:
            n = seed(c, a.seed)
            c.commit()
        print("[✓] 已用 mock 数据生成 %d 条档案" % n)
    if a.serve or not (a.seed or a.reset):
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
        print("[✓] 演示站点已启动： http://127.0.0.1:%d" % a.port)
        print("    接口： GET /api/form-schema  （前端可直接消费的字段与选项 JSON）")
        print("    Ctrl+C 停止")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[=] 已停止")


if __name__ == "__main__":
    main()
