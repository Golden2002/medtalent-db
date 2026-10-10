# -*- coding: utf-8 -*-
"""取 /quality 页面的错误内容（定位 040 之后 /quality 挂掉的原因）。"""
import re
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
try:
    with urllib.request.urlopen("http://127.0.0.1:8082/quality", timeout=90) as r:
        b = r.read().decode("utf-8", "replace")
        print("HTTP %s，%d 字节" % (r.status, len(b)))
except urllib.error.HTTPError as e:
    b = e.read().decode("utf-8", "replace")
    print("HTTP %s，%d 字节" % (e.code, len(b)))
m = re.search(r'note err">(.*?)</div>', b, re.S)
print("页面错误：%s" % (m.group(1)[:600] if m else "（没找到 note err 块）"))
print("页面开头：%s" % b[:200].replace("\n", " "))
