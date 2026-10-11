# -*- coding: utf-8 -*-
"""
ops/tests/data_nature_test.py —— 数据性质隔离与覆盖率分母的回归断言

为什么单独立一套（评审第 1 条的验收标准）
--------------------------------------------------------------------------
验收标准是："**可以明确指出哪些结论由真实数据支撑**"。
这件事要成立，必须机器可检地守住四件事：

  ① **数据性质的判据是"登记的事实"，不是名称匹配**
     · 人才侧：`person.quality_flags` 含 `synthetic_fixture`
     · 岗位侧：`source_registry.is_synthetic`（经 source_id 关联）
     两者都不能靠 name 里有没有 "fixture" 去猜（按命名判断数据性质一定会误伤）。

  ② **覆盖率的分母必须按层级算**
     职业树里 level 1 是大类、level 2 是方向、level 3 是具体职业。
     把 151 个节点混在一个分母里会**系统性低估**（实测 15.2% vs 正确的 23.5%）。
     这条要钉死：分子是**去重职业数**，分母是**同层级节点数**，比例必须 ≤ 100%。

  ③ **"谁都读不到"的对象必须显式登记**
     本库的授权由对账统一生成，新建视图忘了对账 = 没授权（这个坑踩过三次：
     048 / 052 / 061）。而"刻意锁"与"忘了授权"在库里长得一模一样，
     所以判据是"谁都读不到 **且** 没在 `locked_object_registry` 里登记"。

  ④ **新视图必须有授权**（T3 能读、不该公开的匿名读不到）
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "ops"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")


def q1(sql, p=None, conn=None):
    if conn is not None:
        return conn.execute(sql, p).fetchone()
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        return c.execute(sql, p).fetchone()


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        # ---------- ① 判据是登记的事实 ----------
        col = q1("""SELECT count(*) AS n FROM information_schema.columns
                     WHERE table_schema='mt' AND table_name='source_registry'
                       AND column_name='is_synthetic'""", conn=c)["n"]
        check(col == 1, "source_registry 有**显式**的 is_synthetic 列（不是靠名称猜）")

        person_col = q1("""SELECT count(*) AS n FROM information_schema.columns
                            WHERE table_schema='mt' AND table_name='person'
                              AND column_name='quality_flags'""", conn=c)["n"]
        check(person_col == 1, "人才侧判据仍是 person.quality_flags（两套判据各自明确）")

        # 两侧的合成判定都要能给出数
        p = q1("""SELECT count(*) FILTER (WHERE 'synthetic_fixture' = ANY(quality_flags)) AS syn,
                         count(*) AS all_n FROM mt.person""", conn=c)
        check(p["all_n"] > 0 and p["syn"] <= p["all_n"],
              "人才侧能算出合成数（%d/%d）" % (p["syn"], p["all_n"]))
        j = q1("""SELECT count(*) FILTER (WHERE is_synthetic) AS syn, count(*) AS all_n
                    FROM mt.v_job_nature""", conn=c)
        check(j["all_n"] > 0 and j["syn"] <= j["all_n"],
              "岗位侧能算出合成数（%d/%d）" % (j["syn"], j["all_n"]))

        # ---------- ② 覆盖率的分母必须按层级 ----------
        lv = {r["level"]: r for r in c.execute(
            "SELECT level, count(*) AS n FROM mt.occupation WHERE status='active' "
            "GROUP BY level").fetchall()}
        check(3 in lv and lv[3]["n"] > 0, "有 level 3（具体职业）节点")
        check(sum(x["n"] for x in lv.values()) > lv[3]["n"],
              "全部节点数(%d) > level 3 节点数(%d) —— 层级混用会算错分母"
              % (sum(x["n"] for x in lv.values()), lv[3]["n"]))

        occ_cov = q1("SELECT count(DISTINCT occupation_id) AS n FROM mt.job_posting "
                     "WHERE occupation_id IS NOT NULL", conn=c)["n"]
        all_occ = sum(x["n"] for x in lv.values())
        check(occ_cov < all_occ,
              "分母用全部节点(%d)时比例会更小 —— 这就是「分母口径错误」的量化"
              % all_occ)

        for mid, lvl, label in (("M_DEMAND_OCC_COVERAGE_L3", 3, "level 3"),
                                ("M_DEMAND_OCC_COVERAGE_L2", 2, "level 2")):
            r = q1("SELECT * FROM mt.metric_value(%s)", (mid,), conn=c)
            share = (occ_cov / lv[lvl]["n"] * 100.0) if mid.endswith("L3") else None
            check(r["denominator"] == lv[lvl]["n"],
                  "%s 的分母就是 %s 的节点数（%d）" % (mid, label, lv[lvl]["n"]))
            check(0 <= (r["value"] or 0) <= 100,
                  "%s 的比例在 0–100%% 之间（实得 %s）—— 超过 100%% 说明分子写错了"
                  % (mid, r["value"]))
        # 分子必须是**去重职业数**：要是误写成岗位数，比例会超过 100%
        jobs = q1("SELECT count(*) AS n FROM mt.job_posting", conn=c)["n"]
        check(jobs > lv[3]["n"],
              "岗位数(%d) 明显大于 level 3 节点数(%d) —— 所以把岗位数当分子必然爆表"
              % (jobs, lv[3]["n"]))

        # ---------- ③ "谁都读不到"的对象必须显式登记 ----------
        nolock = c.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
             WHERE ns.nspname = 'mt' AND c.relkind IN ('r','v')
               AND c.relname NOT IN (SELECT object_name FROM mt.locked_object_registry)
               AND NOT (has_table_privilege('mt_t0', c.oid, 'SELECT')
                     OR has_table_privilege('mt_t1', c.oid, 'SELECT')
                     OR has_table_privilege('mt_t2', c.oid, 'SELECT')
                     OR has_table_privilege('mt_t3', c.oid, 'SELECT')
                     OR has_any_column_privilege('mt_t0', c.oid, 'SELECT')
                     OR has_any_column_privilege('mt_t1', c.oid, 'SELECT')
                     OR has_any_column_privilege('mt_t2', c.oid, 'SELECT')
                     OR has_any_column_privilege('mt_t3', c.oid, 'SELECT'))
             ORDER BY 1""").fetchall()
        check(not nolock,
              "没有「谁都读不到、又没登记为刻意锁」的对象（%s）"
              % ("、".join(r["relname"] for r in nolock) or "无"))
        locked = q1("SELECT count(*) AS n FROM mt.locked_object_registry", conn=c)["n"]
        check(locked >= 1, "刻意锁的对象都有登记与理由（%d 个）" % locked)

        # ---------- ④ 视图授权：分析视图是 T3 专属，这是**机制决定的** ----------
        # 未登记进 public_view_registry 的视图，对账只授 T3
        # （视图以属主权限执行，绕过列级策略，所以默认收紧）。
        # **不要试图给 T1 单独 GRANT** —— 下次对账会撤销它（不可持久，048 踩过）。
        # 所以通用展示（首页数据性质卡）必须直接查底表，不能依赖这类视图。
        for v in ("v_job_nature",):
            t3 = q1("SELECT has_table_privilege('mt_t3','mt.%s','SELECT') AS x" % v,
                    conn=c)["x"]
            t0 = q1("SELECT has_table_privilege('mt_t0','mt.%s','SELECT') AS x" % v,
                    conn=c)["x"]
            check(t3, "%s 对 T3 可读（忘了对账会连 T3 都读不到）" % v)
            check(not t0, "%s 对匿名不可读（它不是公开定义层）" % v)
        # 首页数据性质卡依赖的底表必须是 T1 可读，否则 T1 用户会看到"需要登录"
        c.execute("SET ROLE mt_t1")
        try:
            n = q1("""SELECT count(*) AS n FROM mt.job_posting j
                        JOIN mt.source_registry s ON s.source_id = j.source_id""",
                   conn=c)["n"]
            check(n >= 0, "T1 能直接查数据性质卡依赖的底表（%d 个岗位）" % n)
            try:
                q1("SELECT count(*) FROM mt.v_job_nature", conn=c)
                check(False, "T1 竟然能读 v_job_nature —— 与「未登记视图只给 T3」不一致")
            except psycopg.Error:
                check(True, "T1 读分析视图 v_job_nature 被如实拒绝（机制一致：它只给 T3）")
        finally:
            c.execute("RESET ROLE")

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
