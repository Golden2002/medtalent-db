# -*- coding: utf-8 -*-
"""
code/analytics/build_competency.py —— 概念映射 + 职业映射 + 能力权重矩阵（T09/T10）

三步，全部幂等、可反复重跑：

  ① 载入概念词表（schema/catalog/concept_seed.csv）→ mt.concept
     关键词存在 concept.alt_labels（它就是"同一概念的其它说法"）。

  ② 要求文本 → 概念映射：对每条 job_requirement.raw_text 做关键词匹配，
     **最长关键词优先**（避免"数据分析"被"数据"抢走）。映射不上就留 NULL，
     不硬塞——留空本身是信息（说明词表还缺这个概念）。

  ③ 岗位 → 职业映射：标题与 occupation.label_zh 做包含匹配 + 最长公共子串兜底，
     写入 job_posting.occupation_id / job_family。

  ④ 能力权重矩阵：按 occupation × concept 聚合
     importance = 出现频次占比 × 硬性系数（hard 1.0 / soft 0.6 / bonus 0.3）
     样本 <30 条时标记 insufficient_sample 并跳过（避免小样本得出荒谬结论）。

用法：
  python code/analytics/build_competency.py
  python code/analytics/build_competency.py --min-sample 20
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
CONCEPT_CSV = os.path.join(BASE, "schema", "catalog", "concept_seed.csv")
HARDNESS = {"RK1": 1.0, "RK2": 0.6, "RK3": 0.3}


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def load_concepts(c) -> int:
    with io.open(CONCEPT_CSV, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    n = 0
    with c.cursor() as cur:
        for r in rows:
            kws = [k.strip() for k in (r["keywords"] or "").split("|") if k.strip()]
            cur.execute("""
                INSERT INTO concept (concept_id, parent_id, concept_type, preferred_label,
                    label_en, alt_labels, reusability, definition, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')
                ON CONFLICT (concept_id) DO UPDATE SET
                    concept_type=EXCLUDED.concept_type,
                    preferred_label=EXCLUDED.preferred_label,
                    label_en=EXCLUDED.label_en,
                    alt_labels=EXCLUDED.alt_labels,
                    reusability=EXCLUDED.reusability,
                    definition=EXCLUDED.definition""",
                (r["concept_id"], (r["parent_id"] or "").strip() or None, r["concept_type"],
                 r["preferred_label"], r["label_en"], kws,
                 r["reusability"], r["definition"]))
            n += 1
    return n


def map_requirements(c) -> tuple[int, int]:
    """返回 (已映射条数, 总条数)。最长关键词优先。"""
    with c.cursor() as cur:
        cur.execute("SELECT concept_id, preferred_label, alt_labels FROM concept "
                    "WHERE status='active' AND alt_labels IS NOT NULL")
        concepts = cur.fetchall()
        # (关键词, concept_id, 关键词长度) 按长度降序 → 最长优先
        kws = sorted(((k, r["concept_id"], len(k)) for r in concepts for k in r["alt_labels"]),
                     key=lambda x: -x[2])
        cur.execute("SELECT requirement_id, raw_text FROM job_requirement")
        reqs = cur.fetchall()

        n_map = 0
        for r in reqs:
            text = r["raw_text"] or ""
            hit = next((cid for k, cid, _ in kws if k in text), None)
            if hit:
                cur.execute("UPDATE job_requirement SET concept_id=%s WHERE requirement_id=%s",
                            (hit, r["requirement_id"]))
                n_map += 1
            else:
                cur.execute("UPDATE job_requirement SET concept_id=NULL WHERE requirement_id=%s",
                            (r["requirement_id"],))
        return n_map, len(reqs)


def lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（dp），用于标题模糊匹配。"""
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


def norm_title(s: str) -> str:
    import re
    s = re.sub(r"[（(].*?[)）]", "", s or "")      # 去掉括号补充说明
    return re.sub(r"[\s\-—/、，,。.]+", "", s).lower()


def map_occupations(c) -> tuple[int, int]:
    with c.cursor() as cur:
        cur.execute("""SELECT occupation_id, family, label_zh FROM occupation
                       WHERE status='active' AND level>=2""")
        occs = [(o["occupation_id"], o["family"], norm_title(o["label_zh"]),
                 len(norm_title(o["label_zh"]))) for o in cur.fetchall()]
        cur.execute("SELECT job_id, title_raw FROM job_posting")
        jobs = cur.fetchall()

        n_map = 0
        for j in jobs:
            t = norm_title(j["title_raw"])
            best, best_len = None, 0
            # 1) 包含匹配，取最长标签
            for oid, fam, lab, ln in sorted(occs, key=lambda x: -x[3]):
                if lab and (lab in t or t in lab):
                    best = (oid, fam)
                    break
            # 2) 兜底：最长公共子串 >= 40% 视为命中
            if not best:
                cand, score = None, 0.0
                for oid, fam, lab, ln in occs:
                    s = lcs_len(lab, t) / max(len(lab), 1)
                    if s > score:
                        cand, score = (oid, fam), s
                if score >= 0.4:
                    best = cand
            if best:
                cur.execute("UPDATE job_posting SET occupation_id=%s, job_family=%s "
                            "WHERE job_id=%s", (best[0], best[1], j["job_id"]))
                n_map += 1
        return n_map, len(jobs)


def build_weights(c, min_sample: int) -> tuple[int, int]:
    """委托给版本化的重算器 code/evolve/recompute.py。

    改造原因：这里原来是 `DELETE FROM job_competency_weight` 再重建，
    每算一次就抹掉历史，导致无法回答"半年前的这项能力需求是多少"。
    现在每次重算都是一个**新版本**（competency_run），旧版本被 valid_to 封口，
    并且自动产出 competency_drift 漂移记录。

    **必须把当前连接 c 传进去**：上面两步的映射还在本事务里没提交，
    重算器若自己另开连接就会读到旧数据，算出 0 行（问题 #33 实测）。
    """
    if os.path.join(BASE, "code", "evolve") not in sys.path:
        sys.path.insert(0, os.path.join(BASE, "code", "evolve"))
    import importlib
    rc = importlib.import_module("recompute")
    r = rc.recompute(min_sample=min_sample, note="由 build_competency 触发", db=c)
    return r["rows"], r["skipped"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-sample", type=int, default=30)
    a = ap.parse_args()

    with conn() as c:
        n_con = load_concepts(c)
        print("[✓] 概念词表：%d 个概念入库" % n_con)

        n_map, n_req = map_requirements(c)
        print("[✓] 要求→概念映射：%d/%d（覆盖率 %.1f%%）"
              % (n_map, n_req, 100.0 * n_map / max(n_req, 1)))

        n_job, n_tot = map_occupations(c)
        print("[✓] 岗位→职业映射：%d/%d（覆盖率 %.1f%%）"
              % (n_job, n_tot, 100.0 * n_job / max(n_tot, 1)))

        n_w, n_skip = build_weights(c, a.min_sample)
        print("[✓] 能力权重矩阵：本版写入 %d 行，跳过 %d 组合（样本 < %d）"
              % (n_w, n_skip, a.min_sample))
        c.commit()

        with c.cursor() as cur:
            # 只统计**当前有效版本**（valid_to IS NULL）。统计全表会把历史版本
            # 也算进来，于是"本版写入 0 行"却显示覆盖 25 个职业——说谎的统计。
            cur.execute("""SELECT count(DISTINCT occupation_id) AS occ,
                                  count(DISTINCT concept_id) AS con,
                                  round(avg(importance),3) AS avg_imp,
                                  count(*) AS rows
                           FROM job_competency_weight WHERE valid_to IS NULL""")
            r = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM job_competency_weight")
            all_rows = cur.fetchone()["n"]
            print("    当前有效版本：%s 行，覆盖职业 %s 个 / 概念 %s 个 / 平均重要性 %s（全表历史 %d 行）"
                  % (r["rows"], r["occ"], r["con"], r["avg_imp"], all_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
