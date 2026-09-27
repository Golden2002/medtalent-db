# -*- coding: utf-8 -*-
"""
code/gates/jd_quality_gate.py —— 岗位侧质量门（T09）

对应 docs/03 §7 的验收指标，全部算成可判定的数字：

  1. 结构化 JD 总数 / 覆盖岗位族数
  2. 必填字段完整率（title_raw / employer_name_raw / city / raw_text_ref / raw_sha256）
  3. 原文可回溯率（raw_text_ref 指向的 L0 文件确实存在）
  4. **能力概念映射覆盖率**（分母只算"应当映射到能力概念"的要求类型）
  5. 每族样本量是否达标（不足者列 coverage_gap）
  6. 抽检导出（每族随机抽 N 条，生成人工核对 CSV）

关于第 4 条的口径（重要）：
  学历(RT1)/经验(RT3)/证照(RT4) 属**资格门槛**，由 education_req / license_req /
  experience_req 字段承载，不应强求映射到 concept（那是能力/知识/技能）。
  把它们算进分母会把覆盖率指标变成一个没有意义的数字。因此分母 =
  requirement_type ∈ (RT5 技能, RT6 知识, RT7 语言, RT8 其他)。

用法：python code/gates/jd_quality_gate.py --source src_fixture_careers
     python code/gates/jd_quality_gate.py --source src_fixture_careers --sample 5
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import random

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import metrics  # noqa: E402  ← 度量的单一口径定义（与门户/可视化共用）

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
# 目标值不再在本文件另写一遍：全部取自 metrics（唯一定义）。
# 这里只把它转成"比率"形式（metrics 里的常量以百分数保存）。
# jd_total 与 family_sample 是**本门禁特有**的规模目标，metrics 不表达，保留在本地。
TARGETS = {
    "jd_total": 3000,
    "families": metrics.FAMILY_TARGET,
    "required_complete": metrics.REQUIRED_COMPLETE_TARGET / 100.0,
    "traceable": metrics.TRACEABLE_TARGET / 100.0,
    "concept_coverage": metrics.COVERAGE_TARGET / 100.0,
    "family_sample": 30,
}


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def q1(c, sql, p=None):
    with c.cursor() as cur:
        cur.execute(sql, p)
        r = cur.fetchone()
        return list(r.values())[0] if r else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None)
    ap.add_argument("--sample", type=int, default=0, help="每族抽检条数，>0 时导出 CSV")
    a = ap.parse_args()

    # 统一用命名参数，避免 %-拼接导致的类型推断失败。
    # types / quals 来自 metrics.coverage_params()——本文件不再自带一份口径常量。
    WHERE = "WHERE (%(src)s::text IS NULL OR jp.source_id = %(src)s::text)"
    P = dict(metrics.coverage_params(), src=a.source)
    fails = []

    def verdict(name, value, target, ok, fmt="%s"):
        flag = "PASS" if ok else "FAIL"
        if not ok:
            fails.append(name)
        print("  [%s] %-22s 实际=%s  目标=%s" % (flag, name, fmt % value, fmt % target))

    with conn() as c:
        print("=" * 78)
        print("岗位侧质量门（T09）%s" % ("  来源：" + a.source if a.source else "  全部来源"))
        print("=" * 78)

        total = q1(c, "SELECT count(*) FROM job_posting jp " + WHERE, P)
        print("\n【规模】")
        print("  %-24s %d" % ("结构化 JD 总数", total))
        print("  %-24s %d" % ("职责条目", q1(c, "SELECT count(*) FROM job_task")))
        print("  %-24s %d" % ("要求条目", q1(c, "SELECT count(*) FROM job_requirement")))
        print("  %-24s %d" % ("能力权重矩阵行数", q1(c, "SELECT count(*) FROM job_competency_weight")))

        print("\n【覆盖率】")
        fams = q1(c, "SELECT count(DISTINCT job_family) FROM job_posting jp " + WHERE, P)
        verdict("岗位族覆盖", fams, TARGETS["families"], fams >= TARGETS["families"])

        req_ok = q1(c, """SELECT count(*) FROM job_posting jp
                          WHERE (%(src)s::text IS NULL OR jp.source_id = %(src)s::text)
                          AND jp.title_raw IS NOT NULL AND jp.title_raw<>''
                          AND jp.employer_name_raw IS NOT NULL
                          AND jp.city IS NOT NULL
                          AND jp.raw_text_ref IS NOT NULL
                          AND jp.raw_sha256 IS NOT NULL""", P)
        rate = req_ok / max(total, 1)
        verdict("必填字段完整率", "%.1f%%" % (rate * 100),
                "%.0f%%" % (TARGETS["required_complete"] * 100), rate >= TARGETS["required_complete"])

        # 原文可回溯：L0 文件真实存在
        rows = []
        with c.cursor() as cur:
            cur.execute("SELECT job_id, raw_text_ref FROM job_posting jp " + WHERE, P)
            rows = cur.fetchall()
        missing = [r for r in rows if not os.path.exists(os.path.join(BASE, r["raw_text_ref"] or ""))]
        tra = 1 - len(missing) / max(len(rows), 1)
        verdict("原文可回溯率", "%.1f%%" % (tra * 100), "100%", tra >= TARGETS["traceable"])
        if missing:
            print("     缺失样例：%s" % [m["job_id"] for m in missing[:3]])

        # 概念映射覆盖率：口径来自 code/metrics.py（**单一定义**，不在此另写一遍）。
        # 分母只算"应当映射到能力概念"的类型（RT5 技能 / RT6 知识 / RT7 语言 / RT8 其他）；
        # RT1/RT3/RT4 是资格门槛（学历/经验/证照），由专用字段承载，不计入。
        # P 已含 types/quals，直接传即可。
        with c.cursor() as cur:                       # 需要整行计数，不能用 q1（它只取首列）
            cur.execute(metrics.CONCEPT_COVERAGE_SQL
                        + " JOIN job_posting jp ON jp.job_id = r.job_id " + WHERE, P)
            cov_row = cur.fetchone()
        denom = cov_row["denom"] or 0
        numer = cov_row["numer"] or 0
        # 判定用**未四舍五入**的比率；显示用 metrics.coverage_pct()，
        # 与门户 /quality、可视化面板 m_gate 走同一个函数（避免 round() 口径不一致）。
        cov = numer / max(denom, 1)
        verdict("能力概念映射覆盖率",
                "%.1f%% (%d/%d)" % (metrics.coverage_pct(cov_row), numer, denom),
                "%.0f%%" % metrics.COVERAGE_TARGET,
                cov >= TARGETS["concept_coverage"])
        gate_n = cov_row["qualification"] or 0
        print("     （资格门槛类 %d 条学历/经验/证照要求不计入分母，由专用字段承载）" % gate_n)

        print("\n【分布】")
        fam_rows = []
        with c.cursor() as cur:
            cur.execute("""SELECT jp.job_family, f.label_zh, count(*) AS n
                           FROM job_posting jp
                           LEFT JOIN code_value f ON f.code_table_id='CT_JOB_FAMILY'
                                AND f.code = jp.job_family
                           %s GROUP BY 1,2 ORDER BY 1""" % WHERE, P)
            fam_rows = cur.fetchall()
        gaps = []
        for r in fam_rows:
            flag = "OK " if r["n"] >= TARGETS["family_sample"] else "GAP"
            if r["n"] < TARGETS["family_sample"]:
                gaps.append(r["job_family"])
            print("  [%s] %-4s %-24s %4d 条" % (flag, r["job_family"], r["label_zh"], r["n"]))
        if gaps:
            print("  coverage_gap（样本 < %d）：%s" % (TARGETS["family_sample"], "、".join(gaps)))

        print("\n【质量标记】")
        with c.cursor() as cur:
            cur.execute("""SELECT unnest(quality_flags) AS f, count(*) AS n
                           FROM job_posting jp %s GROUP BY 1 ORDER BY 2 DESC""" % WHERE, P)
            for r in cur.fetchall():
                print("  %-24s %d" % (r["f"], r["n"]))

    # 抽检导出
    if a.sample:
        out = os.path.join(BASE, "analysis", "qc_sample.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        rnd = random.Random(42)
        with conn() as c, c.cursor() as cur:
            cur.execute("""SELECT jp.job_id, jp.job_family, jp.title_raw, jp.city,
                                  jp.salary_min, jp.salary_max, jp.education_req,
                                  count(jr.requirement_id) AS n_req
                           FROM job_posting jp
                           LEFT JOIN job_requirement jr ON jr.job_id=jp.job_id
                           %s GROUP BY 1,2,3,4,5,6,7""" % WHERE, P)
            allrows = cur.fetchall()
        byfam = {}
        for r in allrows:
            byfam.setdefault(r["job_family"] or "?", []).append(r)
        picked = []
        for fam, rs in byfam.items():
            picked += rnd.sample(rs, min(a.sample, len(rs)))
        with open(out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["job_id", "job_family", "title_raw", "city", "salary_min",
                        "salary_max", "education_req", "n_req",
                        "人工判定_岗位族是否正确(Y/N)", "人工判定_学历是否正确(Y/N)",
                        "人工判定_硬性要求是否正确(Y/N)", "备注"])
            for r in picked:
                w.writerow([r["job_id"], r["job_family"], r["title_raw"], r["city"],
                            r["salary_min"], r["salary_max"], r["education_req"], r["n_req"],
                            "", "", "", ""])
        print("\n【抽检】已导出 %d 条（每族 %d 条）→ %s" % (len(picked), a.sample,
                                                    os.path.relpath(out, BASE)))

    print("\n" + "=" * 78)
    if fails:
        print("未达标项：%s" % "、".join(fails))
    else:
        print("全部指标达标（注：JD 总数目标 %d 是 v1 目标，当前样本为 mock 语料）"
              % TARGETS["jd_total"])
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
