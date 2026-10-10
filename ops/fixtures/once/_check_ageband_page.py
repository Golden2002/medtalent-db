# -*- coding: utf-8 -*-
"""验证「年龄公开」在匿名页面上的落地：年龄段分布可见，精确出生年不可见。"""
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8082"
b = urllib.request.urlopen(BASE + "/", timeout=30).read().decode("utf-8")

m = re.search(r"年龄段分布(.*?)</table>", b, re.S)
print("页面含「年龄段分布」表：%s" % bool(m))
if m:
    pat = (r'<tr><td>([^<]+)</td><td class="n">([^<]+)</td>'
           r'<td class="n">([^<]+)</td></tr>')
    for lbl, n, pct in re.findall(pat, m.group(1)):
        print("   %-14s %6s  %s" % (lbl, n, pct))

# 分级口径必须在页面上说明（否则读者会以为"年龄"就是出生年）
for probe in ("年龄段是<b>统计属性", "精确出生年份", "快照"):
    print("含说明「%s」：%s" % (probe, probe in b))

# 匿名页面里不该出现具体出生年（T2 才可读）
years = re.findall(r"\b(19[5-9]\d|20[0-2]\d)\b", b)
print("页面里出现的 19xx/20xx 数字：%s" % (sorted(set(years))[:12] or "无"))
