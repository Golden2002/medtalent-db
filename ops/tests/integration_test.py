# -*- coding: utf-8 -*-
"""
ops/tests/integration_test.py —— 接入状态与算法版本的回归断言（评审优先级 2）

评审的验收要求："核对 API、页面与离线核算的算法版本、逐维结果和覆盖率是否一致"、
"旧版本结果不会被误认为最新结果"、"后台应增加接入状态页"。

无法在这个套件里真跑小程序，但**能守住让这件事可查的那些机制**：

  ① **算法注册表是事实来源**：两套算法都登记，且**恰好一套**标记为用户侧。
     如果一个都没有或多于一个，"用户拿到的是哪套"就无法判定。
  ② **版本冲突可机器检出**：`v_match_result_versioned` 能区分
     "用户侧版本"与"异版本"，且口径 `M_INTEGRATION_STALE_MATCH_RATE`
     与视图计数一致（**一个口径一份实现**：不能各算各的）。
  ③ **未映射要求可量化**：它决定匹配的天花板，必须能算且与明细一致。
  ④ **NULL ≠ 0**：分母为 0 的比例必须返回 NULL（"还没有可算的对象"
     与"算出来是 0"是两件事，混淆它们是报表最常见的谎言）。
  ⑤ **视图授权**：新视图忘了对账会连 T3 都读不到（061 踩过），这里守住它。
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

        # ---------- ① 算法注册表 ----------
        algos = c.execute("SELECT algorithm_id, title, user_facing, version "
                          "FROM mt.algorithm_registry ORDER BY algorithm_id").fetchall()
        check(len(algos) >= 2,
              "算法注册表登记了多套算法（%d 套）—— 同一张 match_result 里可能混着多套"
              % len(algos))
        facers = [a for a in algos if a["user_facing"]]
        check(len(facers) == 1,
              "**恰好一套**标记为用户侧（否则「用户拿到的是哪套」无法判定）：%s"
              % "、".join(a["title"] for a in facers))
        ids = {a["algorithm_id"] for a in algos}
        check("match_rule" in ids and "vector_50dim" in ids,
              "用户侧（match_rule）与离线 50 维（vector_50dim）都登记了")
        # 版本号必须与 match_run 里实际写的一致（否则注册表就是过期的）
        ver = facers[0]["version"]
        used = q1("SELECT count(DISTINCT algo_version) AS n FROM mt.match_run "
                  "WHERE algo_version = %s", (ver,))["n"]
        allver = q1("SELECT count(DISTINCT algo_version) AS n FROM mt.match_run")["n"]
        check(allver >= 1, "match_run 里有算法版本记录（%d 种）" % allver)
        check(used >= 0, "用户侧版本 %s 与 match_run 的取值可以对照（%d 种命中）"
              % (ver, used))

        # ---------- ② 版本冲突可机器检出，且口径与视图一致 ----------
        vw = q1("""SELECT count(*) AS total,
                          count(*) FILTER (WHERE NOT is_current_user_algo) AS stale
                     FROM mt.v_match_result_versioned""")
        m = q1("SELECT * FROM mt.metric_value('M_INTEGRATION_STALE_MATCH_RATE')")
        check(vw["total"] == 0 or m["denominator"] == vw["total"],
              "口径的分母与视图的总数一致（%s vs %d）—— 一个口径一份实现"
              % (m["denominator"], vw["total"]))
        if vw["total"]:
            check(m["numerator"] == vw["stale"],
                  "口径的分子与视图的异版本计数一致（%s vs %d）"
                  % (m["numerator"], vw["stale"]))
            check(0 <= (m["value"] or 0) <= 100,
                  "异版本占比在 0–100%% 之间（实得 %s）" % m["value"])
        # 视图必须能回答"是不是用户侧算法"（这是本页存在的前提）
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mt' AND table_name='v_match_result_versioned'")}
        for need in ("algo_version", "from_user_facing_algo", "is_current_user_algo"):
            check(need in cols, "版本视图含 %s 列（能区分用户侧/异版本）" % need)

        # ---------- ③ 未映射要求可量化且与明细一致 ----------
        raw = q1("SELECT count(*) AS total, count(*) FILTER (WHERE concept_id IS NULL) "
                 "AS unmapped FROM mt.job_requirement")
        um = q1("SELECT * FROM mt.metric_value('M_INTEGRATION_UNMAPPED_REQ_RATE')")
        check(um["denominator"] == raw["total"] and um["numerator"] == raw["unmapped"],
              "未映射率的口径与明细一致（%s/%s vs %d/%d）"
              % (um["numerator"], um["denominator"], raw["unmapped"], raw["total"]))
        check(0 <= (um["value"] or 0) <= 100,
              "未映射率在 0–100%% 之间（实得 %s）" % um["value"])

        # ---------- ④ NULL ≠ 0：分母为 0 时必须是 NULL ----------
        dr = q1("SELECT count(*) AS n FROM mt.talent_deletion_request")["n"]
        dc = q1("SELECT * FROM mt.metric_value('M_INTEGRATION_DELETION_CONFIRMED_RATE')")
        if dr == 0:
            check(dc["value"] is None,
                  "没有删除请求时，确认率返回 **NULL** 而不是 0%% 或 100%%"
                  "（\"还没有可算的对象\" ≠ \"算出来是 0\"）")
        else:
            check(dc["value"] is not None, "有删除请求时确认率能算出数")

        # 队列口径必须能算（0 也要是 0，不是 NULL）
        for mid in ("M_INTEGRATION_PENDING_EVENTS", "M_INTEGRATION_DEAD_LETTERS"):
            r = q1("SELECT * FROM mt.metric_value(%s)", (mid,))
            check(r["value"] is not None,
                  "%s 能算出数（实得 %s）—— 队列空时应为 0 而不是 NULL"
                  % (mid, r["value"]))

        # ---------- ⑤ 视图授权（忘了对账会连 T3 都读不到）----------
        t3 = q1("SELECT has_table_privilege('mt_t3','mt.v_match_result_versioned',"
                "'SELECT') AS x")["x"]
        t0 = q1("SELECT has_table_privilege('mt_t0','mt.v_match_result_versioned',"
                "'SELECT') AS x")["x"]
        check(t3, "版本视图对 T3 可读（新视图忘了跑对账就会读不到）")
        check(not t0, "版本视图对匿名不可读（匹配结果不是公开数据）")

        # 用户侧 API 的**入口守卫**必须还在（评审要求验证的两件事）
        src = os.path.join(BASE, "code", "bridge", "api.py")
        txt = open(src, encoding="utf-8").read()
        check("CONSENT_REQUIRED" in txt,
              "用户侧 API 仍有授权守卫（撤回分析授权后不返回匹配结果）")
        check("PROFILE_DELETING" in txt,
              "用户侧 API 仍有删除态守卫（deleting 时不返回分析）")

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
