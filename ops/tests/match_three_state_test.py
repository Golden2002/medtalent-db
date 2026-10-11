# -*- coding: utf-8 -*-
"""
ops/tests/match_three_state_test.py —— 匹配三态展示的回归断言（评审优先级 3）

评审要求："结果卡固定显示三块（资格判定 / 匹配依据 / 信息完整度）"、
"**已知维度上的高分不能覆盖未知的硬门槛**"、"并列应显示并列组或名次范围"、
"覆盖率分母应是该岗位适用且应评估的要求"。

这一套把四条**变成机器可检的**：
  ① **硬门槛未知 → 结论必须是"待核实"**。
     这是本优先级最要紧的一条：实测 150 条结果全是"满分/近满分"，
     而平均 5.7/8 个硬门槛未知。若这里断言失效，
     页面就会把"信息不足"说成"匹配"，用户据此做决定会出事。
  ② **覆盖率的两个分母方向正确**：排除"不适用"（岗位侧没有值）后应当**变大**；
     若写反（把不适用也算进分母）修正值会变小 —— 方向错了要当场发现。
  ③ **并列给范围**：同分结果的 rank_min 必须相同、rank_max > rank_min，
     不能靠 ID 顺序造出精确名次。
  ④ **阈值是注册的、且标注是否已定稿**：不能硬编码在页面里，
     也不能把"临时值"当成已验证的结论。
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

        n = q1("SELECT count(*) AS n FROM mt.v_match_coverage")["n"]
        check(n >= 0, "三态视图能读（%d 条匹配结果）" % n)

        # ---------- ① 硬门槛未知 → 结论只能是"待核实" ----------
        bad = q1("""SELECT count(*) AS n FROM mt.v_match_coverage
                     WHERE n_hard_unknown > 0 AND verdict <> '待核实'""")["n"]
        check(bad == 0,
              "**硬门槛未知时结论都是「待核实」**（违规 %d 条）—— "
              "已知维度上的高分不得覆盖未知硬门槛" % bad)
        # 反证：硬门槛全部已评估时，才允许出现"已确认满足/不满足"
        ok_rows = q1("""SELECT count(*) AS n FROM mt.v_match_coverage
                         WHERE verdict = '已确认满足' AND n_hard_unknown > 0""")["n"]
        check(ok_rows == 0, "不存在「硬门槛还未知却判定已确认满足」的行")
        hard_all = q1("SELECT count(*) AS n FROM mt.dimension "
                      "WHERE status='active' AND is_hard")["n"]
        check(hard_all >= 1,
              "硬门槛维度已定义（%d 个：来自 mt.dimension.is_hard）" % hard_all)
        hv = q1("SELECT * FROM mt.metric_value('M_MATCH_HARDGATE_UNKNOWN_RATE')")
        check(0 <= (hv["value"] or 0) <= 100,
              "硬门槛待核实率能算且在 0–100%%（实得 %s）—— "
              "这个数字高说明资格判定做不了，而不是候选人不合格" % hv["value"])

        # ---------- ② 覆盖率分母方向正确 ----------
        rev = q1("""SELECT count(*) AS n FROM mt.v_match_coverage
                     WHERE coverage_corrected IS NOT NULL AND coverage_raw IS NOT NULL
                       AND coverage_corrected < coverage_raw""")["n"]
        check(rev == 0,
              "修正覆盖率**不小于**原始覆盖率（违规 %d 条）—— "
              "排除「不适用」后分母变小、比例应变大；写反了就是分母口径错" % rev)
        na = q1("SELECT coalesce(sum(n_not_applicable),0) AS n FROM mt.v_match_coverage")["n"]
        check(na >= 0, "「不适用」（岗位侧没有值）被单独计数（合计 %d 项）" % na)
        # 分母必须等于"已评估 + 应评估但缺失"，不能含不适用
        mix = q1("""SELECT count(*) AS n FROM mt.v_match_coverage
                     WHERE n_applicable <> n_scored + n_unknown""")["n"]
        check(mix == 0,
              "覆盖率分母 = 已评估 + 应评估但缺失（不含不适用），违规 %d 条" % mix)

        # ---------- ③ 并列给名次范围 ----------
        if q1("SELECT count(*) AS n FROM mt.v_match_ranked")["n"]:
            bad_tie = q1("""
                SELECT count(*) AS n FROM (
                  SELECT match_run_id, person_id, score_total,
                         count(DISTINCT rank_min) AS k, max(rank_max) AS mx,
                         min(rank_min) AS mn
                    FROM mt.v_match_ranked WHERE tie_n > 1
                   GROUP BY 1,2,3 HAVING count(DISTINCT rank_min) <> 1
                       OR max(rank_max) <= min(rank_min)) t""")["n"]
            check(bad_tie == 0,
                  "同分结果的 rank_min 相同且 rank_max > rank_min（违规 %d 组）—— "
                  "并列必须共享名次范围" % bad_tie)
            t = q1("SELECT count(*) FILTER (WHERE tie_n > 1) AS tie, count(*) AS all_n "
                   "FROM mt.v_match_ranked")
            check(t["tie"] <= t["all_n"], "并列计数不超过总数")
            # 名次标签必须体现范围（同分时不能只显示单一名次）
            lbl = q1("""SELECT count(*) AS n FROM mt.v_match_ranked
                         WHERE tie_n > 1 AND rank_label NOT LIKE '%–%'""")["n"]
            check(lbl == 0, "同分结果的名次标签写成范围（如「并列第 1–50 名」），违规 %d 条" % lbl)

        # ---------- ④ 阈值是注册的，且标注是否已定稿 ----------
        pol = q1("""SELECT value, unit, provisional, note FROM mt.match_display_policy
                     WHERE policy_id='min_coverage_for_conclusion'""")
        check(pol is not None, "展示阈值登记在 match_display_policy（不是硬编码在页面里）")
        if pol:
            check(0 < float(pol["value"]) <= 1,
                  "阈值在 (0,1] 内（实得 %s）" % pol["value"])
            check(pol["provisional"] is True,
                  "阈值被标注为**临时值**（provisional=true）—— "
                  "评审明确说正式阈值要通过真实验证确定，未验证前不能当结论")

        # ---------- 口径与视图同源（一个口径一份实现）----------
        cm = q1("SELECT * FROM mt.metric_value('M_MATCH_COVERAGE_CORRECTED_MEDIAN')")
        if n:
            check(cm["value"] is not None,
                  "修正覆盖率中位数能算出（实得 %s%%）" % cm["value"])
            check(0 <= cm["value"] <= 100, "中位数在 0–100%% 内")
        tr = q1("SELECT * FROM mt.metric_value('M_MATCH_TIE_RATE')")
        if n:
            view_tie = q1("SELECT count(*) FILTER (WHERE tie_n > 1) AS n "
                          "FROM mt.v_match_ranked")["n"]
            check(tr["numerator"] == view_tie,
                  "并列率口径的分子与视图计数一致（%s vs %d）"
                  % (tr["numerator"], view_tie))

        # ---------- 视图授权（忘了对账会连 T3 都读不到）----------
        for v in ("v_match_coverage", "v_match_ranked"):
            t3 = q1("SELECT has_table_privilege('mt_t3','mt.%s','SELECT') AS x" % v)["x"]
            t0 = q1("SELECT has_table_privilege('mt_t0','mt.%s','SELECT') AS x" % v)["x"]
            check(t3, "%s 对 T3 可读" % v)
            check(not t0, "%s 对匿名不可读（匹配结果不是公开数据）" % v)

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
