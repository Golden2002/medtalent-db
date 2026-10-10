# -*- coding: utf-8 -*-
"""
ops/tests/import_test.py —— 表格导入器的回归断言（含**字段缺失处理**）

为什么单独立一套
--------------------------------------------------------------------------
导入器是"外部数据进库"的唯一通道，最容易出的错不是崩溃，而是**静默地记错**：
把"他没填"记成"他答了空"、把不认识的列悄悄丢掉、把同一批人导两遍。
这些错不会报错，只会让后续统计的数字慢慢失真。所以要专门守住：
  ① 三种缺失（整列缺失 / 单元格为空 / 值非法）**分别被识别、分别被报告**；
  ② 缺的字段**不写行**（而不是写空值）—— 这是"缺失 ≠ 空值"的机器可检形式；
  ③ 不认识的列**被报告**，不静默丢弃；
  ④ 授权门真的会拒绝（不是靠自觉）；
  ⑤ 幂等：同一个外部编号再导一次，是**版本更新**而不是多一个人；
  ⑥ **清理覆盖面 = 构造覆盖面**：导完再清，库必须回到基线（含 field_value）。
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.join(BASE, "ops"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")
SRC = "xlsimport"
TMP = os.path.join(BASE, ".tools", "import_test.csv")


def run(args):
    return subprocess.run([sys.executable, os.path.join(BASE, "ops", "import_persons.py")]
                          + args, capture_output=True, text=True, encoding="utf-8",
                          cwd=BASE)


def snapshot():
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        return {
            "ext": c.execute("SELECT count(*) AS n FROM mt.external_identity "
                             "WHERE source_system=%s", (SRC,)).fetchone()["n"],
            "fv": c.execute("SELECT count(*) AS n FROM mt.field_value").fetchone()["n"],
            "person": c.execute("SELECT count(*) AS n FROM mt.person").fetchone()["n"],
        }


def write_csv():
    """构造一份**故意含三种缺失**的 CSV：整列缺、单元格空、值非法，外加一列未知列。"""
    rows = [
        # 外部编号, 同意个人分析, 当前阶段, 学历, 专业, 意向城市, 海外经历, 备注
        ["t001", "是", "ST1", "DG3", "MA01", "REG_11", "Y", "齐全"],
        ["t002", "是", "ST1", "DG2", "",      "REG_12", "N", "专业空"],
        ["t003", "否", "ST2", "DG3", "MA02",  "REG_11", "Y", "未授权 → 应被拒"],
        ["t004", "是", "",    "",     "",      "",       "",  "几乎全缺"],
        # 非法码那行：动态字段会被拒，但**契约字段合法 → 人还是会建**
        ["t005", "是", "ST1", "DG4", "MA04",  "REG_13", "Y1", "非法码"],
    ]
    head = ["外部编号", "同意个人分析", "当前阶段", "学历", "专业", "意向城市",
            "海外经历", "备注（这是不认识的列）"]
    os.makedirs(os.path.dirname(TMP), exist_ok=True)
    with open(TMP, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        w.writerows(rows)
    return TMP


def main():
    before = snapshot()
    run(["--cleanup", "--yes"])                        # 先自愈：清掉上次失败留下的
    base = snapshot()
    check(base["ext"] == 0, "起点干净：该来源在库里的外部身份 0 条")

    sample = write_csv()
    out = run(["--file", sample])
    check("【计划】" in out.stdout and "未写任何数据" in out.stdout,
          "默认只做计划、不写库（加 --apply 才写）")
    check(snapshot()["ext"] == base["ext"], "计划模式确实没写入任何数据")

    out = run(["--file", sample, "--apply"])
    txt = out.stdout + out.stderr
    # ① 单元格为空 → 报告里逐字段列出
    check("单元格为空" in txt and "F_PSN_RES_OVERSEAS" in txt,
          "① 单元格为空被识别并列出（海外经历那列有空的）")
    check("education.major" in txt, "① 专业列的空白被识别")
    # ③ 非法码 → 该字段被拒（不是静默接受）
    check("值无法解析" in txt and "F_PSN_RES_OVERSEAS" in txt,
          "③ 非法码（Y1 不是 CT_YES_NO 的码）被识别为该字段被拒")
    # ④ 授权门
    check("授权门拒绝" in txt and "CONSENT_REQUIRED" in txt,
          "④ 未授权的行被授权门拒绝（门在工作，不是靠自觉）")
    # 不认识的列
    check("不认识的列" in txt, "不认识的列被报告，不静默丢弃")

    after = snapshot()
    # 4 个新身份：5 行 − 1 行未授权。
    # 注意"非法码"那一行**仍会建人** —— 它被拒的只是那一个动态字段，
    # 而契约字段（阶段/学历/专业/城市）都是合法的。（我第一版把期望写成 3，错了。）
    check(after["ext"] == 4,
          "导入 4 个新身份（5 行 − 1 未授权；非法码那行只拒了那个字段）= %d"
          % after["ext"])
    check(after["person"] > base["person"], "person 表确实增加了人")
    # ② 缺失 ≠ 空值：没填的人**没有那一行**
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        n_ov = c.execute("""SELECT count(*) AS n FROM mt.field_value
                             WHERE field_id='F_PSN_RES_OVERSEAS'
                               AND subject_type='person'""").fetchone()["n"]
        n_blank = c.execute("""SELECT count(*) AS n FROM mt.field_value
                                WHERE value_code IS NULL OR value_code = ''""").fetchone()["n"]
    check(n_ov >= 1, "动态字段写入了值（%d 条）" % n_ov)
    check(n_blank == 0,
          "**没有任何空值行** —— 缺的字段是不写行，而不是写空（缺失 ≠ 空值）")

    # ⑤ 幂等：同一批再导一次，不应多出人
    run(["--file", sample, "--apply"])
    again = snapshot()
    check(again["ext"] == after["ext"] and again["person"] == after["person"],
          "⑤ 重复导入是**版本更新**而不是多建人（身份 %d、person %d 都没变）"
          % (again["ext"], again["person"]))

    # ⑥ 清理覆盖面 = 构造覆盖面
    run(["--cleanup", "--yes"])
    after_clean = snapshot()
    check(after_clean["ext"] == 0, "清理后该来源外部身份清零")
    check(after_clean["person"] == base["person"],
          "清理后 person 回到基线（%d → %d）" % (after["person"], after_clean["person"]))
    check(after_clean["fv"] == base["fv"],
          "**field_value 也回到基线**（%d → %d）—— 清理覆盖面等于构造覆盖面"
          % (after["fv"], after_clean["fv"]))
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        orphan = c.execute("""
            SELECT count(*) AS n FROM mt.answer a
             WHERE NOT EXISTS (SELECT 1 FROM mt.response_session s
                                WHERE s.session_id = a.session_id)""").fetchone()["n"]
    check(orphan == 0, "清理后没有孤儿答卷行（%d）" % orphan)

    try:
        os.remove(sample)
    except OSError:
        pass
    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
