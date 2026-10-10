# -*- coding: utf-8 -*-
"""一次性补丁：viz_test.py 的面板渲染改用明确的高等级会话。

为什么必须改：门户现在跑在受限角色上，`P.db()` 不带会话 = 匿名 = T0，
面板会（正确地）抛 InsufficientPrivilege。而 viz_test 要回答的是
"面板口径对不对"，不是"某等级能不能看" —— 把权限问题伪装成口径问题，
会让两个信号互相掩盖（这正是本项目的排查纪律要避免的）。
"""
import io

P = "ops/tests/viz_test.py"
s = io.open(P, encoding="utf-8").read()

OLD = "with P.db() as c:"
NEW = "with P.db(TEST_SESSION) as c:"
n = s.count(OLD)
if n == 0:
    raise SystemExit("没有找到 %r —— 补丁未生效，不要继续" % OLD)
s = s.replace(OLD, NEW)

ANCHOR = "def conn():"
if "TEST_SESSION = " in s:
    print("TEST_SESSION 已存在，跳过插入")
else:
    if ANCHOR not in s:
        raise SystemExit("找不到锚点 %r" % ANCHOR)
    add = (
        "# 面板渲染用**明确的高等级会话**：本套测试回答的是「面板口径对不对」，\n"
        "# 不是「某个等级能不能看」（后者由 ops/tests/access_test.py 负责）。\n"
        "# 用匿名会话渲染会得到 403，那会把权限问题伪装成口径问题。\n"
        'TEST_SESSION = P.Session(actor="viz_test", tier="T3", ok=True)\n'
        "\n"
        "\n"
    )
    s = s.replace(ANCHOR, add + ANCHOR, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
print("替换 %d 处，TEST_SESSION 就绪" % n)
