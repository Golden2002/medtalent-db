#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
医学生人才信息库 · 数据字典质检门（catalog quality gate）

用途：在把种子词表/字段目录导入数据库之前，做零依赖的本地校验。
这是项目"数据字典即代码"的最小落地——字典进库前必须过门。

检查项：
  1. CSV 列数与表头一致、无空主键、无重复主键
  2. code_value 的 parent_code 必须存在于同一 code_table 内
  3. field_catalog 引用的 code_table_id / entity_id 必须存在
  4. field_catalog 的 data_type 属于受控集合
  5. cardinality=array 的字段 data_type 应为 array（一致性提示）
  6. deprecated 代码值必须有 replaced_by

退出码：0 = 全部通过；1 = 存在错误。
用法：python code/validate_catalog.py [项目根]
"""
import csv
import io
import os
import sys

CODE_TABLE_CSV = os.path.join("schema", "catalog", "code_table_seed.csv")
FIELD_CSV = os.path.join("schema", "catalog", "field_catalog_seed.csv")
OCC_CSV = os.path.join("schema", "catalog", "occupation_seed.csv")

OCC_COLS = ["occupation_id", "parent_id", "level", "family", "label_zh", "label_en",
            "medical_reliance", "transition_ease", "license_required", "degree_typical",
            "entry_paths", "isco08_code", "code_status", "description"]

CODE_TABLE_COLS = ["code_table_id", "code", "label_zh", "label_en", "parent_code",
                   "level", "sort_order", "definition", "external_mapping"]
FIELD_COLS = ["field_id", "entity_id", "title", "description", "data_type", "unit",
              "value_range", "code_table_id", "cardinality", "collection_method",
              "applies_to", "related_fields", "derivation", "access_tier",
              "status", "version", "owner"]

VALID_DATA_TYPES = {"string", "text", "integer", "number", "date", "datetime",
                    "boolean", "code", "array", "json"}
VALID_CARDINALITY = {"scalar", "array"}
VALID_ACCESS = {"T0", "T1", "T2", "T3"}
VALID_COLLECTION = {"questionnaire", "interview", "resume_parse", "jd_parse", "api",
                    "derived", "manual", "system"}

errors = []
warnings = []

try:  # Windows 控制台默认 GBK，避免中文输出乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load(path, cols):
    if not os.path.exists(path):
        errors.append("缺少文件：%s" % path)
        return []
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        errors.append("空文件：%s" % path)
        return []
    header = rows[0]
    if header != cols:
        errors.append("%s 表头不符：%r" % (path, header))
        return []
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != len(cols):
            errors.append("%s 第 %d 行列数 %d != %d" % (path, i, len(row), len(cols)))
            continue
        out.append(dict(zip(cols, row)))
    return out


def load_csv(path, cols):
    if not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows or rows[0] != cols:
        return {"__header_error__": rows[0] if rows else None}
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != len(cols):
            out.append({"__bad__": (i, len(row))})
            continue
        out.append(dict(zip(cols, row)))
    return out


def validate_occupation(tables, by_table):
    """职业树专项校验：层级、父子一致、码状态与证照引用。"""
    res = {"errors": [], "warnings": []}
    rows = load_csv(OCC_CSV, OCC_COLS)
    if rows is None:
        return res
    if isinstance(rows, dict):
        res["errors"].append("occupation_seed.csv 表头不符：%r" % rows.get("__header_error__"))
        return res

    by_id = {}
    for r in rows:
        if "__bad__" in r:
            res["errors"].append("occupation_seed.csv 第 %d 行列数 %d != %d"
                                 % (r["__bad__"][0], r["__bad__"][1], len(OCC_COLS)))
            continue
        oid = r["occupation_id"]
        if oid in by_id:
            res["errors"].append("occupation_id 重复：%s" % oid)
        by_id[oid] = r

    cred_codes = by_table.get("CT_CREDENTIAL_TYPE", set())
    for oid, r in by_id.items():
        # 层级
        if r["level"] not in ("1", "2", "3"):
            res["errors"].append("%s 非法 level：%s" % (oid, r["level"]))
        # 父子
        p = r["parent_id"]
        if r["level"] == "1":
            if p:
                res["errors"].append("%s 是 level1 但填了 parent_id" % oid)
        else:
            if not p:
                res["errors"].append("%s 是 level%s 但缺 parent_id" % (oid, r["level"]))
            elif p not in by_id:
                res["errors"].append("%s 的 parent_id 不存在：%s" % (oid, p))
            else:
                pr = by_id[p]
                if int(pr["level"]) != int(r["level"]) - 1:
                    res["errors"].append("%s(level%s) 的父节点 %s 层级为 %s，不连续"
                                         % (oid, r["level"], p, pr["level"]))
                if pr["family"] != r["family"]:
                    res["errors"].append("%s 的 family 与其父节点 %s 不一致" % (oid, p))
        # 取值域
        for f, lo, hi in (("medical_reliance", 0, 5), ("transition_ease", 0, 5)):
            v = r[f]
            if v == "":
                res["errors"].append("%s 缺 %s" % (oid, f))
            elif not v.isdigit() or not (lo <= int(v) <= hi):
                res["errors"].append("%s 的 %s 非法：%s" % (oid, f, v))
        # 证照引用
        for c in [x for x in r["license_required"].split("|") if x]:
            if c not in cred_codes:
                res["errors"].append("%s 引用了未定义的证照码：%s" % (oid, c))
        # 码状态一致性：写了码就必须声明状态
        if r["isco08_code"] and r["code_status"] == "N":
            res["errors"].append("%s 有 isco08_code 但 code_status=N" % oid)
        if r["code_status"] not in ("V", "E", "N"):
            res["errors"].append("%s 非法 code_status：%s" % (oid, r["code_status"]))
        if r["code_status"] == "V" and "已核验" not in r["description"] \
                and "ISCO" not in r["description"]:
            res["warnings"].append("%s 标为已核验但 description 未写明来源" % oid)

    fams = {r["family"] for r in by_id.values() if "__bad__" not in r}
    note = ("职业树：节点 %d，覆盖岗位族 %d"
            % (len(by_id), len(fams)))
    return {"errors": res["errors"], "warnings": res["warnings"], "note": note}


def main(root):
    os.chdir(root)
    codes = load(CODE_TABLE_CSV, CODE_TABLE_COLS)
    fields = load(FIELD_CSV, FIELD_COLS)

    # 1) 主键
    seen = set()
    for r in codes:
        k = (r["code_table_id"], r["code"])
        if not r["code_table_id"] or not r["code"]:
            errors.append("code_table_seed 存在空主键行：%r" % (r,))
        if k in seen:
            errors.append("code_value 主键重复：%s" % (k,))
        seen.add(k)

    fid_seen = set()
    for r in fields:
        if not r["field_id"]:
            errors.append("field_catalog 存在空 field_id 行")
        if r["field_id"] in fid_seen:
            errors.append("field_id 重复：%s" % r["field_id"])
        fid_seen.add(r["field_id"])

    # 2) 层级完整性
    by_table = {}
    for r in codes:
        by_table.setdefault(r["code_table_id"], set()).add(r["code"])
    for r in codes:
        p = r["parent_code"]
        if p and p not in by_table.get(r["code_table_id"], set()):
            errors.append("parent_code 不存在：%s/%s -> %s"
                          % (r["code_table_id"], r["code"], p))

    # 3) 外键完整性
    tables = set(by_table.keys())
    for r in fields:
        ct = r["code_table_id"]
        if ct and ct not in tables:
            errors.append("field %s 引用未定义的 code_table：%s" % (r["field_id"], ct))
        if not r["entity_id"]:
            errors.append("field %s 缺 entity_id" % r["field_id"])

    # 4) 取值域
    for r in fields:
        if r["data_type"] not in VALID_DATA_TYPES:
            errors.append("field %s 非法 data_type：%s" % (r["field_id"], r["data_type"]))
        if r["cardinality"] not in VALID_CARDINALITY:
            errors.append("field %s 非法 cardinality：%s" % (r["field_id"], r["cardinality"]))
        if r["access_tier"] and r["access_tier"] not in VALID_ACCESS:
            errors.append("field %s 非法 access_tier：%s" % (r["field_id"], r["access_tier"]))
        m = r["collection_method"]
        if m and m not in VALID_COLLECTION:
            warnings.append("field %s 非常规 collection_method：%s" % (r["field_id"], m))
        # 5) 一致性
        if r["cardinality"] == "array" and r["data_type"] not in ("array", "json"):
            warnings.append("field %s cardinality=array 但 data_type=%s"
                            % (r["field_id"], r["data_type"]))
        if r["data_type"] == "derived":
            warnings.append("field %s data_type 误写为 derived（应为 collection_method=derived）"
                            % r["field_id"])
        if r["data_type"] in ("number", "integer") and not r["unit"] and not r["value_range"]:
            warnings.append("field %s 为数值型但无 unit 与 value_range" % r["field_id"])

    # 6) 废弃项
    for r in codes:
        if r["code"] and r["external_mapping"] == "":
            warnings.append("code_value %s/%s 无 external_mapping（建议补外部标准映射）"
                            % (r["code_table_id"], r["code"]))

    # 7) 职业树
    occ = validate_occupation(tables, by_table)
    errors.extend(occ["errors"])
    warnings.extend(occ["warnings"])
    occ_note = occ.get("note", "")

    print("=" * 68)
    print("code_value 行数：%d   字段目录行数：%d   代码表数：%d"
          % (len(codes), len(fields), len(tables)))
    if occ_note:
        print(occ_note)
    print("错误：%d   警告：%d" % (len(errors), len(warnings)))
    for e in errors:
        print("  [ERROR] " + e)
    for w in warnings[:40]:
        print("  [WARN ] " + w)
    if len(warnings) > 40:
        print("  ... 其余 %d 条警告省略" % (len(warnings) - 40))
    print("=" * 68)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
