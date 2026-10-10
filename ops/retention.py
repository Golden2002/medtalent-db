# -*- coding: utf-8 -*-
"""
ops/retention.py —— 保留策略的**执行器**（不是策略的发明者）

为什么需要它
--------------------------------------------------------------------------
独立审查指出 `change_log` 无界增长。核查后发现：**策略早就有了** ——
`mt.retention_policy` 里已经登记：

    rp_change_log   change_log   retain_days=1825（60 个月）  action=RA3
    rp_access_log   access_log   retain_days=1095（36 个月）  action=RA3
    rp_pii_contact  person_pii   30 天 / legal_hold 标记

**缺的是执行器**：策略写着 60 个月，却没有任何工具去执行它 ——
到期该清的不清、该归档的不归档，于是"有策略"和"没策略"在实践中一样。

（我第一版本想新建一张 retention_policy 表并写 365/730 天，
  被 `CREATE TABLE IF NOT EXISTS` 撞上同名异构表而报错拦下 ——
  否则我会用一张新表**覆盖掉项目早已定下的 60 个月**。
  这正是"静默跳过 + 我以为它是新表"造成的制度性倒退。所以本工具**只读既有策略**。）

安全设计
--------------------------------------------------------------------------
1. **默认只 plan，不删任何东西**。删除审计是数据资产决策。
2. 真正执行必须同时给 `--apply` **和**确认量（`--max-rows N`）；
   超过 N 行就拒绝 —— 避免"跑一下清掉了半年的审计"。
3. `legal_hold = true` 的策略**一律跳过**（法务保留要求优先于保留期）。
4. 执行前先 `--archive` 把待删行导出到 `.tools/archive/`，且**导出成功才删**。
5. 每一步都写进 access_log（谁在什么时候清理了多少行）。

用法
    python ops/retention.py plan                 # 只报告（默认）
    python ops/retention.py plan --table change_log
    python ops/retention.py apply --table change_log --max-rows 100000 --archive
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
import time

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "ops"))
ARCHIVE_DIR = os.path.join(BASE, ".tools", "archive")

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")

# 时间列名：不同表不同（change_log.access_log 用 at；login_attempt 用 at）
TIME_COL = {"change_log": "at", "access_log": "at", "login_attempt": "at",
            "match_result": "created_at"}


def policies(c):
    return c.execute("""
        SELECT policy_id, entity_name, field_pattern, retain_days, action,
               legal_hold, basis_note
          FROM mt.retention_policy ORDER BY entity_name""").fetchall()


def eligible(c, pol):
    """该策略对应的表是否可被本工具处理。返回 (可处理?, 原因)。"""
    t = pol["entity_name"]
    if pol["legal_hold"]:
        return False, "legal_hold=true（法务保留优先于保留期）"
    if t not in TIME_COL:
        return False, "本工具不知道该表的时间列（TIME_COL 里没有登记）"
    if not c.execute("SELECT count(*) AS n FROM information_schema.tables "
                     "WHERE table_schema='mt' AND table_name=%s", (t,)).fetchone()["n"]:
        return False, "表不存在"
    return True, ""


def count_due(c, table, col, days):
    return c.execute(
        "SELECT count(*) AS n FROM mt.%s WHERE %s < now() - make_interval(days => %%s)"
        % (table, col), (days,)).fetchone()["n"]


def cmd_plan(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        print("%-14s %-22s %-10s %-8s %-8s %s"
              % ("策略", "表", "保留天数", "到期行数", "动作", "说明"))
        print("-" * 118)
        total = 0
        for p in policies(c):
            if a.table and p["entity_name"] != a.table:
                continue
            ok, why = eligible(c, p)
            if not ok:
                print("%-14s %-22s %-10s %-8s %-8s %s"
                      % (p["policy_id"], p["entity_name"], p["retain_days"], "—",
                         p["action"], why))
                continue
            n = count_due(c, p["entity_name"], TIME_COL[p["entity_name"]],
                          p["retain_days"])
            total += n
            print("%-14s %-22s %-10s %-8s %-8s %s"
                  % (p["policy_id"], p["entity_name"], p["retain_days"], f"{n:,}",
                     p["action"], p["basis_note"] or ""))
        print("-" * 118)
        print("到期行数合计：%s（**plan 不删任何东西**）" % f"{total:,}")
        print("要真正执行：apply --table <表> --max-rows N [--archive]")
    return 0


def archive(c, table, col, days, outdir):
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "%s_before_%dd_%s.csv.gz"
                        % (table, days, time.strftime("%Y%m%d_%H%M%S")))
    cols = [r["column_name"] for r in c.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='mt' AND table_name=%s ORDER BY ordinal_position", (table,))]
    n = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        with c.cursor() as cur:
            cur.execute("SELECT %s FROM mt.%s WHERE %s < now() - make_interval(days => %%s)"
                        % (", ".join('"%s"' % x for x in cols), table, col), (days,))
            for row in cur:
                w.writerow([row[x] for x in cols])
                n += 1
    return path, n


def cmd_apply(a):
    if not a.table:
        print("[X] apply 必须用 --table 指定一张表（不支持「全部一起删」）")
        return 2
    if not a.max_rows:
        print("[X] apply 必须用 --max-rows 明确给出本次允许删除的上限。")
        print("    为什么强制：保留期是制度，执行是操作 —— 一次删掉半年的审计")
        print("    是不可逆的，必须由人给出具体数字。")
        return 2
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        pol = c.execute("SELECT * FROM mt.retention_policy WHERE entity_name=%s",
                        (a.table,)).fetchone()
        if not pol:
            print("[X] %s 没有登记保留策略 —— 本工具不发明保留期" % a.table)
            return 2
        ok, why = eligible(c, pol)
        if not ok:
            print("[X] 不能处理 %s：%s" % (a.table, why))
            return 2
        col, days = TIME_COL[a.table], pol["retain_days"]
        n = count_due(c, a.table, col, days)
        print("策略 %s：%s 保留 %d 天 → 到期 %s 行" % (pol["policy_id"], a.table, days, f"{n:,}"))
        if n == 0:
            print("[=] 没有到期的行，无需处理")
            return 0
        if n > a.max_rows:
            print("[X] 到期 %s 行 > 你给的上限 %s —— 拒绝执行。" % (f"{n:,}", f"{a.max_rows:,}"))
            print("    这正是不设上限的危险：一条命令删掉远超预期的审计。")
            return 3
        saved = None
        if a.archive:
            saved, got = archive(c, a.table, col, days, ARCHIVE_DIR)
            print("[✓] 已归档 %s 行 → %s" % (f"{got:,}", saved))
            if got != n:
                print("[X] 归档行数（%s）与预计（%s）不一致 —— 拒绝删除" % (got, n))
                return 3
        else:
            print("[!] 未指定 --archive：将**直接删除**，没有副本。")
            if not a.yes:
                print("    确认请再加 --yes")
                return 2
        with c.cursor() as cur:
            cur.execute("DELETE FROM mt.%s WHERE %s < now() - make_interval(days => %%s)"
                        % (a.table, col), (days,))
            deleted = cur.rowcount
        c.execute("SELECT mt.log_access(%s,%s,%s,%s,%s,%s)",
                  ("cli:retention.py", "retention", a.table, deleted, None,
                   psycopg.types.json.Jsonb({"keep_days": days, "action": "delete",
                                             "archive": saved})))
        c.commit()
        print("[✓] 已删除 %s 行（保留期 %d 天）；本次操作已写入 access_log"
              % (f"{deleted:,}", days))
    return 0


def main():
    ap = argparse.ArgumentParser(description="保留策略执行器（默认只 plan，不删）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--table")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("apply")
    p.add_argument("--table", required=True)
    p.add_argument("--max-rows", type=int, default=0,
                   help="本次允许删除的行数上限（必填；超过则拒绝）")
    p.add_argument("--archive", action="store_true", help="先导出到 .tools/archive/ 再删")
    p.add_argument("--yes", action="store_true", help="不归档时确认删除")
    p.set_defaults(fn=cmd_apply)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
