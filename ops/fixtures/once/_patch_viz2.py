# -*- coding: utf-8 -*-
"""一次性补丁：viz_test.py 的 HTTP 抓取带上门户会话 cookie。

与 portal_test 同一个理由：门户现在跑在受限角色上，匿名只能读 T0，
/viz、/viz/build、/analyze/a1 这些页面会（正确地）少图或 403。
本套测试要回答的是"图与口径对不对"，所以先用一个明确的 T3 账号进去。
账号自建自删，前缀 viz_test@，不污染库。
"""
import io

P = "ops/tests/viz_test.py"
s = io.open(P, encoding="utf-8").read()

OLD_GET = '''def get(path, timeout=180):'''
if OLD_GET not in s:
    raise SystemExit("找不到 get() —— 补丁未生效")

HELPER = '''COOKIE = None
TEST_EMAIL = "viz_test@local.test"
TEST_PW = "viz-test-password-4c19"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302：登录成功返回 302 + Set-Cookie，跟随会把 cookie 丢掉。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def login():
    """以 T3 账号登录，取回会话 cookie（账号自建自删）。"""
    global COOKIE
    with H.connect(P.DSN) as c:
        c.execute("SELECT mt.web_user_add(%s, %s, 'T3', '可视化测试账号')",
                  (TEST_EMAIL, TEST_PW))
        c.commit()
    req = urllib.request.Request(ROOT + "/login")
    req.data = urllib.parse.urlencode({"email": TEST_EMAIL, "password": TEST_PW}).encode()
    req.method = "POST"
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as e:
        sc = e.headers.get("Set-Cookie") or ""
    COOKIE = sc.split(";")[0]
    return COOKIE


def cleanup_login():
    """先子后父：web_session 引用 app_user。审计行也清掉，避免影响其它套件的计数。"""
    with H.connect(P.DSN) as c:
        c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                       (SELECT user_id FROM mt.app_user WHERE email = %s)""", (TEST_EMAIL,))
        c.execute("DELETE FROM mt.app_user WHERE email = %s", (TEST_EMAIL,))
        c.execute("DELETE FROM mt.access_log WHERE actor = %s", (TEST_EMAIL,))
        c.commit()


'''
s = s.replace(OLD_GET, HELPER + OLD_GET, 1)

# get() 里带上 cookie
OLD_BODY = 'with urllib.request.urlopen(ROOT + path, timeout=timeout) as r:'
NEW_BODY = ('req = urllib.request.Request(ROOT + path)\n'
            '        if COOKIE:\n'
            '            req.add_header("Cookie", COOKIE)\n'
            '        with urllib.request.urlopen(req, timeout=timeout) as r:')
if OLD_BODY not in s:
    raise SystemExit("找不到 urlopen 行 —— 补丁未生效")
s = s.replace(OLD_BODY, NEW_BODY, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
print("补丁完成")
