# -*- coding: utf-8 -*-
"""HTTP 层诊断：POST /login 到底返回了什么。"""
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import psycopg

BASE = "http://127.0.0.1:8082"
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")
EMAIL, PW = "http_diag@local.test", "http-diag-password-31c"

sys.stdout.reconfigure(encoding="utf-8")

with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    c.execute("SELECT mt.web_user_add(%s,%s,'T3','HTTP诊断')", (EMAIL, PW))
    n0 = c.execute("SELECT count(*) FROM mt.access_log").fetchone()[0]

req = urllib.request.Request(BASE + "/login")
req.data = urllib.parse.urlencode({"email": EMAIL, "password": PW}).encode()
req.method = "POST"
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        st, body, hdr = r.status, r.read().decode("utf-8", "replace"), r.headers
except urllib.error.HTTPError as e:
    st, body, hdr = e.code, e.read().decode("utf-8", "replace"), e.headers

print("HTTP %s" % st)
print("Set-Cookie: %r" % hdr.get("Set-Cookie"))
print("Location: %r" % hdr.get("Location"))
for pat in ("不正确", "权限不足", "数据库错误", "出错了", "登录"):
    print("  含「%s」：%s" % (pat, pat in body))
m = re.search(r'<div class="note (err|info|warn)">(.*?)</div>', body, re.S)
print("首个提示：%s" % (m.group(2)[:200] if m else "(无)"))

with psycopg.connect(ADMIN, autocommit=True) as c:
    rows = c.execute("""SELECT actor, actor_role, action, target, access_tier, detail
                          FROM mt.access_log WHERE access_id > %s
                         ORDER BY access_id""", (n0,)).fetchall()
    print("\n本次新增审计 %d 条：" % len(rows))
    for r in rows:
        print("   %s" % (r,))
    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    c.execute("DELETE FROM mt.access_log WHERE actor=%s", (EMAIL,))
    print("\n清理完成，残留=%d" % c.execute(
        "SELECT count(*) FROM mt.app_user WHERE email=%s", (EMAIL,)).fetchone()[0])
