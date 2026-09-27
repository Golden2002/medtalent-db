# -*- coding: utf-8 -*-
"""
ops/tests/form_server_test.py —— 演示站点端到端测试

验证的闭环（用户提出的前端倾向）：
  每个字段 n 个给定选项 → 前端从库读取并渲染 → 用户在页面选择 → 提交写入数据库
外加上一环节的现场验证：**通过页面新增一个维度，刷新后表单立刻多出该字段，且零 DDL**。

做法：在同一个进程里起一个后台 HTTP 服务，用 urllib 驱动真实请求，
断言全部落在数据库状态上（不是页面文本）。

用法：python ops/tests/form_server_test.py
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))

import app as demo  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

PASS, FAIL = H.PASS, H.FAIL
PORT = 8099
ROOT = "http://127.0.0.1:%d" % PORT
check, q1 = H.check, H.q1


def conn():
    return H.connect(demo.DSN)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随 3xx —— 否则断言 303 时会拿到最终 200，掩盖真实状态码。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def get(path):
    with OPENER.open(ROOT + path, timeout=20) as r:
        return r.status, r.read().decode("utf-8")


def post(path, data: dict):
    body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    req = urllib.request.Request(ROOT + path, data=body, method="POST")
    try:
        with OPENER.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8"), r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), e.headers.get("Location", "")


def _rows(c, sql, params=None):
    return H.q(c, sql, params)


def _strip(s: str) -> str:
    """把服务端返回的整页 HTML 压成一行，便于在测试输出里看错误。"""
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def main():
    print("=" * 78)
    print("演示站点端到端测试：n 个选项 → 用户选择 → 写入数据库")
    print("=" * 78)

    # 准备干净的数据
    with conn() as c:
        demo.reset_demo(c)
        c.commit()
        n_seed = demo.seed(c, 5)
        c.commit()
    print("\n【准备】重建 %d 条 mock 档案" % n_seed)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), demo.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("【准备】演示站点已起：%s" % ROOT)

    try:
        # ---------------------------------------------------------------
        print("\n【T1】首页可访问且渲染出表单")
        st, page = get("/")
        check(st == 200, "GET / 返回 200")
        check("基层服务意愿" in page, "页面渲染出动态维度「基层服务意愿」")
        check("<select" in page or "<option" in page, "枚举字段渲染成了下拉/单选控件")
        check("请选择" in page, "下拉框含「请选择」占位项")

        # ---------------------------------------------------------------
        print("\n【T2】表单 schema 接口给出每个字段的 n 个选项")
        st, body = get("/api/form-schema")
        schema = json.loads(body)
        check(st == 200 and schema.get("entity") == "person", "GET /api/form-schema 返回 person 的 schema")
        fmap = {f["field_id"]: f for f in schema["fields"]}
        check("F_DEMO_A" in fmap, "schema 含动态维度 F_DEMO_A")
        check(fmap.get("F_DEMO_A", {}).get("option_count") == 4,
              "F_DEMO_A 有 4 个候选选项（数据库是选项的唯一来源）")
        check(fmap.get("F_DEMO_B", {}).get("cardinality") == "array",
              "F_DEMO_B 标记为多选（cardinality=array）")
        check(all(o.get("code") and o.get("label") for o in fmap["F_DEMO_A"]["options"]),
              "每个选项都同时带 code 与中文 label")

        # ---------------------------------------------------------------
        print("\n【T3】现场新增一个维度（列）——零 DDL")
        with conn() as c:
            tables_before = q1(c, "SELECT count(*) FROM information_schema.tables "
                                  "WHERE table_schema='mt' AND table_type='BASE TABLE'")
            cols_before = q1(c, "SELECT count(*) FROM information_schema.columns "
                                "WHERE table_schema='mt' AND table_name='person'")

        st, _, loc = post("/dimension", {
            "title": "是否愿意异地工作",
            "options": "非常愿意,可以考虑,不愿意",
            "multi": "0"})
        check(st == 303, "POST /dimension 返回 303 重定向（表单提交成功）")
        check("F_CUSTOM" in urllib.parse.unquote(loc) or "已新增维度" in urllib.parse.unquote(loc),
              "重定向消息确认维度已新增")

        st, body = get("/api/form-schema")
        schema2 = json.loads(body)
        fmap2 = {f["field_id"]: f for f in schema2["fields"]}
        new_fid = [k for k in fmap2 if k.startswith("F_CUSTOM_")]
        check(len(new_fid) == 1, "新维度已出现在表单 schema 中（field_id=%s）"
              % (new_fid[0] if new_fid else "无"))
        if new_fid:
            check(fmap2[new_fid[0]]["option_count"] == 3, "新维度带 3 个选项")
            check(fmap2[new_fid[0]]["title"] == "是否愿意异地工作", "新维度标题正确")

        st, page2 = get("/")
        check("是否愿意异地工作" in page2, "刷新首页后表单里立刻多出该字段")

        with conn() as c:
            tables_after = q1(c, "SELECT count(*) FROM information_schema.tables "
                                 "WHERE table_schema='mt' AND table_type='BASE TABLE'")
            cols_after = q1(c, "SELECT count(*) FROM information_schema.columns "
                               "WHERE table_schema='mt' AND table_name='person'")
        check(tables_after == tables_before, "基础表数量未变（%d）" % tables_before)
        check(cols_after == cols_before, "person 表列数未变（%d）→ 新增维度零 DDL" % cols_before)

        # ---------------------------------------------------------------
        print("\n【T4】模拟用户填写并提交（含动态新增的那个维度）")
        payload = {
            "F_SUBJECT_CODE": ["MT-DEMO-TEST"],
            "F_PERSON_SEX": ["S2"],
            "F_DEMO_A": ["O2"],
            "F_DEMO_B": ["O1", "O3"],
            "F_DEMO_C": ["28"],
            "F_DEMO_D": ["O1"],
        }
        if new_fid:
            payload[new_fid[0]] = ["O1"]
        st, _, loc = post("/submit", payload)
        if st != 303:
            print("     提交失败，服务端返回：%s" % _strip(body)[:300])
        check(st == 303, "POST /submit 返回 303")
        pid = urllib.parse.unquote(loc).split("：")[-1].strip() if "：" in urllib.parse.unquote(loc) else ""
        check(pid.startswith("per_demo_"), "重定向带回新档案 ID（%s）" % pid)

        with conn() as c:
            n_vals = q1(c, "SELECT count(*) FROM field_value WHERE subject_id=%s", (pid,))
            got = {r["field_id"]: (r["value_code"], r["value_codes"], r["value_num"], r["value_text"])
                   for r in _rows(c, "SELECT field_id, value_code, value_codes, value_num, "
                                     "value_text FROM field_value WHERE subject_id=%s "
                                     "ORDER BY field_id", (pid,))}
            print("     实际入库字段 %d 个：%s" % (n_vals, ", ".join(sorted(got))))
            check(q1(c, "SELECT count(*) FROM person WHERE person_id=%s", (pid,)) == 1,
                  "person 实例已入库")
            check(n_vals >= 6, "该档案的各字段选择结果已写入 field_value（实际 %d 条）" % n_vals)
            check(got.get("F_DEMO_A", (None,))[0] == "O2",
                  "单选字段按 code 存储（F_DEMO_A=%s）" % (got.get("F_DEMO_A", (None,))[0],))
            check(got.get("F_DEMO_B", (None, None))[1] == ["O1", "O3"],
                  "多选字段按数组存储（F_DEMO_B=%s）" % (got.get("F_DEMO_B", (None, None))[1],))
            num = got.get("F_DEMO_C", (None, None, None))[2]
            check(num is not None and float(num) == 28.0,
                  "数值字段按 numeric 存储（F_DEMO_C=%s）" % (num,))
            if new_fid:
                check(got.get(new_fid[0], (None,))[0] == "O1",
                      "动态新增维度的选择也已入库（%s）" % new_fid[0])

        # ---------------------------------------------------------------
        print("\n【T5】非法选项必须被拒绝（越权写入防线）")
        st, body, _ = post("/submit", {"F_SUBJECT_CODE": ["MT-DEMO-BAD"], "F_DEMO_A": ["O9"]})
        check(st == 500 and "不在字段" in body, "写入未定义选项 O9 被数据库拒绝（页面返回错误）")

        # ---------------------------------------------------------------
        print("\n【T6】清理演示数据")
        st, _, loc = post("/reset", {})
        check(st == 303, "POST /reset 返回 303")
        with conn() as c:
            check(q1(c, "SELECT count(*) FROM person WHERE person_id LIKE %s", ("per_demo%",)) == 0,
                  "演示档案已清空")
            check(q1(c, "SELECT count(*) FROM field_catalog WHERE entity_id='person' "
                        "AND (field_id LIKE 'F_DEMO%' OR field_id LIKE 'F_CUSTOM%')") == 0,
                  "演示维度（含页面新增的自定义维度）已清空")
            check(q1(c, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='mt' AND table_type='BASE TABLE'") == tables_before,
                  "始至终基础表数量未变（%d）" % tables_before)

    finally:
        srv.shutdown()

    return H.report()


if __name__ == "__main__":
    sys.exit(main())
