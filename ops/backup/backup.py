#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ops/backup/backup.py —— 备份 / 清理 / 校验 / 恢复（T12）

四件事，各自独立可跑，也可由 --all 串起来：

  backup   全库逻辑备份（pg_dump -Fc）+ 用户数据 XLSX 副本 + manifest(sha256) + 入库留痕
  prune    按策略清理超期备份（GFS：日/周/月分层；**只删文件，记录保留并标记 pruned**）
  verify   校验备份文件与 manifest 的 sha256（发现静默损坏）
  restore  从备份恢复到**新数据库**并逐表核对行数（不覆盖生产库）
  list     列出备份及其保留状态

目录结构：
  ops/backup/<backup_id>/
      medtalent.dump          全库（自定义格式，pg_restore 可读）
      user_data.xlsx          用户数据副本（多 sheet；默认剔除 PII 列）
      manifest.json           文件清单 + sha256 + 各表行数 + 策略快照
      RESTORE.md              恢复指引

用法：
  python ops/backup/backup.py backup
  python ops/backup/backup.py backup --include-pii          # 显式导出 PII（T3）
  python ops/backup/backup.py list
  python ops/backup/backup.py prune --dry-run
  python ops/backup/backup.py verify --all
  python ops/backup/backup.py restore --backup <id> --target-db medtalent_verify
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
PGBIN = r"D:\wbo-workspace\.tools\pgsql\bin"
BACKUP_ROOT = os.path.join(BASE, "ops", "backup")
HOST, PORT, DB, USER = "127.0.0.1", "55432", "medtalent", "postgres"

# 用户数据副本要导出的表（顺序即 sheet 顺序）。
# pii=False 表示该表的敏感列会被剔除；剔除规则见 PII_COLUMNS。
USER_TABLES = [
    ("person", False), ("person_demographics", True), ("education_record", False),
    ("training_record", False), ("credential", False), ("employment_record", False),
    ("research_output", False), ("skill_assertion", False), ("evidence", False),
    ("preference", False), ("person_constraint", True), ("observation_window", False),
    ("consent_record", False), ("external_identity", False),
    ("experience_episode", False), ("experience_task", False),
    ("response_session", False), ("answer", False),
]
BUSINESS_TABLES = [
    ("occupation", False), ("job_posting", False), ("job_task", False),
    ("job_requirement", False), ("job_competency_weight", False), ("employer", False),
    ("concept", False), ("code_table", False), ("code_value", False),
    ("field_catalog", False), ("category_node", False), ("source_registry", False),
    ("backup_run", False), ("change_log", False),
]
# 这些列属于可识别信息，默认不导出（--include-pii 时才带上）
PII_COLUMNS = {"person_pii": ["full_name_enc", "phone_enc", "email_enc",
                              "wechat_enc", "id_hash", "emergency_contact_enc"],
               "person_demographics": ["health_limits"],
               "person_constraint": ["description"]}


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, env_extra=None):
    env = dict(os.environ)
    env["PGCLIENTENCODING"] = "UTF8"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def do_backup(include_pii=False, note=None, backup_type="full"):
    with conn() as c:
        pol = c.cursor()
        pol.execute("SELECT * FROM backup_policy WHERE enabled ORDER BY policy_id LIMIT 1")
        policy = pol.fetchone()
    if not policy:
        print("[X] 没有启用的备份策略")
        return 1
    if include_pii:
        policy = dict(policy)
        policy["include_pii"] = True

    bid = "bk_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(BACKUP_ROOT, bid)
    os.makedirs(outdir, exist_ok=True)
    expires = dt.date.today() + dt.timedelta(days=int(policy["keep_daily"]))

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO backup_run (backup_id, policy_id, backup_type,
                              status, contains_pii, access_tier, expires_at, note)
                           VALUES (%s,%s,%s,'running',%s,%s,%s,%s)""",
                        (bid, policy["policy_id"], backup_type, bool(policy["include_pii"]),
                         "T3" if policy["include_pii"] else "T2", expires, note))
        c.commit()

    print("[*] 备份 %s → %s" % (bid, outdir))
    try:
        return _do_backup_body(bid, outdir, policy, note, backup_type)
    except Exception as e:  # noqa: BLE001
        # 任何异常都要把记录落成 failed，不能让它停在 running
        _fail(bid, "%s: %s" % (type(e).__name__, e))
        return 1


def _do_backup_body(bid, outdir, policy, note, backup_type):
    expires = dt.date.today() + dt.timedelta(days=int(policy["keep_daily"]))
    files = []
    table_counts = {}

    # 1) 全库逻辑备份
    dump = os.path.join(outdir, "medtalent.dump")
    rc, so, se = run([os.path.join(PGBIN, "pg_dump.exe"), "-h", HOST, "-p", PORT,
                      "-U", USER, "-d", DB, "-Fc", "-f", dump])
    if rc != 0:
        _fail(bid, "pg_dump 失败：%s" % se[:200])
        return 1
    size = os.path.getsize(dump)
    files.append(_file_entry(dump))
    print("    [✓] 全库备份 %.1f MB" % (size / 2**20))

    # 2) 用户数据 XLSX 副本
    xlsx = None
    if policy["export_xlsx"]:
        xlsx = os.path.join(outdir, "user_data.xlsx")
        table_counts = _export_xlsx(xlsx, include_pii=bool(policy["include_pii"]))
        files.append(_file_entry(xlsx))
        print("    [✓] 用户数据副本 %d 张表（PII %s）"
              % (len(table_counts), "已包含" if policy["include_pii"] else "已剔除"))

    # 3) manifest
    manifest = {
        "backupId": bid,
        "createdAt": dt.datetime.now().isoformat(timespec="seconds"),
        "database": DB,
        "backupType": backup_type,
        "containsPii": bool(policy["include_pii"]),
        "accessTier": "T3" if policy["include_pii"] else "T2",
        "policy": {k: policy[k] for k in ("policy_id", "keep_daily", "keep_weekly",
                                          "keep_monthly", "min_copies")},
        "expiresAt": expires.isoformat(),
        "tableCounts": table_counts,
        "files": files,
        "pgDumpFormat": "custom (-Fc)，用 pg_restore 恢复",
        "restoreHint": "python ops/backup/backup.py restore --backup %s --target-db <新库名>" % bid,
    }
    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    # 4) 恢复指引（脱离代码也能恢复）
    with open(os.path.join(outdir, "RESTORE.md"), "w", encoding="utf-8") as fh:
        fh.write(_restore_md(bid, manifest))

    # 包哈希只覆盖 manifest 里列出的产物（dump / xlsx）。
    # 不要把 manifest.json 自己算进去——它自身的内容又引用这份清单，会自指矛盾，
    # 表现为 verify 永远报"清单哈希与库中记录不符"（实测踩到过）。
    pkg_hash = hashlib.sha256(
        json.dumps([f["sha256"] for f in manifest["files"]], sort_keys=True)
        .encode()).hexdigest()

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""UPDATE backup_run SET finished_at=now(), status='ok',
                              dump_path=%s, xlsx_path=%s, manifest_path=%s, size_bytes=%s,
                              sha256=%s, table_counts=%s::jsonb
                           WHERE backup_id=%s""",
                        (os.path.relpath(dump, BASE),
                         os.path.relpath(xlsx, BASE) if xlsx else None,
                         os.path.relpath(mpath, BASE), size, pkg_hash,
                         json.dumps(table_counts, ensure_ascii=False), bid))
        c.commit()
    print("[✓] 备份完成：%s（%.1f MB，清单哈希 %s）"
          % (bid, size / 2**20, pkg_hash[:16]))
    return 0


def _file_entry(path):
    return {"name": os.path.basename(path), "bytes": os.path.getsize(path),
            "sha256": sha256_file(path)}


def _export_xlsx(path, include_pii=False) -> dict:
    """把用户/业务数据导出为多 sheet XLSX。返回 {表名: 行数}。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    counts = {}
    drop = {} if include_pii else PII_COLUMNS
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F6FEB")

    with conn() as c, c.cursor() as cur:
        for table, _sensitive in USER_TABLES + BUSINESS_TABLES:
            cur.execute("SELECT count(*) AS n FROM information_schema.tables "
                        "WHERE table_schema='mt' AND table_name=%s", (table,))
            if cur.fetchone()["n"] == 0:
                continue
            cols = [r["column_name"] for r in _columns(cur, table)
                    if r["column_name"] not in drop.get(table, [])]
            cur.execute("SELECT %s FROM mt.%s" % (
                ", ".join('"%s"' % c_ for c_ in cols), table))
            rows = cur.fetchall()
            ws = wb.create_sheet(title=table[:31])
            ws.append(cols)
            for cell in ws[1]:
                cell.font = head_font
                cell.fill = head_fill
            for r in rows:
                ws.append([_cell(r[c_]) for c_ in cols])
            for i, c_ in enumerate(cols, 1):
                ws.column_dimensions[get_column_letter(i)].width = min(
                    36, max(10, len(c_) + 4))
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            counts[table] = len(rows)
    wb.save(path)
    return counts


def _columns(cur, table):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='mt' AND table_name=%s ORDER BY ordinal_position""",
                (table,))
    return cur.fetchall()


def _cell(v):
    """把 JSONB/数组/时间/数值等转成 Excel 可读值。

    踩过的坑：NUMERIC 列返回 decimal.Decimal，直接 json.dumps 会抛
    "Object of type Decimal is not JSON serializable"，整次备份因此中断。
    """
    from decimal import Decimal
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<%d bytes>" % len(v)     # 加密字段只记长度，不落明文
    if isinstance(v, list):
        return ", ".join(str(_cell(x)) for x in v)
    return json.dumps(v, ensure_ascii=False, default=str)


def _restore_md(bid, manifest) -> str:
    return """# 恢复指引 · 备份 %s

- 备份时间：%s
- 数据库：%s（pg_dump 自定义格式）
- 含 PII：%s（访问层级 %s）
- 保留到期：%s

## 方式一：整库恢复（推荐，用于灾难恢复）

```powershell
# 1) 建一个空库（不要直接覆盖生产库）
& "D:\\wbo-workspace\\.tools\\pgsql\\bin\\createdb.exe" -h 127.0.0.1 -p 55432 -U postgres medtalent_restored

# 2) 恢复
& "D:\\wbo-workspace\\.tools\\pgsql\\bin\\pg_restore.exe" -h 127.0.0.1 -p 55432 -U postgres `
    -d medtalent_restored --no-owner --role postgres medtalent.dump

# 3) 核对行数（对照 manifest.json 的 tableCounts）
```

或用本脚本一条命令（会新建库并逐表核对）：

```powershell
python ops\\backup\\backup.py restore --backup %s --target-db medtalent_restored
```

## 方式二：仅用户数据（XLSX）

`user_data.xlsx` 是多 sheet 副本，用于人工核对或迁移到别处。
它**不是**权威恢复源（缺主键约束与关系），仅作业务连续性参考。

## 校验

```powershell
python ops\\backup\\backup.py verify --backup %s
```
""" % (bid, manifest["createdAt"], manifest["database"],
       "是" if manifest["containsPii"] else "否", manifest["accessTier"],
       manifest["expiresAt"], bid, bid)


def _fail(bid, msg):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE backup_run SET status='failed', finished_at=now(), "
                        "note=%s WHERE backup_id=%s", (msg, bid))
        c.commit()
    print("[X] " + msg)


# ---------------------------------------------------------------------------
# prune（GFS 保留）
# ---------------------------------------------------------------------------
def plan_prune(policy, backups) -> tuple[list, list]:
    """返回 (保留, 清理)。分层：最近 keep_daily 天每天一份、最近 keep_weekly 周每周一份、
    最近 keep_monthly 月每月一份，且至少保留 min_copies 份。"""
    if not backups:
        return [], []
    today = dt.date.today()
    keep, seen_d, seen_w, seen_m = [], set(), set(), set()
    for b in backups:                       # 已按时间倒序
        d = b["started_at"].date()
        age = (today - d).days
        wk = "%d-W%02d" % d.isocalendar()[:2]
        mo = "%d-%02d" % (d.year, d.month)
        mark = False
        if age < policy["keep_daily"] and d not in seen_d:
            seen_d.add(d)
            mark = True
        elif age < policy["keep_weekly"] * 7 and wk not in seen_w:
            seen_w.add(wk)
            mark = True
        elif age < policy["keep_monthly"] * 31 and mo not in seen_m:
            seen_m.add(mo)
            mark = True
        if mark:
            keep.append(b)
    # 兜底：至少保留 min_copies 份最新的
    keep_ids = {b["backup_id"] for b in keep}
    for b in backups:
        if len(keep_ids) >= policy["min_copies"]:
            break
        if b["backup_id"] not in keep_ids:
            keep_ids.add(b["backup_id"])
            keep.append(b)
    drop = [b for b in backups if b["backup_id"] not in keep_ids]
    return keep, drop


def do_prune(dry_run=False):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM backup_policy WHERE enabled ORDER BY policy_id LIMIT 1")
            policy = cur.fetchone()
            cur.execute("""SELECT backup_id, started_at, dump_path, xlsx_path, manifest_path,
                                  status, size_bytes
                           FROM backup_run WHERE status='ok' ORDER BY started_at DESC""")
            backups = cur.fetchall()
    keep, drop = plan_prune(policy, backups)
    freed = sum(b["size_bytes"] or 0 for b in drop)
    print("[*] 策略 %s：日 %d / 周 %d / 月 %d，至少 %d 份"
          % (policy["policy_id"], policy["keep_daily"], policy["keep_weekly"],
             policy["keep_monthly"], policy["min_copies"]))
    print("    保留 %d 份，清理 %d 份（可释放 %.1f MB）"
          % (len(keep), len(drop), freed / 2**20))
    for b in drop:
        print("      - %s  (%s)" % (b["backup_id"], b["started_at"].date()))
    if dry_run:
        print("[dry-run] 未执行删除")
        return 0
    for b in drop:
        d = os.path.join(BACKUP_ROOT, b["backup_id"])
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        with conn() as c:
            with c.cursor() as cur:
                # 只删文件，记录保留并标记 pruned（审计可查"备份曾存在过"）
                cur.execute("UPDATE backup_run SET status='pruned', pruned_at=now() "
                            "WHERE backup_id=%s", (b["backup_id"],))
            c.commit()
    print("[✓] 已清理 %d 份" % len(drop))
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def do_verify(backup_id=None, all_=False):
    sql = "SELECT backup_id, manifest_path, sha256 FROM backup_run WHERE status='ok'"
    args = ()
    if backup_id:
        sql += " AND backup_id=%s"
        args = (backup_id,)
    sql += " ORDER BY started_at DESC"
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    if not rows:
        print("[X] 没有可校验的备份")
        return 1
    if not all_:
        rows = rows[:1]
    bad = 0
    for r in rows:
        d = os.path.join(BACKUP_ROOT, r["backup_id"])
        mpath = os.path.join(BASE, r["manifest_path"] or "")
        if not os.path.isfile(mpath):
            # 自愈：目录被手工删掉时，把记录标记为 failed，
            # 避免出现"库里显示 ok、实际文件已丢"的假象
            with conn() as c:
                with c.cursor() as cur:
                    cur.execute("UPDATE backup_run SET status='failed', "
                                "note='备份文件缺失（verify 自愈标记）' WHERE backup_id=%s",
                                (r["backup_id"],))
                c.commit()
            print("  [X] %s：manifest 缺失（已标记为 failed）" % r["backup_id"])
            bad += 1
            continue
        with open(mpath, encoding="utf-8") as fh:
            man = json.load(fh)
        errs = []
        for f in man["files"]:
            p = os.path.join(d, f["name"])
            if not os.path.isfile(p):
                errs.append("%s 缺失" % f["name"])
            elif sha256_file(p) != f["sha256"]:
                errs.append("%s 哈希不符（疑似损坏）" % f["name"])
        pkg = hashlib.sha256(json.dumps([f["sha256"] for f in man["files"]],
                                        sort_keys=True).encode()).hexdigest()
        if pkg != r["sha256"]:
            errs.append("清单哈希与库中记录不符")
        if errs:
            bad += 1
            print("  [X] %s：%s" % (r["backup_id"], "；".join(errs)))
        else:
            print("  [✓] %s：%d 个文件全部校验通过" % (r["backup_id"], len(man["files"])))
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
def do_restore(backup_id, target_db, keep=False):
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM backup_run WHERE backup_id=%s", (backup_id,))
        b = cur.fetchone()
    if not b:
        print("[X] 找不到备份 %s" % backup_id)
        return 1
    if b["status"] == "pruned":
        print("[X] 该备份已被清理，无法恢复")
        return 1
    dump = os.path.join(BASE, b["dump_path"])
    with open(os.path.join(BASE, b["manifest_path"]), encoding="utf-8") as fh:
        man = json.load(fh)

    rid = "rs_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO restore_run (restore_id, backup_id, target_db,
                              status, operator) VALUES (%s,%s,%s,'running','cli')""",
                        (rid, backup_id, target_db))
        c.commit()

    print("[*] 恢复到新库 %s（不覆盖生产库）" % target_db)
    run([os.path.join(PGBIN, "dropdb.exe"), "-h", HOST, "-p", PORT, "-U", USER,
         "--if-exists", target_db])
    rc, _, se = run([os.path.join(PGBIN, "createdb.exe"), "-h", HOST, "-p", PORT,
                     "-U", USER, target_db])
    if rc != 0:
        _restore_fail(rid, "createdb 失败：%s" % se[:200])
        return 1
    rc, _, se = run([os.path.join(PGBIN, "pg_restore.exe"), "-h", HOST, "-p", PORT,
                     "-U", USER, "-d", target_db, "--no-owner", "--role", USER, dump])
    # pg_restore 对已存在对象会返回非 0，这里以"能否查到数据"为准
    if rc != 0 and "error" in se.lower() and "already exists" not in se.lower():
        print("    [!] pg_restore 返回 %d：%s" % (rc, se.strip().splitlines()[:1]))

    # 逐表核对
    dsn2 = DSN.replace("dbname=medtalent", "dbname=" + target_db)
    counts, mismatch = {}, {}
    try:
        with psycopg.connect(dsn2, row_factory=dict_row) as c2, c2.cursor() as cur:
            for tbl, expect in (man.get("tableCounts") or {}).items():
                cur.execute("SELECT count(*) AS n FROM mt.%s" % tbl)
                got = cur.fetchone()["n"]
                counts[tbl] = got
                if got != expect:
                    mismatch[tbl] = {"expected": expect, "got": got}
    except Exception as e:  # noqa: BLE001
        _restore_fail(rid, "连接恢复库失败：%s" % e)
        return 1

    status = "verified" if not mismatch else "ok"
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""UPDATE restore_run SET finished_at=now(), status=%s,
                              rows_restored=%s::jsonb, mismatch=%s::jsonb
                           WHERE restore_id=%s""",
                        (status, json.dumps(counts, ensure_ascii=False),
                         json.dumps(mismatch, ensure_ascii=False), rid))
        c.commit()

    print("    [✓] 已恢复 %d 张表" % len(counts))
    if mismatch:
        print("    [!] %d 张表行数不一致：%s" % (len(mismatch), list(mismatch)[:5]))
    else:
        print("    [✓] 全部表行数与备份清单一致")
    if not keep:
        run([os.path.join(PGBIN, "dropdb.exe"), "-h", HOST, "-p", PORT, "-U", USER,
             "--if-exists", target_db])
        print("    [=] 已删除临时恢复库（--keep 可保留）")
    return 1 if mismatch else 0


def _restore_fail(rid, msg):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE restore_run SET status='failed', finished_at=now(), "
                        "note=%s WHERE restore_id=%s", (msg, rid))
        c.commit()
    print("[X] " + msg)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def do_list():
    with conn() as c, c.cursor() as cur:
        cur.execute("""SELECT backup_id, started_at, status, size_bytes, contains_pii,
                              expires_at, pruned_at, table_counts
                       FROM backup_run ORDER BY started_at DESC""")
        rows = cur.fetchall()
        cur.execute("SELECT * FROM v_backup_health")
        h = cur.fetchone()
    print("备份健康：可用 %d 份 / 失败 %d 份 / 合计 %.1f MB / 最早到期 %s / 已验证恢复 %d 次"
          % (h["ok_backups"], h["failed_backups"], (h["total_bytes"] or 0) / 2**20,
             h["earliest_expiry"], h["verified_restores"]))
    print("%-22s %-19s %-8s %9s %6s %s"
          % ("备份 ID", "时间", "状态", "大小", "含PII", "表数"))
    for r in rows:
        print("%-22s %-19s %-8s %8.1fM %6s %s"
              % (r["backup_id"], r["started_at"].strftime("%Y-%m-%d %H:%M"),
                 r["status"], (r["size_bytes"] or 0) / 2**20,
                 "是" if r["contains_pii"] else "否",
                 len(r["table_counts"] or {})))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("backup"); p.add_argument("--include-pii", action="store_true")
    p.add_argument("--note"); p.add_argument("--data-only", action="store_true")
    p = sub.add_parser("prune"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("verify"); p.add_argument("--backup"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("restore"); p.add_argument("--backup", required=True)
    p.add_argument("--target-db", required=True); p.add_argument("--keep", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("all"); p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "backup":
        return do_backup(a.include_pii, a.note, "data_only" if a.data_only else "full")
    if a.cmd == "prune":
        return do_prune(a.dry_run)
    if a.cmd == "verify":
        return do_verify(a.backup, a.all)
    if a.cmd == "restore":
        return do_restore(a.backup, a.target_db, a.keep)
    if a.cmd == "list":
        return do_list()
    if a.cmd == "all":
        rc = do_backup(False)
        if rc:
            return rc
        do_prune(a.dry_run)
        return do_verify(None, True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
