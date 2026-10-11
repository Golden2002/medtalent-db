# -*- coding: utf-8 -*-
"""
ops/validation.py —— 有时间截点的真实验证集（评审优先级 4）

评审要求（逐条对应到本文件的实现）
--------------------------------------------------------------------------
1. "先区分两类目标：解释已有履历（可用全部历史）与**推荐下一步岗位**
   （只能使用决策时点之前的信息，不能泄漏）"
   → 本工具只建**推荐下一步**的预测点，每个点带 `cutoff_date`；
     泄漏由 `v_validation_leakage` 与 `M_VALID_LEAKAGE_POINTS` 盯住。

2. "按人隔离开发/留出集" → `validation_point` 上的触发器
   `check_split_person_isolation()`：**同一个人所有预测点必须在同一侧**，
   跨集合插入会被数据库直接拒绝（实测已验证）。

3. "历史预测按时间隔离" → 每个转换点一个 cutoff（下一个角色开始前一个月），
   特征只允许用事件时间 ≤ cutoff 的事实。

4. "**不要只以最终去向作为真值**，由评审人标注多个合理方向"
   → `validation_annotation` 支持多个 rater × 多个 direction
     (primary / acceptable / unacceptable)，且四项分开评、**可为 NULL**。

5. "报告时应分开评估：目标职业是否在库、资格判定是否正确、排序是否合理、解释是否有证据"
   → `report` 分四段输出，**不合成一个总分**。

核对运行状态时发现的三件事（都影响怎么做验证）
--------------------------------------------------------------------------
① **事件时间有、入库时间没有历史**：`recorded_at` 全部是 2026-09-27/28（一次性批量入库），
   所以只能按**事件时间**切；这一点写进了口径的注意项 ——
   真实增量采集开始后必须改成按 `recorded_at` 判。
② **真实案例只有 3 人**（`real_cases.json` 等三个文件），`transition_case` 表是空的。
   → 本验证集**不足以支撑统计结论**，只能验证机制、并暴露**定性失败**。
   报告里会明确写出来，不让读者误以为是"指标"。
③ **`employment_record.occupation_id` 全空** → 每个转换点的目标职业**只对最终去向有映射**
   （来自 `real_targets.json`）。中间转换点的目标记为 NULL 并在 note 里说明 ——
   这是**数据缺口**，不是「没有目标」。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "ops"))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
       "options='-c search_path=mt,public'")

FIX = os.path.join(BASE, "ops", "fixtures")
RATER = "fixture_source"          # 初始标注来自真值文件（不是真人评审），要如实标明


def _load(name):
    p = os.path.join(FIX, name)
    if not os.path.isfile(p):
        return None
    return json.load(io.open(p, encoding="utf-8"))


def seed(split_map=None):
    """把 3 位真实公开案例装进验证集。

    split_map: {person_id: 'dev'|'holdout'}；默认 2 人 dev / 1 人 holdout（按人隔离）。
    """
    cases = _load("real_cases.json")
    targets = _load("real_targets.json")
    if not cases or not targets:
        print("[!] 缺少 real_cases.json / real_targets.json，无法建集")
        return 1
    tgt = {c["person_id"]: c for c in targets["cases"]}
    if split_map is None:
        ids = sorted(tgt.keys())
        split_map = {pid: ("dev" if i < len(ids) - 1 else "holdout")
                     for i, pid in enumerate(ids)}

    n_case = n_point = n_ann = 0
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=False) as c:
        for case in cases["cases"]:
            key = case["case_key"]
            pid = "per_real_" + key
            if pid not in tgt:
                continue
            cid = "vc_" + key
            c.execute("""INSERT INTO mt.validation_case (case_id, person_id, nature, source, note)
                         VALUES (%s,%s,'real',%s,%s)
                         ON CONFLICT (case_id) DO UPDATE
                           SET note = EXCLUDED.note, person_id = EXCLUDED.person_id""",
                      (cid, pid, case.get("public_name"),
                       "公开二手来源（见 real_cases.json 的 sources）；"
                       "真值来自 real_targets.json 的 actual_path/occupation_ids/mapping_reason"))
            n_case += 1
            t = tgt[pid]
            occ_ids = t.get("occupation_ids") or []
            target = occ_ids[0] if occ_ids else None
            # 目标职业在库里**是否有 JD 样本** —— 这一项单独判，且不是算法的责任
            in_lib = None
            if target:
                r = c.execute("""SELECT count(*) AS n FROM mt.job_posting
                                  WHERE occupation_id = %s""", (target,)).fetchone()
                in_lib = (r["n"] > 0)
            # **语义上更贴、但库里 0 岗位**的目标（real_targets.json 的 nodes_without_jd）。
            # 必须单独采集：被选中的 occupation_ids 常常是"映射规则刻意挑了语料里
            # 存在的最近节点"的结果，只看 in_library 会得出「覆盖没问题」的错误结论
            # —— 069 第一版就是只报了后者，于是输出"在库 3/3"把真问题盖掉了。
            nodes_no_jd = t.get("nodes_without_jd") or []
            sem_missing = ("、".join(nodes_no_jd) if nodes_no_jd else None)
            # 每个有日期的经历 → 一个"下一步"预测点（cutoff＝该段开始前一个月）
            emps = c.execute("""SELECT employment_id, employer_name, title_raw, start_date
                                  FROM mt.employment_record
                                 WHERE person_id=%s AND start_date IS NOT NULL
                                 ORDER BY start_date""", (pid,)).fetchall()
            for i, e in enumerate(emps):
                is_last = (i == len(emps) - 1)
                ptid = "vp_%s_%d" % (key, i)
                c.execute("""INSERT INTO mt.validation_point
                      (point_id, case_id, cutoff_date, split, target_occupation_id,
                       target_label, in_library, semantic_targets_without_jd, basis)
                      VALUES (%s,%s, (%s::date - INTERVAL '1 month')::date, %s, %s, %s, %s, %s, %s)
                      ON CONFLICT (point_id) DO UPDATE
                        SET cutoff_date = EXCLUDED.cutoff_date, split = EXCLUDED.split,
                            target_occupation_id = EXCLUDED.target_occupation_id,
                            target_label = EXCLUDED.target_label,
                            in_library = EXCLUDED.in_library,
                            semantic_targets_without_jd = EXCLUDED.semantic_targets_without_jd,
                            basis = EXCLUDED.basis""",
                      (ptid, cid, e["start_date"], split_map[pid],
                       target if is_last else None,
                       e["title_raw"],
                       in_lib if is_last else None,
                       sem_missing if is_last else None,
                       "入职「%s / %s」前一个月 —— 此时只能看到之前发生的事"
                       % (e["employer_name"], e["title_raw"])))
                n_point += 1
                # 初始标注：来自真值文件，**不是真人评审**（rater 如实写 fixture_source）
                if is_last and target:
                    note = ("真实去向映射自 real_targets.json。"
                            "mapping_reason 里写明：语义更贴的方向在 690 份 JD 里 0 岗位，"
                            "库里存在的最近节点是「券商医药行业研究员」。"
                            "**中间转换点的目标没有映射**（employment_record.occupation_id 全空）"
                            "—— 那是数据缺口，不是「没有目标」。")
                    # 四项分开：目标在库＝可判；其余三项**留 NULL＝「没评」**
                    # （不把「没评」写成 false —— 那会把没做的工作显示成做错了）
                    c.execute("""INSERT INTO mt.validation_annotation
                          (point_id, rater, direction, occupation_id, label,
                           target_in_library, note)
                          VALUES (%s,%s,'primary',%s,%s,%s,%s)
                          ON CONFLICT (point_id, rater, direction, label) DO UPDATE
                            SET target_in_library = EXCLUDED.target_in_library,
                                note = EXCLUDED.note""",
                              (ptid, RATER, target, e["title_raw"], in_lib, note))
                    n_ann += 1
        c.commit()
    print("验证集已建立：案例 %d · 预测点 %d · 初始标注 %d" % (n_case, n_point, n_ann))
    print("  划分（按人隔离）: %s" % ", ".join("%s=%s" % (k.replace('per_real_', ''), v)
                                              for k, v in sorted(split_map.items())))
    return 0


def report():
    """四项分开报告 —— **不合成一个总分**。"""
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as c:
        rows = c.execute("SELECT * FROM mt.v_validation_report ORDER BY case_id, cutoff_date").fetchall()
        pts = c.execute("SELECT split, count(*) AS n, count(DISTINCT case_id) AS ppl "
                        "FROM mt.validation_point GROUP BY 1 ORDER BY 1").fetchall()
        leak = c.execute("SELECT count(*) AS n FROM v_validation_leakage "
                         "WHERE edu_after > 0 OR emp_after > 0").fetchone()["n"]
    if not rows:
        print("[!] 验证集为空 —— 先跑 `python ops/validation.py seed`")
        return 1

    n_ppl = len({r["case_id"] for r in rows})
    print("=" * 74)
    print("真实验证集报告（**四项分开，不合成总分**）")
    print("=" * 74)
    print("规模：%d 位真人 · %d 个预测点 · %s" %
          (n_ppl, len(rows),
           " / ".join("%s %d 点(%d人)" % (p["split"], p["n"], p["ppl"]) for p in pts)))
    print()
    print("⚠ **样本量声明**：%d 位真人**不足以支撑任何统计结论**。" % n_ppl)
    print("  它只能验证机制是否成立、并暴露**定性失败**。下面任何比例都不应当被当成指标。")
    print()

    # ① 目标职业是否在库 —— **两件事必须分开看**
    judged = [r for r in rows if r["aspect1_target_in_library"] is not None]
    print("① 目标职业是否在库")
    print("   ①a 被映射到的目标在库：可判 %d 个点；在库 %d、不在库 %d"
          % (len(judged), sum(1 for r in judged if r["aspect1_target_in_library"]),
             sum(1 for r in judged if not r["aspect1_target_in_library"])))
    sem = [r for r in rows if r["semantic_targets_without_jd"]]
    print("   ①b **语义上更贴的目标不在库**：%d 个点" % len(sem))
    for r in sem:
        print("     ⚠ %s · cutoff %s：语义更贴的方向 **%s** 在 690 份 JD 里 0 岗位；"
              % (r["case_id"], r["cutoff_date"], r["semantic_targets_without_jd"]))
        print("        而被映射到的是 %s（%s）—— "
              % (r["target_occupation_id"],
                 "在库" if r["aspect1_target_in_library"] else "也不在库"))
    if len(judged) and sum(1 for r in judged if r["aspect1_target_in_library"]) == len(judged):
        print("   → ⚠ **只看 ①a 会得出「覆盖没问题」**（本例就是 3/3 在库），")
        print("     但那是**映射规则刻意挑了语料里存在的最近节点**的结果；")
        print("     ①b 才回答「语料够不够」。这两个数字必须一起看。")
    print("   → 目标不在库时，任何「推荐错了」的结论**都不成立**：")
    print("     库里没有该职业的岗位，任何匹配算法都不可能推荐出来。该补的是语料，不是算法。")
    print()

    # ②③④ 三项分开，且只统计"评过的"
    def seg(label, key, explain):
        vals = [r[key] for r in rows if r[key] is not None]
        n = len(vals)
        print("%s" % label)
        if not n:
            print("   **没有任何一个点评过这一项**（NULL＝「没评」，不是不合格）")
            print("   → 所以这一项**目前给不出结论**。要出结论必须有人真去评。")
        else:
            print("   评过 %d 项：通过 %d / 不通过 %d" % (n, sum(1 for v in vals if v), n - sum(1 for v in vals if v)))
        print("   %s" % explain)
        print()

    seg("② 资格判定是否正确", "aspect2_qualification_ok",
        "衡量**取数**：资格判定依赖硬门槛，而实测 150/150 条匹配结果都因硬门槛未知而"
        "「待核实」——所以这一项在补采集之前本来就评不出「正确」。")
    seg("③ 排序是否合理", "aspect3_ranking_reasonable",
        "衡量**权重**：排序合理与资格判对是两件事（可能把不合格的人排前面但顺序看着顺眼）。")
    seg("④ 解释是否有证据", "aspect4_evidence_present",
        "衡量**溯源**：一个错误结论也可以有完整证据链，所以这一项与「对不对」分开。")

    print("-" * 74)
    # ⚠ 两个概念必须分开：**cutoff 之后存在的材料**（正常，预测时要排除）
    # vs **已产出预测却没有排除**（这才是泄漏）。
    n_pred = sum(1 for r in rows if r["predicted_at"] is not None)
    n_leak = sum(1 for r in rows if r["leaked"])
    must_exclude = sum((r["edu_after"] or 0) + (r["emp_after"] or 0) for r in rows)
    print("时间截点：")
    print("  · cutoff 之后**存在的材料** %d 条 —— 这是正常现象（真实世界会继续发生事情），" % must_exclude)
    print("    预测时**必须排除**它们；它不是泄漏。")
    if n_pred == 0:
        print("  · **实际泄漏**：尚无结论 —— 还没有产出过任何预测（分母 0）。")
        print("    → 所以这里**不报 0、也不报报警**：NULL 表示「还没有可算的对象」，")
        print("      与「算出来是 0」是两件事。（069 第一版把上面那个数字直接写成")
        print("      「泄漏 7 个点」，语义是错的。）")
    else:
        print("  · **实际泄漏**：%d / %d 个预测点**已产出预测却没有应用截点过滤**" % (n_leak, n_pred))
        if n_leak:
            print("    ⚠ 有泄漏 → 离线指标会虚高而线上无效，必须先修")
    print()
    print("本报告**刻意不给总分**：四项的失败原因完全不同 ——")
    print("  目标不在库 → 补语料；资格判错 → 补取数；排序不合理 → 调权重；解释无据 → 补溯源。")
    print("  合成一个分数会把它们混在一起，于是「该补语料」会被误读成「该调算法」。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="有时间截点的真实验证集")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="从真实公开案例建立验证集（按人隔离）")
    sub.add_parser("report", help="四项分开报告")
    sub.add_parser("cleanup", help="清空验证集")
    a = ap.parse_args()
    if a.cmd == "seed":
        return seed()
    if a.cmd == "report":
        return report()
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("DELETE FROM mt.validation_annotation")
        c.execute("DELETE FROM mt.validation_point")
        c.execute("DELETE FROM mt.validation_case")
    print("验证集已清空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
