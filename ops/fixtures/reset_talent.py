# -*- coding: utf-8 -*-
"""
ops/fixtures/reset_talent.py —— 清除 mock 人才档案（**只删 per_mock_% 及其从属行**）

为什么要单独一个脚本：mock 人才是"结构可分析"的合成样本，重新生成前必须先把旧样本
清干净，否则行数会翻倍、分布会失真。但人才侧的子表有 29 张（person 的外键子表），
顺序错了会被 FK 拒绝，漏了会留下孤儿行。

本脚本的两条纪律：
  1. **绝不碰非 mock 数据**：所有 DELETE 都带 `person_id LIKE 'per_mock_%'`
     （没有 person_id 的少数表用 subject_id，同样带前缀过滤）。
  2. **删除顺序服从外键**：先删"引用别人"的表（skill_assertion 引用 evidence），
     再删被引用的表；person 最后删。person 的外键子表清单从 pg_constraint 动态读取，
     不手写白名单——表会继续增加，白名单式清理必然漏项。

用法：python ops/fixtures/reset_talent.py
      python ops/fixtures/reset_talent.py --keep-registry   # 保留 mock 来源/采集批次登记
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")

MOCK_PREFIX = "per_mock_"
SRC_ID = "src_fixture_talent_mock"
RUN_ID = "run_fixture_talent_mock"

# 必须先删的"引用别人"的表：skill_assertion.evidence_id -> evidence.evidence_id
DELETE_FIRST = ["skill_assertion", "credential", "evidence"]

# 没有指向 person 的外键、但带 person_id/subject_id 的表（手写补齐，动态查询查不到）
DELETE_EXTRA = [
    ("consent_record", "person_id"),
    ("assertion", "subject_id"),
    ("field_value", "subject_id"),
    ("derived_feature", "subject_id"),
    ("search_document", "subject_id"),
]

# 这些表是派生/统计结果，清 mock 人才时一并按 person 前缀清理
DELETE_ALSO = ["match_result", "gap_analysis"]


def person_fk_children(c) -> list:
    """person(person_id) 的全部外键子表（按表名字典序返回）。"""
    c.execute("""
        SELECT DISTINCT cl.relname
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_class rf ON rf.oid = con.confrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE con.contype = 'f' AND rf.relname = 'person' AND n.nspname = 'mt'
        ORDER BY 1""")
    return [r["relname"] for r in c.fetchall()]


def _table_exists(c, name) -> bool:
    c.execute("""SELECT 1 FROM information_schema.tables
                 WHERE table_schema='mt' AND table_name=%s""", (name,))
    return c.fetchone() is not None


def _has_column(c, table, col) -> bool:
    c.execute("""SELECT 1 FROM information_schema.columns
                 WHERE table_schema='mt' AND table_name=%s AND column_name=%s""",
              (table, col))
    return c.fetchone() is not None


def reset(c, prefix=MOCK_PREFIX, purge_registry=True, quiet=False) -> dict:
    """删除 prefix 开头的人才及其从属行；返回 {表名: 删除行数}。"""
    like = prefix + "%"
    deleted = {}

    with c.cursor() as cur:
        children = person_fk_children(cur)

    ordered = []
    for t in DELETE_FIRST:
        if t in children and t not in ordered:
            ordered.append(t)
    for t in DELETE_ALSO:
        if t in children and t not in ordered:
            ordered.append(t)
    for t in children:
        if t not in ordered:
            ordered.append(t)

    with c.cursor() as cur:
        for t in ordered:
            cur.execute(sql.SQL("DELETE FROM mt.{} WHERE person_id LIKE %s")
                        .format(sql.Identifier(t)), (like,))
            if cur.rowcount:
                deleted[t] = cur.rowcount

        for t, col in DELETE_EXTRA:
            if _table_exists(c=cur, name=t) and _has_column(cur, t, col):
                cur.execute(sql.SQL("DELETE FROM mt.{} WHERE {} LIKE %s")
                            .format(sql.Identifier(t), sql.Identifier(col)), (like,))
                if cur.rowcount:
                    deleted[t] = cur.rowcount

        cur.execute("DELETE FROM mt.person WHERE person_id LIKE %s", (like,))
        deleted["person"] = cur.rowcount

        if purge_registry:
            # 此时已无 person 引用它们，可以安全删除 mock 来源与采集批次
            cur.execute("DELETE FROM mt.ingest_run WHERE ingest_run_id = %s", (RUN_ID,))
            if cur.rowcount:
                deleted["ingest_run"] = cur.rowcount
            cur.execute("DELETE FROM mt.source_registry WHERE source_id = %s", (SRC_ID,))
            if cur.rowcount:
                deleted["source_registry"] = cur.rowcount

    c.commit()
    if not quiet:
        total = sum(v for k, v in deleted.items() if k != "person")
        print("[✓] mock 人才已清除：person %d 行，从属行 %d 行（共 %d 张表）"
              % (deleted.get("person", 0), total, len(deleted)))
        for t in sorted(deleted):
            print("      %-22s %d" % (t, deleted[t]))
    return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-registry", action="store_true",
                    help="保留 src_fixture_talent_mock / run_fixture_talent_mock 登记行")
    a = ap.parse_args()

    with psycopg.connect(DSN, row_factory=dict_row) as c:
        before = count_non_mock(c)
        reset(c, purge_registry=not a.keep_registry)
        after = count_non_mock(c)
    print("    非 mock 人才行数：%d → %d（应保持不变）" % (before, after))
    return 0


def count_non_mock(c) -> int:
    with c.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM mt.person WHERE person_id NOT LIKE %s",
                    (MOCK_PREFIX + "%",))
        return cur.fetchone()["n"]


if __name__ == "__main__":
    sys.exit(main())
