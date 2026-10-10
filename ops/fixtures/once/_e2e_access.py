# -*- coding: utf-8 -*-
"""
端到端验证（一次性 dev 脚本）：门户的列级访问控制是否真的在拦。

要回答四个问题，全部用**真实 HTTP 请求**，不看代码推断：
  Q1 匿名（T0）访问需要 T1 的数据页 → 应该是「权限不足」页，而不是 500、也不是正常数据
  Q2 匿名连 count(*) 这种"数量"能不能拿到 → 应该能（用户要求"数量可以公开"）
  Q3 用一个 T3 账号登录后，同一个页面 → 应该正常显示
  Q4 每次访问有没有写进 access_log → "记录访问用户"这条需求的直接证据

用后清理：删除本次创建的测试账号与会话。用后即删，不留测试垃圾。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import psycopg

BASE = "http://127.0.0.1:8082"
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")
EMAIL = "e2e_probe@local.test"
PW = "e2e-probe-password-9f3a"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随 302。

    为什么必须关掉：登录成功返回 302 + Set-Cookie。urllib 默认会跟随跳转，
    于是拿到的是**跳转之后**那个响应的头 —— Set-Cookie 丢了、Location 也没了，
    看起来像"登录失败返回了 200 首页"。本探针第一版就是这么误判的
    （真正的证据在别处：access_log 里躺着两条 login T3）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def get(path, cookie=None, data=None):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    if data is not None:
        req.data = urllib.parse.urlencode(data).encode()
        req.method = "POST"
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


def main():
    out = []
    with psycopg.connect(ADMIN, autocommit=True) as c:
        # 清理顺序必须**先子后父**：person 的 29 张子表全是 NO ACTION（实测），
        # 这里同理 —— app_user 被 web_session 引用，先删会话再删账号。
        # 这个顺序错误是本探针第一版实际踩到的（ForeignKeyViolation）。
        c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                       (SELECT user_id FROM mt.app_user WHERE email = %s)""", (EMAIL,))
        c.execute("DELETE FROM mt.app_user WHERE email = %s", (EMAIL,))
        uid = c.execute("SELECT mt.web_user_add(%s, %s, 'T3', '端到端探针')",
                        (EMAIL, PW)).fetchone()[0]
        n_log0 = c.execute("SELECT count(*) FROM mt.access_log").fetchone()[0]

        # Q1 匿名看数据页
        st, body, _ = get("/talent")
        denied = ("权限不足" in body)
        leaked = ("per_real_fengtang" in body or "per_mock_0001" in body)
        out.append(("Q1 匿名访问 /talent", "HTTP %d，权限不足页=%s，是否泄露真实行=%s"
                    % (st, denied, leaked), st == 403 and denied and not leaked))

        # Q2 匿名拿数量（首页与目录页只用 T0 列）
        st2, body2, _ = get("/")
        out.append(("Q2 匿名访问 /（只用 T0 列）",
                    "HTTP %d，%d 字节" % (st2, len(body2)), st2 == 200))

        # Q3 登录后看同一个页面
        st3, body3, hdr = get("/login")
        st4, _, hdr4 = get("/login", data={"email": EMAIL, "password": PW})
        cookie = (hdr4.get("Set-Cookie") or "").split(";")[0]
        st5, body5, _ = get("/talent", cookie=cookie)
        out.append(("Q3 登录(T3)后访问 /talent",
                    "POST /login → HTTP %d，cookie=%s…；/talent → HTTP %d，%d 字节，有真实行=%s"
                    % (st4, cookie[:16], st5, len(body5), "per_mock_0001" in body5),
                    st4 in (302, 200) and bool(cookie) and st5 == 200
                    and "per_mock_0001" in body5))

        # Q3b 错误口令必须被拒
        st6, body6, hdr6 = get("/login", data={"email": EMAIL, "password": "wrong-password"})
        out.append(("Q3b 错误口令",
                    "HTTP %d，提示包含'不正确'=%s" % (st6, "不正确" in body6),
                    st6 == 200 and "不正确" in body6 and "Set-Cookie" not in hdr6))

        # Q4 访问日志
        n_log1 = c.execute("SELECT count(*) FROM mt.access_log").fetchone()[0]
        rows = c.execute("""SELECT actor, actor_role, action, target, access_tier
                              FROM mt.access_log ORDER BY access_id DESC LIMIT 8""").fetchall()
        out.append(("Q4 审计写入", "access_log %d → %d 行" % (n_log0, n_log1), n_log1 > n_log0))
        for r in rows:
            print("      %-18s %-8s %-12s %-10s %s" % r)

        # 清理
        c.execute("""DELETE FROM mt.web_session WHERE user_id = %s
                        OR actor = %s""", (uid, EMAIL))
        c.execute("DELETE FROM mt.app_user WHERE user_id = %s", (uid,))
        c.execute("DELETE FROM mt.access_log WHERE actor = %s", (EMAIL,))
        left = c.execute("""SELECT count(*) FROM mt.app_user WHERE email=%s""", (EMAIL,)).fetchone()[0]
        print("\n清理后残留测试账号：%d" % left)

    print("\n" + "=" * 76)
    ok = True
    for name, detail, passed in out:
        print("%s %-26s %s" % ("[PASS]" if passed else "[FAIL]", name, detail))
        ok = ok and passed
    print("=" * 76)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
