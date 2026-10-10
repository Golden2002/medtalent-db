# -*- coding: utf-8 -*-
"""验证：登录之后，日志里记的到底是谁（不清理，先把证据取出来）。"""
import sys
import urllib.error
import urllib.parse
import urllib.request

import psycopg

BASE = "http://127.0.0.1:8082"
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")
EMAIL, PW = "audit_probe@local.test", "audit-probe-pw-71ff"
sys.stdout.reconfigure(encoding="utf-8")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(path, cookie=None, data=None):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    if data is not None:
        req.data = urllib.parse.urlencode(data).encode()
        req.method = "POST"
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    c.execute("DELETE FROM mt.access_log WHERE actor=%s", (EMAIL,))
    c.execute("SELECT mt.web_user_add(%s,%s,'T2','审计验证账号')", (EMAIL, PW))
    n0 = c.execute("SELECT coalesce(max(access_id),0) FROM mt.access_log").fetchone()[0]

    st, _, hdr = get("/login", data={"email": EMAIL, "password": PW})
    ck = (hdr.get("Set-Cookie") or "").split(";")[0]
    print("登录 HTTP %d，拿到 cookie=%s…" % (st, ck[:18]))
    get("/talent", cookie=ck)
    get("/catalog", cookie=ck)

    rows = c.execute("""SELECT access_id, actor, actor_role, action, target, access_tier
                          FROM mt.access_log WHERE access_id > %s ORDER BY access_id""",
                     (n0,)).fetchall()
    print("\n登录后新增的审计记录（本次请求链路）：")
    for r in rows:
        print("  #%d  actor=%-24s role=%-10s %-8s %s"
              % (r[0], r[1], r[2], r[3], r[4]))

    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    c.execute("DELETE FROM mt.access_log WHERE actor=%s", (EMAIL,))
    print("\n已清理（残留账号 %d）" % c.execute(
        "SELECT count(*) FROM mt.app_user WHERE email=%s", (EMAIL,)).fetchone()[0])
