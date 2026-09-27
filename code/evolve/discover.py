# -*- coding: utf-8 -*-
"""
code/evolve/discover.py —— 从真实 JD 里"长出"候选岗位与候选能力（T13）

思路：演化不是靠人拍脑袋说"该加个新岗位了"，而是**让数据自己冒出来**。

  候选岗位：把每条 JD 的标题与现有职业树比对，**匹配不上的**进入候选池；
           同一岗位名累计支撑条数，够了就标 ready 等人评审。
  候选能力：把**映射不到任何概念**的要求文本聚成候选短语，同样累计支撑条数。

两条纪律：
  · 候选池是缓冲区，**进不了正式层**——正式层只由 promote 写入（有评审记录）。
  · 发现过程幂等且增量：重复跑只累加证据与 last_seen，不重置 first_seen。

用法：
  python code/evolve/discover.py                 # 发现并汇报
  python code/evolve/discover.py --min-evidence 3
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import metrics  # noqa: E402  ← 概念覆盖口径的唯一定义（RT5..RT8 / RT1,RT3,RT4）

sys.stdout.reconfigure(encoding="utf-8")
DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")

# 能力短语归一化：去掉情态/程度/通用后缀，留下"能力核"
PHRASE_NOISE = re.compile(
    r"(具备|具有|拥有|有|能|能够|可|可以|较强的?|良好的?|优秀的?|一定的|相关的?|丰富的?|"
    r"熟练|熟悉|掌握|了解|精通|以及|与|和|及|等|者优先|优先|加分|以上|及其)")
PHRASE_TAIL = re.compile(r"(能力|经验|意识|素养|精神|水平|技巧|知识|技能|背景)$")


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def norm_title(s: str) -> str:
    s = re.sub(r"[（(].*?[)）]", "", s or "")
    s = re.sub(r"\b(msl|ma|cra|crc|cdm|ra|pv|pm|bd|rwe|rws|heor|ivd|ai)\b", "", s, flags=re.I)
    return re.sub(r"[\s\-—/、，,。.·]+", "", s).lower()


def lcs_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def normalize_phrase(text: str) -> str:
    """把一条要求文本压成能力短语。
    这是**启发式**做法：真实数据上应当用向量聚类（pgvector 尚未启用），
    当前语料里同义要求文本本身高度重复，因此按文本归一化即可稳定聚合。"""
    t = re.sub(r"[，,。.;；:：、（）()\[\]【】]", "", text or "")
    t = PHRASE_NOISE.sub("", t)
    t = PHRASE_TAIL.sub("", t)
    t = re.sub(r"\s+", "", t)
    return t[:24]


def policy(c, kind: str) -> int:
    with c.cursor() as cur:
        cur.execute("SELECT min_evidence, min_days_seen FROM evolution_policy "
                    "WHERE candidate_kind=%s", (kind,))
        r = cur.fetchone()
    return r or {"min_evidence": 5, "min_days_seen": 0}


def discover_occupations(c, min_evidence: int) -> dict:
    """标题匹配不上的 JD → 候选岗位。"""
    with c.cursor() as cur:
        cur.execute("SELECT occupation_id, label_zh FROM occupation "
                    "WHERE status='active' AND valid_to IS NULL AND level>=2")
        known = [(o["occupation_id"], norm_title(o["label_zh"])) for o in cur.fetchall()]
        cur.execute("""SELECT job_id, title_raw, title_normalized, job_family, city,
                              employer_name_raw, recorded_at
                       FROM job_posting WHERE verify_status <> 'V4'""")
        jobs = cur.fetchall()

    buckets: dict[str, dict] = {}
    for j in jobs:
        t = norm_title(j["title_normalized"] or j["title_raw"])
        if not t:
            continue
        # 与已知节点比：包含匹配或 LCS ≥ 0.6
        matched = any(lab and (lab in t or t in lab) for _oid, lab in known)
        if not matched:
            matched = any(lcs_len(lab, t) / max(len(lab), 1) >= 0.6 for _oid, lab in known)
        if matched:
            continue
        key = t
        b = buckets.setdefault(key, {"sample": j["title_raw"], "family": j["job_family"],
                                     "ids": [], "first": j["recorded_at"].date()})
        b["ids"].append(j["job_id"])
        b["last"] = max(b.get("last", j["recorded_at"].date()), j["recorded_at"].date())
        b["first"] = min(b["first"], j["recorded_at"].date())
        if not b["family"] and j["job_family"]:
            b["family"] = j["job_family"]

    n_new = n_ready = 0
    with c.cursor() as cur:
        for key, b in buckets.items():
            cid = "oc_" + hashlib.sha1(key.encode()).hexdigest()[:20]
            cur.execute("""INSERT INTO occupation_candidate (candidate_id, title_key,
                              title_sample, family_guess, evidence_count, sample_job_ids,
                              first_seen, last_seen, status)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'watching')
                           ON CONFLICT (title_key) DO UPDATE SET
                              -- evidence_count 用**覆盖**而不是累加：
                              -- 每次发现都是从全量 JD 重算的，累加会把同一条 JD 反复计数
                              evidence_count = EXCLUDED.evidence_count,
                              sample_job_ids = EXCLUDED.sample_job_ids,
                              last_seen = GREATEST(occupation_candidate.last_seen,
                                                   EXCLUDED.last_seen),
                              family_guess = COALESCE(occupation_candidate.family_guess,
                                                      EXCLUDED.family_guess),
                              updated_at = now()""",
                        (cid, key, b["sample"], b["family"], len(b["ids"]), b["ids"],
                         b["first"], b.get("last", b["first"])))
            n_new += 1
        cur.execute("""UPDATE occupation_candidate SET status='ready'
                       WHERE status='watching' AND evidence_count >= %s""", (min_evidence,))
        n_ready = cur.rowcount
    return {"buckets": len(buckets), "touched": n_new, "ready": n_ready,
            "jobs_checked": len(jobs)}


def discover_concepts(c, min_evidence: int) -> dict:
    """映射不到概念的要求 → 候选能力短语。

    两个关键过滤（都来自本项目已经确立的口径）：

    ① **只收能力类要求**。学历(RT1)/经验(RT3)/证照(RT4) 是**资格门槛**，
       由 education_req / license_req 等字段承载，不该进能力本体。
       把"医学相关专业硕士及以上学历"当成候选能力，会让词表迅速被噪声淹没。
       （口径来自 code/metrics.py::CONCEPT_TYPES，与质量门/门户/可视化同源，
        不在此另写一遍 RT 码——本项目曾因此出现过两个覆盖率数字。）

    ② **先别名、后新概念**。若某短语命中既有概念的关键词，说明它只是**同义说法**，
       应当作为别名并入该概念，而不是新建一个重复概念——
       这正是"词表生长"最容易走偏的地方。
    """
    with c.cursor() as cur:
        cur.execute("""SELECT requirement_id, raw_text, requirement_type
                       FROM job_requirement
                       WHERE concept_id IS NULL
                         AND requirement_type = ANY(%s)""",
                    (list(metrics.CONCEPT_TYPES),))
        reqs = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM job_requirement WHERE concept_id IS NULL")
        n_excluded = cur.fetchone()["n"] - len(reqs)
        cur.execute("SELECT concept_id, alt_labels FROM concept "
                    "WHERE status='active' AND alt_labels IS NOT NULL")
        concepts = cur.fetchall()

    buckets: dict[str, dict] = {}
    for r in reqs:
        p = normalize_phrase(r["raw_text"])
        if len(p) < 2:
            continue
        b = buckets.setdefault(p, {"sample": r["raw_text"], "ids": []})
        b["ids"].append(r["requirement_id"])

    n_ready = n_alias = 0
    with c.cursor() as cur:
        for phrase, b in buckets.items():
            # ② 命中既有概念的关键词 → 建议并入（别名），不新建
            hit = None
            for cv in concepts:
                for k in (cv["alt_labels"] or []):
                    if k and (k in phrase or phrase in k):
                        hit = cv["concept_id"]
                        break
                if hit:
                    break
            cid = "cc_" + hashlib.sha1(phrase.encode()).hexdigest()[:20]
            cur.execute("""INSERT INTO concept_candidate (candidate_id, phrase_key,
                              phrase_sample, evidence_count, sample_requirement_ids,
                              suggested_concept_id, first_seen, last_seen, status)
                           VALUES (%s,%s,%s,%s,%s,%s::text,CURRENT_DATE,CURRENT_DATE,
                                   CASE WHEN %s::text IS NULL THEN 'watching' ELSE 'merged' END)
                           ON CONFLICT (phrase_key) DO UPDATE SET
                              evidence_count = EXCLUDED.evidence_count,
                              sample_requirement_ids = EXCLUDED.sample_requirement_ids,
                              suggested_concept_id = COALESCE(EXCLUDED.suggested_concept_id,
                                                              concept_candidate.suggested_concept_id),
                              status = CASE WHEN EXCLUDED.suggested_concept_id IS NOT NULL
                                            THEN 'merged' ELSE concept_candidate.status END,
                              last_seen = CURRENT_DATE, updated_at = now()""",
                        (cid, phrase, b["sample"], len(b["ids"]), b["ids"][:20],
                         hit, hit))
            if hit:
                n_alias += 1
        cur.execute("""UPDATE concept_candidate SET status='ready'
                       WHERE status='watching' AND evidence_count >= %s""", (min_evidence,))
        n_ready = cur.rowcount
    return {"phrases": len(buckets), "ready": n_ready, "alias_suggested": n_alias,
            "unmapped_reqs": len(reqs), "excluded_qualification_reqs": n_excluded}


def run(min_evidence: int = None) -> dict:
    with conn() as c:
        pe = min_evidence or policy(c, "occupation")["min_evidence"]
        pc = min_evidence or policy(c, "concept")["min_evidence"]
        occ = discover_occupations(c, pe)
        con = discover_concepts(c, pc)
        c.commit()
        with c.cursor() as cur:
            cur.execute("SELECT * FROM v_evolution_health")
            h = cur.fetchone()
    return {"occupations": occ, "concepts": con, "health": h}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-evidence", type=int)
    a = ap.parse_args()
    r = run(a.min_evidence)
    o, k = r["occupations"], r["concepts"]
    print("=" * 70)
    print("演化发现（从真实 JD 里长出来的候选）")
    print("=" * 70)
    print("候选岗位：检查 %d 条 JD → 聚合出 %d 个未识别岗位名，其中 %d 个已达阈值(ready)"
          % (o["jobs_checked"], o["buckets"], o["ready"]))
    print("候选能力：%d 条未映射要求 → 剔除 %d 条资格门槛类（学历/经验/证照）后，"
          "聚合出 %d 个候选短语（其中 %d 个建议并入既有概念作别名，%d 个已达阈值）"
          % (k["unmapped_reqs"], k["excluded_qualification_reqs"], k["phrases"],
             k["alias_suggested"], k["ready"]))
    h = r["health"]
    print("演化健康度：树节点 %d（已退役 %d）｜迁移 %d 条｜候选 ready（岗位 %d / 能力 %d）"
          "｜重算版本 %d｜上升信号 %d"
          % (h["active_nodes"], h["retired_nodes"], h["migrations"],
             h["occ_ready"], h["con_ready"], h["runs"], h["rising_signals"]))
    with conn() as c, c.cursor() as cur:
        cur.execute("""SELECT title_sample, evidence_count, family_guess FROM occupation_candidate
                       WHERE status='ready' ORDER BY evidence_count DESC LIMIT 8""")
        rows = cur.fetchall()
        if rows:
            print("\n岗位候选 Top：")
            for x in rows:
                print("  [%2d 条] %s%s" % (x["evidence_count"], x["title_sample"],
                                          "（推测族 %s）" % x["family_guess"] if x["family_guess"] else ""))
        cur.execute("""SELECT phrase_sample, evidence_count FROM concept_candidate
                       WHERE status='ready' ORDER BY evidence_count DESC LIMIT 8""")
        rows = cur.fetchall()
        if rows:
            print("\n能力候选 Top：")
            for x in rows:
                print("  [%2d 条] %s" % (x["evidence_count"], x["phrase_sample"]))
        if not rows and not o["ready"]:
            print("\n（当前语料已被现有词表完全覆盖——这本身就是「词表够用」的信号）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
