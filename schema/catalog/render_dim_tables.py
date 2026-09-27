#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 schema/catalog/talent_field_seed.csv 生成设计文档用的 markdown 表（避免手抄出错）。"""
import csv
import io
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

def _project_root(start):
    d = start
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "schema", "catalog")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("找不到项目根目录（应含 schema/catalog）")


ROOT = _project_root(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "schema", "catalog", "talent_field_seed.csv")

OPT = {  # 词表 -> 选项数（实测 count(*)）
    "CT_DEGREE_LEVEL": 6, "CT_MAJOR": 21, "CT_YES_NO": 3, "CT_RANK_BAND": 6,
    "CT_SCHOOL_TIER": 9, "CT_CREDENTIAL_TYPE": 12, "CT_CRED_STATUS": 5,
    "CT_TRAINING_TYPE": 8, "CT_HEALTH_LIMIT": 5, "CT_POLITICAL": 5, "CT_AGE_BAND": 7,
    "CT_SEX": 3, "CT_EXPERIENCE_BAND": 7, "CT_EMPLOYER_TYPE": 19, "CT_CLINICAL_BAND": 6,
    "CT_CLINICAL_DEPARTMENT": 29, "CT_VOLUME_BAND": 6, "CT_PROJECT_TYPE": 10,
    "CT_PROJECT_ROLE": 6, "CT_JOB_ZONE": 5, "CT_RESEARCH_LEVEL": 6,
    "CT_RESEARCH_OUTPUT_TYPE": 9, "CT_AWARD_LEVEL": 6, "CT_ABILITY_LEVEL": 6,
    "CT_LEVEL_BASIS": 6, "CT_ASSESSMENT_DIMENSION": 8, "CT_LANGUAGE_LEVEL": 7,
    "CT_INTEREST_DOMAIN": 29, "CT_CAREER_GOAL": 8, "CT_WORK_STYLE": 12,
    "CT_VALUE_ORIENT": 10, "CT_ORG_CULTURE": 6, "CT_CITY": 45, "CT_CITY_TIER": 5,
    "CT_MOBILITY": 4, "CT_HUKOU_TYPE": 2, "CT_WORK_MODE": 6, "CT_SALARY_BAND": 8,
    "CT_WORK_INTENSITY": 5, "CT_SHIFT_WILLING": 3, "CT_GROWTH_PREF": 5,
    "CT_STABILITY_PREF": 5, "CT_JOB_FAMILY": 17, "CT_EVIDENCE_STRENGTH": 5,
}
KIND = {"enum": "枚举", "ordinal": "序数", "set": "多值集合", "range": "区间", "text": "文本"}
ROLE = {"gate": "硬门槛", "score": "加分", "modifier": "折扣", "display": "仅展示"}

with io.open(CSV, encoding="utf-8-sig", newline="") as fh:
    rows = list(csv.DictReader(fh))

groups = []
for r in rows:
    if r["group_id"] not in groups:
        groups.append(r["group_id"])

for g in groups:
    rs = [r for r in rows if r["group_id"] == g]
    print("\n**%s %s（%d 个维度）**\n" % (g, rs[0]["group_title"], len(rs)))
    print("| 维度 ID | 中文名 | 类型 | 固定选项（数量） | 人侧来源 | 岗位侧来源 | 硬门槛 | 权重 | 自写 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rs:
        ct = r["code_table_id"]
        opt = ("%s（%d）" % (ct, OPT[ct])) if ct else "无（概念本体）" if r["dimension_id"].startswith("DIM_CONCEPT") else "无"
        selfw = "允许" if r["allow_custom"] == "true" else "不允许"
        hard = "**是**" if r["is_hard"] == "true" else "否"
        role = ROLE[r["role"]]
        if r["role"] in ("gate", "score"):
            hardcell = "**是**（%s）" % role if r["is_hard"] == "true" else "否（%s）" % role
        else:
            hardcell = "否（%s）" % role
        print("| `%s` | %s | %s | %s | `%s` | `%s` | %s | %s | %s |" % (
            r["dimension_id"], r["title_zh"], KIND[r["kind"]], opt,
            r["person_locator"], r["job_locator"], hardcell,
            r["weight"].rstrip("0").rstrip(".") if "." in r["weight"] else r["weight"],
            selfw))
    print()
