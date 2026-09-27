# -*- coding: utf-8 -*-
"""
code/bridge/projection.py —— 岗位投影与匹配结果回流（T11，契约 §7.2）

契约对这两类数据的硬要求：

  岗位投影
    · 要求必须拆成 must / preferred / unclear，AND/OR 逻辑保留在 requirementLogic
    · 要求节点引用必须存在（不得凭空补年限）
    · salary 保留币种、周期、**口径**（雇主披露 / 平台估算）与来源
    · 只返回 open 且未过截止日的岗位；带 sourceVersion 供下游校验
    · 分页基于 (updatedAt, jobId) 游标，每页至多 20 条

  匹配结果
    · 命中状态分 met / gap / **unknown**——**unknown 不等于不合格**
    · rankingScore 只是排序分，不是录用概率
    · 结果必须带 coverage、sourceFactIndexes、profileVersion、jobSourceVersion，
      用于校验解释是否对应当前事实；过期版本必须丢弃或重算

用法：
  python code/bridge/projection.py --jobs --limit 5
  python code/bridge/projection.py --match per_xxx
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
SCHEMA_VERSION = "1.0.0"
RULE_VERSION = "match_rule@0.1"
PAGE_SIZE = 20
# 契约：must/preferred/unclear 三态
KIND_MAP = {"RK1": "must", "RK2": "preferred", "RK3": "preferred"}


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


# ---------------------------------------------------------------------------
# 岗位投影
# ---------------------------------------------------------------------------
def export_jobs(c, limit: int = PAGE_SIZE, cursor: str = None, family: str = None) -> dict:
    limit = min(limit or PAGE_SIZE, PAGE_SIZE)      # 契约：每页至多 20 项
    sql = """
        SELECT jp.job_id, jp.title_raw, jp.title_normalized, jp.employer_name_raw,
               jp.city, jp.province, jp.job_family, jp.education_req, jp.experience_req,
               jp.salary_min, jp.salary_max, jp.salary_currency, jp.salary_period,
               jp.valid_from, jp.deadline, jp.recorded_at, jp.parse_version,
               jp.source_id, jp.raw_text_ref
        FROM job_posting jp
        WHERE jp.verify_status <> 'V4'
          AND (jp.deadline IS NULL OR jp.deadline >= CURRENT_DATE)
          AND (%(family)s::text IS NULL OR jp.job_family = %(family)s::text)
          AND (%(cur)s::text IS NULL OR (jp.recorded_at, jp.job_id) > (
                 %(cur_ts)s::timestamptz, %(cur_id)s::text))
        ORDER BY jp.recorded_at, jp.job_id
        LIMIT %(lim)s
    """
    cur_ts, cur_id = (cursor or "|").split("|", 1) if cursor else (None, None)
    with c.cursor() as cur:
        cur.execute(sql, {"family": family, "cur": cursor, "cur_ts": cur_ts or None,
                          "cur_id": cur_id or None, "lim": limit + 1})
        rows = cur.fetchall()
        more = len(rows) > limit
        rows = rows[:limit]

        items = []
        for r in rows:
            cur.execute("""SELECT requirement_id, requirement_kind, requirement_type,
                                  concept_id, raw_text, min_level, essentiality
                           FROM job_requirement WHERE job_id=%s
                           ORDER BY requirement_id""", (r["job_id"],))
            reqs = cur.fetchall()
            must, pref, unclear = [], [], []
            for q in reqs:
                node = {"requirementId": q["requirement_id"], "type": q["requirement_type"],
                        "text": q["raw_text"], "conceptId": q["concept_id"]}
                if q["concept_id"] is None:
                    # 契约：要求节点引用必须存在；映射不上时归入 unclear，不臆断
                    unclear.append(node)
                elif KIND_MAP.get(q["requirement_kind"]) == "must":
                    must.append(node)
                else:
                    pref.append(node)
            items.append({
                "jobId": r["job_id"],
                "sourceId": r["source_id"],
                "sourceVersion": r["parse_version"],
                "title": r["title_normalized"] or r["title_raw"],
                "employer": r["employer_name_raw"],
                "city": r["city"],
                "jobFamily": r["job_family"],
                "educationRequired": r["education_req"],
                "experienceRequired": r["experience_req"],
                "salary": {
                    "min": float(r["salary_min"]) if r["salary_min"] is not None else None,
                    "max": float(r["salary_max"]) if r["salary_max"] is not None else None,
                    "currency": r["salary_currency"] or "CNY",
                    "period": r["salary_period"],
                    # 口径：本库薪资来自 JD 原文（雇主披露口径），非平台估算
                    "basis": "employer_disclosed" if r["salary_min"] is not None
                             else "not_disclosed",
                    "sourceRef": r["raw_text_ref"],
                },
                "validity": {
                    "from": r["valid_from"].isoformat() if r["valid_from"] else None,
                    "deadline": r["deadline"].isoformat() if r["deadline"] else None,
                    "status": "open",
                },
                "requirements": {"must": must, "preferred": pref, "unclear": unclear},
                # AND：本库当前把同组要求视为并列满足条件；OR 组由后续版本补充
                "requirementLogic": {"must": "AND", "preferred": "OR",
                                     "unclear": "UNKNOWN"},
                "updatedAt": int(r["recorded_at"].timestamp() * 1000),
            })
    nxt = None
    if more and items:
        last = rows[-1]
        nxt = "%s|%s" % (last["recorded_at"].isoformat(), last["job_id"])
    return {"items": items, "nextCursor": nxt}


# ---------------------------------------------------------------------------
# 匹配：met / gap / unknown
# ---------------------------------------------------------------------------
def compute_matches(c, person_id: str, limit: int = 50) -> dict:
    """规则版匹配。三条纪律：
       · 只有"能力缺口且落在有效观测窗口内"才算 gap；
       · 岗位要求没映射到概念的 → unknown（未知不是不合格）；
       · 结果带 coverage / sourceFactIndexes / 版本，供下游判断是否过期。"""
    with c.cursor() as cur:
        cur.execute("""SELECT assertion_id, concept_id, level, confidence
                       FROM skill_assertion WHERE person_id=%s""", (person_id,))
        mine = {r["concept_id"]: r for r in cur.fetchall() if r["concept_id"]}
        cur.execute("""SELECT 1 FROM observation_window
                       WHERE person_id=%s AND (end_date IS NULL OR end_date >= CURRENT_DATE)
                       LIMIT 1""", (person_id,))
        has_window = cur.fetchone() is not None

        cur.execute("""SELECT profile_version FROM (
                         SELECT max(aggregate_version) AS profile_version
                         FROM response_session WHERE person_id=%s) t""", (person_id,))
        pv = cur.fetchone()["profile_version"] or 0

        cur.execute("""
            SELECT jp.job_id, jp.title_raw, jp.job_family, jp.employer_name_raw, jp.city,
                   jp.recorded_at, jp.parse_version, jr.concept_id, jr.requirement_kind,
                   jr.requirement_id
            FROM job_posting jp JOIN job_requirement jr ON jr.job_id = jp.job_id
            WHERE jp.verify_status <> 'V4'
              AND (jp.deadline IS NULL OR jp.deadline >= CURRENT_DATE)
            ORDER BY jp.job_id""")
        rows = cur.fetchall()

    jobs = {}
    for r in rows:
        jobs.setdefault(r["job_id"], {"meta": r, "reqs": []})["reqs"].append(r)

    out = []
    for job_id, j in jobs.items():
        met, gap, unknown = [], [], []
        for q in j["reqs"]:
            if not q["concept_id"]:
                unknown.append({"requirementId": q["requirement_id"],
                                "reason": "requirement_not_mapped_to_concept"})
            elif q["concept_id"] in mine:
                met.append({"requirementId": q["requirement_id"],
                            "conceptId": q["concept_id"],
                            "sourceFactIndex": mine[q["concept_id"]]["assertion_id"]})
            elif has_window:
                gap.append({"requirementId": q["requirement_id"],
                            "conceptId": q["concept_id"]})
            else:
                # 没有有效观测窗口 → 只能说"未记录"，不能说"不具备"
                unknown.append({"requirementId": q["requirement_id"],
                                "reason": "no_observation_window"})
        total = len(j["reqs"])
        scored = len(met) + len(gap)
        coverage = round(scored / total, 3) if total else 0.0
        # rankingScore 只是排序分（命中占比），不是录用概率
        ranking = round(len(met) / total, 3) if total else 0.0
        if total == 0:
            continue
        out.append({
            "jobId": job_id,
            "jobTitle": j["meta"]["title_raw"],
            "jobFamily": j["meta"]["job_family"],
            # 同一标题可能有多个真实岗位（不同雇主/城市），带上以便区分
            "employer": j["meta"]["employer_name_raw"],
            "city": j["meta"]["city"],
            "met": met, "gap": gap, "unknown": unknown,
            "coverage": coverage,
            "rankingScore": ranking,
            "profileVersion": pv,
            "jobSourceVersion": j["meta"]["parse_version"],
            "ruleVersion": RULE_VERSION,
            "generatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        })
    out.sort(key=lambda x: -x["rankingScore"])
    return {"items": out[:limit], "nextCursor": None, "profileVersion": pv,
            "ruleVersion": RULE_VERSION}


def persist_matches(c, person_id: str, matches: dict) -> int:
    """把匹配结果写入 match_result，供 getMyTalentMatches 读取。"""
    run_id = "mr_" + hashlib.sha1(("%s|%s" % (person_id, RULE_VERSION)).encode()).hexdigest()[:20]
    with c.cursor() as cur:
        cur.execute("""INSERT INTO match_run (match_run_id, algo_version, params,
                          person_count, job_count, status, finished_at)
                       VALUES (%s,%s,%s::jsonb,1,%s,'ok',now())
                       ON CONFLICT (match_run_id) DO UPDATE SET finished_at=now()""",
                    (run_id, RULE_VERSION,
                     json.dumps({"rule": "concept_overlap"}), len(matches["items"])))
        cur.execute("DELETE FROM match_result WHERE person_id=%s AND match_run_id=%s",
                    (person_id, run_id))
        n = 0
        for m in matches["items"]:
            cur.execute("""INSERT INTO match_result (match_id, match_run_id, person_id,
                              target_type, target_id, score_total, score_breakdown,
                              matched_concepts, gap_concepts, explanation, rank)
                           VALUES (%s,%s,%s,'job_posting',%s,%s,%s::jsonb,%s,%s,%s,%s)""",
                        ("mat_" + hashlib.sha1(("%s|%s" % (person_id, m["jobId"])).encode())
                         .hexdigest()[:20], run_id, person_id, m["jobId"],
                         round(m["rankingScore"] * 100, 2),
                         json.dumps({"coverage": m["coverage"],
                                     "met": len(m["met"]), "gap": len(m["gap"]),
                                     "unknown": len(m["unknown"]),
                                     "ruleVersion": m["ruleVersion"],
                                     "jobSourceVersion": m["jobSourceVersion"]},
                                    ensure_ascii=False),
                         [x.get("conceptId") for x in m["met"]],
                         [x.get("conceptId") for x in m["gap"]],
                         "命中 %d 项、缺口 %d 项、未知 %d 项（未知不是不合格）"
                         % (len(m["met"]), len(m["gap"]), len(m["unknown"])), n + 1))
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--family")
    ap.add_argument("--match", metavar="PERSON_ID")
    a = ap.parse_args()

    with conn() as c:
        if a.jobs:
            r = export_jobs(c, a.limit, family=a.family)
            print(json.dumps({"count": len(r["items"]), "nextCursor": r["nextCursor"],
                              "sample": r["items"][0] if r["items"] else None},
                             ensure_ascii=False, indent=2))
            return 0
        if a.match:
            m = compute_matches(c, a.match)
            n = persist_matches(c, a.match, m)
            c.commit()
            print(json.dumps({"personId": a.match, "profileVersion": m["profileVersion"],
                              "matched": len(m["items"]), "persisted": n,
                              "top": m["items"][:2]}, ensure_ascii=False, indent=2))
            return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
