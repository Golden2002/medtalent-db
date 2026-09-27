# -*- coding: utf-8 -*-
"""
code/analytics/dimension_check.py —— 画像维度的三项体检：正交性 / 充分性 / 有效性

为什么要有这个模块
--------------------------------------------------------------------------
"我们设计了 50 个维度"这句话本身不是证据。要能回答三个可证伪的问题：

① **正交性**：这 50 个维度真的在说不同的事吗？
   如果有两个维度在 120 个人身上几乎完全同步（Cramér's V ≈ 1），
   那它们不是两个维度，是一个维度占了两份权重 —— 加权求和时被**重复计权**。
   还有一种更隐蔽的失效：某个维度**所有人取值相同**（零方差）。
   它不制造冗余，但白占一份权重：它能贡献的信息量为 0，却稀释了真正有区分度的维度。

② **充分性**：这些维度够不够描述一个人、够不够跟岗位对上？
   关键不是"人侧填了多少"，而是**两侧都能取到值**的维度有多少、
   它们占总权重的多少。只有人侧有值 = 只能做画像展示，做不了匹配。

③ **有效性**：分数分得开人吗？排出来的岗位对不对？
   · 分不开：690 个岗位里大量并列同分、分数标准差极小 → 排序没有信息量；
   · 排不对：拿**真实案例**（公开履历 + 真实去向）当标尺，
     看真实去向落在第几名。命中就是证据，不命中就是缺口，都要如实报。

统计量全部手写（纯 Python，不引入 numpy/scipy/pandas）：
  · 类别 × 类别 → Cramér's V（卡方 + 偏差校正）
  · 序数 × 序数 → Spearman 秩相关（并列取平均秩）
  · 集合 × 集合 → 平均 Jaccard
理由是这些数字要能被复核：公式在下面每处的注释里，读者可以自己算一遍。

用法：
    python code/analytics/dimension_check.py --all          # 三项全跑，输出 JSON + 摘要
    python code/analytics/dimension_check.py --orthogonality
    python code/analytics/dimension_check.py --sufficiency
    python code/analytics/dimension_check.py --effectiveness [--persons 12]
    python code/analytics/dimension_check.py --all --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # = code/
ROOT = os.path.dirname(BASE)                                          # = 项目根
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vector as V  # noqa: E402

# 判定阈值。都是**约定**，不是自然常数，所以写在这里方便按数据规模调，
# 而不是散落在比较表达式里让人猜。
V_REDUNDANT = 0.75     # Cramér's V 认为"可能重复计权"
RHO_REDUNDANT = 0.90   # Spearman 同上
JACCARD_REDUNDANT = 0.80
V_STRONG = 0.90        # 更强的证据：几乎完全同步


# ---------------------------------------------------------------------------
# 取值形态归一
# ---------------------------------------------------------------------------
def _person_keys(c, dims, persons):
    """把每个人的每个维度归一成可比较的形态。

    返回 {did: {(pid): ('single', v) | ('set', frozenset) | ('num', x) | ('range',(lo,hi))}}
    """
    out = {d["dimension_id"]: {} for d in dims}
    for pid in persons:
        pv, _ = V.person_payloads(c, pid, dims)
        for d in dims:
            did = d["dimension_id"]
            pay = pv.get(did)
            if pay is None:
                continue
            if "codes" in pay and pay["codes"]:
                out[did][pid] = ("set", frozenset(str(x) for x in pay["codes"]))
            elif "code" in pay and pay["code"] is not None:
                out[did][pid] = ("single", str(pay["code"]))
            elif "rank" in pay and pay["rank"] is not None:
                out[did][pid] = ("num", float(pay["rank"]))
            elif "lo" in pay and "hi" in pay:
                out[did][pid] = ("range", (float(pay["lo"]), float(pay["hi"])))
    return out


# ---------------------------------------------------------------------------
# 统计量（手写，公式在注释里）
# ---------------------------------------------------------------------------
def cramers_v(pairs):
    """Cramér's V = sqrt(chi2 / (n * min(r-1, c-1)))，带偏差校正。

    chi2 = sum((O-E)^2/E)，E = 行合计*列合计/n。
    校正版（Bergsma 2013）在小样本、多类别时不会系统性高估 ——
    本库很多维度只有 2–5 个类别、样本 120，必须用校正版。
    """
    n = len(pairs)
    if n < 5:
        return None
    rows = Counter(a for a, _ in pairs)
    cols = Counter(b for _, b in pairs)
    if len(rows) < 2 or len(cols) < 2:
        return None
    cell = Counter(pairs)
    chi2 = 0.0
    # 必须遍历 **r×k 全部格子**，包括观测数为 0 的格子：
    # 空格子对卡方的贡献是 (0-E)^2/E = E，不是 0。
    # 只遍历出现过的格子会把完全相关的 2×2 表的卡方少算一半
    # （实测：chi2 应为 60 却算成 30，V 从 1.0 掉到 0.71）——
    # 这个错是被 ops/tests/real_test.py 的一条手算断言抓出来的。
    for a, ra in rows.items():
        for b, cb in cols.items():
            e = ra * cb / n
            if e > 0:
                o = cell.get((a, b), 0)
                chi2 += (o - e) ** 2 / e
    r, k = len(rows), len(cols)
    phi2 = chi2 / n
    phi2c = max(0.0, phi2 - (r - 1) * (k - 1) / (n - 1))
    rc = r - (r - 1) ** 2 / (n - 1)
    kc = k - (k - 1) ** 2 / (n - 1)
    denom = min(rc - 1, kc - 1)
    if denom <= 0:
        return None
    return math.sqrt(phi2c / denom)


def _rank(vals):
    """平均秩（并列取平均）—— Spearman 必须这样处理并列，否则同分会被算成不同名次。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman rho = Pearson(rank(x), rank(y))。"""
    n = len(xs)
    if n < 5:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def mean_jaccard(sets_a, sets_b):
    """平均 Jaccard = |A∩B| / |A∪B|，逐人算再取均值。空集对跳过。"""
    vals = []
    for a, b in zip(sets_a, sets_b):
        if not a and not b:
            continue
        u = len(a | b)
        if u:
            vals.append(len(a & b) / u)
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------------------
# ① 正交性
# ---------------------------------------------------------------------------
def orthogonality(c, dims=None, persons=None):
    dims = dims if dims is not None else V.load_dims(c)
    persons = persons or [r["person_id"] for r in
                          c.execute("SELECT person_id FROM mt.person ORDER BY person_id")]
    keys = _person_keys(c, dims, persons)
    n = len(persons)

    per_dim, constant = [], []
    for d in dims:
        did = d["dimension_id"]
        kv = keys[did]
        vals = list(kv.values())
        distinct = len(set(repr(v) for v in vals))
        row = {"dimension_id": did, "group_id": d["group_id"], "title": d["title_zh"],
               "kind": d["kind"], "role": d["role"], "weight": float(d["weight"]),
               "filled": len(vals), "filled_pct": round(100.0 * len(vals) / n, 1),
               "distinct_values": distinct}
        # 三种"信息量低"要分开，因为它们要采取的行动不同：
        #   · constant            几乎所有人同值 → 真的没有区分度；
        #   · single-valued-sparse 只有少数人有值且值相同 → 是**覆盖稀疏**，
        #     不是"这个维度没用"。把它叫零方差会误导人去砍维度，
        #     而正确的动作是把数据填起来。（第一版就是这么误判 DIM_TRAINING 的：
        #     只有 24/120 人有 C02，值确实都一样，但那说明的是样本稀疏。）
        #   · near-binary         只有两个取值且几乎人人都有 → 区分度有限但真实。
        if not vals:
            row["flag"] = "empty"
        elif distinct == 1 and len(vals) >= 0.9 * n:
            row["flag"] = "constant"
            constant.append(row)
        elif distinct == 1:
            row["flag"] = "single-valued-sparse"
        elif distinct == 2 and len(vals) >= 0.9 * n:
            row["flag"] = "near-binary"
        else:
            row["flag"] = "ok"
        per_dim.append(row)

    pairs = []
    ds = [d for d in dims]
    for i in range(len(ds)):
        for j in range(i + 1, len(ds)):
            a, b = ds[i]["dimension_id"], ds[j]["dimension_id"]
            common = [p for p in persons if p in keys[a] and p in keys[b]]
            if len(common) < 5:
                continue
            ka = [keys[a][p] for p in common]
            kb = [keys[b][p] for p in common]
            kinds = {x[0] for x in ka} | {x[0] for x in kb}
            stat, kind = None, None
            if kinds == {"single"}:
                kind = "cramers_v"
                stat = cramers_v(list(zip([x[1] for x in ka], [x[1] for x in kb])))
                thr = V_REDUNDANT
            elif kinds == {"num"}:
                kind = "spearman"
                stat = spearman([x[1] for x in ka], [x[1] for x in kb])
                thr = RHO_REDUNDANT
            elif kinds == {"set"}:
                kind = "jaccard"
                stat = mean_jaccard([x[1] for x in ka], [x[1] for x in kb])
                thr = JACCARD_REDUNDANT
            else:
                continue
            if stat is None:
                continue
            if abs(stat) >= thr:
                pairs.append({"a": a, "b": b, "stat": kind, "value": round(stat, 3),
                              "n": len(common),
                              "verdict": "强冗余" if abs(stat) >= V_STRONG else "疑似冗余"})
    pairs.sort(key=lambda x: -abs(x["value"]))
    return {"n_person": n, "per_dimension": per_dim,
            "constant_dimensions": constant, "redundant_pairs": pairs,
            "thresholds": {"cramers_v": V_REDUNDANT, "spearman": RHO_REDUNDANT,
                           "jaccard": JACCARD_REDUNDANT}}


# ---------------------------------------------------------------------------
# ② 充分性
# ---------------------------------------------------------------------------
def sufficiency(c, dims=None, persons=None, jobs=None):
    cov = V.coverage(c, dims, persons)
    dims = dims if dims is not None else V.load_dims(c)
    w_by_id = {d["dimension_id"]: float(d["weight"]) for d in dims}

    two = [d for d in cov["dims"] if d["person_ok"] and d["job_ok"]]
    person_only = [d for d in cov["dims"] if d["person_ok"] and not d["job_ok"]]
    neither = [d for d in cov["dims"] if not d["person_ok"] and not d["job_ok"]]
    w_all = sum(w_by_id.values())
    w_two = sum(w_by_id[d["dimension_id"]] for d in two)
    gates = [d for d in cov["dims"] if d["role"] == "gate"]
    gates_ok = [d for d in gates if d["person_ok"] and d["job_ok"]]
    # 分数维（真正决定排序的那些）里有多少两侧可用 —— 门禁只负责淘汰，不决定排序。
    score_dims = [d for d in cov["dims"] if d["role"] == "score"]
    score_two = [d for d in score_dims if d["person_ok"] and d["job_ok"]]
    w_score_two = sum(w_by_id[d["dimension_id"]] for d in score_two)
    return {
        "n_person": cov["n_person"], "n_job": cov["n_job"],
        "n_dimension": len(cov["dims"]),
        "two_sided": sorted(d["dimension_id"] for d in two),
        "person_only": sorted(d["dimension_id"] for d in person_only),
        "neither": sorted(d["dimension_id"] for d in neither),
        "weight_total": round(w_all, 3),
        "weight_two_sided": round(w_two, 3),
        "weight_two_sided_pct": round(100.0 * w_two / w_all, 1) if w_all else None,
        "gates_total": len(gates),
        "gates_evaluable": [d["dimension_id"] for d in gates_ok],
        "gates_not_evaluable": sorted(d["dimension_id"] for d in gates
                                      if d["dimension_id"] not in
                                      {x["dimension_id"] for x in gates_ok}),
        "score_dims_two_sided": sorted(d["dimension_id"] for d in score_two),
        "weight_score_two_sided": round(w_score_two, 3),
        "weight_score_two_sided_pct": (round(100.0 * w_score_two / w_all, 1)
                                       if w_all else None),
        "per_dimension": cov["dims"],
    }


# ---------------------------------------------------------------------------
# ③ 有效性
# ---------------------------------------------------------------------------
def effectiveness(c, dims=None, persons=None, sample=12, target_pids=None):
    """分数分布 + 真实案例命中率。

    真实案例的"真实去向"怎么对比：`case_targets` 由调用方给出
    {person_id: {'occupation_ids': [...], 'labels': [...]}}。
    命中 = 该去向出现在 top-k 的岗位里。
    **注意**：真实去向未必在 690 份 JD 里 —— 那就不是"没匹配上"，
    而是"岗位库没覆盖"，这两种要分开报，否则会把自己的覆盖缺口说成算法不准。
    """
    dims = dims if dims is not None else V.load_dims(c)
    jobs = c.execute("SELECT job_id, title_raw, occupation_id, city, salary_min, "
                     "salary_max FROM mt.job_posting ORDER BY job_id").fetchall()
    jobvecs = V.job_vectors_all(c, dims)
    persons = persons or [r["person_id"] for r in
                          c.execute("SELECT person_id FROM mt.person ORDER BY person_id")]
    step = max(1, len(persons) // sample)
    probe = persons[::step][:sample]

    dist = {"n_person": len(probe), "n_job": len(jobs), "per_person": []}
    all_scores = []
    for pid in probe:
        rows = V.rank_jobs(c, pid, limit=0, dims=dims, jobvecs=jobvecs, jobs=jobs)
        sc = [r["score"] for r in rows if r["score"] is not None]
        covs = [r["coverage"] for r in rows if r["coverage"] is not None]
        if not sc:
            continue
        all_scores.extend(sc)
        top = sc[0]
        tie = sum(1 for s in sc if abs(s - top) < 1e-9)
        mean = sum(sc) / len(sc)
        var = sum((x - mean) ** 2 for x in sc) / len(sc)
        dist["per_person"].append({
            "person_id": pid, "n_scored": len(sc),
            "distinct_scores": len(set(sc)),
            "top_score": round(top, 4), "top_tie_size": tie,
            "median": round(sorted(sc)[len(sc) // 2], 4),
            "std": round(math.sqrt(var), 4),
            "mean_coverage": round(sum(covs) / len(covs), 4) if covs else None,
            "blocked_all": all(r["gate_failed"] for r in rows),
        })
    if all_scores:
        m = sum(all_scores) / len(all_scores)
        dist["overall"] = {
            "n_scores": len(all_scores),
            "mean": round(m, 4),
            "std": round(math.sqrt(sum((x - m) ** 2 for x in all_scores) / len(all_scores)), 4),
            "distinct": len(set(all_scores)),
            "max": round(max(all_scores), 4), "min": round(min(all_scores), 4),
        }

    # ---- 真实案例命中率 ----
    tgt_path = os.path.join(ROOT, "ops", "fixtures", "real_targets.json")
    targets = {}
    if os.path.isfile(tgt_path):
        with open(tgt_path, encoding="utf-8") as fh:
            tj = json.load(fh)
        for x in tj.get("cases", []):
            targets[x["person_id"]] = x
    hits = []
    for pid in (target_pids or sorted(targets)):
        want = targets.get(pid) or {}
        rows = V.rank_jobs(c, pid, limit=0, dims=dims, jobvecs=jobvecs, jobs=jobs)
        occ = want.get("occupation_ids") or []
        labels = want.get("labels") or []
        # 岗位库里有没有这个去向：先看 occupation_id 是否被任何 JD 引用
        present = 0
        if occ:
            present = c.execute(
                "SELECT count(*) AS n FROM mt.job_posting WHERE occupation_id = ANY(%s)",
                (occ,)).fetchone()["n"]
        rank = None
        for r in rows:
            if occ and r["occupation_id"] in occ:
                rank = r["rank"]
                break
        sc = [r["score"] for r in rows if r["score"] is not None]
        top = sc[0] if sc else None
        tie = (sum(1 for s in sc if abs(s - top) < 1e-9) if top is not None else None)
        # 命中要看并列规模：如果 690 个岗位全部同分，那"排第 1"是 job_id 的字典序，
        # 不是模型的判断。把这种情况报成"命中"就是拿巧合当能力。
        meaningful = bool(tie is not None and tie <= 20)
        hits.append({"person_id": pid, "target_occupations": occ, "target_labels": labels,
                     "actual_path": want.get("actual_path"),
                     "mapping_reason": want.get("mapping_reason"),
                     "nodes_without_jd": want.get("nodes_without_jd") or [],
                     "target_present_in_job_library": present,
                     # 能不能评，和评出来几分，是两件事，必须分开报
                     "evaluable": bool(present),
                     "top_tie_size": tie,
                     "hit_meaningful": meaningful,
                     "best_rank_of_target": rank,
                     "top1": rows[0]["title"] if rows else None,
                     "top5": [{"rank": r["rank"], "title": r["title"],
                               "occupation_id": r["occupation_id"],
                               "score": r["score"], "coverage": r["coverage"]}
                              for r in rows[:5]],
                     "hit_at": {k: bool(rank and rank <= k) for k in (1, 5, 10, 50)}})
    return {"distribution": dist, "real_cases": hits,
            "targets_file": os.path.relpath(tgt_path, ROOT).replace("\\", "/"),
            "targets_loaded": len(targets)}


# 由 real_cases fixture 在导入时填；effectiveness 用它做标尺。
TARGETS: dict = {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def summary_text(res):
    L = []
    if "orthogonality" in res:
        o = res["orthogonality"]
        L.append("【正交性】%d 人" % o["n_person"])
        const = o["constant_dimensions"]
        sparse = [d for d in o["per_dimension"] if d["flag"] == "single-valued-sparse"]
        # 只有"两侧都能取值"的恒定维度才真的白占权重：单侧恒定根本进不了打分分母。
        two_sided = set((res.get("sufficiency") or {}).get("two_sided") or [])
        wasted = [d for d in const if not two_sided or d["dimension_id"] in two_sided]
        L.append("  恒定维度（>=90%% 的人同值）：%d 个；其中两侧可评=真正白占权重：%d 个"
                 % (len(const), len(wasted) if two_sided else len(const)))
        for d in const[:8]:
            L.append("    · %-26s w=%-5s filled=%5.1f%%  %s"
                     % (d["dimension_id"], d["weight"], d["filled_pct"], d["title"]))
        if sparse:
            L.append("  稀疏且单值（是覆盖不足，不是没区分度，别误砍）：%d 个" % len(sparse))
            for d in sparse[:6]:
                L.append("    · %-26s w=%-5s 只有 %.1f%% 的人有值  %s"
                         % (d["dimension_id"], d["weight"], d["filled_pct"], d["title"]))
        L.append("  疑似重复计权的维度对：%d 对" % len(o["redundant_pairs"]))
        for p in o["redundant_pairs"][:8]:
            L.append("    · %-24s × %-24s %s=%.3f (%s)"
                     % (p["a"], p["b"], p["stat"], p["value"], p["verdict"]))
    if "sufficiency" in res:
        s = res["sufficiency"]
        L.append("【充分性】%d 维度 / %d 人 / %d 岗位" % (s["n_dimension"], s["n_person"], s["n_job"]))
        L.append("  两侧都有值：%d 个，占总权重 %.1f%%（%.2f / %.2f）"
                 % (len(s["two_sided"]), s["weight_two_sided_pct"] or 0,
                    s["weight_two_sided"], s["weight_total"]))
        L.append("  只有人侧有值：%d 个；两侧都没有：%d 个"
                 % (len(s["person_only"]), len(s["neither"])))
        L.append("  score 维度里两侧可用：%d 个，占权重 %.1f%%"
                 % (len(s["score_dims_two_sided"]),
                    s["weight_score_two_sided_pct"] or 0))
        L.append("  门禁维度 %d 个，其中两侧可评：%d 个；不可评：%s"
                 % (s["gates_total"], len(s["gates_evaluable"]),
                    "、".join(s["gates_not_evaluable"]) or "无"))
    if "effectiveness" in res:
        e = res["effectiveness"]["distribution"]
        L.append("【有效性】抽样 %d 人 × %d 岗位" % (e["n_person"], e["n_job"]))
        if e.get("overall"):
            ov = e["overall"]
            L.append("  得分分布：均值 %.3f 标准差 %.3f 极差 [%.3f, %.3f] 不同取值 %d 个 / %d 条"
                     % (ov["mean"], ov["std"], ov["min"], ov["max"],
                        ov["distinct"], ov["n_scores"]))
        for p in e["per_person"][:6]:
            L.append("    · %-16s 首位 %.3f（并列 %d 个）中位 %.3f 可评覆盖 %s 全部被门槛挡=%s"
                     % (p["person_id"], p["top_score"], p["top_tie_size"], p["median"],
                        p["mean_coverage"], p["blocked_all"]))
        for h in res["effectiveness"]["real_cases"]:
            L.append("  %s" % h["person_id"])
            L.append("    真实去向：%s" % (h.get("actual_path") or "-"))
            L.append("    对照节点：%s（岗位库里 %d 份 JD）→ 首次出现名次 %s；hit@1=%s hit@5=%s"
                     % ("、".join(h["target_labels"]) or "-",
                        h["target_present_in_job_library"], h["best_rank_of_target"],
                        h["hit_at"][1], h["hit_at"][5]))
            if not h["evaluable"]:
                L.append("    ⚠ 该去向在 690 份 JD 里**没有岗位** → 这一条**无法评价匹配效果**，")
                L.append("      这是语料覆盖缺口，不是算法判错。缺的节点：%s"
                         % "、".join(h.get("nodes_without_jd") or ["-"]))
            L.append("    首位并列 %s 个；实际首位：%s" % (h["top_tie_size"], h["top1"]))
            if h["hit_at"][1] and not h.get("hit_meaningful", True):
                L.append("    ⚠ 虽然命中第 1 名，但并列 %d 个 → **不构成有效性证据**："
                         "名次是 ID 字典序，不是模型判断。" % (h["top_tie_size"] or 0))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--orthogonality", action="store_true")
    ap.add_argument("--sufficiency", action="store_true")
    ap.add_argument("--effectiveness", action="store_true")
    ap.add_argument("--persons", type=int, default=12,
                    help="有效性抽样多少人（每个都要对全部岗位打分）")
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()
    if not any([a.all, a.orthogonality, a.sufficiency, a.effectiveness]):
        a.all = True

    res = {}
    with V.connect() as c:
        dims = V.load_dims(c)
        if a.all or a.orthogonality:
            res["orthogonality"] = orthogonality(c, dims)
        if a.all or a.sufficiency:
            res["sufficiency"] = sufficiency(c, dims)
        if a.all or a.effectiveness:
            res["effectiveness"] = effectiveness(c, dims, sample=a.persons)
    print(summary_text(res))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print("\n[JSON] %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
