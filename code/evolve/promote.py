# -*- coding: utf-8 -*-
"""
code/evolve/promote.py —— 把候选提升为正式节点（T13）

**提升是有门槛的动作**，不是随手加数据：
  · 候选必须已有足够证据（status='ready'）才能提升；
  · 每次提升都写 occupation_change（谁、何时、依据、理由）；
  · 新节点用**新 ID**，旧 ID 通过 occupation_migration 保留可追溯；
  · 提升只做"加"，不做"改"——旧节点的含义永远不变（P2）。

支持的演化动作：
  add       新岗位长出来（从 occupation_candidate 提升）
  split     一个岗位分化成多个（如"医学顾问"拆成"医学顾问(肿瘤)/医学顾问(免疫)"）
  merge     多个岗位合并成一个
  retire    岗位退出（无继任）
  rename    同一节点改名（ID 不变，因为语义未变）
  promote_concept  候选能力提升为新概念，或并入既有概念作别名

用法：
  python code/evolve/promote.py list                                  # 看候选
  python code/evolve/promote.py add <candidate_id> --family F09 --parent OCC-F09
  python code/evolve/promote.py split OCC-F02-01-02 --parts "A:0.6,B:0.4"
  python code/evolve/promote.py retire OCC-F17-01-02 --reason "需求萎缩"
  python code/evolve/promote.py concept <candidate_id> --type K1 --label "……"
  python code/evolve/promote.py alias <candidate_id> --into CON-K1-LIT
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


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def new_occupation_id(family: str, label: str, salt: str = "") -> str:
    """新节点 ID：永不复用。
    用内容+日期+随机盐派生，保证同一标签在不同时间提升会得到不同 ID（可追溯是哪次加的）。"""
    h = hashlib.sha1(("%s|%s|%s|%s" % (family, label, dt.date.today(), salt))
                     .encode()).hexdigest()[:8].upper()
    return "OCC-%s-X%s" % (family, h)


def _change(c, change_type, from_ids, to_ids, family, label, reason,
            evidence_count, evidence_sample, reviewer, effective_from=None) -> str:
    cid = "chg_" + hashlib.sha1(("%s|%s|%s|%s" % (
        change_type, ",".join(from_ids or []), ",".join(to_ids or []),
        dt.datetime.now().isoformat())).encode()).hexdigest()[:20]
    with c.cursor() as cur:
        cur.execute("""INSERT INTO occupation_change (change_id, change_type, from_ids,
                          to_ids, family, label_zh, reason, evidence_count, evidence_sample,
                          status, proposed_by, decided_by, decided_at, effective_from)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'applied','cli',%s,now(),%s)""",
                    (cid, change_type, from_ids, to_ids, family, label, reason,
                     evidence_count, _json(evidence_sample), reviewer,
                     effective_from or dt.date.today()))
    return cid


def _json(o):
    import json
    return json.dumps(o, ensure_ascii=False) if o is not None else None


def promote_occupation(candidate_id, family, parent_id, label_zh=None, label_en=None,
                       reviewer="cli", medical_reliance=None, transition_ease=None,
                       note=None) -> dict:
    with conn() as c0, c0.cursor() as cur:
        cur.execute("SELECT * FROM occupation_candidate WHERE candidate_id=%s "
                    "OR title_key=%s", (candidate_id, candidate_id))
        cand = cur.fetchone()
    if not cand:
        raise SystemExit("找不到候选 %s" % candidate_id)
    if cand["status"] not in ("ready", "watching"):
        raise SystemExit("候选状态为 %s，不可提升" % cand["status"])
    if cand["status"] == "watching":
        raise SystemExit("候选证据不足（%d 条），尚未 ready；可用 --force 语境下由评审决定"
                         % cand["evidence_count"])

    label = label_zh or cand["title_sample"]
    oid = new_occupation_id(family, label)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO occupation (occupation_id, parent_id, level, family,
                              label_zh, label_en, medical_reliance, transition_ease,
                              status, description, code_status, valid_from)
                           VALUES (%s,%s,3,%s,%s,%s,%s,%s,'active',%s,'N',%s)""",
                        (oid, parent_id, family, label,
                         label_en or label, medical_reliance, transition_ease,
                         "由候选提升：%s（支撑 %d 条 JD）" % (note or "", cand["evidence_count"]),
                         dt.date.today()))
            _change(c, "add", [], [oid], family, label,
                    note or "从真实 JD 候选提升", cand["evidence_count"],
                    {"title_sample": cand["title_sample"], "jobIds": cand["sample_job_ids"][:5]},
                    reviewer)
            cur.execute("""UPDATE occupation_candidate SET status='promoted', promoted_to=%s,
                              note=%s, updated_at=now() WHERE candidate_id=%s""",
                        (oid, note, cand["candidate_id"]))
        c.commit()
    return {"occupation_id": oid, "label": label, "family": family}


def split_occupation(old_id, parts, reviewer="cli", reason=None) -> dict:
    """parts: [{"label": "...", "share": 0.6, "medical_reliance": 4}, ...]"""
    today = dt.date.today()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM occupation WHERE occupation_id=%s", (old_id,))
            old = cur.fetchone()
            if not old or old["valid_to"] is not None:
                raise SystemExit("节点 %s 不存在或已退役" % old_id)
            new_ids = []
            for p in parts:
                nid = new_occupation_id(old["family"], p["label"], salt=old_id)
                cur.execute("""INSERT INTO occupation (occupation_id, parent_id, level, family,
                                  label_zh, label_en, medical_reliance, transition_ease,
                                  status, description, code_status, valid_from)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s)""",
                            (nid, old["parent_id"], old["level"], old["family"], p["label"],
                             p.get("label_en") or p["label"],
                             p.get("medical_reliance", old["medical_reliance"]),
                             p.get("transition_ease", old["transition_ease"]),
                             "由 %s 拆分而来" % old_id, old["code_status"], today))
                cur.execute("""INSERT INTO occupation_migration (migration_id, change_id,
                                  old_id, new_id, relation, coverage, effective_from, note)
                               VALUES (%s,NULL,%s,%s,'split_into',%s,%s,%s)""",
                            ("mig_" + hashlib.sha1(("%s|%s" % (old_id, nid)).encode())
                             .hexdigest()[:20], old_id, nid, p.get("share"), today, reason))
                new_ids.append(nid)
            cur.execute("""UPDATE occupation SET status='retired', valid_to=%s,
                              retired_reason=%s, updated_at=now() WHERE occupation_id=%s""",
                        (today, reason or "拆分为 %s" % ",".join(new_ids), old_id))
            chg = _change(c, "split", [old_id], new_ids, old["family"], old["label_zh"],
                          reason, 0, None, reviewer, today)
            cur.execute("UPDATE occupation_migration SET change_id=%s WHERE old_id=%s",
                        (chg, old_id))
        c.commit()
    return {"from": old_id, "to": new_ids, "change_id": chg}


def merge_occupations(old_ids, label, family, reviewer="cli", reason=None) -> dict:
    today = dt.date.today()
    with conn() as c:
        with c.cursor() as cur:
            nid = new_occupation_id(family, label, salt=",".join(old_ids))
            cur.execute("""INSERT INTO occupation (occupation_id, parent_id, level, family,
                              label_zh, label_en, status, description, code_status, valid_from)
                           VALUES (%s,NULL,3,%s,%s,%s,'active',%s,'N',%s)""",
                        (nid, family, label, label, "由 %s 合并而来" % ",".join(old_ids), today))
            for oid in old_ids:
                cur.execute("""INSERT INTO occupation_migration (migration_id, old_id, new_id,
                                  relation, effective_from, note)
                               VALUES (%s,%s,%s,'merged_into',%s,%s)""",
                            ("mig_" + hashlib.sha1(("%s|%s" % (oid, nid)).encode()).hexdigest()[:20],
                             oid, nid, today, reason))
                cur.execute("""UPDATE occupation SET status='retired', valid_to=%s,
                                  retired_reason=%s, updated_at=now() WHERE occupation_id=%s""",
                            (today, reason or "合并为 %s" % nid, oid))
            chg = _change(c, "merge", old_ids, [nid], family, label, reason, 0, None,
                          reviewer, today)
            cur.execute("UPDATE occupation_migration SET change_id=%s WHERE new_id=%s",
                        (chg, nid))
        c.commit()
    return {"from": old_ids, "to": nid, "change_id": chg}


def retire_occupation(old_id, reason, reviewer="cli") -> dict:
    today = dt.date.today()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM occupation WHERE occupation_id=%s", (old_id,))
            old = cur.fetchone()
            if not old:
                raise SystemExit("节点 %s 不存在" % old_id)
            cur.execute("""UPDATE occupation SET status='retired', valid_to=%s,
                              retired_reason=%s, updated_at=now() WHERE occupation_id=%s""",
                        (today, reason, old_id))
            chg = _change(c, "retire", [old_id], [], old["family"], old["label_zh"],
                          reason, 0, None, reviewer, today)
        c.commit()
    # 退役节点不写迁移表：没有继任者，occupation_resolve 返回空即为"无继任"
    return {"retired": old_id, "change_id": chg}


def promote_concept(candidate_id, concept_type, label, reusability="RU3",
                    parent_id=None, reviewer="cli") -> dict:
    """把候选能力提升为**新概念**，并把候选短语作为别名写进 alt_labels，
    这样既有的关键词映射会自动把相关要求映射到它。"""
    with conn() as c0, c0.cursor() as cur:
        cur.execute("SELECT * FROM concept_candidate WHERE candidate_id=%s OR phrase_key=%s",
                    (candidate_id, candidate_id))
        cand = cur.fetchone()
    if not cand:
        raise SystemExit("找不到候选 %s" % candidate_id)
    cid = "CON-" + concept_type + "-" + hashlib.sha1(
        cand["phrase_key"].encode()).hexdigest()[:6].upper()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO concept (concept_id, parent_id, concept_type,
                              preferred_label, alt_labels, reusability, definition, status)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'active')
                           ON CONFLICT (concept_id) DO UPDATE SET
                              alt_labels = (SELECT array_agg(DISTINCT x) FROM unnest(
                                  concept.alt_labels || EXCLUDED.alt_labels) x)""",
                        (cid, parent_id, concept_type, label, [cand["phrase_key"]],
                         reusability, "由候选提升：%d 条要求支撑" % cand["evidence_count"]))
            cur.execute("""UPDATE concept_candidate SET status='promoted', promoted_to=%s,
                              updated_at=now() WHERE candidate_id=%s""",
                        (cid, cand["candidate_id"]))
        c.commit()
    return {"concept_id": cid, "label": label, "evidence": cand["evidence_count"]}


def merge_alias(candidate_id, into_concept_id, reviewer="cli") -> dict:
    """把候选短语并入既有概念作为**别名**（不新建概念）。
    这是词表生长的首选路径——重复概念是词表腐化的开始。"""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM concept_candidate WHERE candidate_id=%s OR phrase_key=%s",
                        (candidate_id, candidate_id))
            cand = cur.fetchone()
            if not cand:
                raise SystemExit("找不到候选 %s" % candidate_id)
            cur.execute("""UPDATE concept SET alt_labels = (SELECT array_agg(DISTINCT x)
                              FROM unnest(COALESCE(alt_labels,'{}') || %s::text[]) x),
                              updated_at = now()
                           WHERE concept_id=%s""",
                        ([cand["phrase_key"]], into_concept_id))
            if cur.rowcount == 0:
                raise SystemExit("概念 %s 不存在" % into_concept_id)
            cur.execute("""UPDATE concept_candidate SET status='merged', promoted_to=%s,
                              updated_at=now() WHERE candidate_id=%s""",
                        (into_concept_id, cand["candidate_id"]))
        c.commit()
    return {"alias": cand["phrase_key"], "into": into_concept_id}


def cmd_list(a):
    with conn() as c, c.cursor() as cur:
        cur.execute("""SELECT * FROM v_evolution_health""")
        h = cur.fetchone()
        print("演化健康度：树节点 %d（退役 %d）｜迁移 %d｜候选 ready：岗位 %d / 能力 %d｜"
              "重算版本 %d｜上升信号 %d"
              % (h["active_nodes"], h["retired_nodes"], h["migrations"],
                 h["occ_ready"], h["con_ready"], h["runs"], h["rising_signals"]))
        cur.execute("""SELECT candidate_id, title_sample, evidence_count, family_guess, status
                       FROM occupation_candidate WHERE status IN ('ready','watching')
                       ORDER BY status, evidence_count DESC LIMIT 10""")
        print("\n岗位候选：")
        for r in cur.fetchall():
            print("  [%-8s] %-28s %2d 条  族=%s  id=%s"
                  % (r["status"], r["title_sample"], r["evidence_count"],
                     r["family_guess"], r["candidate_id"][:16]))
        cur.execute("""SELECT candidate_id, phrase_sample, evidence_count, suggested_concept_id
                       FROM concept_candidate WHERE status='ready'
                       ORDER BY evidence_count DESC LIMIT 10""")
        print("\n能力候选（ready）：")
        for r in cur.fetchall():
            print("  %-30s %2d 条  建议并入=%s" % (r["phrase_sample"], r["evidence_count"],
                                                 r["suggested_concept_id"] or "（新概念）"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)

    p = sub.add_parser("add")
    p.add_argument("candidate_id"); p.add_argument("--family", required=True)
    p.add_argument("--parent"); p.add_argument("--label"); p.add_argument("--label-en")
    p.add_argument("--medical-reliance", type=int); p.add_argument("--transition-ease", type=int)
    p.add_argument("--reviewer", default="cli"); p.add_argument("--note")
    p.set_defaults(func=lambda a: print(promote_occupation(
        a.candidate_id, a.family, a.parent, a.label, a.label_en, a.reviewer,
        a.medical_reliance, a.transition_ease, a.note)))

    p = sub.add_parser("split")
    p.add_argument("old_id"); p.add_argument("--parts", required=True,
                                             help="标签:占比,标签:占比 如 A:0.6,B:0.4")
    p.add_argument("--reason"); p.add_argument("--reviewer", default="cli")
    p.set_defaults(func=lambda a: print(split_occupation(
        a.old_id, [{"label": x.split(":")[0], "share": float(x.split(":")[1])}
                   for x in a.parts.split(",")], a.reviewer, a.reason)))

    p = sub.add_parser("merge")
    p.add_argument("old_ids", help="逗号分隔"); p.add_argument("--label", required=True)
    p.add_argument("--family", required=True); p.add_argument("--reason")
    p.add_argument("--reviewer", default="cli")
    p.set_defaults(func=lambda a: print(merge_occupations(
        a.old_ids.split(","), a.label, a.family, a.reviewer, a.reason)))

    p = sub.add_parser("retire")
    p.add_argument("old_id"); p.add_argument("--reason", required=True)
    p.add_argument("--reviewer", default="cli")
    p.set_defaults(func=lambda a: print(retire_occupation(a.old_id, a.reason, a.reviewer)))

    p = sub.add_parser("concept")
    p.add_argument("candidate_id"); p.add_argument("--type", default="K1")
    p.add_argument("--label", required=True); p.add_argument("--reusability", default="RU3")
    p.add_argument("--parent"); p.add_argument("--reviewer", default="cli")
    p.set_defaults(func=lambda a: print(promote_concept(
        a.candidate_id, a.type, a.label, a.reusability, a.parent, a.reviewer)))

    p = sub.add_parser("alias")
    p.add_argument("candidate_id"); p.add_argument("--into", required=True)
    p.add_argument("--reviewer", default="cli")
    p.set_defaults(func=lambda a: print(merge_alias(a.candidate_id, a.into, a.reviewer)))

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
