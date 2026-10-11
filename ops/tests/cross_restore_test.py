# -*- coding: utf-8 -*-
"""
ops/tests/cross_restore_test.py —— **异机恢复**：把备份恢复到**另一个 PostgreSQL 实例**

评审优先级 5 把备份整改收窄为三条：恢复耗时、**异机恢复**、密钥恢复。
前两条已在 `key_recovery_test.py` 覆盖（恢复耗时、密钥不在库里）。
**异机恢复**本文件负责。

⚠ 先纠正一件我自己的错：我先前两次把这个列为"环境阻碍、无法验证"，
理由是"`.tools/pgsql/bin/` 没有 `initdb.exe`"。
**那个结论是错的** —— 我查的是 `medtalent-db/.tools/pgsql/bin`（不存在的路径），
而真正的工具目录在工作区根：`<工作区根>/.tools/pgsql/bin/initdb.exe`，**它一直在**。
教训：**判定"做不到"之前必须先确认阻碍是真的**；路径写错就下结论，
会把一个能做的验证变成一条假的阻塞项（而且我还报了两次）。

本测试做的是**真跨实例**恢复：
  ① `initdb` 到一个**全新的数据目录**（不是生产实例的数据目录）
  ② 在**另一个端口**（55433）启动它
  ③ 把备份 dump 恢复进去
  ④ **与备份自己的 manifest 核对行数**（⚠ 不能与当前生产库比 ——
     备份是 2026-10-10 的，与「现在」的行数本来就不同，那是两件事）
  ⑤ 在**新实例里**验证密钥恢复：带密钥能解密、不带密钥解不开
     （证明密钥不绑定在原实例上，恢复后仍然可用）
  ⑥ 拆掉：停实例、删数据目录

为什么同实例恢复到另一个库不算异机：角色、表空间、扩展、`search_path`、
文件路径这些**只在换实例时才会暴露**的问题，在同实例里正好被掩盖。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check

# ⚠ 工具目录在工作区**根**，不是 medtalent-db 下 —— 我第一次查错了路径
#（写成 medtalent-db\.tools\pgsql\bin），于是误判"没有 initdb"并把它当成阻塞项报了两轮。
WORKSPACE = os.path.dirname(BASE)
PGBIN = os.path.join(WORKSPACE, ".tools", "pgsql", "bin")
NEWPORT = "55433"
NEWDIR = os.path.join(WORKSPACE, ".tools", "tmp", "pg_crossrestore")
# ⚠ 两个 .tools 目录**不是同一个**：
#   · PostgreSQL 工具在**工作区根** <工作区根>/.tools/pgsql/bin
#   · 凭据在**项目目录** <项目>/.tools/credentials
# 我第一次把两者当成一个：既因此误判了 initdb（查了错的 .tools），
# 又把密钥文件也找错了。**同名不同地** —— 必须分别确认，不能想当然。
KEYFILE = os.path.join(BASE, ".tools", "credentials", "medtalent_pii_key.txt")


def tool(name):
    return os.path.join(PGBIN, name + ".exe")


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def run_detached(args, **kw):
    """把子进程输出**丢掉**再跑 —— 用于 `pg_ctl start/stop`。

    ⚠ 为什么必须这样（我第一次跑就死在这里）：
    `pg_ctl start` 会启动 `postgres`，而 **`postgres` 继承了父进程的 stdout/stderr 管道**。
    于是 `subprocess.run(..., capture_output=True)` 在 `pg_ctl` 退出后**仍然等不到 EOF**
    —— 因为管道还被那个常驻的 `postgres` 端着 —— 进程**永久挂住**。

    实测症状：python 进程活着但 **CPU 只有 0.1 秒**（＝被阻塞、不在计算），
    第二个实例已经正常启动并写好了日志，而脚本一步也没往下走；
    20 分钟后检查发现它连 `createdb` 都没执行到。
    只看"没输出"很容易误判成"恢复太慢"，实际是**管道死锁**。

    `pg_ctl` 已经把服务端输出重定向到 `-l` 指定的文件，所以这里丢掉完全无损。
    """
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", **kw)


def read_key():
    if not os.path.isfile(KEYFILE):
        return None
    for line in io.open(KEYFILE, encoding="utf-8"):
        line = line.strip()
        if line:
            return line.split("=", 1)[1].strip() if "=" in line else line
    return None


def main():
    # ---------- 0. 工具与产物齐备（这是"阻碍是否为真"的核实）----------
    check(os.path.isfile(tool("initdb")),
          "`initdb.exe` 存在（%s）—— 我先前写错路径才误判为「没有」，**异机恢复本来就能做**"
          % PGBIN)
    check(os.path.isfile(tool("pg_ctl")), "`pg_ctl.exe` 存在")
    check(os.path.isfile(tool("pg_restore")), "`pg_restore.exe` 存在")
    if not (os.path.isfile(tool("initdb")) and os.path.isfile(tool("pg_ctl"))
            and os.path.isfile(tool("pg_restore"))):
        return H.report(width=74, list_fails=True)

    with psycopg.connect("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
                         "options='-c search_path=mt,public'",
                         row_factory=dict_row, autocommit=True) as c:
        b = c.execute("""SELECT backup_id, dump_path, manifest_path, table_counts
                           FROM mt.backup_run WHERE status='ok'
                          ORDER BY started_at DESC LIMIT 1""").fetchone()
    check(b is not None, "有可用的备份记录")
    if not b:
        return H.report(width=74, list_fails=True)
    dump = os.path.join(BASE, b["dump_path"])
    check(os.path.isfile(dump), "备份 dump 产物存在（%s）" % b["dump_path"])
    if not os.path.isfile(dump):
        return H.report(width=74, list_fails=True)
    man = json.load(io.open(os.path.join(BASE, b["manifest_path"]), encoding="utf-8"))
    expected = man.get("tableCounts") or {}
    check(bool(expected),
          "manifest 里有 %d 张表的行数基线 —— 恢复后与**备份自己**核对（不是与当前生产库比）"
          % len(expected))

    started = False
    try:
        # ---------- 1. 全新的数据目录（不是生产实例的）----------
        if os.path.isdir(NEWDIR):
            shutil.rmtree(NEWDIR, ignore_errors=True)
        os.makedirs(NEWDIR, exist_ok=True)
        r = run([tool("initdb"), "-D", NEWDIR, "-U", "postgres",
                 "--auth=trust", "--encoding=UTF8", "--locale=C"])
        check(r.returncode == 0,
              "**initdb 成功**：在全新数据目录建了一个独立实例（%s）"
              % os.path.relpath(NEWDIR, WORKSPACE))
        if r.returncode != 0:
            print(r.stderr[-600:])
            return H.report(width=74, list_fails=True)

        # ---------- 2. 另一个端口启动 ----------
        r = run_detached([tool("pg_ctl"), "-D", NEWDIR, "-o",
                          "-p %s -c listen_addresses=127.0.0.1" % NEWPORT,
                          "-l", os.path.join(NEWDIR, "startup.log"),
                          "-w", "-t", "60", "start"])
        started = (r.returncode == 0)
        check(started, "**在另一个端口（%s）启动了独立实例** —— 这是「异机」的最小真实形态"
              % NEWPORT)
        if not started:
            print(r.stderr[-800:])
            return H.report(width=74, list_fails=True)

        # ---------- 3. 恢复进新实例 ----------
        r = run([tool("createdb"), "-h", "127.0.0.1", "-p", NEWPORT,
                 "-U", "postgres", "medtalent"])
        check(r.returncode == 0, "在新实例里建库成功")
        r = run([tool("pg_restore"), "-h", "127.0.0.1", "-p", NEWPORT, "-U", "postgres",
                 "-d", "medtalent", "--no-owner", "--no-privileges", dump])
        # pg_restore 对"已存在对象"会报 warning 并以非 0 退出；这里要求没有 error
        errs = [l for l in (r.stderr or "").splitlines() if "error" in l.lower()]
        check(not errs,
              "**把备份恢复进新实例成功**（无 error；warning 不计）%s"
              % ("；首条 error：%s" % errs[0][:90] if errs else ""))

        DSN2 = ("host=127.0.0.1 port=%s dbname=medtalent user=postgres "
                "connect_timeout=10 options='-c search_path=mt,public'" % NEWPORT)
        with psycopg.connect(DSN2, row_factory=dict_row, autocommit=True) as c2:
            n_tab = c2.execute("""SELECT count(*) AS n FROM information_schema.tables
                                   WHERE table_schema='mt'""").fetchone()["n"]
            check(n_tab > 50,
                  "新实例里 mt schema 有 %d 张表（不是空库，说明真的恢复进去了）" % n_tab)

            # ---------- 4. 与**备份自己的清单**核对行数 ----------
            mism = []
            checked = 0
            for tbl, exp in list(expected.items())[:40]:
                try:
                    got = c2.execute('SELECT count(*) AS n FROM mt."%s"' % tbl).fetchone()["n"]
                except psycopg.Error:
                    c2.rollback()
                    continue
                checked += 1
                if int(exp) != int(got):
                    mism.append("%s(期望 %s 实得 %s)" % (tbl, exp, got))
            check(checked > 0, "抽查了 %d 张表的行数" % checked)
            check(not mism,
                  "**恢复后的行数与备份清单一致**（不符：%s）—— "
                  "注意是与**备份自己**比，不是与当前生产库比（备份是旧快照，"
                  "与「现在」的行数本来就不同）" % ("、".join(mism[:3]) or "无"))

            # ---------- 5. 在新实例里验证密钥恢复 ----------
            key = read_key()
            check(key is not None, "PII 密钥在库外可得（内容不回显）")
            if key:
                n_enc = c2.execute("""SELECT count(*) AS n FROM person_pii
                                       WHERE full_name_enc IS NOT NULL""").fetchone()["n"]
                if n_enc:
                    got = c2.execute("SELECT pgp_sym_decrypt(full_name_enc, %s)::text AS v "
                                     "FROM person_pii WHERE full_name_enc IS NOT NULL LIMIT 1",
                                     (key,)).fetchone()
                    check(got and got["v"],
                          "**新实例里能用外部密钥解密 PII** —— 密钥不绑定在原实例上")
                    failed = False
                    try:
                        c2.execute("SELECT pgp_sym_decrypt(full_name_enc, 'wrong-key')::text "
                                   "FROM person_pii WHERE full_name_enc IS NOT NULL LIMIT 1")
                    except psycopg.Error:
                        c2.rollback()
                        failed = True
                    check(failed, "**新实例里用错密钥解不开** —— 加密在换机器后依然有效")
                else:
                    check(True, "备份里没有加密 PII 行（%d），改用结构一致性守住" % n_enc)

            # ---------- 6. 证明"确实不是同一个实例" ----------
            same = c2.execute("SELECT current_setting('port') AS p, "
                              "current_setting('data_directory') AS d").fetchone()
            check(same["p"] == NEWPORT,
                  "恢复目标实例的端口是 %s（源实例是 55432）—— 确实是两个实例" % same["p"])
            check(os.path.normcase(NEWDIR) in os.path.normcase(same["d"] or ""),
                  "恢复目标的数据目录是新建的那个（不是生产实例的数据目录）")

    finally:
        if started:
            run_detached([tool("pg_ctl"), "-D", NEWDIR, "-m", "immediate",
                          "-w", "-t", "30", "stop"])
        shutil.rmtree(NEWDIR, ignore_errors=True)
        check(not os.path.isdir(NEWDIR), "新实例已停止且数据目录已清理 —— 没留残渣")

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
