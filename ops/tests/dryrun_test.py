# -*- coding: utf-8 -*-
"""
ops/tests/dryrun_test.py —— 变更试算的**隔离性**（评审要的 dry-run）

dry-run 的核心承诺只有一条：**试算过程一个字节都不写生产库**。
这一条必须被实证，否则"试算"就变成了"偷偷改数据"。

设计上为什么用**独立实例**而不是"事务回滚"：
  · 长事务会持锁，别人写不进去；
  · 非事务性副作用（序列、dblink、外部调用）回滚不掉；
  · 而重跑真实管道（`build_competency.py`）本来就是多语句、多次提交的，
    塞进一个事务不现实。
所以：initdb 全新实例 → dump 生产 → 恢复 → 在隔离实例上改与重算 → 拆掉。

本测试真跑一次 `ops/dryrun.py`（用**无副作用的变更**：把 0 样本节点并入另一节点），
然后断言生产的四个方面**逐项没变**。
"""
from __future__ import annotations

import os
import subprocess
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


def snap(c):
    return {
        "jobs": c.execute("SELECT count(*) AS n FROM mt.job_posting").fetchone()["n"],
        "weights": c.execute("SELECT count(*) AS n FROM mt.job_competency_weight "
                             "WHERE valid_to IS NULL").fetchone()["n"],
        "changes": c.execute("SELECT count(*) AS n FROM mt.occupation_change").fetchone()["n"],
        "migs": c.execute("SELECT count(*) AS n FROM mt.occupation_migration").fetchone()["n"],
        "retired": c.execute("SELECT count(*) AS n FROM mt.occupation "
                             "WHERE status <> 'active'").fetchone()["n"],
        "per_occ": {r["occupation_id"]: r["n"] for r in c.execute(
            "SELECT occupation_id, count(*) AS n FROM mt.job_posting "
            "GROUP BY 1 ORDER BY 1")},
    }


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        before = snap(c)
        # 挑两个**都是 0 样本**的 level-3 节点做无副作用变更
        rows = c.execute("""SELECT o.occupation_id FROM mt.occupation o
                             WHERE o.level=3
                               AND NOT EXISTS (SELECT 1 FROM mt.job_posting j
                                                WHERE j.occupation_id=o.occupation_id)
                             ORDER BY o.occupation_id LIMIT 2""").fetchall()
    if len(rows) < 2:
        check(False, "找不到两个 0 样本节点做无副作用试算")
        return H.report(width=74, list_fails=True)
    frm, to = rows[0]["occupation_id"], rows[1]["occupation_id"]

    p = subprocess.run([sys.executable, os.path.join(BASE, "ops", "dryrun.py"),
                        "--merge", frm, to], cwd=BASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (p.stdout or "") + (p.stderr or "")
    check(p.returncode == 0,
          "dryrun 跑通（退出码 %d，试算 %s → %s）" % (p.returncode, frm, to))
    check("生产库未被改动" in out,
          "试算自己声明了「生产库未被改动」")
    check("build_competency.py" in out and "退出码 0" in out,
          "**在隔离实例上真跑了真实管道**（build_competency.py 退出码 0），"
          "不是「我估一下」")

    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        after = snap(c)
    check(before["jobs"] == after["jobs"],
          "生产库岗位数未变（%d → %d）" % (before["jobs"], after["jobs"]))
    check(before["weights"] == after["weights"],
          "生产能力权重行数未变（%d → %d）—— 重算发生在隔离实例上"
          % (before["weights"], after["weights"]))
    check(before["changes"] == after["changes"],
          "生产库 occupation_change 未变（%d → %d）" % (before["changes"], after["changes"]))
    check(before["migs"] == after["migs"],
          "生产库 occupation_migration 未变（%d → %d）" % (before["migs"], after["migs"]))
    check(before["retired"] == after["retired"],
          "生产库没有节点被停用（%d → %d）" % (before["retired"], after["retired"]))
    diff = [k for k in set(before["per_occ"]) | set(after["per_occ"])
            if before["per_occ"].get(k, 0) != after["per_occ"].get(k, 0)]
    check(not diff,
          "**逐职业的岗位归属一个都没变**（差异：%s）—— 这是「一个字节都不写」的最强证据"
          % ("、".join(diff[:4]) or "无"))
    # 清理
    tmp = os.path.join(os.path.dirname(BASE), ".tools", "tmp", "pg_dryrun")
    check(not os.path.isdir(tmp), "隔离实例的数据目录已删除（%s）" % tmp)

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
