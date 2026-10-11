# -*- coding: utf-8 -*-
"""
ops/tests/default_columns_test.py —— 默认列（评审优先级 5 的"默认列"）

评审把基础视觉的整改范围收窄为：导航组织、**默认列**、移动端、真实任务验证。

核对运行状态：只有**人才库**有默认列机制，通用表页 `/t/<表>` 是 `SELECT *`
把所有列铺开（`occupation` 25 列、`job_posting` 41 列，最前面常是簿记时间戳）。

这一套守住四件事：
  ① **覆盖 100%**：每张表的每一列都要有解析结果 —— 少一列就意味着
     "那一列在默认视图里不存在"（本库最怕的「看不到 ≠ 没有」）。
  ② **主键必须排第一**（`job_posting` 第一列是 `job_id` 而不是 `employer_id`）。
     ⚠ 判据必须是**系统目录** `pg_index.indisprimary`，不是列名模式 ——
     073 第一版用 `^(id|.*_id)$` 认主键，把外键也当成主键（真 id 排到第三）。
     本库口径纪律写着「判据是事实，不是名字」，这里是同一个要求。
  ③ **隐藏 ≠ 不可达**：默认隐藏的列必须仍然可达（页面展开后列数变多），
     且页面要说明**为什么隐藏**。否则就是「看不到 ≠ 没有」的界面翻版。
  ④ **新表自动获得默认列**：跑一次 `refresh_default_columns()` 就覆盖 ——
     不依赖谁记得改代码（本库在业务域划分上因"两处清单"漏过四次）。
     这里**真的建一张临时表**验证这一点，再删掉。
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        def q1(sql, p=None):
            return c.execute(sql, p).fetchone()

        # ---------- ① 覆盖 100% ----------
        n_cols = q1("""SELECT count(*) AS n FROM information_schema.columns c
                         JOIN pg_class cl ON cl.relname = c.table_name
                         JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname='mt'
                        WHERE c.table_schema='mt' AND cl.relkind='r'""")["n"]
        n_res = q1("SELECT count(*) AS n FROM mt.table_default_columns")["n"]
        check(n_cols == n_res,
              "默认列解析覆盖每一张基础表的每一列（%d == %d）—— "
              "少一列就意味着那一列在默认视图里不存在" % (n_res, n_cols))
        cov = q1("SELECT * FROM mt.metric_value('M_UI_DEFAULT_COL_COVERAGE')")
        check(cov["value"] == 100.0,
              "解析覆盖率口径 = 100（实得 %s）—— 必须保持 100 而不是「可见列占比」"
              % cov["value"])
        hid = q1("SELECT * FROM mt.metric_value('M_UI_HIDDEN_COL_SHARE')")
        check(hid["value"] is not None and 0 < hid["value"] < 100,
              "隐藏列占比在 (0,100) 内（实得 %s%%）—— 全藏或全不藏都说明规则没生效"
              % hid["value"])

        # ---------- ② 主键必须排第一，判据来自系统目录 ----------
        bad = q1("""SELECT count(*) AS n FROM (
                      SELECT c.relname AS t, a.attname AS col
                        FROM pg_index i
                        JOIN pg_class c ON c.oid = i.indrelid
                        JOIN pg_namespace ns ON ns.oid = c.relnamespace AND ns.nspname='mt'
                        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
                       WHERE i.indisprimary) pk
                      LEFT JOIN mt.table_default_columns t
                             ON t.table_name = pk.t AND t.column_name = pk.col
                     WHERE t.position IS DISTINCT FROM 1 OR t.visible IS NOT TRUE""")["n"]
        check(bad == 0,
              "**每张表的主键都排在第一位且默认可见**（违规 %d）—— "
              "默认视图第一列必须是「这是哪条记录」" % bad)
        # 反证：规则里不该再用列名模式认主键（否则外键会混进第一档）
        r = q1("""SELECT position, note FROM mt.default_column_rule WHERE rule_id='pk_id'""")
        check(r and r["position"] > 1,
              "列名规则 `*_id` 已降级为「引用列」（position=%s）—— "
              "不再冒充主键（否则外键会挤到真 id 前面）" % (r["position"] if r else "?"))
        pk_rule = q1("""SELECT count(*) AS n FROM mt.table_default_columns
                         WHERE rule_id='pk_catalog'""")["n"]
        check(pk_rule > 0,
              "有 %d 个主键由**系统目录**判定（rule_id=pk_catalog）" % pk_rule)
        # 抽查：job_posting 第一列是 job_id
        first = q1("""SELECT column_name FROM mt.table_default_columns
                       WHERE table_name='job_posting' AND position=1 LIMIT 1""")
        check(first and first["column_name"] == "job_id",
              "抽查 job_posting 第一列 = job_id（实得 %s）—— "
              "073 第一版这里是 employer_id" % (first["column_name"] if first else "?"))

        # ---------- ③ 隐藏 ≠ 不可达 ----------
        # 至少有一张表真的隐藏了列（否则这一套没测到东西）
        n_hid = q1("SELECT count(*) AS n FROM mt.table_default_columns "
                   "WHERE NOT visible")["n"]
        check(n_hid > 0, "确实有列被默认隐藏（%d 列）—— 规则生效了" % n_hid)
        # 隐藏列必须**仍然在解析表里**（不是被删掉），只是 visible=false
        orphan = q1("""SELECT count(*) AS n FROM information_schema.columns c
                         JOIN pg_class cl ON cl.relname = c.table_name
                         JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname='mt'
                        WHERE c.table_schema='mt' AND cl.relkind='r'
                          AND NOT EXISTS (SELECT 1 FROM mt.table_default_columns t
                                           WHERE t.table_name=c.table_name
                                             AND t.column_name=c.column_name)""")["n"]
        check(orphan == 0, "没有任何列被「删掉」（全部只是 visible=false），违规 %d" % orphan)
        # 每张表都至少有一列默认可见（否则默认视图是空表）
        empty = q1("""SELECT count(*) AS n FROM (
                        SELECT table_name FROM mt.table_default_columns
                         GROUP BY table_name HAVING count(*) FILTER (WHERE visible)=0) x""")["n"]
        check(empty == 0, "没有「默认一列都不显示」的表（违规 %d）" % empty)
        # 隐藏必须有理由（否则用户不知道为什么看不到）
        noreason = q1("""SELECT count(*) AS n FROM mt.table_default_columns
                          WHERE NOT visible AND (reason IS NULL OR reason='')""")["n"]
        check(noreason == 0, "每个隐藏列都写了理由（违规 %d）—— 页面会把它展示出来" % noreason)

        # ---------- ④ 新表自动获得默认列（真的建一张临时表） ----------
        c.execute("CREATE TABLE IF NOT EXISTS mt.__tmp_newtab__ ("
                  "id text PRIMARY KEY, label_zh text, recorded_at timestamptz, "
                  "attrs jsonb, mystery_col text)")
        try:
            c.execute("SELECT mt.refresh_default_columns()")
            rows = c.execute("SELECT column_name, visible, position "
                             "FROM mt.table_default_columns "
                             "WHERE table_name='__tmp_newtab__'").fetchall()
            names = {x["column_name"] for x in rows}
            check(len(rows) == 5,
                  "**新建的表跑一次 refresh 就自动获得默认列**（%d/5 列）—— "
                  "不依赖谁记得改代码" % len(rows))
            check(names == {"id", "label_zh", "recorded_at", "attrs", "mystery_col"},
                  "新表的每一列都被解析（含未命中规则的 mystery_col → 默认可见）")
            d = {x["column_name"]: x["visible"] for x in rows}
            check(d.get("id") is True, "新表主键 id 默认可见")
            check(d.get("recorded_at") is False, "新表簿记列 recorded_at 默认隐藏")
            check(d.get("attrs") is False, "新表内部载荷 attrs 默认隐藏")
            check(d.get("mystery_col") is True,
                  "**未命中任何规则的列默认可见** —— 宁可多给一列，也不要让人以为它不存在")
            pos = {x["column_name"]: x["position"] for x in rows}
            check(pos["id"] == 1, "新表主键排第一位（来自系统目录，不是列名）")
        finally:
            c.execute("DROP TABLE IF EXISTS mt.__tmp_newtab__")
            c.execute("SELECT mt.refresh_default_columns()")
        # 清理后必须回到"无该表"，且覆盖率仍 100%
        left = q1("SELECT count(*) AS n FROM mt.table_default_columns "
                  "WHERE table_name='__tmp_newtab__'")["n"]
        check(left == 0, "临时表已从默认列解析中清除（%d 行残留）—— 没留残渣" % left)
        check(q1("SELECT * FROM mt.metric_value('M_UI_DEFAULT_COL_COVERAGE')")["value"] == 100.0,
              "清理后解析覆盖率仍为 100")

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
