#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
code/load_catalog.py — T02 字典导入器（CSV → PostgreSQL）

设计要点：
  1. **先过质检门**：调用 validate_catalog.py，有错即中止，绝不把坏字典写进库。
  2. **UPSERT 语义**：按主键更新，永不 DELETE —— 词表只增不删（废弃用 deprecated 标记）。
  3. **幂等**：重复执行行数不变。
  4. **entity_catalog 自动派生**：从 field_catalog 的 distinct entity_id 生成实体登记，
     避免"加了字段却忘了登记实体"这类漂移。
  5. 生成可审阅的 SQL 文件（dist/load_catalog.sql）后再执行，便于人工核查与重放。

用法：
  python code/load_catalog.py              # 生成 SQL 并导入
  python code/load_catalog.py --dry-run    # 只生成 SQL，不执行
"""
import argparse
import csv
import io
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "schema", "catalog")
OUTDIR = os.path.join(ROOT, "dist")
OUTSQL = os.path.join(OUTDIR, "load_catalog.sql")

# 实体 → 域 / ID 前缀（entity_catalog 自动登记用）
DOMAIN_MAP = {
    "talent": ["person", "person_pii", "person_demographics", "education_record",
               "training_record", "clinical_exposure", "credential", "employment_record",
               "research_output", "project_record", "award_honor", "preference",
               "person_constraint", "evidence", "skill_assertion", "assessment", "narrative",
               "trajectory_event"],
    "opportunity": ["occupation", "employer", "job_posting", "job_task",
                    "job_requirement", "job_competency_weight", "transition_case",
                    "salary_benchmark"],
    "semantic": ["concept", "code_table", "code_value", "field_catalog", "category_node",
                 "field_value", "concept_mapping", "concept_ancestor", "first_occurrence",
                 "entity_catalog", "attribute_definition", "search_document", "embedding",
                 "metric_definition"],
    "governance": ["source_registry", "ingest_run", "provenance", "consent_record",
                   "access_log", "change_log", "access_policy", "observation_window",
                   "dataset_release", "consent_withdrawal_action", "subject_request",
                   "retention_policy", "data_lifecycle_run", "incident_report"],
    "connection": ["assertion", "derived_feature", "match_run", "match_result",
                   "gap_analysis"],
}
PREFIX_MAP = {
    "person": "per", "person_pii": "pii", "person_demographics": "dem",
    "education_record": "edu", "training_record": "trn", "clinical_exposure": "cln",
    "credential": "cre", "employment_record": "emp", "research_output": "res",
    "project_record": "prj", "award_honor": "awd", "preference": "prf",
    "person_constraint": "cst", "evidence": "evd", "skill_assertion": "skl",
    "assessment": "asm", "narrative": "nar", "trajectory_event": "tre",
    "occupation": "occ", "employer": "ems", "job_posting": "job", "job_task": "jbt",
    "job_requirement": "req", "job_competency_weight": "jcw", "transition_case": "trc",
    "salary_benchmark": "sal", "concept": "con", "code_table": "ct",
    "code_value": "cv", "field_catalog": "fld", "category_node": "cat",
    "field_value": "fvl", "concept_mapping": "cm", "concept_ancestor": "ca",
    "first_occurrence": "fo", "entity_catalog": "ent", "attribute_definition": "att",
    "search_document": "doc", "embedding": "emb", "source_registry": "src",
    "ingest_run": "run", "provenance": "prv", "consent_record": "cns",
    "access_log": "acl", "change_log": "chl", "access_policy": "ap",
    "observation_window": "ow", "dataset_release": "dr", "assertion": "asr",
    "derived_feature": "df", "metric_definition": "met", "match_run": "mr",
    "match_result": "mat", "gap_analysis": "gap", "consent_withdrawal_action": "cwa",
    "subject_request": "sreq", "retention_policy": "rp",
    "data_lifecycle_run": "dlr", "incident_report": "inc",
}


def domain_of(entity):
    for d, names in DOMAIN_MAP.items():
        if entity in names:
            return d
    return "semantic"


def prefix_of(entity):
    if entity in PREFIX_MAP:
        return PREFIX_MAP[entity]
    return "".join(c for c in entity if c.isalpha())[:3] or "obj"


def q(v):
    """SQL 字面量：空串→NULL，单引号转义。"""
    if v is None:
        return "NULL"
    s = str(v).strip()
    if s == "":
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def qn(v):
    if v is None or str(v).strip() == "":
        return "NULL"
    return str(v).strip()


def qarr(v):
    """'A|B|C' → ARRAY['A','B','C']"""
    if v is None or str(v).strip() == "":
        return "NULL"
    parts = [p.strip() for p in str(v).split("|") if p.strip()]
    if not parts:
        return "NULL"
    return "ARRAY[" + ",".join(q(p) for p in parts) + "]"


def load_csv(name):
    path = os.path.join(CATALOG, name)
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def gate():
    print("[*] 质检门：validate_catalog.py")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "code", "validate_catalog.py"), ROOT],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print("[X] 字典未过质检门，中止导入")
        sys.exit(1)


def build():
    tables = load_csv("code_table_seed.csv")
    values = load_csv("code_value_seed.csv") if os.path.exists(
        os.path.join(CATALOG, "code_value_seed.csv")) else tables
    fields = load_csv("field_catalog_seed.csv")

    L = []
    L.append("-- 由 code/load_catalog.py 生成，请勿手工编辑")
    L.append("-- 幂等 UPSERT：重复执行行数不变；从不 DELETE")
    L.append("BEGIN;")
    L.append("SET search_path TO mt, public;")

    # --- code_table（从 code_value 行的 code_table_id 去重派生 + 显式清单） ---
    seen = {}
    for r in values:
        cid = r["code_table_id"].strip()
        if cid and cid not in seen:
            seen[cid] = r
    L.append("\n-- 1. code_table")
    for cid in sorted(seen):
        L.append(
            "INSERT INTO code_table (code_table_id, name, description, hierarchical, "
            "external_standard, version, status, owner) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (code_table_id) DO UPDATE SET name=EXCLUDED.name, "
            "description=EXCLUDED.description, hierarchical=EXCLUDED.hierarchical, "
            "version=EXCLUDED.version, status=EXCLUDED.status;" % (
                q(cid), q(cid), q("由 code_table_seed.csv 导入"), "false",
                "NULL", q("1.0.0"), q("active"), q("data")))

    # --- code_value ---
    L.append("\n-- 2. code_value")
    for r in values:
        L.append(
            "INSERT INTO code_value (code_table_id, code, label_zh, label_en, definition, "
            "parent_code, level, sort_order, aliases, external_mapping, effective_from, "
            "deprecated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s::jsonb,NULL,false) "
            "ON CONFLICT (code_table_id, code) DO UPDATE SET label_zh=EXCLUDED.label_zh, "
            "label_en=EXCLUDED.label_en, definition=EXCLUDED.definition, "
            "parent_code=EXCLUDED.parent_code, level=EXCLUDED.level, "
            "sort_order=EXCLUDED.sort_order, external_mapping=EXCLUDED.external_mapping;" % (
                q(r["code_table_id"]), q(r["code"]), q(r["label_zh"]), q(r["label_en"]),
                q(r["definition"]), q(r["parent_code"]), qn(r["level"]),
                qn(r["sort_order"]), q(r["external_mapping"] or "{}")))

    # --- entity_catalog（从 field_catalog 的 distinct entity_id 自动派生） ---
    L.append("\n-- 3. entity_catalog（自 field_catalog 派生）")
    ents = sorted({r["entity_id"].strip() for r in fields if r["entity_id"].strip()})
    for e in ents:
        L.append(
            "INSERT INTO entity_catalog (entity_id, table_name, domain, description, "
            "id_prefix, schema_version, access_tier, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (entity_id) DO UPDATE SET table_name=EXCLUDED.table_name, "
            "domain=EXCLUDED.domain, id_prefix=EXCLUDED.id_prefix;" % (
                q(e), q(e), q(domain_of(e)), q("由 field_catalog 自动登记"),
                q(prefix_of(e)), q("1.0.0"), q("T1"), q("active")))

    # --- field_catalog ---
    L.append("\n-- 4. field_catalog")
    for r in fields:
        L.append(
            "INSERT INTO field_catalog (field_id, entity_id, title, description, data_type, "
            "unit, value_range, code_table_id, cardinality, collection_method, applies_to, "
            "related_fields, derivation, access_tier, status, version, owner) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (field_id) DO UPDATE SET title=EXCLUDED.title, "
            "description=EXCLUDED.description, data_type=EXCLUDED.data_type, "
            "unit=EXCLUDED.unit, value_range=EXCLUDED.value_range, "
            "code_table_id=EXCLUDED.code_table_id, cardinality=EXCLUDED.cardinality, "
            "collection_method=EXCLUDED.collection_method, "
            "related_fields=EXCLUDED.related_fields, derivation=EXCLUDED.derivation, "
            "access_tier=EXCLUDED.access_tier, status=EXCLUDED.status, "
            "version=EXCLUDED.version, owner=EXCLUDED.owner;" % (
                q(r["field_id"]), q(r["entity_id"]), q(r["title"]), q(r["description"]),
                q(r["data_type"]), q(r["unit"]), q(r["value_range"]),
                q(r["code_table_id"]), q(r["cardinality"]), q(r["collection_method"]),
                q(r["applies_to"]), qarr(r["related_fields"]), q(r["derivation"]),
                q(r["access_tier"]), q(r["status"]), q(r["version"]), q(r["owner"])))

    # --- occupation（职业树，主数据） ---
    L.append("\n-- 6. occupation（职业树）")
    n_occ = 0
    occ_path = os.path.join(CATALOG, "occupation_seed.csv")
    if os.path.exists(occ_path):
        with io.open(occ_path, encoding="utf-8-sig", newline="") as fh:
            occs = list(csv.DictReader(fh))
        # code_status → code_source：把"这个码有多可信"写进数据，而不是靠人记
        SRC = {
            "V": "CZSO/ILO ISCO-08 官方对应表（已逐条核验）",
            "E": "估计值：待用官方 ISCO-08 / O*NET-SOC / 职业分类大典复核，禁止用于对外结论",
            "N": None,
        }
        for r in occs:
            n_occ += 1
            L.append(
                "INSERT INTO occupation (occupation_id, parent_id, level, family, label_zh, "
                "label_en, medical_reliance, transition_ease, license_required, degree_typical, "
                "entry_paths, isco08_code, code_status, code_source, description, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active') "
                "ON CONFLICT (occupation_id) DO UPDATE SET parent_id=EXCLUDED.parent_id, "
                "level=EXCLUDED.level, family=EXCLUDED.family, label_zh=EXCLUDED.label_zh, "
                "label_en=EXCLUDED.label_en, medical_reliance=EXCLUDED.medical_reliance, "
                "transition_ease=EXCLUDED.transition_ease, "
                "license_required=EXCLUDED.license_required, "
                "degree_typical=EXCLUDED.degree_typical, entry_paths=EXCLUDED.entry_paths, "
                "isco08_code=EXCLUDED.isco08_code, code_status=EXCLUDED.code_status, "
                "code_source=EXCLUDED.code_source, description=EXCLUDED.description;" % (
                    q(r["occupation_id"]), q(r["parent_id"]), qn(r["level"]), q(r["family"]),
                    q(r["label_zh"]), q(r["label_en"]), qn(r["medical_reliance"]),
                    qn(r["transition_ease"]), qarr(r["license_required"]),
                    qarr(r["degree_typical"]), qarr(r["entry_paths"]),
                    q(r["isco08_code"]), q(r["code_status"]), q(SRC.get(r["code_status"])),
                    q(r["description"])))

    L.append("\n-- 7. 审计记录")
    L.append("INSERT INTO change_log (actor, object_type, object_name, change_type, "
             "to_version, detail) VALUES ('load_catalog.py','catalog','code_table+code_value"
             "+entity_catalog+field_catalog+occupation','backfill','1.0.0',"
             "'{\"rows\":{\"code_value\":%d,\"field_catalog\":%d,\"entity\":%d,"
             "\"occupation\":%d}}'::jsonb);"
             % (len(values), len(fields), len(ents), n_occ))
    L.append("COMMIT;")
    return "\n".join(L), len(values), len(fields), len(ents), len(seen), n_occ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    gate()
    sql, nv, nf, ne, nt, no = build()
    os.makedirs(OUTDIR, exist_ok=True)
    with io.open(OUTSQL, "w", encoding="utf-8") as fh:
        fh.write(sql)
    print("[✓] 生成 SQL：%s（%.0f KB）" % (OUTSQL, os.path.getsize(OUTSQL) / 1024))
    print("    代码表 %d / 代码值 %d / 实体 %d / 字段 %d / 职业节点 %d" % (nt, nv, ne, nf, no))

    if a.dry_run:
        print("[dry-run] 未执行")
        return

    print("[*] 执行导入 ...")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "pg.py"), "sql", OUTSQL],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print(r.stderr.rstrip())
        sys.exit(r.returncode)
    print("[✓] 导入完成")


if __name__ == "__main__":
    main()
