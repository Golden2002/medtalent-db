# -*- coding: utf-8 -*-
"""
ops/tests/admin_ui_test.py —— 两个"操作界面"的回归断言

被测对象（都在 `portal_admin.py`，挂在开发者模式 :8083）：
  ① `/occupation`  新增职业（写进职业树 + 留审计）
  ② `/import`      批量导入个体（粘贴表格 / 指定文件；含字段缺失处理）

为什么必须有一套
--------------------------------------------------------------------------
这两个界面是**唯一能让非工程人员改库的入口**。它们出错的方式不是崩溃，
而是"看起来成了、其实没写"或"写了两遍"：
  · 实测踩过：职业 INSERT 与审计写在同一个事务里，审计的参数类型推不出来
    （`could not determine data type of parameter $2`）→ **整条回滚**，
    客户端还拿到 500 页；以及 `done` 是 dict 却按元组迭代 → 服务端**没发响应
    就断开**，而库里其实已经写成功（"写成功了但看起来失败"最容易被误判）。
  · 实测踩过：粘贴的是**制表符**分隔，导入器按逗号切 → "表头 1 列"、
    0 条导入、全部"未授权"，症状完全指向数据而不是分隔符。
所以断言要落在**数据库状态**上（不看页面文字），并覆盖：
  写成功 / 拒绝重复 / 拒绝悬挂父节点 / 缺字段不写空值 / 授权门拒绝 / 用完即净。

用法：python ops/tests/admin_ui_test.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "ops"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")
PORT = 8098
OID = "OCC-T-ADMINUI"
# 导入器的来源命名空间是**固定**的（ops/import_persons.py 的 SRC），
# 不是每个测试自己编一个 —— 否则清理会按错的名字去找、什么也清不掉。
# 第一版我在这里写了 "admintest"，断言全部落空（查询按 source_system='admintest'）。
EXT = "xlsimport"


def post(path, data):
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                               data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(r, timeout=600) as x:
            return x.status, x.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def get(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path),
                                    timeout=120) as x:
            return x.status, x.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def q1(sql, p=None):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        return c.execute(sql, p).fetchone()


def cleanup():
    """用完即净：职业 + 审计 + 导入的人（用共享的硬清理，覆盖面等于构造面）。"""
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        c.execute("DELETE FROM mt.change_log WHERE object_name = %s", (OID,))
        c.execute("DELETE FROM mt.occupation WHERE occupation_id = %s", (OID,))
        c.commit()
    subprocess.run([sys.executable, os.path.join(BASE, "ops", "import_persons.py"),
                    "--cleanup", "--yes"], capture_output=True, text=True,
                   encoding="utf-8", cwd=BASE)


def main():
    cleanup()
    base_occ = q1("SELECT count(*) AS n FROM mt.occupation WHERE status='active'")["n"]
    base_person = q1("SELECT count(*) AS n FROM mt.person")["n"]

    srv = subprocess.Popen([sys.executable,
                            os.path.join(BASE, "code", "demo", "portal_dev.py"),
                            "--serve", "--port", str(PORT)],
                           cwd=BASE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                if get("/")[0] == 200:
                    break
            except Exception:                                  # noqa: BLE001
                pass

        # ---------------- ① 新增职业 ----------------
        st, b = get("/occupation")
        check(st == 200 and "父节点" in b,
              "GET /occupation 渲染表单（含父节点下拉、审计理由栏）")
        check('name="note"' in b, "表单强制填「为什么加它」（审计理由）")

        st, _ = post("/occupation", {
            "act": "add", "oid": OID, "label": "回归用职业", "level": "3",
            "parent": "OCC-F02-01", "family": "F02", "med": "2", "tran": "4",
            "cs": "N", "desc": "回归测试", "note": "admin_ui_test"})
        row = q1("SELECT occupation_id, label_zh, level, code_source FROM mt.occupation "
                 "WHERE occupation_id = %s", (OID,))
        check(row is not None, "职业真的写进库了（不看页面文字，查数据库）")
        check(row and row["code_source"] == "web_admin",
              "写库来源标记为 web_admin（%s）" % (row["code_source"] if row else "-"))
        au = q1("SELECT actor, detail->>'via' AS via, detail->>'note' AS note "
                "FROM mt.change_log WHERE object_name = %s", (OID,))
        check(au is not None and au["via"] == "portal_admin",
              "写库同时留了审计（入口 portal_admin，理由 admin_ui_test）")
        check(au is not None and au["actor"], "审计里有 actor（谁干的）")

        # 拒绝重复（编号是主键，但要给出人能看懂的话而不是 500）
        st, b2 = post("/occupation", {
            "act": "add", "oid": OID, "label": "重复", "level": "3",
            "med": "3", "tran": "3", "cs": "N", "note": "重复提交"})
        check(st == 200 and "已存在" in b2, "重复编号被友好拒绝（不是 500）")
        check(q1("SELECT count(*) AS n FROM mt.occupation WHERE occupation_id=%s",
                 (OID,))["n"] == 1, "重复提交**没有**写进第二行")

        # 拒绝悬挂父节点
        st, b3 = post("/occupation", {
            "act": "add", "oid": OID + "X", "label": "悬挂", "level": "3",
            "parent": "OCC-NOT-EXIST", "med": "3", "tran": "3", "cs": "N",
            "note": "测父节点"})
        check(st == 200 and "不存在" in b3, "父节点不存在时被拒绝（不会挂成悬挂引用）")
        check(q1("SELECT count(*) AS n FROM mt.occupation WHERE occupation_id=%s",
                 (OID + "X",))["n"] == 0, "被拒的行确实没有落库")

        # ---------------- ② 批量导入 ----------------
        st, b = get("/import")
        check(st == 200 and "<textarea" in b and "xlsx" in b,
              "GET /import 渲染表单（粘贴框 + 本机文件路径，支持 xlsx）")

        # 用**制表符**粘贴（Excel 复制就是这样）—— 实测这里曾因分隔符嗅探缺失而整体失效
        paste = "\n".join([
            "外部编号\t同意个人分析\t当前阶段\t学历\t专业\t海外经历",
            EXT + "1\t是\tST1\tDG3\tMA01\tY",
            EXT + "2\t是\tST1\tDG2\t\tN",            # 专业空：必须不写该字段
            EXT + "3\t否\tST2\tDG3\tMA02\tY",        # 未授权：必须被授权门拒绝
        ])
        st, b = post("/import", {"act": "run", "mode": "plan", "text": paste,
                                 "note": "回归"})
        check("【计划】" in b, "默认只做计划、不写库")
        check(q1("SELECT count(*) AS n FROM mt.external_identity "
                 "WHERE source_system=%s", (EXT,))["n"] == 0,
              "计划模式确实一条都没写")
        check("制表符" in b, "报告里写明用了哪个分隔符（制表符）—— 出问题能一眼定位")

        st, b = post("/import", {"act": "run", "mode": "apply", "text": paste,
                                 "note": "回归"})
        n_ids = q1("SELECT count(*) AS n FROM mt.external_identity "
                   "WHERE source_system=%s", (EXT,))["n"]
        check(n_ids == 2, "3 行里导入 2 个（未授权那行被授权门拒绝），实得 %d" % n_ids)
        check("授权门拒绝" in b, "报告里说明了有一行被授权门拒绝（门在工作）")
        check(q1("SELECT count(*) AS n FROM mt.field_value WHERE value_code IS NULL "
                 "OR value_code=''")["n"] == 0,
              "**没有任何空值行** —— 缺的字段是不写行，而不是写空（缺失≠空值）")
        check(q1("SELECT count(*) AS n FROM mt.consent_record c WHERE NOT EXISTS "
                 "(SELECT 1 FROM mt.person p WHERE p.person_id=c.person_id)")["n"] == 0,
              "导入后没有孤儿同意记录（清理覆盖面要等于构造覆盖面）")

        # 幂等：同一批再导一次不应多出人
        post("/import", {"act": "run", "mode": "apply", "text": paste, "note": "回归"})
        check(q1("SELECT count(*) AS n FROM mt.external_identity WHERE source_system=%s",
                 (EXT,))["n"] == n_ids,
              "重复导入是版本更新而不是多建人（身份数没变）")

        # ---------------- ③ 用完即净 ----------------
        cleanup()
        check(q1("SELECT count(*) AS n FROM mt.external_identity WHERE source_system=%s",
                 (EXT,))["n"] == 0, "清理后导入来源归零")
        check(q1("SELECT count(*) AS n FROM mt.occupation WHERE occupation_id=%s",
                 (OID,))["n"] == 0, "清理后演示职业归零")
        check(q1("SELECT count(*) AS n FROM mt.occupation WHERE status='active'")["n"]
              == base_occ, "职业树回到测试前的节点数（%d）" % base_occ)
        check(q1("SELECT count(*) AS n FROM mt.person")["n"] == base_person,
              "person 表回到测试前的人数（%d）" % base_person)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
        cleanup()
    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
