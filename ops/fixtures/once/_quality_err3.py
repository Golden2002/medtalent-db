# -*- coding: utf-8 -*-
"""从 /quality 的权限页里抽出「数据库原始错误」那一句。"""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8082"
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

q = urllib.request.Request(BASE + "/quality")
q.add_header("Cookie", ck)
try:
    with urllib.request.urlopen(q, timeout=180) as r:
        b = r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    b = e.read().decode("utf-8", "replace")

# 权限页把库的原始错误放在 <pre> 或 code 里；两种都试
for m in re.finditer(r"<pre[^>]*>(.*?)</pre>", b, re.S) or []:
    txt = re.sub(r"<[^>]+>", "", m.group(1))
    if txt.strip():
        print("PRE: %s" % txt.strip()[:300])
# 直接找关键字
for kw in ("permission denied", "InsufficientPrivilege", "对 ", "目标对象"):
    for m in re.finditer(re.escape(kw), b):
        seg = re.sub(r"<[^>]+>", "", b[max(0, m.start() - 120):m.start() + 200])
        print("[%s] …%s…" % (kw, re.sub(r"\s+", " ", seg).strip()[:260]))
        break
