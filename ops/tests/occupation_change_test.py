# -*- coding: utf-8 -*-
"""
ops/tests/occupation_change_test.py —— 职业树：影响预览 / 发布管理 / 下游一致性

评审把职业树演化的整改范围收窄为三条：**变更影响预览、发布管理、下游一致性**。

核对运行状态后发现：**结构都在、计算全无**——
`occupation_change`（含 retire/remap/reverted）、`occupation_migration`、`dataset_release`
三张表都已建好，但**都是 0 行**，而且没有任何"改这个节点会碰到谁"的计算。

所以这一套要守住的重点**不是"视图存在"，而是"机制真的能发现问题"**：
  · 一个"永远返回 0 的检查"和一个"真的能发现问题的检查"，在空库上长得一模一样；
  · `M_EVO_DOWNSTREAM_GAPS` 当前正是 0，而库里还没有任何变更记录 ——
    **所以单看 0 什么也证明不了**。
因此本测试**真的走一遍**：故意造出不一致 → 确认被检出 → 修好 → 确认消失 → 回滚 → 清理。
（由 `ops/evolution.py selfcheck` 执行，本套件断言它每一步都符合期望，并在数据库层面复核清理。）

⚠ 命名说明：本文件**不是** `evolution_test.py`。后者是**已有的**演化层端到端测试
（第 12 步，测"市场变化 → 发现候选 → 晋升"的生长能力）。两者都在演化域，但测的东西不同：
  · `evolution_test.py`      —— 树**怎么长**（候选发现与晋升）
  · `occupation_change_test.py` —— 树**改了之后下游对不对**（影响预览 / 发布 / 一致性）
我一开始把新文件写成 `evolution_test.py`，被写保护挡下（否则会静默覆盖已有测试）——
这也说明"同名即覆盖"是真实风险，写文件前必须先确认路径。
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


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        def q1(sql, p=None):
            return c.execute(sql, p).fetchone()

        # ---------- 三条要求对应的视图都在且可读 ----------
        for v in ("v_occupation_impact", "v_occupation_release_status",
                  "v_occupation_downstream_gap"):
            n = q1("SELECT count(*) AS n FROM information_schema.views "
                   "WHERE table_schema='mt' AND table_name=%s", (v,))["n"]
            check(n == 1, "%s 存在" % v)
            t3 = q1("""SELECT has_table_privilege('mt_t3','mt.%s','SELECT')
                          OR has_any_column_privilege('mt_t3','mt.%s','SELECT') AS x"""
                    % (v, v))["x"]
            check(t3, "%s 对 T3 可读（忘了对账会连 T3 都读不到）" % v)

        # ---------- ① 影响预览：必须真的算出依赖，不是空壳 ----------
        n_nodes = q1("SELECT count(*) AS n FROM mt.occupation")["n"]
        n_rows = q1("SELECT count(*) AS n FROM mt.v_occupation_impact")["n"]
        check(n_rows == n_nodes,
              "影响视图覆盖全部职业节点（%d == %d）" % (n_rows, n_nodes))
        agg = q1("""SELECT count(*) FILTER (WHERE dependents_total > 0) AS with_dep,
                           count(*) FILTER (WHERE impact_risk LIKE '高%') AS high,
                           count(*) FILTER (WHERE dependents_total = 0) AS orphan,
                           max(dependents_total) AS mx
                      FROM mt.v_occupation_impact""")
        check(agg["with_dep"] > 0 and agg["mx"] > 1,
              "**影响预览真的算出依赖**（%d 个节点有依赖，最多 %d 个）—— 不是全 0 的空壳"
              % (agg["with_dep"], agg["mx"]))
        check(agg["high"] > 0, "有高影响节点（%d 个：有岗位样本/子节点/有效能力权重）"
              % agg["high"])
        check(agg["orphan"] > 0, "能识别零依赖节点（%d 个，没有任何下游引用）"
              % agg["orphan"])
        n_bands = q1("SELECT count(DISTINCT left(impact_risk,1)) AS n "
                     "FROM mt.v_occupation_impact")["n"]
        check(n_bands >= 2, "风险分级有区分度（%d 档）" % n_bands)

        # ---------- ② 发布管理：变更与发布的关联结构 ----------
        n_ri = q1("""SELECT count(*) AS n FROM information_schema.tables
                      WHERE table_schema='mt' AND table_name='occupation_release_item'""")["n"]
        check(n_ri == 1, "occupation_release_item 存在（变更 → 发布的归属）")
        dom = q1("SELECT domain FROM mt.domain_registry "
                 "WHERE table_name='occupation_release_item'")
        check(dom is not None, "它登记了业务域（建表时就该一起做）")
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mt' AND table_name='v_occupation_release_status'")}
        check("published_with_pending" in cols,
              "发布状态视图含 published_with_pending（已发布却还挂着未决变更）")

        # ---------- ③ 下游一致性与口径 ----------
        g = q1("SELECT * FROM mt.metric_value('M_EVO_DOWNSTREAM_GAPS')")
        check(g["value"] == 0, "当前下游一致性缺口为 0（实得 %s）" % g["value"])
        rel = q1("SELECT * FROM mt.metric_value('M_EVO_RELEASED_CHANGE_RATE')")
        n_ch = q1("SELECT count(*) AS n FROM mt.occupation_change")["n"]
        if n_ch == 0:
            check(rel["value"] is None,
                  "**没有任何变更时，发布覆盖率返回 NULL 而不是 0**（实得 %s）"
                  % rel["value"])
        check(q1("SELECT * FROM mt.metric_value('M_EVO_DATASET_RELEASES')")["value"]
              is not None, "数据集发布登记数能算")

        # ---------- 关键：机制**真的能发现问题**（不是永远返回 0 的空壳）----------
        proc = subprocess.run([sys.executable, os.path.join(BASE, "ops", "evolution.py"),
                               "selfcheck"], cwd=BASE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        check(proc.returncode == 0,
              "selfcheck 通过（退出码 %d）—— 走完了 提议→检出→修复→发布→回滚→清理"
              % proc.returncode)
        check("停用但无迁移映射 → 被检出" in out,
              "**故意造出「停用但无迁移映射」，机制真的检出了** —— "
              "这说明 0 缺口是「检查过且一致」，不是「检查是空壳」")
        check("已生效但未纳入发布 → 被检出" in out,
              "**故意造出「已生效未纳入发布」，机制真的检出了**")
        check("迁移指向不存在的节点 → 被检出" in out,
              "**故意造出「迁移指向不存在的节点」，机制真的检出了**")
        check("回滚后该变更不再产生缺口" in out,
              "回滚路径也走通了（reverted 后不再产生缺口）")
        check("清理：变更 0 · 归属 0 · 发布 0 · 迁移 0" in out,
              "自检**清理干净**（回到原始空状态）—— 否则会污染真实数据")
        # 清理必须真的生效（不能只靠输出文字）
        left = q1("""SELECT (SELECT count(*) FROM mt.occupation_change) AS ch,
                            (SELECT count(*) FROM mt.occupation_release_item) AS ri,
                            (SELECT count(*) FROM mt.dataset_release) AS dr,
                            (SELECT count(*) FROM mt.occupation_migration) AS mg""")
        check(left["ch"] == 0 and left["ri"] == 0 and left["dr"] == 0 and left["mg"] == 0,
              "断言数据库层面确实清空了（变更 %d/归属 %d/发布 %d/迁移 %d）"
              % (left["ch"], left["ri"], left["dr"], left["mg"]))
        check(q1("SELECT count(*) AS n FROM mt.v_occupation_downstream_gap")["n"] == 0,
              "自检结束后下游一致性仍为 0（没留残渣）")

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
