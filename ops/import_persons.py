# -*- coding: utf-8 -*-
"""
ops/import_persons.py —— 从表格（.xlsx / .csv）批量导入个体，**含字段缺失处理**

场景
--------------------------------------------------------------------------
小程序那边用户注册后导出一张表格，我们需要把它导进本库。
表格是**调查问卷的原始产物**：有的列没人填、有的单元格空着、有的写了"未填写"。

一条必须讲清楚的纪律：**缺失 ≠ 空值**
--------------------------------------------------------------------------
处理"没填"有两种做法，差别很大：
  ✗ 写一个空串/空值进库 —— 之后没人分得清"他没填"和"他答了空"，
    统计时会被当成一个合法取值（例如"空"变成海外经历的一个类别）。
  ✓ **不写这一条记录** —— 库里根本没有这行，于是"缺失"是**可数的事实**：
    数得出来有多少人没填（`count(*)` 的分母就是填了的人）。
本工具一律用后者。并且把"没填"分成三种，分别统计、分别报告：
  ① **整列缺失**：表格里压根没这列（这一批人全都没这个数据）
  ② **单元格为空**：列在，但这格空着 / 写着"未填写"之类
  ③ **值无法解析**：写了内容，但不是该字段的合法码（例如自由文本填进码字段）
三者的处置完全不同：①要问导出方要列，②③要回去补数据或改字典。

两条写入路径（对应两种字段）
--------------------------------------------------------------------------
  · **契约字段**（当前阶段/学历/专业/城市/意向职业…）→ 走 `exchange.ingest`，
    于是 bridge 的六条规则全部生效（身份、幂等、版本门、墓碑、授权门、crosswalk），
    与小程序注册**完全同一条路**；
  · **动态字段**（`field_catalog` 里登记、entity=person 的，例如"海外经历"）→ 走
    `mt.set_value`，落到 `field_value`。这样新加的分类变量（第 3 件事）也能从表格里导进来。
分开的理由：契约字段是**小程序注册契约**的一部分（有 crosswalk 与校验），
动态字段是**本库自己的扩展**（零 DDL 加列）。把两者混在一条路上会破坏契约边界。

用法
    python ops/import_persons.py --make-sample .tools/sample_persons.xlsx
    python ops/import_persons.py --file .tools/sample_persons.xlsx            # 默认只 plan
    python ops/import_persons.py --file .tools/sample_persons.xlsx --apply
    python ops/import_persons.py --cleanup                                    # 清理本来源导入的数据
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.join(BASE, "ops"))

SRC = "xlsimport"                      # 来源命名空间：清理按它**精确匹配**
EXT_PREFIX = "xls_"
NOTICE_VERSION = "notice_1.0.0"

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")

# 视为"没填"的字面量（**不用 `无`** —— 它在"海外经历"里是合法答案）
MISSING_MARKERS = {"", "未填写", "未填", "空", "n/a", "N/A", "na", "NA",
                   "-", "—", "/", "不知道", "不清楚", "(空)", "null", "NULL"}

# 契约字段：表格表头 → 交换包里的位置
CONTRACT = {
    "当前阶段": "current_stage",
    "current_stage": "current_stage",
    "学历": "education.degree_level",
    "education.degree_level": "education.degree_level",
    "专业": "education.major",
    "education.major": "education.major",
    "意向城市": "current_city",
    "current_city": "current_city",
}
ID_COLS = {"外部编号", "ext_id", "external_person_id", "personid", "person_id"}
CONSENT_COLS = {"同意个人分析", "consent_personal_analysis", "授权"}
OCC_COLS = {"意向职业", "target_occupations", "targetOccupations"}


def read_table(path):
    """读 .xlsx 或 .csv，返回 (表头列表, [行字典…])。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise SystemExit("[X] 需要 openpyxl 才能读 Excel：python -m pip install openpyxl")
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            raise SystemExit("[X] 表格是空的")
        head = [str(h).strip() if h is not None else "" for h in rows[0]]
        out = []
        for r in rows[1:]:
            if all(v is None or str(v).strip() == "" for v in r):
                continue                              # 整行空 → 跳过（Excel 常见的尾部空行）
            out.append({head[i]: ("" if v is None else str(v).strip())
                        for i, v in enumerate(r) if i < len(head)})
        return head, out
    # csv
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=enc, newline="") as fh:
                rd = list(csv.DictReader(fh))
            return (list(rd[0].keys()) if rd else []), rd
        except UnicodeDecodeError:
            continue
    raise SystemExit("[X] 读不出这个 CSV（试过 utf-8-sig 与 gbk）")


def person_fields(c):
    """person 实体上已登记的字段：{小写标识: (field_id, title, code_table_id, data_type)}。

    匹配用**中文标题或 field_id** —— 导出方给的表格用中文表头，
    而工程侧习惯用 field_id，两者都要认（否则用户得手工改名才能导）。
    """
    out = {}
    for r in c.execute("""SELECT field_id, title, code_table_id, data_type
                            FROM mt.field_catalog
                           WHERE entity_id = 'person' AND status = 'active'"""):
        out[r["field_id"].lower()] = r
        if r["title"]:
            out[r["title"].strip().lower()] = r
    return out


def classify(headers, fields):
    """把表格的每一列归到：id / consent / contract / dynamic / 不认识。"""
    plan = {}
    for h in headers:
        k = (h or "").strip()
        kl = k.lower()
        if not k:
            continue
        if kl in ID_COLS:
            plan[k] = ("id", None)
        elif kl in CONSENT_COLS:
            plan[k] = ("consent", None)
        elif k in CONTRACT:
            plan[k] = ("contract", CONTRACT[k])
        elif kl in OCC_COLS:
            plan[k] = ("occupations", None)
        elif kl in fields:
            plan[k] = ("dynamic", fields[kl])
        else:
            plan[k] = ("unknown", None)
    return plan


def is_missing(v):
    return (v is None) or (str(v).strip() in MISSING_MARKERS)


def build_package(ext, ver, facts, occs, consent):
    """与小程序注册**同一个契约**（见 portal_mockreg.build_package）。"""
    import exchange as EX
    seed = "%s|%s|%s" % (SRC, ext, ver)
    h = hashlib.sha1(seed.encode()).hexdigest()[:20]
    return {
        "schemaVersion": EX.SCHEMA_VERSION,
        "sourceSystem": SRC,
        "personId": ext,
        "eventId": "evt_" + h,
        "eventType": "profile_upsert",
        "submissionId": "sub_" + h,
        "requestId": "req_xls_%s" % h[:12],
        "questionnaireId": "xls_import",
        "questionnaireVersion": "1.0.0",
        "catalogId": "xls_1.0.0",
        "mappingVersion": "xw_1.0.0",
        "aggregateVersion": ver,
        "facts": facts,
        "education": [],
        "experience": [],
        "skillClaims": [],
        "preferences": {"targetOccupations": occs, "industries": [], "workModes": []},
        "consents": {"noticeVersion": NOTICE_VERSION, "personalAnalysis": consent,
                     "opportunityNotifications": False},
    }


def cmd_import(a):
    import exchange as EX
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        fields = person_fields(c)
        head, rows = read_table(a.file)

    plan = classify(head, fields)
    print("表格：%s" % a.file)
    print("表头 %d 列：%s" % (len(head), "、".join(head)))
    print()
    print("列映射：")
    unknown = []
    for h, (kind, tgt) in plan.items():
        if kind == "dynamic":
            print("  %-22s → 动态字段 %s（%s）" % (h, tgt["field_id"], tgt["title"]))
        elif kind == "contract":
            print("  %-22s → 契约字段 %s" % (h, tgt))
        elif kind == "unknown":
            print("  %-22s → **不认识**（会跳过并计入报告）" % h)
            unknown.append(h)
        else:
            print("  %-22s → %s" % (h, kind))
    print()

    # ---- 缺失统计：三种，分开数 ----
    missing_col = [h for h, (k, _t) in plan.items() if k == "unknown"]
    # 期望有但没有的列（用于提示导出方）
    expect = ["当前阶段", "学历", "专业", "意向城市", "海外经历"]
    absent = [e for e in expect if e not in head]
    if absent:
        print("[!] ① 整列缺失（这一批人全都没这个数据）：%s" % "、".join(absent))
        print("    → 该字段整体跳过（**不写空值**）；要的话得让导出方补上这一列。")

    stats = {"rows": len(rows), "imported": 0, "failed": 0, "no_consent": 0,
             "cell_missing": {}, "bad_value": {}, "dynamic_written": 0,
             "contract_written": 0}
    results = []
    for i, row in enumerate(rows, 2):                  # 2 = 表头占第 1 行
        ext = ""
        for h, (k, _t) in plan.items():
            if k == "id":
                ext = (row.get(h) or "").strip()
        if not ext:
            ext = EXT_PREFIX + hashlib.sha1(
                (a.file + "|" + str(i)).encode()).hexdigest()[:10]
        if not ext.startswith(EXT_PREFIX):
            ext = EXT_PREFIX + ext

        consent = False
        for h, (k, _t) in plan.items():
            if k == "consent":
                consent = (row.get(h) or "").strip() in ("1", "是", "同意", "Y", "y", "true")

        facts, occs = [], []
        for h, (k, tgt) in plan.items():
            v = row.get(h)
            if is_missing(v):
                if k in ("contract", "dynamic"):
                    stats["cell_missing"][tgt if isinstance(tgt, str)
                                          else tgt["field_id"]] = \
                        stats["cell_missing"].get(
                            tgt if isinstance(tgt, str) else tgt["field_id"], 0) + 1
                continue
            if k == "contract":
                if tgt == "education.major":
                    facts.append({"fieldId": tgt, "instanceId": "education/edu_1",
                                  "status": "answered", "valueCodes": [v]})
                elif tgt == "education.degree_level":
                    facts.append({"fieldId": tgt, "instanceId": "education/edu_1",
                                  "status": "answered", "valueCodes": [v]})
                else:
                    facts.append({"fieldId": tgt, "status": "answered", "valueCodes": [v]})
                stats["contract_written"] += 1
            elif k == "occupations":
                occs.extend([x.strip() for x in str(v).replace("，", ",").split(",") if x.strip()])

        if not facts and not occs:
            facts.append({"fieldId": "target_direction", "status": "prefer_not_to_say"})

        if not a.apply:
            results.append((ext, "计划导入", facts, consent))
            continue

        # 版本：取该身份已处理版本 +1（让版本门自然生效，而不是靠用户猜）
        with psycopg.connect(ADMIN, row_factory=dict_row) as c2:
            r = c2.execute("""SELECT last_seen_version FROM mt.external_identity
                               WHERE source_system=%s AND external_person_id=%s""",
                           (SRC, ext)).fetchone()
        ver = (r["last_seen_version"] if r else 0) + 1
        try:
            res = EX.ingest(build_package(ext, ver, facts, occs, consent))
            stats["imported"] += 1
            status = "已导入（版本 %s）" % ver
        except EX.ExchangeError as e:
            stats["failed"] += 1
            if e.code == "CONSENT_REQUIRED":
                stats["no_consent"] += 1
            status = "被拒：%s —— %s" % (e.code, e.message[:60])
        results.append((ext, status, facts, consent))

        # 动态字段：走 set_value（person 必须先存在）
        if a.apply and not status.startswith("被拒"):
            with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c3:
                pid = c3.execute("""SELECT person_id FROM mt.external_identity
                                     WHERE source_system=%s AND external_person_id=%s""",
                                 (SRC, ext)).fetchone()
                if pid:
                    for h, (k, tgt) in plan.items():
                        if k != "dynamic":
                            continue
                        v = row.get(h)
                        if is_missing(v):
                            continue
                        try:
                            c3.execute("SELECT mt.set_value('person', %s, %s, %s)",
                                       (pid["person_id"], tgt["field_id"], str(v).strip()))
                            stats["dynamic_written"] += 1
                        except psycopg.Error as e:
                            key = tgt["field_id"]
                            stats["bad_value"][key] = stats["bad_value"].get(key, 0) + 1
                            results.append((ext, "  动态字段 %s 拒绝：%s"
                                            % (key, str(e).splitlines()[0][:70]), [], consent))

    # ---- 报告 ----
    print("\n" + "=" * 74)
    if not a.apply:
        print("【计划】共 %d 行，未写任何数据（加 --apply 才真正导入）" % stats["rows"])
    else:
        print("【结果】共 %d 行：导入 %d、失败 %d（其中授权门拒绝 %d）"
              % (stats["rows"], stats["imported"], stats["failed"], stats["no_consent"]))
        print("       契约字段写入 %d 次；动态字段写入 %d 次"
              % (stats["contract_written"], stats["dynamic_written"]))
    if stats["cell_missing"]:
        print("\n② 单元格为空（列在、这格没填）→ **不写该字段**：")
        for k, v in sorted(stats["cell_missing"].items(), key=lambda x: -x[1]):
            print("   %-28s %d 行" % (k, v))
    if stats["bad_value"]:
        print("\n③ 值无法解析（写了但不是合法码）→ 该字段被拒：")
        for k, v in stats["bad_value"].items():
            print("   %-28s %d 行" % (k, v))
    if unknown:
        print("\n不认识的列（跳过，未写入）：%s" % "、".join(unknown))
    print("\n前 5 行明细：")
    for ext, st, facts, consent in results[:5]:
        print("   %-18s %-40s 事实 %d 条 授权 %s"
              % (ext, st[:40], len(facts), "是" if consent else "否"))
    if not a.apply:
        print("\n下一步：加 --apply 真正导入。")
    return 0


def cmd_cleanup(a):
    """清理本来源导入的数据。**按 source_system 精确匹配**，绝不用 LIKE 前缀。"""
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        n_ext = c.execute("""SELECT count(*) AS n FROM mt.external_identity
                              WHERE source_system = %s""", (SRC,)).fetchone()["n"]
        pids = [r["person_id"] for r in c.execute(
            "SELECT person_id FROM mt.external_identity WHERE source_system = %s", (SRC,))]
        if not pids:
            print("[=] 没有 %s 来源的数据，无需清理" % SRC)
            return 0
        n_fv = c.execute("""SELECT count(*) AS n FROM mt.field_value
                             WHERE subject_type='person' AND subject_id = ANY(%s)""",
                         (pids,)).fetchone()["n"]
        n_sess = c.execute("SELECT count(*) AS n FROM mt.response_session "
                           "WHERE person_id = ANY(%s)", (pids,)).fetchone()["n"]
        print("将清理：外部身份 %d、person %d、动态字段值 %d、答卷会话 %d"
              % (n_ext, len(pids), n_fv, n_sess))
        if not a.yes:
            print("[=] 未执行（加 --yes 确认）。提示：person 上还挂着子女表与审计，"
                  "清理会逐表按外键顺序进行。")
            return 0
        # 复用 mock 窗口那套**已实测**的外键顺序清理（不再另写一份：
        # 清理覆盖面必须等于构造覆盖面，两处各写一份必然分叉）
        sys.path.insert(0, os.path.join(BASE, "code", "demo"))
        import portal_mockreg as M
        n_del = M.hardclean_persons(c, pids)
        c.execute("DELETE FROM mt.external_identity WHERE source_system = %s", (SRC,))
        c.commit()
    print("[✓] 已清理 %s 来源：%s" % (SRC, "、".join("%s %d" % (k, v)
                                                    for k, v in sorted(n_del.items()))))
    return 0


def cmd_sample(a):
    """生成一份**故意含缺失**的示例表格（用来演示缺失处理）。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "注册导出"
    head = ["外部编号", "同意个人分析", "当前阶段", "学历", "专业", "意向城市",
            "意向职业", "海外经历", "备注（不认识的列）"]
    ws.append(head)
    rows = [
        ["u001", "是", "ST1", "DG3", "MA01", "REG_11", "occ_1,occ_2", "Y", "正常一行"],
        ["u002", "是", "ST1", "DG2", "", "REG_12", "occ_3", "N", "学历有、专业空"],
        ["u003", "否", "ST2", "DG3", "MA02", "REG_11", "", "Y", "**未授权** → 会被授权门拒绝"],
        ["u004", "是", "", "", "MA03", "", "", "", "**几乎全缺**：只给了专业"],
        ["u005", "是", "ST1", "DG4", "MA04", "REG_13", "occ_1", "未填写", "写了「未填写」"],
        ["u006", "", "ST3", "DG2", "MA05", "REG_11", "occ_2", "N", "授权列也空着 → 按未授权处理"],
        ["u007", "是", "ST1", "DG3", "MA06", "REG_12", "occ_4", "Y", "再一行正常的"],
    ]
    for r in rows:
        ws.append(r)
    os.makedirs(os.path.dirname(os.path.abspath(a.make_sample)), exist_ok=True)
    wb.save(a.make_sample)
    print("[✓] 已生成示例表格：%s" % a.make_sample)
    print("    共 %d 行，**故意含缺失**：" % len(rows))
    print("    · u002 学历有、专业空     · u004 几乎全缺（只给专业）")
    print("    · u003 未授权（验证授权门）· u005 写了「未填写」")
    print("    · 最后一列「备注」是未知列（验证「不认识的列被跳过并报告」）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="从表格批量导入个体（含字段缺失处理）")
    ap.add_argument("--file")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只 plan）")
    ap.add_argument("--make-sample", metavar="PATH", help="生成示例表格")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.make_sample:
        return cmd_sample(a)
    if a.cleanup:
        return cmd_cleanup(a)
    if not a.file:
        raise SystemExit("[X] 需要 --file（或用 --make-sample 生成示例）")
    return cmd_import(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
