# -*- coding: utf-8 -*-
"""用管理员会话取 /quality 的真实错误内容。"""
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8082"
EMAIL = _load_admin_email()
PW = ""
for d in (r"D:\wechat_summary", os.path.join(os.getcwd(), ".tools", "credentials")):
    p = os.path.join(d, "medtalent_admin_password.txt")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            PW = fh.read().strip().split("=", 1)[-1].strip()
        break


def _load_admin_email():
    """从凭据文件读管理员邮箱 —— **不要硬编码在这里**。

    这一行原来写的是明文邮箱，被 ops/secrets_scan.py 抓出来：
    它把"我们确实当凭据保存的东西"作为判据，于是登录邮箱也算凭据。
    凭据文件在仓库外/被 gitignore，仓库里只留读取逻辑。
    """
    for d in (r"D:\wechat_summary", os.path.join(os.path.dirname(
                  os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                  ".tools", "credentials")):
        p = os.path.join(d, "medtalent_admin_email.txt")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as fh:
                return fh.read().strip().split("=", 1)[-1].strip()
    raise SystemExit("[X] 找不到 medtalent_admin_email.txt —— 本脚本不再硬编码邮箱")


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
