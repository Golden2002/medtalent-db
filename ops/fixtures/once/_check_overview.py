# -*- coding: utf-8 -*-
"""验证匿名首页的"公开（T0）概览"确实渲染出数字，且读不到的会明说。"""
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
b = urllib.request.urlopen("http://127.0.0.1:8082/", timeout=20).read().decode("utf-8")
m = re.search(r"公开（T0）概览(.*?)<div class=\"card\">", b, re.S)
seg = m.group(1) if m else ""
print("公开概览区找到：%s" % bool(seg))
pairs = re.findall(r'<div class="n">([^<]+)</div><div class="t">([^<]+)</div>', seg)
print("渲染出的指标 %d 个：" % len(pairs))
for n, t in pairs:
    print("   %-12s %s" % (n, t))
w = re.search(r"以下数字需要登录后查看：([^<（]*)", b)
print("读不到的指标（明说，而不是显示成 0）：%s" % (w.group(1).strip() if w else "无"))
