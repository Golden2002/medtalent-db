# -*- coding: utf-8 -*-
"""看 /mockreg 的真实响应（含 500 时的错误原文）。"""
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
for path in ("/", "/mockreg"):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8083" + path, timeout=60) as r:
            b = r.read().decode("utf-8", "replace")
            print("%-10s HTTP %s  %d 字节" % (path, r.status, len(b)))
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace")
        print("%-10s HTTP %s  %d 字节" % (path, e.code, len(b)))
    except Exception as ex:                                   # noqa: BLE001
        print("%-10s 连接失败：%s" % (path, ex))
        continue
    # 抓出错误提示
    import re
    m = re.search(r'<div class="note err">(.*?)</div>', b, re.S)
    if m:
        print("           错误：%s" % m.group(1)[:300])
