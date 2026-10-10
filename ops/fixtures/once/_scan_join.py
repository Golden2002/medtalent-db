# -*- coding: utf-8 -*-
"""全仓扫同类错误：`X.join(a, b)` 形式（join 只接受一个可迭代参数）。

为什么要扫：`view_audit` 里的 `"".join(metric(..), metric(..))` 一直没被发现，
因为**匿名访问在查询阶段就被拦下、根本走不到那行**，而页面巡检又没包含 /audit。
所以这里做一次静态扫描，把同类写法一次找干净，而不是等下一次踩。
"""
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
FILES = ["code/demo/portal.py", "code/demo/portal_dev.py", "code/demo/portal_viz.py",
         "code/demo/portal_mockreg.py", "code/demo/console.py", "code/demo/app.py",
         "code/demo/static_site.py", "code/demo/serve_all.py", "code/bridge/exchange.py"]

# 只匹配**字符串字面量**的 .join(...)：`"".join(a, b)` 才是错的。
# `os.path.join(a, b)` 天然接受多参数，不该被扫进来（第一版就误报了 25 处）。
PAT = re.compile(r"""(["'](?:[^"'\\]|\\.)*["'])\.join\(([^\n]*?)\)""")
found = 0
for p in FILES:
    try:
        s = io.open(p, encoding="utf-8").read()
    except OSError:
        continue
    for m in PAT.finditer(s):
        seg = m.group(2)
        depth = 0
        top_comma = False
        for ch in seg:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                top_comma = True
        if top_comma:
            found += 1
            ln = s[:m.start()].count("\n") + 1
            print("  %s:%d  %s" % (p, ln, m.group(0)[:100]))
print("扫描完成：发现 %d 处" % found)
sys.exit(1 if found else 0)
