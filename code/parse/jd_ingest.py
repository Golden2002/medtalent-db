# -*- coding: utf-8 -*-
"""
code/parse/jd_ingest.py —— 把 L0 原始招聘页解析入库（T08）

流程：
  L0 原始 HTML（只读）
    → jd_parser 规则抽取
    → job_posting（主干）+ job_task（职责）+ job_requirement（要求）
    → 与 provenance 建立血缘（记录 L0 路径、URL、证据分级）

三条纪律：
  · **原文必须保留**：raw_text_ref 指向 L0 文件，raw_sha256 记录内容哈希；
    每条 requirement 都带 raw_text 原文片段。
  · **机器解析结果一律 V1**，confidence ≤ 0.95，并记录 parse_version，便于换解析器重跑。
  · **可重跑**：同一份 L0 重复解析会覆盖该 job 的职责与要求（先删后插），不产生重复。

用法：
  python code/parse/jd_ingest.py --source src_fixture_careers
  python code/parse/jd_ingest.py --source src_fixture_careers --limit 50
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from jd_parser import PARSER_VERSION, parse  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
RAW_ROOT = os.path.join(BASE, "data", "raw")


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def l0_files(source_id: str) -> list[str]:
    root = os.path.join(RAW_ROOT, source_id)
    out = []
    for r, _, fs in os.walk(root):
        out += [os.path.join(r, f) for f in fs if f.endswith(".html")]
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    files = l0_files(a.source)
    if a.limit:
        files = files[:a.limit]
    if not files:
        print("[X] 没有可解析的 L0 文件：data/raw/%s" % a.source)
        return 1

    n_ok = n_skip = n_err = 0
    n_task = n_req = 0
    with conn() as c:
        # L0 路径 → 原始 URL / 批次（血缘反查）
        with c.cursor() as cur:
            cur.execute("""SELECT note, source_url, ingest_run_id, evidence_grade
                           FROM provenance WHERE source_id=%s AND note IS NOT NULL""", (a.source,))
            prov = {r["note"]: r for r in cur.fetchall()}

        for i, path in enumerate(files, 1):
            rel = os.path.relpath(path, BASE)
            p = prov.get(rel)
            if not p:
                n_skip += 1
                continue
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                sha = hashlib.sha256(raw).hexdigest()
                r = parse(raw.decode("utf-8", "replace"))
                if not r["title_raw"]:
                    n_skip += 1
                    continue

                job_id = "job_" + sha[:20]
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO job_posting (
                          job_id, source_id, ingest_run_id, source_url, employer_name_raw,
                          title_raw, title_normalized, city,
                          salary_min, salary_max, salary_currency, salary_period,
                          education_req, experience_req, headcount,
                          raw_text_ref, raw_sha256, parse_version, confidence,
                          verify_status, quality_flags)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CNY',%s,%s,%s,%s,%s,%s,%s,%s,'V1',%s)
                        ON CONFLICT (job_id) DO UPDATE SET
                          ingest_run_id=EXCLUDED.ingest_run_id,
                          source_url=EXCLUDED.source_url,
                          salary_min=EXCLUDED.salary_min, salary_max=EXCLUDED.salary_max,
                          salary_period=EXCLUDED.salary_period,
                          education_req=EXCLUDED.education_req,
                          experience_req=EXCLUDED.experience_req,
                          headcount=EXCLUDED.headcount,
                          parse_version=EXCLUDED.parse_version,
                          confidence=EXCLUDED.confidence,
                          quality_flags=EXCLUDED.quality_flags,
                          recorded_at=now()""",
                        (job_id, a.source, p["ingest_run_id"], p["source_url"],
                         r["employer_name_raw"], r["title_raw"], r["title_raw"], r["city"],
                         r["salary_min"], r["salary_max"], r["salary_period"],
                         r["education_req"], r["experience_req"], r["headcount"],
                         rel, sha, r["parse_version"], r["confidence"],
                         r["quality_flags"]))

                    # 可重跑：先清掉该 job 的旧职责与要求，再重插
                    cur.execute("DELETE FROM job_task WHERE job_id=%s", (job_id,))
                    cur.execute("DELETE FROM job_requirement WHERE job_id=%s", (job_id,))

                    for k, d in enumerate(r["duties"], 1):
                        cur.execute("""INSERT INTO job_task (task_id, job_id, task_order,
                                          task_text, importance)
                                       VALUES (%s,%s,%s,%s,%s)""",
                                    ("jbt_%s_%02d" % (sha[:16], k), job_id, k, d, 0.8))
                        n_task += 1
                    for k, q in enumerate(r["requirements"], 1):
                        cur.execute("""INSERT INTO job_requirement (requirement_id, job_id,
                                          requirement_kind, requirement_type, raw_text,
                                          min_level, essentiality, substitutable_by)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb)""",
                                    ("req_%s_%02d" % (sha[:16], k), job_id,
                                     q["requirement_kind"], q["requirement_type"],
                                     q["raw_text"], None,
                                     {"RK1": 1.0, "RK2": 0.6, "RK3": 0.3}[q["requirement_kind"]]))
                        n_req += 1
                n_ok += 1
                if i % 50 == 0 or i == len(files):
                    print("    %d/%d  已入库 %d（职责 %d，要求 %d）"
                          % (i, len(files), n_ok, n_task, n_req))
            except Exception as e:  # noqa: BLE001
                n_err += 1
                print("    [失败] %s -> %s: %s" % (rel, type(e).__name__, str(e)[:140]))

        c.commit()
        print("\n[✓] 解析入库完成：成功 %d，跳过 %d，失败 %d" % (n_ok, n_skip, n_err))
        print("    job_task %d 条，job_requirement %d 条，解析器 %s" % (n_task, n_req, PARSER_VERSION))
        # 重要提醒：本脚本会为每个 job **先删后插** job_requirement，
        # 因此概念映射（job_requirement.concept_id）与能力权重矩阵都会被清空。
        # 这是刻意的（映射是派生层，可重算），但必须紧接着重建，否则匹配会全是 unknown。
        print("    [!] 下一步必须重建派生层：python code\\analytics\\build_competency.py")
        print("        （概念映射与能力权重存在 job_requirement.concept_id 上，重解析会重置它们）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
