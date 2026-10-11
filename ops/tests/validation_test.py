# -*- coding: utf-8 -*-
"""
ops/tests/validation_test.py —— 有时间截点的验证集（评审优先级 4）

评审要求："按人隔离开发/留出集；历史预测按时间隔离；**不要只以最终去向作为真值**，
由评审人标注多个合理方向；报告时分开评估：目标职业是否在库、资格判定是否正确、
排序是否合理、解释是否有证据。"

无法在这个套件里真跑一遍人工评审，但**能守住让这件事成立的那些机制**：

  ① **按人隔离是机制，不是约定** —— 同一个人的预测点跨 dev/holdout 必须被数据库拒绝。
     否则"留出集"会被同一人的履历泄漏，指标虚高。
  ② **四项分开且 NULL ≠ false** —— 没评的项不能算作"不合格"
     （把"还没做"报成"做错了"是本库一贯禁止的）。
  ③ **泄漏的语义必须正确**：cutoff 之后**存在的材料**（正常，预测时要排除）
     与**已产出预测却没排除**（真泄漏）是两件事。009 第一版把前者报成泄漏，
     于是"真实世界继续发生事情"被当成错误报警。
  ④ **"目标在库"要分两问**：被映射到的目标在不在库（常常被映射规则"救"成在库）
     vs **语义上更贴的目标在不在库**（这才是"语料够不够"）。
     实测 3/3 真实案例的真实去向在库里 0 岗位 —— 只报前者会得出"覆盖没问题"。
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

        # ---------- 结构就位 ----------
        for t in ("validation_case", "validation_point", "validation_annotation"):
            n = q1("SELECT count(*) AS n FROM information_schema.tables "
                   "WHERE table_schema='mt' AND table_name=%s", (t,))["n"]
            check(n == 1, "%s 存在" % t)
        for v in ("v_validation_report", "v_validation_leakage"):
            # ⚠ 注意列名：information_schema.views 用 table_name，pg_views 才是 viewname
            n = q1("SELECT count(*) AS n FROM information_schema.views "
                   "WHERE table_schema='mt' AND table_name=%s", (v,))["n"]
            check(n == 1, "%s 存在" % v)

        # ---------- ① 按人隔离是**机制** ----------
        # 用一个探针人试插跨集合的点，必须被拒绝
        c.execute("""INSERT INTO mt.validation_case (case_id, person_id, nature, source)
                     VALUES ('__pt_case__','__pt_person__','synthetic','self-check')
                     ON CONFLICT (case_id) DO NOTHING""")
        c.execute("""INSERT INTO mt.validation_point (point_id, case_id, cutoff_date, split, basis)
                     VALUES ('__pt_1__','__pt_case__', DATE '2020-01-01','dev','self-check')
                     ON CONFLICT (point_id) DO NOTHING""")
        rejected = False
        try:
            c.execute("""INSERT INTO mt.validation_point (point_id, case_id, cutoff_date, split, basis)
                         VALUES ('__pt_2__','__pt_case__', DATE '2021-01-01','holdout','self-check')""")
        except psycopg.Error as e:
            rejected = "按人隔离被破坏" in str(e)
        finally:
            c.execute("DELETE FROM mt.validation_case WHERE case_id='__pt_case__'")
        check(rejected,
              "**同一个人的预测点跨 dev/holdout 被数据库拒绝** —— 按人隔离是机制不是约定")

        # ---------- ② 四项分开且 NULL ≠ false ----------
        cols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mt' AND table_name='v_validation_report'")}
        for a in ("aspect1_target_in_library", "aspect2_qualification_ok",
                  "aspect3_ranking_reasonable", "aspect4_evidence_present"):
            check(a in cols, "报告视图含 %s（四项分开，不合成总分）" % a)
        n_pred = q1("SELECT count(*) AS n FROM mt.validation_point")["n"]
        if n_pred:
            unjudged = q1("""SELECT count(*) AS n FROM mt.validation_annotation
                              WHERE qualification_ok IS NULL""")["n"]
            allann = q1("SELECT count(*) AS n FROM mt.validation_annotation")["n"]
            check(unjudged == allann,
                  "现有标注的资格判定**全部为 NULL（没评）**，没有被写成 false —— "
                  "「还没做」与「做错了」必须分开（%d/%d）" % (unjudged, allann))
            m = q1("SELECT * FROM mt.metric_value('M_VALID_QUALIFICATION_OK_RATE')")
            check(m["value"] is None,
                  "全部没评时，资格判定正确率返回 **NULL** 而不是 0%%（实得 %s）"
                  % m["value"])

        # ---------- ③ 泄漏语义：存在 ≠ 用了 ----------
        lk = q1("""SELECT count(*) AS n,
                          count(*) FILTER (WHERE leaked) AS leaked,
                          count(*) FILTER (WHERE predicted_at IS NOT NULL) AS pred
                     FROM mt.v_validation_leakage""")
        check(lk["leaked"] == 0 or lk["pred"] > 0,
              "「已产出预测却没排除」才叫泄漏（leaked=%d, 已产出预测=%d）"
              % (lk["leaked"], lk["pred"]))
        ml = q1("SELECT * FROM mt.metric_value('M_VALID_LEAKAGE_POINTS')")
        if lk["pred"] == 0:
            check(ml["value"] is None,
                  "**还没有产出任何预测时，泄漏口径返回 NULL**（不是 0、也不是报警）—— "
                  "cutoff 之后存在的材料是正常现象")
        cols2 = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mt' AND table_name='v_validation_leakage'")}
        check("leaked" in cols2 and "edu_after" in cols2,
              "泄漏视图把「真泄漏」与「必须排除的材料」分成两列")

        # ---------- ④ "目标在库"要分两问 ----------
        cols3 = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mt' AND table_name='validation_point'")}
        check("semantic_targets_without_jd" in cols3,
              "预测点单独记「语义更贴但库里 0 岗位的目标」")
        if n_pred:
            sem = q1("SELECT count(*) AS n FROM mt.validation_point "
                     "WHERE semantic_targets_without_jd IS NOT NULL")["n"]
            inlib = q1("SELECT count(*) AS n FROM mt.validation_point "
                       "WHERE in_library")["n"]
            check(sem > 0,
                  "**语义目标不在库可被检出**（%d 个点）—— 只报 in_library(%d 个在库) "
                  "会得出「覆盖没问题」的错误结论" % (sem, inlib))
            ms = q1("SELECT * FROM mt.metric_value('M_VALID_SEMANTIC_TARGET_MISSING_RATE')")
            check(ms["value"] is not None,
                  "语义目标不在库率能算（实得 %s%%）" % ms["value"])
        # 真值来源必须是多标注结构（不是只看最终去向）
        dcols = {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mt' AND table_name='validation_annotation'")}
        check("rater" in dcols and "direction" in dcols,
              "标注支持多个评审人 × 多个方向（不是只记一个最终去向）")
        dd = q1("""SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
                    WHERE conname LIKE 'validation_annotation_direction%'""")
        check(dd and "acceptable" in dd["d"] and "unacceptable" in dd["d"],
              "方向枚举含 acceptable/unacceptable（允许多个合理方向）")

        # ---------- 视图授权 ----------
        for v in ("v_validation_report", "v_validation_leakage"):
            t3 = q1("""SELECT has_table_privilege('mt_t3','mt.%s','SELECT')
                          OR has_any_column_privilege('mt_t3','mt.%s','SELECT') AS x"""
                    % (v, v))["x"]
            check(t3, "%s 对 T3 可读（忘了对账会读不到）" % v)

        # ---------- 域登记（建表时就该一起做）----------
        n_dom = q1("""SELECT count(*) AS n FROM mt.domain_registry
                       WHERE table_name IN ('validation_case','validation_point',
                                            'validation_annotation')""")["n"]
        check(n_dom == 3, "三张验证表都登记了业务域（%d/3）" % n_dom)
        # 域名单有外键：拼错会当场报错，而不是静默变成"未归类"
        fk = q1("""SELECT count(*) AS n FROM pg_constraint
                    WHERE conname='domain_registry_domain_fk'""")["n"]
        check(fk == 1, "domain_registry.domain 有外键 —— 域必须是已登记的（拼错当场报错）")

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
