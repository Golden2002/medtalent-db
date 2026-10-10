# -*- coding: utf-8 -*-
"""隔离诊断：登录到底在哪一层失败（SQL 层 / HTTP 层）。"""
import sys

import psycopg

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")
PORTAL = ("host=127.0.0.1 port=55432 dbname=medtalent user=mt_portal connect_timeout=5 "
          "options='-c search_path=mt,public'")
EMAIL, PW = "diag_login@local.test", "diag-password-77ab"

sys.stdout.reconfigure(encoding="utf-8")

with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    uid = c.execute("SELECT mt.web_user_add(%s,%s,'T3','诊断')", (EMAIL, PW)).fetchone()[0]
    row = c.execute("SELECT email, tier, status, left(password_hash,7) AS h, "
                    "length(password_hash) AS n FROM mt.app_user WHERE user_id=%s",
                    (uid,)).fetchone()
    print("建账号：%s" % (row,))

    # 1) 以 postgres 直接校验哈希
    ok = c.execute("SELECT crypt(%s, password_hash) = password_hash AS ok "
                   "FROM mt.app_user WHERE user_id=%s", (PW, uid)).fetchone()[0]
    print("1) crypt 比对（postgres 视角）：%s" % ok)

# 2) 以 mt_portal 调 web_login（不带等级角色）
try:
    with psycopg.connect(PORTAL, autocommit=True) as c:
        r = c.execute("SELECT * FROM mt.web_login(%s,%s)", (EMAIL, PW)).fetchone()
        print("2) mt_portal 调 web_login：%s" % (r,))
        if r and r[0]:
            c.execute("SELECT * FROM mt.web_session_revoke(%s)", (r[0],))
except psycopg.Error as e:
    print("2) mt_portal 调 web_login 失败：%s" % str(e).splitlines()[0])

# 3) 以 mt_portal + SET LOCAL ROLE mt_t0 调（模拟门户第一版的错误做法）
try:
    with psycopg.connect(PORTAL, autocommit=True) as c:
        c.execute("BEGIN")
        c.execute("SET LOCAL ROLE mt_t0")
        r = c.execute("SELECT * FROM mt.web_login(%s,%s)", (EMAIL, PW)).fetchone()
        print("3) mt_t0 调 web_login：%s" % (r,))
        c.execute("ROLLBACK")
except psycopg.Error as e:
    print("3) mt_t0 调 web_login 被拒：%s" % str(e).splitlines()[0])

# 清理
with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute("""DELETE FROM mt.web_session WHERE user_id=%s OR actor=%s""", (uid, EMAIL))
    c.execute("DELETE FROM mt.app_user WHERE user_id=%s", (uid,))
    c.execute("DELETE FROM mt.access_log WHERE actor=%s", (EMAIL,))
    print("清理完成，残留=%d" % c.execute(
        "SELECT count(*) FROM mt.app_user WHERE email=%s", (EMAIL,)).fetchone()[0])
