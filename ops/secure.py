# -*- coding: utf-8 -*-
"""
ops/secure.py —— 账号与凭据管理（管理员账号、改口令、吊销、导出凭据文件）

为什么要有这个工具
--------------------------------------------------------------------------
登录页上写着"账号由 python ops\\secure.py 创建" —— 这份文件就是让那句话成立的东西。
三条纪律写死在实现里：

1. **不预置任何默认口令**。默认口令比没有口令更危险（外面挂着域名时尤其致命），
   所以本工具只**生成随机口令并显示一次**，绝不使用 `admin/123456` 这类东西。
2. **口令只以 bcrypt 哈希入库**（走 `mt.web_user_add`，SECURITY DEFINER），
   本工具自己也不保存哈希、不打印哈希。
3. **凭据文件只写到 gitignored 的目录**，并在写完后用 `git check-ignore` 复核 ——
   "我以为它被忽略了"不是证据。本仓库是 public，一次误提交就无法撤回。

用法
    python ops/secure.py add-admin --email you@example.com --name 你的名字
    python ops/secure.py add-user  --email u@example.com --tier T1 --name 张三
    python ops/secure.py list
    python ops/secure.py passwd --email you@example.com
    python ops/secure.py revoke --email you@example.com
    python ops/secure.py export-credentials --dir D:\\path\\a --dir D:\\path\\b
"""
from __future__ import annotations

import argparse
import os
import secrets
import string
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))
sys.path.insert(0, os.path.join(BASE, "ops"))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
       "options='-c search_path=mt,public'")

# 口令字符集：去掉容易看错的 0/O/1/l/I，因为这是要**人工抄一次**的口令
ALPHABET = "".join(c for c in (string.ascii_letters + string.digits + "-_#@%+")
                   if c not in "0O1lI")


def gen_password(n=20):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def conn():
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


def cmd_add(a):
    tier = a.tier
    pw = gen_password()
    with conn() as c:
        try:
            uid = c.execute("SELECT mt.web_user_add(%s, %s, %s, %s) AS u",
                            (a.email, pw, tier, a.name)).fetchone()["u"]
        except psycopg.Error as e:
            print("[X] 建账号失败：%s" % str(e).splitlines()[0])
            return 1
        row = c.execute("""SELECT user_id, email, display_name, tier, status
                             FROM mt.app_user WHERE user_id=%s""", (uid,)).fetchone()
    print("\n[✓] 账号已创建")
    print("    用户 ID   : %s" % row["user_id"])
    print("    邮箱(登录) : %s" % row["email"])
    print("    显示名     : %s" % row["display_name"])
    print("    等级       : %s" % row["tier"])
    print("\n" + "=" * 62)
    print("  一次性口令（**只显示这一次**，请立刻存到密码管理器）")
    print("  %s" % pw)
    print("=" * 62)
    print("  口令以 bcrypt 哈希入库，数据库里没有明文，我也读不回来。")
    print("  忘记口令就用： python ops\\secure.py passwd --email %s" % a.email)
    if a.save_to:
        wrote = save_credentials(a.save_to, [("MEDTALENT_ADMIN_EMAIL", a.email),
                                             ("MEDTALENT_ADMIN_PASSWORD", pw)])
        print("  已另存到 %d 个位置：%s" % (len(wrote), "；".join(wrote)))
    return 0


def save_credentials(dirs, pairs):
    """把凭据写成 KEY=VALUE 文件到指定目录，并**用 git check-ignore 复核**。

    为什么必须复核：本仓库是 public，一次误提交的凭据就永久泄露。
    "我加了 .gitignore 所以应该没问题"是猜测；`git check-ignore` 才是证据。
    """
    wrote = []
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        for key, val in pairs:
            path = os.path.join(d, key.lower() + ".txt")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("%s=%s\n" % (key, val))
            # 复核是否被忽略（只在仓库内才有意义）
            r = subprocess.run(["git", "check-ignore", "-q", path],
                               cwd=BASE, capture_output=True)
            inside = os.path.abspath(path).lower().startswith(os.path.abspath(BASE).lower())
            mark = ("已忽略" if r.returncode == 0 else "**未被忽略**") if inside else "仓库外"
            wrote.append("%s（%s）" % (path, mark))
            if inside and r.returncode != 0:
                print("[!] 警告：%s 在仓库内且**未被忽略** —— 有误提交风险！" % path)
    return wrote


def cmd_export(a):
    import env as E
    E.load_into_environ()
    pairs = []
    for k in ("GITHUB_TOKEN", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        v, _src = E.effective(k)
        if v:
            pairs.append((k, v))
        else:
            print("[=] %s 未配置，跳过" % k)
    if not pairs:
        print("[X] 没有任何已配置的凭据可导出")
        return 1
    # 项目目录内默认写到 .tools/credentials（.tools/ 整体被忽略）
    dirs = a.dir or [r"D:\wechat_summary",
                     os.path.join(BASE, ".tools", "credentials")]
    wrote = save_credentials(dirs, pairs)
    print("[✓] 已导出 %d 个凭据到 %d 个位置：" % (len(pairs), len(dirs)))
    for w in wrote:
        print("    %s" % w)
    print("\n提醒：这些文件是**明文**。在公开仓库目录内它们被 .gitignore 挡住，")
    print("      但同账号下任何程序都能读到 —— 这是方便与安全的取舍，不是加密存储。")
    return 0


def cmd_list(a):
    with conn() as c:
        rows = c.execute("""SELECT user_id, email, display_name, tier, status,
                                   created_at, last_login_at,
                                   (password_hash IS NOT NULL) AS has_pw
                              FROM mt.app_user ORDER BY tier DESC, created_at""").fetchall()
    print("%-26s %-30s %-10s %-5s %-9s %s" % ("用户 ID", "邮箱", "显示名", "等级", "状态", "最近登录"))
    print("-" * 110)
    for r in rows:
        print("%-26s %-30s %-10s %-5s %-9s %s"
              % (r["user_id"], r["email"], r["display_name"] or "—", r["tier"],
                 r["status"], str(r["last_login_at"])[:19] if r["last_login_at"] else "从未"))
    print("\n共 %d 个账号（**不显示任何口令哈希**）" % len(rows))
    return 0


def cmd_passwd(a):
    pw = a.password or gen_password()
    with conn() as c:
        n = c.execute("""UPDATE mt.app_user SET password_hash = crypt(%s, gen_salt('bf'))
                          WHERE lower(email) = lower(%s)""", (pw, a.email)).rowcount
    if not n:
        print("[X] 没有这个邮箱：%s" % a.email)
        return 1
    print("[✓] 已重置 %s 的口令" % a.email)
    if not a.password:
        print("\n  新口令（只显示这一次）：\n  %s" % pw)
    return 0


def cmd_revoke(a):
    with conn() as c:
        n = c.execute("""UPDATE mt.app_user SET status='suspended'
                          WHERE lower(email)=lower(%s)""", (a.email,)).rowcount
        c.execute("""UPDATE mt.web_session SET revoked_at = now()
                      WHERE revoked_at IS NULL AND user_id IN
                            (SELECT user_id FROM mt.app_user WHERE lower(email)=lower(%s))""",
                  (a.email,))
    print("[✓] 已停用 %s 并撤销其全部会话" % a.email if n else "[=] 没有这个邮箱")
    return 0


def main():
    ap = argparse.ArgumentParser(description="账号与凭据管理（不预置默认口令）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(p, tier_default=None):
        p.add_argument("--email", required=True)
        p.add_argument("--name", default=None)
        if tier_default is not None:
            p.add_argument("--tier", default=tier_default)
        p.add_argument("--save-to", action="append", metavar="DIR",
                       help="把账号口令另存到该目录（可多次），会复核是否被 git 忽略")

    p = sub.add_parser("add-admin", help="创建管理员（T3）")
    add(p, "T3")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("add-user", help="创建普通用户")
    add(p, "T1")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("passwd")
    p.add_argument("--email", required=True)
    p.add_argument("--password", default=None, help="不传则随机生成")
    p.set_defaults(fn=cmd_passwd)
    p = sub.add_parser("revoke")
    p.add_argument("--email", required=True)
    p.set_defaults(fn=cmd_revoke)
    p = sub.add_parser("export-credentials", help="把已配置的凭据导出成文件（含 git 忽略复核）")
    p.add_argument("--dir", action="append", metavar="DIR")
    p.set_defaults(fn=cmd_export)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
