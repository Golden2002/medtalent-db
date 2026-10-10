# -*- coding: utf-8 -*-
"""用管理员会话取任意页面的真实错误（默认 /tree）。"""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8082"
PATH = sys.argv[1] if len(sys.argv) > 1 else "/tree"
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
req.data = urllib.parse.urlencode({"email": "1293869083@qq.com", "password": PW}).encode()
req.method = "POST"
try:
    with urllib.request.build_opener(NR()).open(req, timeout=30) as r:
        ck = (r.headers.get("Set-Cookie") or "").split(";")[0]
except urllib.error.HTTPError as e:
    ck = (e.headers.get("Set-Cookie") or "").split(";")[0]

q = urllib.request.Request(BASE + PATH)
q.add_header("Cookie", ck)
try:
    with urllib.request.urlopen(q, timeout=180) as r:
        b = r.read().decode("utf-8", "replace")
        print("HTTP %s，%d 字节" % (r.status, len(b)))
except urllib.error.HTTPError as e:
    b = e.read().decode("utf-8", "replace")
    print("HTTP %s，%d 字节" % (e.code, len(b)))
t = re.sub(r"<[^>]+>", " ", b)
t = re.sub(r"\s+", " ", t)
i = t.find("数据库原话")
if i >= 0:
    print("数据库原话：%s" % t[i:i + 300])
j = t.find("被拒绝的对象")
if j >= 0:
    print("被拒绝的对象：%s" % t[j:j + 200])
if i < 0 and j < 0:
    print("正文片段：%s" % t[:400])
