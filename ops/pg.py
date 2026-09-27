#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ops/pg.py — 便携版 PostgreSQL 集群管理（无需管理员权限）

子命令：
  init      初始化数据目录（initdb）
  config    写入 postgresql.conf 监听设置
  start     启动服务
  stop      停止服务
  status    查看状态
  psql      交互/执行 psql
  sql <f>   执行 SQL 文件（ON_ERROR_STOP）
  apply     按序执行 schema/sql/*.sql 全部迁移
  createdb  创建角色与数据库
  up        init + config + start + createdb + apply（一键起库）

约定：
  可执行文件  D:\\wbo-workspace\\.tools\\pgsql\\bin
  数据目录    D:\\wbo-workspace\\.tools\\pgdata
  端口        55432（避开系统默认 5432，避免与既有实例冲突）
  监听        127.0.0.1（仅本机，不对外暴露）
"""
import argparse
import glob
import hashlib
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TOOLS = r"D:\wbo-workspace\.tools"
PGBIN = os.path.join(TOOLS, "pgsql", "bin")
PGDATA = os.path.join(TOOLS, "pgdata")
PGLOG = os.path.join(TOOLS, "pgdata", "server.log")
PORT = "55432"
HOST = "127.0.0.1"
SUPERUSER = "postgres"
DBNAME = "medtalent"
APPUSER = "medtalent"
PROJECT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(PROJECT)  # ops/ 的上一级


def exe(name):
    p = os.path.join(PGBIN, name + ".exe")
    if not os.path.exists(p):
        print("[X] 找不到 %s —— 请先运行 python ops\\install_postgres.py" % p)
        sys.exit(2)
    return p


def run(cmd, check=True, capture=True, env=None):
    print("  $ " + " ".join('"%s"' % c if " " in c else c for c in cmd))
    r = subprocess.run(cmd, capture_output=capture, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if capture:
        if r.stdout and r.stdout.strip():
            print(r.stdout.rstrip())
        if r.stderr and r.stderr.strip():
            print(r.stderr.rstrip())
    if check and r.returncode != 0:
        print("[X] 命令失败，退出码 %d" % r.returncode)
        sys.exit(r.returncode)
    return r


def cmd_init(a):
    if os.path.exists(os.path.join(PGDATA, "PG_VERSION")):
        print("[=] 数据目录已初始化：%s" % PGDATA)
        return
    os.makedirs(PGDATA, exist_ok=True)
    run([exe("initdb"), "-D", PGDATA, "-U", SUPERUSER, "-A", "trust",
         "--encoding=UTF8", "--locale=C"])
    print("[✓] initdb 完成")


def cmd_config(a):
    conf = os.path.join(PGDATA, "postgresql.conf")
    if not os.path.exists(conf):
        print("[X] 未初始化")
        sys.exit(1)
    with open(conf, encoding="utf-8") as fh:
        text = fh.read()
    if "# === medtalent overrides ===" in text:
        print("[=] 配置已写入")
        return
    extra = """
# === medtalent overrides ===
port = %s
listen_addresses = '%s'
shared_buffers = 256MB
work_mem = 16MB
maintenance_work_mem = 128MB
max_connections = 50
log_timezone = 'Asia/Shanghai'
timezone = 'Asia/Shanghai'
lc_messages = 'C'
""" % (PORT, HOST)
    with open(conf, "a", encoding="utf-8") as fh:
        fh.write(extra)
    print("[✓] 已写入 port=%s listen=%s" % (PORT, HOST))


def cmd_start(a):
    if is_running():
        print("[=] 已在运行")
        return
    run([exe("pg_ctl"), "-D", PGDATA, "-l", PGLOG, "-w", "-t", "60", "start"],
        capture=False)
    time.sleep(1)
    cmd_status(a)


def cmd_stop(a):
    if not is_running():
        print("[=] 未运行")
        return
    run([exe("pg_ctl"), "-D", PGDATA, "-m", "fast", "-w", "stop"], capture=False)


def is_running():
    r = subprocess.run([exe("pg_ctl"), "-D", PGDATA, "status"],
                       capture_output=True, text=True)
    return r.returncode == 0


def cmd_status(a):
    r = subprocess.run([exe("pg_ctl"), "-D", PGDATA, "status"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((r.stdout or "") + (r.stderr or ""))
    if r.returncode == 0:
        psql(["-c", "SELECT version();"], db="postgres", quiet_ok=True)


def psql(args, db=DBNAME, quiet_ok=False, capture=True):
    # PGOPTIONS 让每个会话默认带上 mt 搜索路径，避免手写查询时反复加 schema 前缀
    env = dict(os.environ)
    env["PGOPTIONS"] = "-c search_path=mt,public"
    env["PGCLIENTENCODING"] = "UTF8"

    # Windows 下 psql.exe 以 ANSI 代码页解释 argv，含中文的 SQL 用 -c 传入会报
    # "invalid byte sequence for encoding UTF8"。统一改走 UTF-8 临时文件。
    if len(args) == 2 and args[0] in ("-c", "-tAc", "-tA"):
        flags = args[0]
        sql = args[1]
        os.makedirs(os.path.join(TOOLS, "tmp"), exist_ok=True)
        tmp = os.path.join(TOOLS, "tmp", "_pg_tmp.sql")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(sql.rstrip().rstrip(";") + ";\n")
        newargs = (["-tA"] if flags.startswith("-tA") else []) + \
                  ["-v", "ON_ERROR_STOP=1", "-f", tmp]
        args = newargs

    cmd = [exe("psql"), "-h", HOST, "-p", PORT, "-U", SUPERUSER, "-d", db] + args
    return run(cmd, check=not quiet_ok, capture=capture, env=env)


def cmd_psql(a):
    psql(a.rest)


def cmd_sql(a):
    psql(["-v", "ON_ERROR_STOP=1", "-f", os.path.abspath(a.file)])


def cmd_createdb(a):
    psql(["-v", "ON_ERROR_STOP=1", "-c",
          "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='%s') "
          "THEN CREATE ROLE %s LOGIN; END IF; END $$;" % (APPUSER, APPUSER)],
         db="postgres")
    r = psql(["-tAc", "SELECT 1 FROM pg_database WHERE datname='%s'" % DBNAME],
             db="postgres")
    if "1" not in (r.stdout or ""):
        psql(["-c", "CREATE DATABASE %s OWNER %s ENCODING 'UTF8'" % (DBNAME, APPUSER)],
             db="postgres")
        print("[✓] 数据库 %s 已创建" % DBNAME)
    else:
        print("[=] 数据库 %s 已存在" % DBNAME)


MIGRATION_LEDGER = "schema_migration"


def _applied_ledger():
    """读迁移台账 {filename: sha256}。表不存在则返回 None（尚未启用台账）。"""
    r = psql(["-tAc", "SELECT to_regclass('mt.%s') IS NOT NULL" % MIGRATION_LEDGER],
             capture=True, quiet_ok=True)
    if "t" not in (r.stdout or ""):
        return None
    r = psql(["-tAF", "|", "-c", "SELECT filename, coalesce(sha256,'') FROM mt.%s"
              % MIGRATION_LEDGER], capture=True, quiet_ok=True)
    out = {}
    for ln in (r.stdout or "").splitlines():
        if "|" in ln:
            k, v = ln.split("|", 1)
            out[k.strip()] = v.strip()
    return out


def _ledger_ensure():
    psql(["-c", "CREATE SCHEMA IF NOT EXISTS mt"], quiet_ok=True)
    psql(["-c", """CREATE TABLE IF NOT EXISTS mt.%s (
                     filename    TEXT PRIMARY KEY,
                     sha256      TEXT,
                     applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                     note        TEXT)""" % MIGRATION_LEDGER], quiet_ok=True)


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ledger_record(name, sha, note=""):
    psql(["-c", """INSERT INTO mt.%s (filename, sha256, note) VALUES ('%s','%s','%s')
                   ON CONFLICT (filename) DO UPDATE SET sha256=EXCLUDED.sha256,
                     note=EXCLUDED.note, applied_at=now()"""
          % (MIGRATION_LEDGER, name.replace("'", "''"), sha, note.replace("'", "''"))],
         quiet_ok=True)


def cmd_apply(a):
    """按**迁移台账**只应用尚未应用的迁移。

    为什么改（这是本项目的一个真实缺陷，由审查 subagent 发现）：
      原实现只有"全跑"和"直接拒绝"两种行为 —— 在已有结构的库上，不加 --force 被拒，
      加 --force 从 001 重跑、第一句就报 `type "verify_status_t" already exists`。
      结果是**新增迁移根本没有可用的应用路径**，013 只能绕过 pg.py 用 `sql` 手跑。
      而"迁移必须可重放、必须有唯一入口"正是本项目的纪律，工具自己先破了纪律。

    现在：每个迁移文件记录 filename + sha256 + applied_at，只跑未记录的；文件被改过会告警。
    旧库（台账还不存在）做一次 **baseline 采纳**：把已存在的对象对应的文件直接记入台账，
    不重跑 —— 因为重跑非幂等的基线迁移只会坏掉一个正在工作的库。
    """
    files = sorted(glob.glob(os.path.join(PROJECT, "schema", "sql", "*.sql")))
    if not files:
        print("[X] 没有找到 SQL 文件")
        sys.exit(1)

    force = getattr(a, "force", False)
    _ledger_ensure()
    ledger = _applied_ledger() or {}

    # 库里有表但没有台账 → 说明这个库是在台账之前建的，做一次 baseline 采纳
    if not ledger and not force:
        r = psql(["-tAc", "SELECT count(*) FROM information_schema.tables "
                          "WHERE table_schema='mt' AND table_type='BASE TABLE'"],
                 capture=True, quiet_ok=True)
        try:
            existing = int((r.stdout or "0").strip() or 0)
        except ValueError:
            existing = 0
        if existing > 0:
            print("[=] 检测到已有结构的库（mt 下 %d 张表），但还没有迁移台账。" % existing)
            print("    将把 schema/sql/*.sql 全部**记入台账**而不重跑 ——")
            print("    重跑非幂等的基线迁移（001~005）只会弄坏一个正在工作的库。")
            for f in files:
                _ledger_record(os.path.basename(f), _file_sha(f), "baseline-adopted")
            print("[✓] baseline 采纳完成：%d 个迁移已登记。以后 apply 只跑新增的。\n"
                  % len(files))
            ledger = _applied_ledger() or {}

    todo, changed = [], []
    for f in files:
        name = os.path.basename(f)
        sha = _file_sha(f)
        if name in ledger:
            if ledger[name] and ledger[name] != sha:
                changed.append(name)
            continue
        todo.append((name, f, sha))

    for name in changed:
        print("[!] %s 已被修改（内容哈希与台账不符）。迁移应当是追加式的："
              "改已应用的迁移会让别人的库和你的库结构不一致。" % name)
        print("    如果你确实要改结构，请**新增**一个迁移文件。")

    if not todo:
        print("[✓] 没有待应用的迁移（台账已登记 %d 个）" % len(ledger))
        return

    print("待应用 %d 个：%s\n" % (len(todo), "、".join(n for n, _f, _s in todo)))
    for name, f, sha in todo:
        if name.startswith("004_vector") and not vector_available():
            print("[skip] %s —— pgvector 未安装，跳过（见 docs/05 §8 分期）" % name)
            _ledger_record(name, sha, "skipped: pgvector unavailable")
            continue
        print("=== 应用 %s ===" % name)
        rc = psql(["-v", "ON_ERROR_STOP=1", "-f", f], quiet_ok=True)
        if rc is None or getattr(rc, "returncode", 1) != 0:
            print("[X] %s 应用失败，台账未登记。修好后重跑 apply 即可（不会重跑已成功的）"
                  % name)
            sys.exit(1)
        _ledger_record(name, sha)
        print("    [✓] 已登记到台账")
    print("\n[✓] 全部待应用迁移完成（共 %d 个）" % len(todo))


def vector_available():
    r = subprocess.run(
        [exe("psql"), "-h", HOST, "-p", PORT, "-U", SUPERUSER, "-d", "postgres",
         "-tAc", "SELECT 1 FROM pg_available_extensions WHERE name='vector'"],
        capture_output=True, text=True)
    return "1" in (r.stdout or "")


def cmd_reset(a):
    if not getattr(a, "yes", False):
        print("[X] reset 会删除 mt schema 及其全部数据。确认请加 --yes")
        sys.exit(1)
    print("[!] 删除 schema mt（CASCADE）...")
    psql(["-c", "DROP SCHEMA IF EXISTS mt CASCADE"])
    cmd_apply(a)


def cmd_up(a):
    cmd_init(a)
    cmd_config(a)
    cmd_start(a)
    cmd_createdb(a)
    cmd_apply(a)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n, f in [("init", cmd_init), ("config", cmd_config), ("start", cmd_start),
                 ("stop", cmd_stop), ("status", cmd_status), ("createdb", cmd_createdb),
                 ("up", cmd_up)]:
        sub.add_parser(n).set_defaults(func=f)
    pa = sub.add_parser("apply")
    pa.add_argument("--force", action="store_true")
    pa.set_defaults(func=cmd_apply)
    pr = sub.add_parser("reset")
    pr.add_argument("--yes", action="store_true")
    pr.set_defaults(func=cmd_reset)
    p = sub.add_parser("psql")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_psql)
    p = sub.add_parser("sql")
    p.add_argument("file")
    p.set_defaults(func=cmd_sql)
    # parse_known_args：允许 `pg.py psql -c "..."` 这类以 - 开头的参数原样透传
    a, extra = ap.parse_known_args()
    if a.cmd == "psql":
        a.rest = extra + a.rest
    elif extra:
        print("[X] 无法识别的参数：%s" % " ".join(extra))
        sys.exit(2)
    a.func(a)


if __name__ == "__main__":
    main()
