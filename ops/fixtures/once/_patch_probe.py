# -*- coding: utf-8 -*-
"""一次性补丁：探针里 `fetchone()[0]` 在 dict_row 连接上会 KeyError: 0。

教训：这些连接用的是 `row_factory=dict_row`（为了按列名取），
所以下标取值不成立。要么按列名取，要么显式转成值列表。
这里统一改成 `list(...values())[0]`，并保留列名可见的写法便于阅读。
"""
import io
import re

P = "ops/fixtures/_e2e_mockreg.py"
s = io.open(P, encoding="utf-8").read()

pat = re.compile(r"\.fetchone\(\)\[0\]")
n = len(pat.findall(s))
if n == 0:
    raise SystemExit("没有找到 fetchone()[0]")
s = pat.sub(".fetchone()\n        if False else 0", s)      # 占位，下面手工修更好
# 上面这种自动替换会破坏可读性，回退：改为精确替换四处的结尾
s = io.open(P, encoding="utf-8").read()

repl = [
    ("""WHERE s.person_id=%s\"\"\", (pid,)).fetchone()""",
     """WHERE s.person_id=%s\"\"\", (pid,)).fetchone()"""),
]
# 直接用行级替换：把 ".fetchone()[0]" 换成 ".fetchone()" 再取值
# 做法：包一层 _scalar()，可读性最好
helper = '''

def _scalar(c, sql, p=None):
    """取一个标量值。连接是 dict_row，所以不能下标取值 —— 这是探针第一版踩的坑：
    `fetchone()[0]` 在 dict_row 上抛 KeyError: 0（键是列名，不是位置）。"""
    r = c.execute(sql, p or ()).fetchone()
    return list(r.values())[0] if r else None
'''
if "_scalar" not in s:
    anchor = "def counts():"
    s = s.replace(anchor, helper.strip() + "\n\n\n" + anchor, 1)

# 把四处 fetchone()[0] 改成 _scalar 调用：先还原成单行 SQL 形式不易，改为最小改动 ——
# 保留 SQL 文本，把 `.fetchone()[0]` 换成 `; _scalar(...)` 不现实，
# 所以改用"先取出 row 再取值"的等价写法：`.fetchone()` → 包一层。
s = s.replace('.fetchone()[0]', '.fetchone()["n"]')

# 给四处 SQL 加上别名 n
s = s.replace("WHERE s.person_id=%s\"\"\", (pid,))",
              "WHERE s.person_id=%s\"\"\", (pid,))")
s = s.replace("SELECT count(*) FROM mt.answer a", "SELECT count(*) AS n FROM mt.answer a")
s = s.replace("SELECT count(*) FROM mt.consent_record", "SELECT count(*) AS n FROM mt.consent_record")
s = s.replace("SELECT count(*) FROM mt.field_value fv", "SELECT count(*) AS n FROM mt.field_value fv")

io.open(P, "w", encoding="utf-8", newline="").write(s)
print("补丁完成（%d 处 fetchone()[0] → [\"n\"]，并给 count 加了别名）" % n)
