# -*- coding: utf-8 -*-
"""
ops/tests/devmode_test.py —— 开发者模式端到端测试（需求 ⑤ + ④）

这个模块能写库，所以测试的重点不是"按钮能点"，而是**写操作被正确地约束住**：

  · GET 绝不写库          —— 用 GET 带上写语句和 commit=1，断言表没有被建出来
  · POST 默认试运行回滚    —— 断言执行后数据库状态没变，且页面明确说"已回滚"
  · 勾选提交才落地        —— 断言表真的建出来了，然后清理掉
  · 加「列」零 DDL        —— 加一个维度后：基础表数不变、person 列数不变、
                             字段目录 +1、码值 +1（这才是"结构可扩展"的证据）
  · 加「行」端到端        —— create_instance 建记录、set_value 写值，都能查到
  · 未登记字段被拒        —— 通过开发者控制台写一个未登记的 attrs 键，必须被数据库拒绝
  · 运维工具真的驱动 CLI  —— 跑只读工具，断言输出里有真实结果
  · 全部测试数据都被清掉  —— 结束时校验，不留残留

用法：python ops/tests/devmode_test.py
"""
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

import portal as P  # noqa: E402
import portal_dev as D  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

PASS, FAIL = H.PASS, H.FAIL
PORT = 8103
ROOT = "http://127.0.0.1:%d" % PORT
FIELD = "F_DEV_TEST01"
CT = "CT_F_DEV_TEST01"
PER = "per_dev_test01"
PROBE_TABLE = "_devmode_probe"


check = H.check


def conn():
    return H.connect(P.DSN)


def q1(sql, p=None):
    """本测试沿历史用法：不传连接，自己开一条（只读查询）。"""
    with conn() as c:
        return H.q1(c, sql, p)


def get(path):
    try:
        with urllib.request.urlopen(ROOT + path, timeout=120) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def post(path, data: dict):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(ROOT + path, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def counters():
    with conn() as c:
        return D.ddl_counter(c)


def cleanup():
    """只清本测试造出来的东西。"""
    with conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM field_value WHERE subject_id = %s OR field_id = %s",
                    (PER, FIELD))
        cur.execute("DELETE FROM person WHERE person_id = %s", (PER,))
        cur.execute("DELETE FROM field_catalog WHERE field_id = %s", (FIELD,))
        cur.execute("DELETE FROM code_value WHERE code_table_id = %s", (CT,))
        cur.execute("DELETE FROM code_table WHERE code_table_id = %s", (CT,))
        cur.execute("DELETE FROM attribute_definition WHERE attr_key LIKE '_devtest%%'")
        cur.execute("DROP TABLE IF EXISTS mt.%s" % PROBE_TABLE)
    c2 = conn()
    c2.commit()
    c2.close()


def main():
    P.meta()
    cleanup()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), D.DevHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        # ===============================================================
        print("\n【T1】六个页面可访问")
        for p, key in (("/", "开发者模式"), ("/sql", "SQL 控制台"), ("/model", "动态建模"),
                       ("/tools", "运维工具"), ("/audit", "审计流水"),
                       ("/migrate", "迁移与结构"), ("/portal", "只读门户")):
            st, b = get(p)
            check(st == 200 and key in b, "GET %s 返回 200 且含「%s」" % (p, key))

        # ===============================================================
        print("\n【T2】GET 绝不能写库（防预取/爬虫误触发）")
        st, b = get("/sql?commit=1&q=" + urllib.parse.quote(
            "CREATE TABLE %s(x int);" % PROBE_TABLE))
        check(st == 200, "带写语句的 GET 返回 200（只渲染表单）")
        exists = q1("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='mt' AND table_name=%s", (PROBE_TABLE,))
        check(exists == 0,
              "GET 带 commit=1 也没有建表 —— 写操作只走 POST")
        check("填入表单" in b or "执行请按按钮" in b,
              "页面说明示例链接只填入表单、不执行")

        # ===============================================================
        print("\n【T3】POST 默认试运行：执行后回滚")
        st, b = post("/sql", {"q": "CREATE TABLE %s(x int);\nINSERT INTO %s VALUES (1);"
                                   % (PROBE_TABLE, PROBE_TABLE)})
        check(st == 200 and "回滚" in b, "POST 未勾选提交 → 页面显示已回滚")
        exists = q1("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='mt' AND table_name=%s", (PROBE_TABLE,))
        check(exists == 0, "试运行后表确实不存在（数据库状态没变）")
        check("影响 1 行" in b or "影响" in b, "试运行仍给出逐条影响行数")

        # ===============================================================
        print("\n【T4】POST 勾选提交：真的落地")
        st, b = post("/sql", {"q": "CREATE TABLE %s(x int);\nINSERT INTO %s VALUES (1),(2);"
                                   % (PROBE_TABLE, PROBE_TABLE), "commit": "1"})
        check(st == 200 and "已" in b and "提交" in b, "勾选提交 → 页面显示已提交")
        n = q1("SELECT count(*) FROM mt.%s" % PROBE_TABLE)
        check(n == 2, "表已创建且有 2 行（影响行数属实）")
        post("/sql", {"q": "DROP TABLE mt.%s;" % PROBE_TABLE, "commit": "1"})
        check(q1("SELECT count(*) FROM information_schema.tables "
                 "WHERE table_schema='mt' AND table_name=%s", (PROBE_TABLE,)) == 0,
              "已清理探测表")

        # ===============================================================
        print("\n【T5】加「列」：零 DDL（需求 ④）")
        before = counters()
        st, b = post("/model", {"act": "add_dim", "entity": "person",
                                "field_id": FIELD, "title": "开发者模式测试维度",
                                "options": "A1=选项一\nA2=选项二\nA3=选项三"})
        after = counters()
        check(st == 200 and "已新增维度" in b, "POST /model 新增维度成功")
        check(after["tables"] == before["tables"],
              "基础表数不变（%d → %d）→ DDL 次数 = 0" % (before["tables"], after["tables"]))
        check(after["person_cols"] == before["person_cols"],
              "person 表列数不变（%d → %d）→ 没有 ALTER TABLE"
              % (before["person_cols"], after["person_cols"]))
        check(after["fields"] == before["fields"] + 1,
              "已登记字段 +1（%d → %d）" % (before["fields"], after["fields"]))
        check(after["code_values"] == before["code_values"] + 3,
              "代码值 +3（%d → %d）—— 3 个选项各自写进了码表"
              % (before["code_values"], after["code_values"]))
        check("不变" in b, "页面给出「证据」表并标注不变项")
        check(q1("SELECT count(*) FROM field_catalog WHERE field_id=%s", (FIELD,)) == 1,
              "field_catalog 里确实有了这个字段")
        check(q1("SELECT count(*) FROM code_value WHERE code_table_id=%s", (CT,)) == 3,
              "该字段的 3 个选项都在 code_value 里")
        check(q1("SELECT count(*) FROM v_form_schema WHERE field_id=%s", (FIELD,)) == 1,
              "新字段立刻出现在表单 schema 里（前端可直接消费）")

        # ===============================================================
        print("\n【T6】加「行」+ 写值：端到端（需求 ④）")
        st, b = post("/model", {"act": "make_row", "entity": "person", "subject_id": PER})
        check(st == 200 and "已创建记录" in b, "POST /model 建记录成功")
        check(q1("SELECT count(*) FROM person WHERE person_id=%s", (PER,)) == 1,
              "person 表里确实有了这条记录")
        st, b = post("/model", {"act": "set_val", "entity": "person", "subject_id": PER,
                                "field_id": FIELD, "code": "A2"})
        check(st == 200 and "已写入" in b, "POST /model 写值成功")
        v = q1("SELECT value_code FROM field_value WHERE subject_type='person' "
               "AND subject_id=%s AND field_id=%s", (PER, FIELD))
        check(v == "A2", "field_value 里存的是选项码 A2 → '%s'" % v)
        check(q1("SELECT count(*) FROM v_form_schema WHERE field_id=%s", (FIELD,)) == 1,
              "写值后字段仍在表单里")

        # 非法选项必须被拒（assert_option）
        st, b = post("/model", {"act": "set_val", "entity": "person", "subject_id": PER,
                                "field_id": FIELD, "code": "NOT_A_CODE"})
        check("拒绝" in b or "err" in b.lower() or "不存在" in b,
              "写入未定义的选项码被数据库拒绝")

        # ===============================================================
        print("\n【T7】未登记字段被拒（纪律由数据库执行）")
        st, b = post("/sql", {"q": "INSERT INTO person (person_id, subject_code, attrs) "
                                   "VALUES ('_devtest_attrs','MT-X','{\"未登记字段\":1}'::jsonb);",
                              "commit": "1"})
        check("未登记" in b or "check" in b.lower() or "constraint" in b.lower(),
              "通过开发者控制台写未登记 attrs 键 → 被数据库拒绝")
        check(q1("SELECT count(*) FROM person WHERE person_id='_devtest_attrs'") == 0,
              "被拒的写入没有留下记录")

        # ===============================================================
        print("\n【T8】运维工具真的驱动既有 CLI")
        st, b = post("/tools", {"run": "cat"})
        check(st == 200 and "退出码 0" in b, "运行只读工具（数据字典校验）→ 退出码 0")
        check("错误：0" in b, "工具输出里含真实结果「错误：0」")
        st, b2 = post("/tools", {"run": "nope"})
        check(st == 200 and "未知工具" in b2, "未知工具被拒绝")

        # ===============================================================
        print("\n【T9】审计流水记录了刚才的写入")
        st, b = get("/audit")
        check(st == 200 and "change_log" in b, "/audit 可访问")
        n_cl = q1("SELECT count(*) FROM change_log")
        check(n_cl > 0, "change_log 有 %s 行（触发器强制记录）" % f"{n_cl:,}")

    finally:
        srv.shutdown()
        srv.server_close()

    # ===============================================================
    print("\n【T10】清理并确认无残留")
    cleanup()
    left = {
        "field_catalog": q1("SELECT count(*) FROM field_catalog WHERE field_id=%s", (FIELD,)),
        "code_value": q1("SELECT count(*) FROM code_value WHERE code_table_id=%s", (CT,)),
        "code_table": q1("SELECT count(*) FROM code_table WHERE code_table_id=%s", (CT,)),
        "person": q1("SELECT count(*) FROM person WHERE person_id=%s", (PER,)),
        "field_value": q1("SELECT count(*) FROM field_value WHERE field_id=%s", (FIELD,)),
        "probe_table": q1("SELECT count(*) FROM information_schema.tables "
                          "WHERE table_schema='mt' AND table_name=%s", (PROBE_TABLE,)),
    }
    check(all(v == 0 for v in left.values()),
          "测试数据已全部清理：%s" % left)

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
