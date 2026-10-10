# -*- coding: utf-8 -*-
"""用管理员会话取 /quality 的真实错误内容。"""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8082"
EMAIL = "1293869083@qq.com"
PW = ""
for d in (r"D:\wechat_summary", os.path.join(os.getcwd(), ".tools", "credentials")):
    p = os.path.join(d, "medtalent_admin_password.txt")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            PW = fh.read().strip().split("=", 1)[-1].strip()
        break


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


req = urllib.request.Request(BASE + "/login")
req.data = urllib.parse.urlencode({"email": EMAIL, "password": PW}).encode()
req.method = "POST"
try:
    with urllib.request.build_opener(NR()).open(req, timeout=30) as r:
        ck = (r.headers.get("Set-Cookie") or "").split(";")[0]
except urllib.error.HTTPError as e:
    ck = (e.headers.get("Set-Cookie") or "").split(";")[0]
print("登录 cookie：%s…" % ck[:20])

r2 = urllib.request.Request(BASE + "/quality")
r2.add_header("Cookie", ck)
try:
    with urllib.request.urlopen(r2, timeout=180) as r:
        b = r.read().decode("utf-8", "replace")
        print("HTTP %s，%d 字节" % (r.status, len(b)))
except urllib.error.HTTPError as e:
    b = e.read().decode("utf-8", "replace")
    print("HTTP %s，%d 字节" % (e.code, len(b)))
m = re.search(r'note err">(.*?)</div>', b, re.S)
print("错误内容：%s" % (m.group(1)[:700] if m else "（无 note err 块）"))
