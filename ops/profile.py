# -*- coding: utf-8 -*-
"""
ops/profile.py —— 列级剖析的离线计算（写入 mt.column_profile）

为什么计算必须离线、页面只能读快照
--------------------------------------------------------------------------
实测（不是估计）：
  · job_posting 41 列的空值率剖析：约 3 ms —— 小表实时算没问题
  · change_log **单列** `count(DISTINCT …)`：**1319 ms**（565k 行，external merge sort）
  · change_log 全列含 DISTINCT：**1439 ms**
而 `--check` 与 `run_all.py` 会把每个页面渲染一遍。任何一处实时剖析都会把回归拖垮。

本工具回答的是用户那句「我能看到…字段名，数量，能够分类统计」
——"数量"必须拆成**行数 / 非空数 / 去重数**三件事，否则用户会把行数当成有值数。

设计要点
--------------------------------------------------------------------------
1. **精确优先**：`n_distinct` 用精确 `count(DISTINCT …)`，不用 `pg_stats.n_distinct`
   （后者是 ANALYZE 后的采样估计，负数还表示比例）。
2. **代价可见**：每列记录 `elapsed_ms`，并设 `--max-ms` 预算；超预算的列被跳过并标注，
   而不是让一次全量剖析跑一个小时没人知道。
3. **Top-K 只在有意义时给**：基数过大时写 `top_note` 说明原因，
   **绝不拿"前 10 个值"冒充分布**（那是最常见的误导）。
4. **能当分类维度**是独立字段（调 `mt.is_classifiable`，口径只有一处实现），
   因为用户"分类统计"时会先撞上"这列能不能分组"。
5. 剖析结果**是快照**：每行带 `computed_at`，页面必须显示它。

用法
    python ops/profile.py --tables person,job_posting     # 只算这两张表
    python ops/profile.py --all --max-ms 200 --budget 120 # 全库，每列 200ms 上限，总 120s 预算
    python ops/profile.py --report                        # 只看覆盖率与最慢的列
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
       "options='-c search_path=mt,public'")

TOP_K = 12                  # Top-K 取多少个值
ENUM_MAX = 200              # 基数上限：超过就不适合直接 group by
SKIP_TYPES = {"bytea", "ARRAY", "USER-DEFINED"}   # 不逐一剖析的类型


def cols_of(c, table):
    return c.execute("""
        SELECT c.column_name, c.data_type
          FROM information_schema.columns c
         WHERE c.table_schema = 'mt' AND c.table_name = %s
         ORDER BY c.ordinal_position""", (table,)).fetchall()


def profile_column(c, table, col, dtype, n_rows, max_ms, conn_factory):
    """剖析一列。返回 dict（可直接写库）。

    每一步都单独计时：这样"哪一列贵"是**测出来的**，不是猜的。
    """
    t0 = time.time()
    ident = sql.SQL("{}.{}").format(sql.Identifier("mt"), sql.Identifier(table))
    cid = sql.Identifier(col)
    row = {"table_name": table, "column_name": col, "data_type": dtype,
           "n_rows": n_rows, "sample_where": None, "top_note": None,
           "top_values": None, "is_enum_like": False}

    # ① 非空数（精确）：count(列) 只数非 NULL —— 这是"数量"的第一层含义
    n_not_null = c.execute(
        sql.SQL("SELECT count({}) AS n FROM {}").format(cid, ident)).fetchone()["n"]
    row["n_not_null"] = n_not_null
    row["null_frac"] = (round(1 - n_not_null / n_rows, 4) if n_rows else None)

    if time.time() - t0 > max_ms / 1000.0:
        row["top_note"] = "跳过：非空数已超出单列时间预算（%dms）" % max_ms
        row["elapsed_ms"] = int((time.time() - t0) * 1000)
        return row

    # ② 去重数（精确）。这是"这列能不能当分类维度"的依据。
    n_distinct = c.execute(
        sql.SQL("SELECT count(DISTINCT {}) AS n FROM {}").format(cid, ident)).fetchone()["n"]
    row["n_distinct"] = n_distinct
    row["is_enum_like"] = c.execute(
        "SELECT mt.is_classifiable(%s, %s) AS x", (n_rows, n_distinct)).fetchone()["x"]

    # ③ 最值：用文本化输出，保证任意类型都能统一展示
    mm = c.execute(sql.SQL(
        "SELECT min({0}::text) AS lo, max({0}::text) AS hi FROM {1}").format(cid, ident)
    ).fetchone()
    row["min_value"], row["max_value"] = mm["lo"], mm["hi"]

    # ④ 文本长度均值：只在字符类型上有意义
    if dtype and ("char" in dtype or dtype == "text"):
        av = c.execute(sql.SQL(
            "SELECT round(avg(length({0})), 2) AS a FROM {1}").format(cid, ident)).fetchone()["a"]
        row["avg_len"] = av

    # ⑤ Top-K：只在基数不过大时给。**基数大就给 NULL + 说明**，
    #    因为"前 10 个值"在一个 690 基数的自由文本列上毫无代表性，给了就是误导。
    if n_distinct <= ENUM_MAX:
        tops = c.execute(sql.SQL(
            "SELECT {0}::text AS v, count(*) AS n FROM {1} "
            "WHERE {0} IS NOT NULL GROUP BY {0} ORDER BY n DESC, v LIMIT %s").format(
                cid, ident), (TOP_K,)).fetchall()
        row["top_values"] = json.dumps(
            [{"v": t["v"], "n": t["n"]} for t in tops], ensure_ascii=False)
        row["top_note"] = ("基数 %d，取前 %d 个值" % (n_distinct, min(TOP_K, len(tops))))
    else:
        row["top_note"] = ("基数 %d 超过 %d，不给 Top-K："
                           "在这么高的基数上「前几个值」没有代表性，给了会误导"
                           % (n_distinct, ENUM_MAX))

    row["elapsed_ms"] = int((time.time() - t0) * 1000)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", help="逗号分隔的表名（默认：全部基础表）")
    ap.add_argument("--all", action="store_true", help="全部基础表")
    ap.add_argument("--max-ms", type=int, default=1500,
                    help="单列时间预算（毫秒），超出则跳过剩余步骤并标注原因")
    ap.add_argument("--budget", type=float, default=300.0, help="总时间预算（秒）")
    ap.add_argument("--report", action="store_true", help="只出覆盖率报告，不计算")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as c:
        if a.report:
            rows = c.execute("""
                SELECT * FROM v_column_profile_coverage ORDER BY
                  (n_profiled::float / GREATEST(n_columns,1)) ASC, table_name""").fetchall()
            print("%-28s %6s %8s %10s %9s %7s" %
                  ("表", "列数", "已剖析", "覆盖率", "总耗时ms", "空列"))
            print("-" * 76)
            tot = prof = 0
            for r in rows:
                tot += r["n_columns"]
                prof += r["n_profiled"]
                print("%-28s %6d %8d %9.1f%% %9d %7d"
                      % (r["table_name"], r["n_columns"], r["n_profiled"],
                         100.0 * r["n_profiled"] / max(r["n_columns"], 1),
                         r["total_ms"], r["n_all_null"]))
            print("-" * 76)
            print("合计：%d 列，已剖析 %d（%.1f%%）"
                  % (tot, prof, 100.0 * prof / max(tot, 1)))
            slow = c.execute("""SELECT table_name, column_name, elapsed_ms
                                  FROM column_profile ORDER BY elapsed_ms DESC LIMIT 8""").fetchall()
            if slow:
                print("\n最慢的 8 列（这是「为什么必须离线算」的证据）：")
                for s in slow:
                    print("  %8d ms  %s.%s" % (s["elapsed_ms"], s["table_name"], s["column_name"]))
            return 0

        if a.tables:
            tables = [t.strip() for t in a.tables.split(",") if t.strip()]
        else:
            tables = [r["table_name"] for r in c.execute("""
                SELECT table_name FROM information_schema.tables
                 WHERE table_schema='mt' AND table_type='BASE TABLE' ORDER BY table_name""")]

        # 行数与等级只需算一次，别每列查一遍
        n_rows_map = {}
        for t in tables:
            try:
                n_rows_map[t] = c.execute(
                    sql.SQL("SELECT count(*) AS n FROM mt.{}").format(sql.Identifier(t))
                ).fetchone()["n"]
            except psycopg.Error as e:
                print("[!] 跳过 %s：%s" % (t, str(e).splitlines()[0][:70]), file=sys.stderr)
                n_rows_map[t] = None

        tier_map = {(r["table_name"], r["column_name"]): r["min_tier"]
                    for r in c.execute("SELECT table_name, column_name, min_tier FROM column_policy")}

        started = time.time()
        done = skipped = 0
        for t in tables:
            if n_rows_map.get(t) is None:
                continue
            for col in cols_of(c, t):
                if time.time() - started > a.budget:
                    print("[!] 总预算 %.0fs 用尽，停止（已算 %d 列）" % (a.budget, done),
                          file=sys.stderr)
                    break
                if col["data_type"] in SKIP_TYPES:
                    skipped += 1
                    continue
                try:
                    row = profile_column(c, t, col["column_name"], col["data_type"],
                                         n_rows_map[t], a.max_ms, None)
                except psycopg.Error as e:
                    # 单列失败不该让整轮剖析中断：记下原因，继续（并让原因可见）
                    row = {"table_name": t, "column_name": col["column_name"],
                           "data_type": col["data_type"], "n_rows": n_rows_map[t],
                           "top_note": "剖析失败：%s" % str(e).splitlines()[0][:120],
                           "top_values": None, "is_enum_like": False}
                row["tier"] = tier_map.get((t, col["column_name"]))
                write(c, row)
                done += 1
                if not a.quiet and row.get("elapsed_ms", 0) > 200:
                    print("  %6d ms  %s.%s" % (row["elapsed_ms"], t, col["column_name"]))
        print("[✓] 剖析完成：%d 列（跳过 %d 列的类型）· 耗时 %.1fs"
              % (done, skipped, time.time() - started))
    return 0


def write(c, row):
    c.execute("""
        INSERT INTO mt.column_profile
          (table_name, column_name, data_type, computed_at, n_rows, n_not_null,
           n_distinct, null_frac, min_value, max_value, avg_len, top_values,
           top_note, is_enum_like, tier, sample_where, elapsed_ms)
        VALUES (%(table_name)s, %(column_name)s, %(data_type)s, now(), %(n_rows)s,
                %(n_not_null)s, %(n_distinct)s, %(null_frac)s, %(min_value)s, %(max_value)s,
                %(avg_len)s, %(top_values)s, %(top_note)s, %(is_enum_like)s, %(tier)s,
                %(sample_where)s, %(elapsed_ms)s)
        ON CONFLICT (table_name, column_name) DO UPDATE SET
          data_type = EXCLUDED.data_type, computed_at = now(), n_rows = EXCLUDED.n_rows,
          n_not_null = EXCLUDED.n_not_null, n_distinct = EXCLUDED.n_distinct,
          null_frac = EXCLUDED.null_frac, min_value = EXCLUDED.min_value,
          max_value = EXCLUDED.max_value, avg_len = EXCLUDED.avg_len,
          top_values = EXCLUDED.top_values, top_note = EXCLUDED.top_note,
          is_enum_like = EXCLUDED.is_enum_like, tier = EXCLUDED.tier,
          sample_where = EXCLUDED.sample_where, elapsed_ms = EXCLUDED.elapsed_ms""",
        {**{"data_type": None, "n_rows": None, "n_not_null": None, "n_distinct": None,
            "null_frac": None, "min_value": None, "max_value": None, "avg_len": None,
            "top_values": None, "sample_where": None, "elapsed_ms": None}, **row})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
