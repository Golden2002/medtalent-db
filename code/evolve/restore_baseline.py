#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
code/evolve/restore_baseline.py —— 把「种子基线」与「演化增量」分开管理

背景（踩过的坑）：
  演化测试会拆分/退役节点。测试清理时若"先删增量再删节点"的顺序不当，
  可能把**种子基线里的真实节点**也一起退役掉，导致职业树凭空少几枝。

这里的处理原则：
  · `schema/catalog/occupation_seed.csv` 是**基线**，它列出的节点必须存在且 active；
  · 演化新长出来的节点（ID 形如 OCC-xx-X******）是**增量**，允许被退役；
  · 本脚本把基线节点恢复为 active/未退役，并且**不动**任何增量节点的状态；
  · 同时可选用 `--purge-increment` 清掉遗留的测试增量（默认关闭，避免误删真实演化成果）。

用法：
  python code/evolve/restore_baseline.py                     # 只恢复基线
  python code/evolve/restore_baseline.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
SEED = os.path.join(BASE, "schema", "catalog", "occupation_seed.csv")
INCREMENT_ID = re.compile(r"^OCC-F\d+-X[0-9A-F]{8}$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-increment", action="store_true",
                    help="同时清掉形如 OCC-Fxx-X******** 的遗留增量节点（谨慎）")
    a = ap.parse_args()

    with io.open(SEED, encoding="utf-8-sig", newline="") as fh:
        seed_ids = [r["occupation_id"] for r in csv.DictReader(fh)]

    with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute("""SELECT occupation_id, status, valid_to FROM occupation
                       WHERE occupation_id = ANY(%s)""", (seed_ids,))
        rows = {r["occupation_id"]: r for r in cur.fetchall()}
        missing = [i for i in seed_ids if i not in rows]
        retired = [i for i, r in rows.items() if r["status"] != "active" or r["valid_to"]]

        print("基线节点 %d 个：缺失 %d，被退役 %d"
              % (len(seed_ids), len(missing), len(retired)))
        if missing:
            print("  缺失（需要重新导入种子）：%s" % "、".join(missing[:8]))
        if retired:
            print("  将被恢复：%s" % "、".join(retired[:8]))

        if a.dry_run:
            print("[dry-run] 未执行")
            return 0

        if retired:
            cur.execute("""UPDATE occupation SET status='active', valid_to=NULL,
                              retired_reason=NULL, updated_at=now()
                           WHERE occupation_id = ANY(%s)""", (retired,))
            print("[✓] 已恢复 %d 个基线节点" % len(retired))

        # 清理这些节点相关的迁移记录（恢复后它们不该再指向不存在的后继）
        if retired:
            cur.execute("""DELETE FROM occupation_migration
                           WHERE old_id = ANY(%s) OR new_id = ANY(%s)""",
                        (retired, retired))
            print("[✓] 已清理相关迁移记录 %d 条" % cur.rowcount)

        # 悬空变更记录：to_ids 指向的节点已不存在（节点从不物理删除，出现即脏数据）。
        # 踩过的坑（问题 #34）：演化测试清理只删了迁移表，留下 to_ids 悬空的
        # occupation_change，看起来像"某个职业被拆分过"，其实后继早已不存在。
        cur.execute("""DELETE FROM occupation_change ch
                       WHERE coalesce(array_length(ch.to_ids,1),0) > 0
                         AND NOT EXISTS (SELECT 1 FROM occupation o
                                         WHERE o.occupation_id = ANY(ch.to_ids))""")
        if cur.rowcount:
            print("[✓] 已清理悬空变更记录 %d 条" % cur.rowcount)
        if retired:
            cur.execute("DELETE FROM occupation_change WHERE from_ids && %s", (retired,))
            if cur.rowcount:
                print("[✓] 已清理与恢复节点相关的变更记录 %d 条" % cur.rowcount)

        # 重新导入缺失的种子节点
        if missing:
            print("  → 请运行：python code\\load_catalog.py   （会重新导入缺失的种子节点）")

        if a.purge_increment:
            cur.execute("SELECT occupation_id, label_zh FROM occupation "
                        "WHERE status='active' AND valid_to IS NULL")
            inc = [r["occupation_id"] for r in cur.fetchall() if INCREMENT_ID.match(r["occupation_id"])]
            if inc:
                cur.execute("DELETE FROM job_competency_weight WHERE occupation_id = ANY(%s)", (inc,))
                cur.execute("DELETE FROM competency_drift WHERE occupation_id = ANY(%s)", (inc,))
                cur.execute("DELETE FROM occupation_migration WHERE old_id = ANY(%s) OR new_id = ANY(%s)", (inc, inc))
                # 顺序不能反：occupation_migration.change_id 外键指向 occupation_change
                cur.execute("""DELETE FROM occupation_change
                               WHERE from_ids && %s OR to_ids && %s
                                  OR (coalesce(array_length(to_ids,1),0) > 0
                                      AND NOT EXISTS (SELECT 1 FROM occupation o
                                                      WHERE o.occupation_id = ANY(to_ids)))""",
                            (inc, inc))
                cur.execute("DELETE FROM occupation WHERE occupation_id = ANY(%s)", (inc,))
                print("[✓] 已清除 %d 个遗留增量节点" % len(inc))

        cur.execute("SELECT count(*) AS n FROM occupation WHERE status='active' AND valid_to IS NULL")
        print("[✓] 当前树节点：%d" % cur.fetchone()["n"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
