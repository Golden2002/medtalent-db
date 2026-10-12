# -*- coding: utf-8 -*-
"""
ops/dryrun.py —— 职业树变更**试算**（dry-run）：改之前先看后果

用户要求："dry-run 做一下，按最佳实践。"

【最佳实践是什么：完全隔离 + 重跑真实管道 + 变更前后对比】
  ① **完全隔离**：`initdb` 一个**全新实例**（另一端口、另一数据目录），
     把生产库 dump 过去。试算过程**一个字节都不写生产库** ——
     不靠"事务回滚"（长事务会持锁，且非事务性副作用会漏）。
  ② **重跑真实管道**：变更后用 `MT_DSN` 指向隔离实例，
     真跑 `code/analytics/build_competency.py`，
     **而不是"我估一下应该会怎样"**。
  ③ **变更前后对比**：同一个实例上先取快照、再改、再取快照，逐项 diff。
  ④ **拆干净**：停实例、删数据目录。

【它回答什么、不回答什么】
  回答：这次变更**会不会把某些职业的岗位样本搞没**、**哪些职业的能力权重变了**。
  不回答：完整匹配名单的进出（那要把匹配管道也接进来，见文件末尾"后续可扩展"）。
  ——**我不把没做的事说成做了。**

用法：
  python ops/dryrun.py --merge OCC-F15-03-02 OCC-F15-03-01
  python ops/dryrun.py --retire OCC-F01-01-06 --to OCC-F01-01-05
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.dirname(BASE)
PGBIN = os.path.join(WORKSPACE, ".tools", "pgsql", "bin")
TMPDIR = os.path.join(WORKSPACE, ".tools", "tmp", "pg_dryrun")
PORT = "55434"
PROD = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
        "client_encoding=UTF8 options='-c search_path=mt,public'")
DRY = ("host=127.0.0.1 port=%s dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'" % PORT)

import psycopg                                          # noqa: E402
from psycopg.rows import dict_row                       # noqa: E402


def tool(n):
    return os.path.join(PGBIN, n + ".exe")


def run_detached(args, **kw):
    """丢弃输出的运行 —— **必须用于 pg_ctl**：它启动的 postgres 会继承管道，
    捕获输出会让父进程永远等不到 EOF（我上次就死在这里，见 cross_restore_test）。"""
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", **kw)


def snapshot(c, label):
    cov = c.execute("""
        SELECT o.occupation_id, o.label_zh, count(j.job_id) AS n,
               (SELECT count(*) FROM job_competency_weight w
                 WHERE w.occupation_id = o.occupation_id AND w.valid_to IS NULL) AS n_w
          FROM occupation o LEFT JOIN job_posting j ON j.occupation_id = o.occupation_id
         WHERE o.level = 3 GROUP BY 1,2 ORDER BY 1""").fetchall()
    weights = c.execute("""
        SELECT w.occupation_id, count(*) AS n,
               md5(string_agg(w.concept_id || ':' || coalesce(w.importance::text,''), ','
                              ORDER BY w.concept_id)) AS sig
          FROM job_competency_weight w WHERE w.valid_to IS NULL
         GROUP BY 1 ORDER BY 1""").fetchall()
    total_jobs = c.execute("SELECT count(*) AS n FROM job_posting").fetchone()["n"]
    return {"cov": {r["occupation_id"]: r for r in cov},
            "w": {r["occupation_id"]: r for r in weights},
            "total_jobs": total_jobs, "label": label}


def diff(before, after):
    print("=" * 92)
    print("试算结果：变更前 vs 变更后")
    print("=" * 92)
    print("岗位总数：%d → %d（%+d）"
          % (before["total_jobs"], after["total_jobs"],
             after["total_jobs"] - before["total_jobs"]))

    print("\n① **覆盖率变化**（哪些职业从「有样本」变成「没样本」，或反过来）")
    lost, gained, changed = [], [], []
    for oid in sorted(set(before["cov"]) | set(after["cov"])):
        b = before["cov"].get(oid, {}).get("n", 0)
        a = after["cov"].get(oid, {}).get("n", 0)
        name = (before["cov"].get(oid) or after["cov"].get(oid))["label_zh"]
        if b > 0 and a == 0:
            lost.append((oid, name, b))
        elif b == 0 and a > 0:
            gained.append((oid, name, a))
        elif b != a:
            changed.append((oid, name, b, a))
    print("   **变成 0 样本（危险）**：%d 个" % len(lost))
    for oid, nm, b in lost:
        print("      ✗ %s %s：%d → 0" % (oid, nm, b))
    print("   从 0 变有样本：%d 个" % len(gained))
    for oid, nm, a in gained:
        print("      ✓ %s %s：0 → %d" % (oid, nm, a))
    print("   样本数变化（非 0）：%d 个" % len(changed))
    for oid, nm, b, a in changed[:8]:
        print("      · %s %s：%d → %d" % (oid, nm, b, a))

    print("\n② **能力权重变化**（受影响职业的权重向量签名是否变了）")
    wlost, wchg = [], []
    for oid in sorted(set(before["w"]) | set(after["w"])):
        bw, aw = before["w"].get(oid), after["w"].get(oid)
        if bw and not aw:
            wlost.append((oid, bw["n"]))
        elif bw and aw and bw["sig"] != aw["sig"]:
            wchg.append((oid, bw["n"], aw["n"]))
        elif not bw and aw:
            wchg.append((oid, 0, aw["n"]))
    print("   权重**整组消失**：%d 个 %s"
          % (len(wlost), "、".join("%s(%d条)" % x for x in wlost[:5]) or ""))
    print("   权重向量**发生变化**：%d 个" % len(wchg))
    for oid, b, a in wchg[:10]:
        print("      · %s：%d 条 → %d 条" % (oid, b, a))

    print("\n" + "-" * 92)
    if lost:
        print("⚠ **结论：这次变更会让 %d 个职业失去全部岗位样本** —— "
              "它们之后再也不可能被推荐出来。改之前必须确认这是有意的。" % len(lost))
    else:
        print("结论：没有职业因为这次变更变成 0 样本。")
    print("⚠ 未覆盖：完整匹配名单的进出（需把匹配管道也接进试算，见文件末尾说明）。")


def main():
    ap = argparse.ArgumentParser(description="职业树变更试算（隔离实例）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--merge", nargs=2, metavar=("FROM", "TO"), help="把 FROM 并入 TO")
    g.add_argument("--retire", metavar="FROM", help="停用 FROM（需 --to 给重映射目标）")
    ap.add_argument("--to", help="--retire 时的重映射目标")
    ap.add_argument("--keep", action="store_true", help="保留临时实例（排查用）")
    a = ap.parse_args()
    if a.retire and not a.to:
        print("[X] --retire 必须给 --to（本库 occupation_migration.new_id 是 NOT NULL："
              "停用也必须给出下游去哪）")
        return 2
    frm = a.merge[0] if a.merge else a.retire
    to = a.merge[1] if a.merge else a.to

    started = False
    try:
        # ---------- 1. 全新实例 ----------
        if os.path.isdir(TMPDIR):
            shutil.rmtree(TMPDIR, ignore_errors=True)
        os.makedirs(TMPDIR, exist_ok=True)
        r = run_detached([tool("initdb"), "-D", TMPDIR, "-U", "postgres",
                          "--auth=trust", "--encoding=UTF8", "--locale=C"])
        if r.returncode != 0:
            print("[X] initdb 失败"); return 1
        r = run_detached([tool("pg_ctl"), "-D", TMPDIR, "-o",
                          "-p %s -c listen_addresses=127.0.0.1" % PORT,
                          "-l", os.path.join(TMPDIR, "startup.log"), "-w", "-t", "60", "start"])
        started = (r.returncode == 0)
        if not started:
            print("[X] 临时实例启动失败"); return 1
        print("[1/6] 隔离实例已就绪（端口 %s）" % PORT)

        # ---------- 2. dump 生产 → 恢复 ----------
        run_detached([tool("createdb"), "-h", "127.0.0.1", "-p", PORT,
                      "-U", "postgres", "medtalent"])
        dump = os.path.join(TMPDIR, "prod.dump")
        # 排除审计流水数据：试算与它无关，而它是库里最大的一张表（210MB）→ 大幅提速
        r = run_detached([tool("pg_dump"), "-h", "127.0.0.1", "-p", "55432", "-U", "postgres",
                          "-d", "medtalent", "-Fc", "--exclude-table-data=mt.change_log",
                          "-f", dump])
        if r.returncode != 0:
            print("[X] pg_dump 失败"); return 1
        r = subprocess.run([tool("pg_restore"), "-h", "127.0.0.1", "-p", PORT,
                            "-U", "postgres", "-d", "medtalent",
                            "--no-owner", "--no-privileges", dump],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        errs = [l for l in (r.stderr or "").splitlines() if "error" in l.lower()]
        print("[2/6] 生产快照已恢复到隔离实例（审计流水数据已排除以提速）；error %d 条" % len(errs))
        if errs:
            print("      首条：", errs[0][:110])

        with psycopg.connect(DRY, row_factory=dict_row, autocommit=True) as c:
            # ---------- 3. 变更前快照 ----------
            before = snapshot(c, "before")
            n_from = before["cov"].get(frm, {}).get("n", 0)
            n_to = before["cov"].get(to, {}).get("n", 0)
            print("[3/6] 变更前：%s 有 %s 份岗位样本；%s 有 %s 份"
                  % (frm, n_from, to, n_to))

            # ---------- 4. 应用变更 ----------
            c.execute("""INSERT INTO occupation_change
                  (change_id, change_type, from_ids, to_ids, reason, status,
                   proposed_by, effective_from)
                  VALUES ('dryrun_change', %s, ARRAY[%s]::text[], ARRAY[%s]::text[],
                          'dry-run 试算（隔离实例）', 'applied', 'dryrun', CURRENT_DATE)""",
                      ("merge" if a.merge else "retire", frm, to))
            c.execute("""INSERT INTO occupation_migration
                  (migration_id, change_id, old_id, new_id, relation, coverage,
                   effective_from, note)
                  VALUES ('dryrun_mig', 'dryrun_change', %s, %s, %s, 1.0,
                          CURRENT_DATE, 'dry-run 试算')""",
                      (frm, to, "merged_into" if a.merge else "retired"))
            moved = c.execute("UPDATE job_posting SET occupation_id = %s "
                              "WHERE occupation_id = %s", (to, frm)).rowcount
            c.execute("UPDATE occupation SET status = 'retired' WHERE occupation_id = %s", (frm,))
            print("[4/6] 已在隔离实例上应用变更：%d 份岗位从 %s 移到 %s" % (moved, frm, to))

            # ---------- 5. 重跑真实管道 ----------
            env = dict(os.environ, MT_DSN=DRY, PYTHONIOENCODING="utf-8")
            p = subprocess.run([sys.executable, os.path.join(BASE, "code", "analytics",
                                                             "build_competency.py")],
                               cwd=BASE, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            print("[5/6] 能力权重重算完成（真实管道 build_competency.py，退出码 %d）"
                  % p.returncode)
            if p.returncode != 0:
                print((p.stderr or "")[-500:])

            # ---------- 6. 变更后快照 ----------
            after = snapshot(c, "after")
            diff(before, after)
            print("\n[6/6] 试算完成。**生产库未被改动**（全程只读它做 dump）。")
    finally:
        if started and not a.keep:
            run_detached([tool("pg_ctl"), "-D", TMPDIR, "-m", "immediate",
                          "-w", "-t", "30", "stop"])
        if not a.keep:
            shutil.rmtree(TMPDIR, ignore_errors=True)
        if a.keep:
            print("[!] 保留了隔离实例：%s（端口 %s），排查完请手工停掉并删除" % (TMPDIR, PORT))
        else:
            print("隔离实例已停止、数据目录已删除。")

    print("""
后续可扩展（**现在没做，明说**）：
  · 把匹配管道（code/analytics/vector.py / ops/fixtures/real_cases.py）也接进试算，
    输出"哪些人的推荐名单进了/出了"；
  · 试算多个候选变更并对比（现在一次一个）。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
