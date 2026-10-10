# -*- coding: utf-8 -*-
"""
ops/seed_demo_analysis.py —— 为"分类变量影响分析"生成**合成演示数据**（可清理）

为什么要它
--------------------------------------------------------------------------
实测：`field_value` **总共 0 行** —— 52 个字段登记了，但一个值都没有。
所以"分析海外经历/年龄对求职结果的影响"此前**根本跑不起来**：
不是分析方法不对，而是**没有数据**。

本工具为 `per_mock_*`（合成人）写入三个分类变量，让分析有东西可跑：
  · 年龄段   F_PSN_CRED_AGE_BAND   ← 从 person_demographics.age_band **如实搬运**（不是编的）
  · 海外经历 F_PSN_RES_OVERSEAS     ← 合成（按 person_id 的哈希确定性生成）
  · 求职结果 F_PSN_RES_JOB_OUTCOME  ← 合成，**刻意带了效应**：有海外经历的就业率更高

⚠ 必须写清楚的话
--------------------------------------------------------------------------
这是**合成数据**，用它跑出来的"影响"是**我们自己塞进去的**，不是真实发现。
它唯一的用途是验证"分析口径能不能跑、数字对不对"。
**不要**把这里的结论当成对真实人才的判断 —— 相关不等于因果，而且这批数据本来就是假的。
工具因此把三个变量的来源、生成规则、以及"这是合成数据"全部打出来。

用法
    python ops/seed_demo_analysis.py --apply      # 写入演示数据
    python ops/seed_demo_analysis.py --show       # 只看分布，不写
    python ops/seed_demo_analysis.py --cleanup    # 删掉本工具写入的值
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")

FIELDS = ("F_PSN_CRED_AGE_BAND", "F_PSN_RES_OVERSEAS", "F_PSN_RES_JOB_OUTCOME")

# 合成规则（确定性：同一个人每次算出来一样，便于复核与复现）
SQL_PLAN = """
WITH base AS (
  SELECT p.person_id, d.age_band,
         -- 海外经历：按 person_id 的哈希确定性分配，约 30% 为"有"
         CASE WHEN abs(hashtext(p.person_id || 'ov')) % 100 < 30 THEN 'Y' ELSE 'N' END AS ov
    FROM mt.person p
    LEFT JOIN mt.person_demographics d ON d.person_id = p.person_id
   WHERE p.person_id LIKE 'per_mock_%'
), plan AS (
  SELECT person_id, coalesce(age_band, 'AG9') AS age_band, ov,
         -- 求职结果：**刻意带效应**（有海外经历 → 就业率更高；低龄更可能深造）
         -- 注意分支顺序：把"深造"放在最前，否则低龄的人会被前面的就业分支吃掉，
         -- 导致 JO3 分支不可达（第一版就是这样，实测 JO3 = 0 人才发现）
         CASE
           WHEN age_band IN ('AG1','AG2')
                AND abs(hashtext(person_id || 'jo')) % 100 < 20 THEN 'JO3'
           WHEN ov = 'Y' AND abs(hashtext(person_id || 'jo')) % 100 < 70 THEN 'JO1'
           WHEN ov = 'N' AND abs(hashtext(person_id || 'jo')) % 100 < 45 THEN 'JO1'
           WHEN abs(hashtext(person_id || 'jo')) % 100 < 80 THEN 'JO2'
           ELSE 'JO4'
         END AS outcome
    FROM base
)
SELECT * FROM plan ORDER BY person_id
"""


def rows(c):
    return c.execute(SQL_PLAN).fetchall()


def cmd_show(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        plan = rows(c)
        print("将写入 %d 个 per_mock_* 人 × 3 个分类变量 = %d 条值\n"
              % (len(plan), len(plan) * 3))
        print("合成规则（**确定性**：同一个人每次算出来一样）：")
        print("  海外经历  按 person_id 哈希，约 30% 为「有」")
        print("  求职结果  **刻意带效应**：有海外经历 → 就业概率 70%；无 → 45%")
        print("           低龄(AG1/AG2)另有 20% 概率走「升学深造」")
        print("  年龄段    **如实搬运** person_demographics.age_band（不是编的）")
        print()
        print("求职结果分布：")
        from collections import Counter
        for k, v in Counter(r["outcome"] for r in plan).most_common():
            print("  %-4s %3d 人" % (k, v))
    return 0


def cmd_apply(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        plan = rows(c)
        for f in FIELDS:
            n = c.execute("SELECT count(*) AS n FROM mt.field_catalog WHERE field_id=%s "
                          "AND status='active'", (f,)).fetchone()["n"]
            if not n:
                raise SystemExit("[X] 字段 %s 还没登记 —— 先按指南第 3 步创建它" % f)
    n = 0
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        for r in plan:
            for fid, code in (("F_PSN_CRED_AGE_BAND", r["age_band"]),
                              ("F_PSN_RES_OVERSEAS", r["ov"]),
                              ("F_PSN_RES_JOB_OUTCOME", r["outcome"])):
                c.execute("SELECT mt.set_value('person', %s, %s, %s)",
                          (r["person_id"], fid, code))
                n += 1
    print("[✓] 已写入 %d 条合成演示值（%d 人 × 3 变量）" % (n, len(plan)))
    print("    [!] 这是**合成数据**：分析出来的「影响」是我们塞进去的，不是真实发现。")
    print("    清理：python ops\\seed_demo_analysis.py --cleanup")
    return 0


def cmd_cleanup(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        n = c.execute("""DELETE FROM mt.field_value
                          WHERE field_id = ANY(%s)
                            AND subject_type = 'person'
                            AND subject_id LIKE 'per_mock_%%'""", (list(FIELDS),)).rowcount
        c.commit()
    print("[✓] 已删除 %d 条演示值（只删这 3 个字段 × per_mock_* 主体）" % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description="分类变量影响分析用的合成演示数据")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("apply").set_defaults(fn=cmd_apply)
    sub.add_parser("show").set_defaults(fn=cmd_show)
    sub.add_parser("cleanup").set_defaults(fn=cmd_cleanup)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
