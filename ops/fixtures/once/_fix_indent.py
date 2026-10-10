# -*- coding: utf-8 -*-
"""一次性修正：把 T18 插入块整体缩进到 try 内（8 空格）。"""
import io

P = "ops/tests/portal_test.py"
lines = io.open(P, encoding="utf-8").read().split("\n")

# 1-based 行号：591..650 是我的 T18 块，651 是 T17 的 print（也少了 4 空格）
targets = list(range(591, 652))
fixed = 0
for i in targets:
    ln = lines[i - 1]
    if ln.strip():
        lines[i - 1] = "    " + ln
        fixed += 1

io.open(P, "w", encoding="utf-8", newline="").write("\n".join(lines))
print("缩进修正 %d 行" % fixed)
