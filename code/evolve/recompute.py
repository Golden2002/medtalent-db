# -*- coding: utf-8 -*-
"""
code/evolve/recompute.py —— 能力↔职业映射的**版本化**重算与漂移检测（T13）

改造前的做法是"DELETE 全表 + 重建"，后果是：
  · 无法回答"半年前这项能力的需求是多少"
  · 无法做趋势/漂移分析
  · 每算一次，历史就没了

改造后：**每次重算 = 一个新版本**。
  · 新版本的行 valid_to 为空（当前有效），旧版本被"封口"（valid_to = 本次 as_of）
  · 两版之间逐 (职业, 能力) 比对，产出 competency_drift
  · 所有查询走 v_competency_current，历史版本永远查得到

漂移类型：new（上版没有）｜rising｜falling｜vanished（本版没有）｜stable

用法：
  python code/evolve/recompute.py                                  # 按当前数据算一版
  python code/evolve/recompute.py --as-of 2026-06-30 --period 2026H1
  python code/evolve/recompute.py --history                        # 看版本历史
  python code/evolve/recompute.py --drift                          # 看最新漂移
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
RULE_VERSION = "match_rule@0.1"
MAPPING_VERSION = "xw_1.0.0"
HARDNESS = {"RK1": 1.0, "RK2": 0.6, "RK3": 0.3}
DRIFT_EPS = 0.05          # 重要性变化小于此值算 stable


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def recompute(as_of=None, period_label=None, min_sample=30, rule_version=RULE_VERSION,
              note=None, db=None) -> dict:
    """重算一版能力权重。

    db 参数（可选连接）：调用方（如 build_competency）常常在**同一事务里**刚写完
    概念映射与职业映射、尚未提交。若这里另开连接，就看不到那些修改，
    结果是"映射明明写了，权重却是 0 行"——实测踩到过（问题 #33）。
    传入连接时由调用方负责 commit。
    """
    as_of = as_of or dt.date.today()
    period_label = period_label or as_of.strftime("%Y-%m")
    own = db is None
    c = db or psycopg.connect(DSN, row_factory=dict_row)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT run_id FROM competency_run ORDER BY created_at DESC LIMIT 1")
            prev = cur.fetchone()
            prev_run = prev["run_id"] if prev else None

            cur.execute("""SELECT jp.occupation_id, jp.job_family, jr.concept_id,
                                  jr.requirement_kind, count(*) AS n
                           FROM job_posting jp
                           JOIN job_requirement jr ON jr.job_id = jp.job_id
                           WHERE jp.occupation_id IS NOT NULL AND jr.concept_id IS NOT NULL
                             AND jp.verify_status <> 'V4'
                           GROUP BY 1,2,3,4""")
            rows = cur.fetchall()
            cur.execute("""SELECT occupation_id, count(*) AS n FROM job_posting
                           WHERE occupation_id IS NOT NULL GROUP BY 1""")
            totals = {r["occupation_id"]: r["n"] for r in cur.fetchall()}

        agg = {}
        for r in rows:
            k = (r["occupation_id"], r["job_family"], r["concept_id"])
            a = agg.setdefault(k, {"w": 0.0, "n": 0, "kinds": set()})
            a["w"] += r["n"] * HARDNESS.get(r["requirement_kind"], 0.5)
            a["n"] += r["n"]
            a["kinds"].add(r["requirement_kind"])

        run_id = "cr_" + hashlib.sha1(("%s|%s|%s" % (as_of, period_label,
                                                     dt.datetime.now().isoformat())).encode()
                                      ).hexdigest()[:20]
        n_row = 0
        n_skip = 0
        with c.cursor() as cur:
            for (occ, fam, con), a in sorted(agg.items()):
                total = totals.get(occ, 0)
                if total < min_sample:
                    n_skip += 1
                    continue
                importance = min(1.0, round(a["w"] / (total * 1.0), 3))
                essentiality = "essential" if "RK1" in a["kinds"] else "bonus"
                cur.execute("""INSERT INTO job_competency_weight (weight_id, occupation_id,
                                  concept_id, importance, level_required, essentiality,
                                  evidence_basis, sample_size, run_id, period_label,
                                  rule_version, mapping_version, valid_from, valid_to,
                                  computed_at, demand_weight)
                               VALUES (%s,%s,%s,%s,NULL,%s,'JD统计',%s,%s,%s,%s,%s,%s,NULL,now(),%s)""",
                            ("jcw_%s_%s_%s" % (run_id, occ, con), occ, con, importance,
                             essentiality, total, run_id, period_label, rule_version,
                             MAPPING_VERSION, as_of, a["n"]))
                n_row += 1

            # 封口旧版本：本次 as_of 起旧版本不再有效
            cur.execute("""UPDATE job_competency_weight SET valid_to=%s
                           WHERE valid_to IS NULL AND (run_id IS NULL OR run_id <> %s)""",
                        (as_of, run_id))

            cur.execute("""INSERT INTO competency_run (run_id, as_of, period_label,
                              rule_version, mapping_version, min_sample, occupation_count,
                              concept_count, row_count, note)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (run_id, as_of, period_label, rule_version, MAPPING_VERSION,
                         min_sample, len({k[0] for k in agg}), len({k[2] for k in agg}),
                         n_row, note))
        if own:
            c.commit()
        drift = _compute_drift(c, prev_run, run_id)
        if own:
            c.commit()
        return {"run_id": run_id, "as_of": str(as_of), "period": period_label,
                "rows": n_row, "skipped": n_skip, "prev_run": prev_run, "drift": drift}
    finally:
        if own:
            c.close()


def _compute_drift(c, prev_run, run_id) -> dict:
    """比对两个版本，产出漂移记录。"""
    if not prev_run:
        return {"rising": 0, "falling": 0, "new": 0, "vanished": 0, "stable": 0}
    with c.cursor() as cur:
        cur.execute("""SELECT occupation_id, concept_id, importance, essentiality, sample_size
                       FROM job_competency_weight WHERE run_id=%s""", (prev_run,))
        old = {(r["occupation_id"], r["concept_id"]): r for r in cur.fetchall()}
        cur.execute("""SELECT occupation_id, concept_id, importance, essentiality, sample_size
                       FROM job_competency_weight WHERE run_id=%s""", (run_id,))
        new = {(r["occupation_id"], r["concept_id"]): r for r in cur.fetchall()}

    stats = {"rising": 0, "falling": 0, "new": 0, "vanished": 0, "stable": 0}
    with c.cursor() as cur:
        for key in set(old) | set(new):
            o, n = old.get(key), new.get(key)
            occ, con = key
            if o and not n:
                dtype, fo, to = "vanished", float(o["importance"]), None
            elif n and not o:
                dtype, fo, to = "new", None, float(n["importance"])
            else:
                d = float(n["importance"]) - float(o["importance"])
                dtype = "rising" if d > DRIFT_EPS else ("falling" if d < -DRIFT_EPS else "stable")
                fo, to = float(o["importance"]), float(n["importance"])
            stats[dtype] += 1
            if dtype == "stable":
                continue
            did = "dr_" + hashlib.sha1(("%s|%s|%s" % (run_id, occ, con)).encode()).hexdigest()[:20]
            cur.execute("""INSERT INTO competency_drift (drift_id, from_run_id, to_run_id,
                              occupation_id, concept_id, from_importance, to_importance,
                              delta, drift_type, from_essentiality, to_essentiality, sample_size)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (drift_id) DO NOTHING""",
                        (did, prev_run, run_id, occ, con, fo, to,
                         (to - fo) if (fo is not None and to is not None) else None,
                         dtype, o["essentiality"] if o else None,
                         n["essentiality"] if n else None,
                         (n or o)["sample_size"]))
    return stats


def cmd_history(a):
    with conn() as c, c.cursor() as cur:
        cur.execute("""SELECT r.*, (SELECT count(*) FROM competency_drift d
                                     WHERE d.to_run_id=r.run_id) AS drifts
                       FROM competency_run r ORDER BY created_at DESC LIMIT 12""")
        print("%-22s %-12s %-10s %-8s %6s %6s %s"
              % ("run", "as_of", "period", "rule", "职业", "行数", "漂移"))
        for r in cur.fetchall():
            print("%-22s %-12s %-10s %-8s %6d %6d %d"
                  % (r["run_id"], r["as_of"], r["period_label"] or "-",
                     r["rule_version"], r["occupation_count"] or 0,
                     r["row_count"] or 0, r["drifts"]))
    return 0


def cmd_drift(a):
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT run_id FROM competency_run ORDER BY created_at DESC LIMIT 1")
        r = cur.fetchone()
        if not r:
            print("还没有任何重算版本")
            return 0
        run_id = r["run_id"]
        cur.execute("""SELECT d.drift_type, count(*) AS n FROM competency_drift d
                       WHERE d.to_run_id=%s GROUP BY 1 ORDER BY 2 DESC""", (run_id,))
        print("最新版本 %s 的漂移分布：" % run_id)
        for x in cur.fetchall():
            print("  %-9s %d" % (x["drift_type"], x["n"]))
        cur.execute("""SELECT d.drift_type, o.label_zh AS occ, c.preferred_label AS con,
                              d.from_importance, d.to_importance, d.delta
                       FROM competency_drift d
                       JOIN occupation o ON o.occupation_id=d.occupation_id
                       JOIN concept c ON c.concept_id=d.concept_id
                       WHERE d.to_run_id=%s AND d.drift_type <> 'stable'
                       ORDER BY d.drift_type, abs(coalesce(d.delta,0)) DESC LIMIT 15""", (run_id,))
        rows = cur.fetchall()
        if rows:
            print("\n最显著的漂移：")
            for x in rows:
                print("  [%-8s] %-16s × %-14s  %s → %s  (Δ%s)"
                      % (x["drift_type"], x["occ"], x["con"],
                         x["from_importance"], x["to_importance"], x["delta"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--period")
    ap.add_argument("--min-sample", type=int, default=30)
    ap.add_argument("--note")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--drift", action="store_true")
    a = ap.parse_args()

    if a.history:
        return cmd_history(a)
    if a.drift:
        return cmd_drift(a)

    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else None
    r = recompute(as_of, a.period, a.min_sample, note=a.note)
    print("[✓] 重算版本 %s（as-of %s，期间 %s）" % (r["run_id"], r["as_of"], r["period"]))
    print("    写入 %d 行能力权重；上一版本 %s" % (r["rows"], r["prev_run"] or "（首版）"))
    d = r["drift"]
    if r["prev_run"]:
        print("    漂移：新出现 %d｜上升 %d｜下降 %d｜消失 %d｜稳定 %d"
              % (d["new"], d["rising"], d["falling"], d["vanished"], d["stable"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
