# -*- coding: utf-8 -*-
"""快速诊断：把 403 页里那句"数据库原话"打出来 —— 它会点名是哪个对象被拒。"""
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8082"


def show(path, data=None, cookie=None, label=""):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    if data is not None:
        req.data = urllib.parse.urlencode(data).encode()
        req.method = "POST"
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            st, body, hdr = r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        st, body, hdr = e.code, e.read().decode("utf-8", "replace"), e.headers
    m = re.search(r"数据库原话：<code>([^<]*)</code>", body)
    print("%-28s HTTP %-4s %s" % (label or path, st,
                                  ("原话: " + m.group(1)) if m else "(无 403 原话)"))
    sc = hdr.get("Set-Cookie")
    if sc:
        print("      Set-Cookie: %s" % sc[:60])
    return st, body, hdr


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    show("/", label="匿名 GET /")
    show("/schema", label="匿名 GET /schema")
    show("/login", label="匿名 GET /login")
    show("/login", data={"email": "x@y.z", "password": "nope-nope-nope"}, label="POST 登录(错)")
