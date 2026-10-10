# -*- coding: utf-8 -*-
"""打印硬清理的真实响应，找出为什么没删掉。"""
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ext = "regmock_e2e_1791549314"          # 上一次运行留下的那条

req = urllib.request.Request("http://127.0.0.1:8083/mockreg")
req.data = urllib.parse.urlencode({"act": "hardclean", "ext_id": ext}).encode()
req.method = "POST"
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        st, body = r.status, r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    st, body = e.code, e.read().decode("utf-8", "replace")

print("HTTP %s，%d 字节" % (st, len(body)))
for m in re.finditer(r"""<div class=['"]note (err|ok|warn|info)['"]>(.*?)</div>""", body, re.S):
    txt = re.sub(r"<[^>]+>", "", m.group(2)).strip()
    if txt:
        print("\n[%s] %s" % (m.group(1), txt[:500]))
m = re.search(r"<h1>(.*?)</h1>", body, re.S)
print("\n页面标题：%s" % (m.group(1).strip() if m else "?"))
for probe in ("清理了", "全部干净", "发现问题", "拒绝", "没有可清理"):
    print("  含「%s」：%s" % (probe, probe in body))
