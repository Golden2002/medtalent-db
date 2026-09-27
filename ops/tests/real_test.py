# -*- coding: utf-8 -*-
"""
ops/tests/real_test.py —— 真实公开案例 + 画像向量组装 + 维度体检 的回归

这一套守的是"**真数据上还能不能站住**"这一类问题。它不是合成数据的回归：
  · 合成样本里 35/50 个维度 100% 覆盖；真实公开案例只有 6–10/50。
    所以断言必须写成"真实案例的覆盖落在某个区间并给出原因"，而不是"覆盖很高"。
  · 组装层与体检模块是新写的，它们错了不会让任何页面报错 —— 只会让结论悄悄失真。
    所以这里断言的是**具体数字**（两侧可评维度数、冗余对、恒定维度），
    数字变了就失败，逼人回来看是数据变了还是口径变了。

用法：python ops/tests/real_test.py
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "analytics"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
import _harness as H  # noqa: E402
import vector as V  # noqa: E402
import dimension_check as DC  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check
DSN = V.DSN

# 实测基线。改动数据或口径时这些数字会变 —— 那正是让人停下来看一眼的目的。
N_DIM = 50
N_TWO_SIDED = 14          # 两侧都能组装出值的维度数（改设计/补字段/开通派生都会动它）
# 历史：10（只有列直读）→ 13（016 开通岗位侧派生）→ 14（修 _as_codes 的数组嵌套 + 岗位侧文本档位归一化）
REAL_PREFIX = "per_real_"
REAL_BATCH = os.path.join(BASE, "ops", "fixtures", "real_cases.json")
REAL_TARGETS = os.path.join(BASE, "ops", "fixtures", "real_targets.json")


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def q1(c, sql, p=None):
    return c.execute(sql, p or ()).fetchone()


def main():
    with conn() as c:
        dims = V.load_dims(c)

        # ===============================================================
        print("\n【R1】组装层：每个维度要么有值、要么有原因，不许两者皆无")
        pid = q1(c, "SELECT person_id FROM mt.person ORDER BY person_id LIMIT 1")["person_id"]
        jid = q1(c, "SELECT job_id FROM mt.job_posting ORDER BY job_id LIMIT 1")["job_id"]
        check(len(dims) == N_DIM, "维度注册表 %d 个（基线 %d）" % (len(dims), N_DIM))
        pv, pp = V.person_payloads(c, pid, dims)
        jv, jp = V.job_payloads(c, jid, dims)
        for side, prov, pay in (("人侧", pp, pv), ("岗位侧", jp, jv)):
            bad = [d for d in prov
                   if (prov[d]["status"] == "ok") != (d in pay) or not prov[d].get("reason")
                   and prov[d]["status"] != "ok"]
            check(not bad, "%s：%d 个维度全部有值或有原因（异常：%s）"
                  % (side, len(prov), bad[:3] or "无"))
        check(all("reason" not in v or v["reason"] is None
                  for k, v in pp.items() if v["status"] == "ok"),
              "人侧取值成功的维度不带失败原因")

        # ===============================================================
        print("\n【R2】充分性：两侧可评维度数必须与实测基线一致")
        cov = V.coverage(c, dims)
        two = sorted(d["dimension_id"] for d in cov["dims"] if d["person_ok"] and d["job_ok"])
        check(len(two) == N_TWO_SIDED,
              "两侧都能取到值的维度 = %d（基线 %d）" % (len(two), N_TWO_SIDED))
        # 硬门槛必须至少有一个能真的评 —— 否则"门槛"只是装饰
        gates = [d for d in cov["dims"] if d["role"] == "gate" and d["person_ok"] and d["job_ok"]]
        check(len(gates) >= 3,
              "可评的硬门槛维度 >= 3 个（实得 %d：%s）"
              % (len(gates), "、".join(x["dimension_id"] for x in gates)))
        # 016 的核心成果：权重 1.0 的经验门禁以前**两侧都取不到值**（等于从不拦人），
        # 现在人侧由任职起止求和、岗位侧由经验要求文本归一化，两侧都必须接近满覆盖。
        byid = {d["dimension_id"]: d for d in cov["dims"]}
        wy = byid["DIM_WORK_YEARS"]
        check(wy["job_ok"] == cov["n_job"],
              "DIM_WORK_YEARS 岗位侧全覆盖（%d/%d）—— 这条门禁以前是 0，从来不拦人"
              % (wy["job_ok"], cov["n_job"]))
        check(wy["person_ok"] >= 0.8 * cov["n_person"],
              "DIM_WORK_YEARS 人侧覆盖率 >= 80%%（%d/%d）"
              % (wy["person_ok"], cov["n_person"]))
        et = byid["DIM_EMPLOYER_TYPE"]
        check(et["job_ok"] >= 0.9 * cov["n_job"],
              "DIM_EMPLOYER_TYPE 岗位侧由 employer_name_raw 派生（%d/%d）"
              % (et["job_ok"], cov["n_job"]))
        ct = byid["DIM_CITY_TIER"]
        check(ct["person_ok"] and ct["job_ok"] == cov["n_job"],
              "DIM_CITY_TIER 两侧都有值（人侧 %d，岗位侧 %d/%d）—— 映射来自数据字典"
              % (ct["person_ok"], ct["job_ok"], cov["n_job"]))
        st = byid["DIM_SCHOOL_TIER"]
        check(st["person_ok"] >= 0.8 * cov["n_person"],
              "DIM_SCHOOL_TIER 由院校中文标签归一化（%d/%d）—— 修 _as_codes 的数组嵌套之前是 0"
              % (st["person_ok"], cov["n_person"]))
        # 岗位侧缺落点是"待采集"而不是"错误"，但缺口规模要看得见
        no_landing = [d for d in cov["dims"] if d["job_ok"] == 0]
        check(len(no_landing) > 0,
              "岗位侧无落点的维度 %d 个（这是待采集清单，不是 bug）" % len(no_landing))

        # ===============================================================
        print("\n【R3】015 修正：规培维度不再与证照维度同源")
        row = q1(c, "SELECT person_locator FROM mt.dimension WHERE dimension_id='DIM_TRAINING'")
        check(row["person_locator"] == "credential.credential_type:C02,C03",
              "DIM_TRAINING 的 locator 带 C02/C03 过滤（实得 %s）" % row["person_locator"])
        # 过滤真的生效：取到的人身上出现的码只能是 C02/C03
        seen = set()
        for r in c.execute("SELECT DISTINCT credential_type FROM mt.credential"):
            seen.add(r["credential_type"])
        sample = [x["person_id"] for x in c.execute(
            "SELECT person_id FROM mt.person ORDER BY person_id LIMIT 30")]
        bad = []
        for s in sample:
            p, _ = V.person_payloads(c, s, dims)
            codes = (p.get("DIM_TRAINING") or {}).get("codes") or []
            if any(x not in ("C02", "C03") for x in codes):
                bad.append((s, codes))
        check(not bad, "规培维度取到的码只可能是 C02/C03（异常：%s）" % (bad[:2] or "无"))

        # ===============================================================
        print("\n【R4】正交性：重复计权必须为 0，且恒定维度要有结论")
        ort = DC.orthogonality(c, dims, sample_persons(c))
        check(not ort["redundant_pairs"],
              "没有疑似重复计权的维度对（实得 %d 对：%s）"
              % (len(ort["redundant_pairs"]),
                 "；".join("%s×%s" % (p["a"], p["b"]) for p in ort["redundant_pairs"][:3])))
        # 拿具体的一对做守卫：DIM_CREDENTIAL 与 DIM_TRAINING 曾 Jaccard=1.000
        keys = {d["dimension_id"]: DC._person_keys(c, dims, sample_persons(c))[d["dimension_id"]]
                for d in dims if d["dimension_id"] in ("DIM_CREDENTIAL", "DIM_TRAINING")}
        common = [s for s in sample_persons(c)
                  if s in keys["DIM_CREDENTIAL"] and s in keys["DIM_TRAINING"]]
        if common:
            jac = DC.mean_jaccard([keys["DIM_CREDENTIAL"][s][1] for s in common],
                                  [keys["DIM_TRAINING"][s][1] for s in common])
            check(jac is None or jac < 0.99,
                  "证照与规培两个维度不再完全同源（Jaccard=%.3f，修正前为 1.000）"
                  % (jac if jac is not None else -1))
        else:
            check(True, "证照与规培无共同样本，跳过同源性检查")
        flags = {d["dimension_id"]: d["flag"] for d in ort["per_dimension"]}
        check(len(ort["constant_dimensions"]) <= 2,
              "恒定维度不超过 2 个（实得 %d：%s）"
              % (len(ort["constant_dimensions"]),
                 "、".join(d["dimension_id"] for d in ort["constant_dimensions"])))
        check(flags.get("DIM_TRAINING") in ("single-valued-sparse", "ok", "near-binary"),
              "稀疏且单值的维度不被误判成零方差（DIM_TRAINING = %s）"
              % flags.get("DIM_TRAINING"))

        # ===============================================================
        print("\n【R5】真实案例：落库形状、证据链、隐私红线")
        n = q1(c, "SELECT count(*) AS n FROM mt.person WHERE person_id LIKE %s",
               (REAL_PREFIX + "%",))["n"]
        check(n == 3, "三个真实案例已落库（实得 %d）" % n)
        n_pii = q1(c, """SELECT count(*) AS n FROM mt.person_pii
                          WHERE person_id LIKE %s""", (REAL_PREFIX + "%",))["n"]
        check(n_pii == 0, "真实案例不产生任何 person_pii 行（实测 %d）" % n_pii)
        rows = c.execute("""SELECT p.person_id, p.access_tier, p.quality_flags,
                                   count(e.evidence_id) AS n_ev,
                                   count(e.evidence_id) FILTER (WHERE e.uri IS NULL) AS n_no_uri
                              FROM mt.person p LEFT JOIN mt.evidence e ON e.person_id=p.person_id
                             WHERE p.person_id LIKE %s GROUP BY 1,2,3 ORDER BY 1""",
                         (REAL_PREFIX + "%",)).fetchall()
        check(len(rows) == 3, "三个人都有画像行")
        check(all(r["n_ev"] >= 3 for r in rows),
              "每人至少 3 条公开来源（实得 %s）" % [r["n_ev"] for r in rows])
        check(all(r["n_no_uri"] == 0 for r in rows), "每条证据都有真实 URI（没有占位空链）")
        check(all("real_public_case" in (r["quality_flags"] or []) for r in rows),
              "三人都带 real_public_case 自证标记（可一句话筛出对外/不对外）")
        check(all(r["access_tier"] == "T2" for r in rows),
              "可识别的真实个人标为 T2（标签，不是拦截——访问控制仍在路线图里）")
        ev = q1(c, """SELECT count(*) AS n,
                             count(*) FILTER (WHERE e.attrs ? 'source_kind') AS n_kind
                        FROM mt.evidence e WHERE e.person_id LIKE %s""",
                (REAL_PREFIX + "%",))
        check(ev["n"] == ev["n_kind"],
              "每条证据都标了 source_kind（%d/%d）" % (ev["n_kind"], ev["n"]))

        # 画像向量的覆盖必须落在真实数据的量级上 —— 不能悄悄变成合成数据的覆盖率
        for r in rows:
            p, _ = V.person_payloads(c, r["person_id"], dims)
            check(3 <= len(p) <= 20,
                  "%s 的画像向量取到 %d 个维度（真实公开数据应落在 3–20）"
                  % (r["person_id"], len(p)))

        # 日期精度必须显式记录：公开资料只到年/月，补的 -01 不能被当成事实
        prec = q1(c, """SELECT count(*) AS n FROM mt.education_record
                         WHERE person_id LIKE %s AND attrs ? 'edu_date_precision'""",
                  (REAL_PREFIX + "%",))["n"]
        n_edu = q1(c, """SELECT count(*) AS n FROM mt.education_record
                          WHERE person_id LIKE %s""", (REAL_PREFIX + "%",))["n"]
        check(prec == n_edu, "每条教育记录都记了日期精度（%d/%d）" % (prec, n_edu))

        # ===============================================================
        print("\n【R6】真实案例的匹配结果：落库、可解释、且如实标注可评性")
        m = q1(c, """SELECT count(*) AS n,
                            count(*) FILTER (WHERE score_breakdown <> '{}'::jsonb) AS n_bd
                       FROM mt.match_result WHERE person_id LIKE %s""",
               (REAL_PREFIX + "%",))
        check(m["n"] >= 100, "匹配结果已落库（%d 行，每人前 50）" % m["n"])
        check(m["n"] == m["n_bd"], "每行都带逐维度拆解 score_breakdown（%d/%d）"
              % (m["n_bd"], m["n"]))
        run = q1(c, "SELECT status FROM mt.match_run WHERE match_run_id='run_real_cases_015'")
        check(run and run["status"] == "ok", "匹配运行状态为 ok（可追溯到一次运行）")

        # 真实去向的对照表必须与岗位语料实际覆盖一致：没 JD 就不能声称"能评"
        with open(REAL_TARGETS, encoding="utf-8") as fh:
            targets = {x["person_id"]: x for x in json.load(fh)["cases"]}
        check(len(targets) == 3, "对照表覆盖三个案例")
        for pid2, t in targets.items():
            cnt = q1(c, "SELECT count(*) AS n FROM mt.job_posting WHERE occupation_id = ANY(%s)",
                     (t["occupation_ids"],))["n"]
            check(cnt > 0,
                  "%s 的对照节点在语料里有岗位（%d 份）→ 命中率可评" % (pid2, cnt))
            check(bool(t.get("nodes_without_jd")),
                  "%s 写明了语料未覆盖的更贴切节点（否则容易把覆盖缺口读成算法不准）" % pid2)

        # ===============================================================
        print("\n【R7】体检模块的统计量可复算（不是拍脑袋的分数）")
        # 手算一个小例子对照：两个完全同步的类别序列，Cramér's V 应接近 1
        pairs = [(str(i % 2), str(i % 2)) for i in range(60)]
        check(abs(DC.cramers_v(pairs) - 1.0) < 1e-9, "完全同步的类别序列 Cramér's V = 1.0")
        pairs2 = [(str(i % 2), str(i // 2 % 2)) for i in range(60)]
        check(DC.cramers_v(pairs2) < 0.2, "独立类别序列 Cramér's V 接近 0（实得 %.3f）"
              % DC.cramers_v(pairs2))
        check(abs(DC.spearman(list(range(20)), list(range(20))) - 1.0) < 1e-9,
              "单调递增序列 Spearman = 1.0")
        check(abs(DC.spearman(list(range(20)), list(range(19, -1, -1))) + 1.0) < 1e-9,
              "单调递减序列 Spearman = -1.0")
        check(abs(DC.mean_jaccard([frozenset("ab")], [frozenset("ab")]) - 1.0) < 1e-9,
              "相同集合 Jaccard = 1.0")
        # 平均秩：并列必须取平均，否则同分会被算成不同名次
        rk = DC._rank([5, 5, 9])
        check(rk[0] == rk[1] == 1.5 and rk[2] == 3, "并列取平均秩（实得 %s）" % rk)

        # 日期精度归一
        sys.path.insert(0, os.path.join(BASE, "ops", "fixtures"))
        import real_cases as RC  # noqa: E402
        check(RC.norm_date("1993") == ("1993-01-01", "year"), "年精度补到 01-01 并标记 year")
        check(RC.norm_date("2001-06") == ("2001-06-01", "month"), "月精度补到 01 并标记 month")
        check(RC.norm_date("2013-06-08") == ("2013-06-08", "day"), "日精度原样保留")
        check(RC.norm_date(None) == (None, None), "空日期仍为空（不补默认值）")

    return H.report(width=74, list_fails=True)


def sample_persons(c, n=40):
    return [r["person_id"] for r in c.execute(
        "SELECT person_id FROM mt.person ORDER BY person_id LIMIT %s", (n,))]


if __name__ == "__main__":
    sys.exit(main())
