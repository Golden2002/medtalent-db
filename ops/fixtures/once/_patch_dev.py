# -*- coding: utf-8 -*-
"""补丁：开发者模式（8083）改用特权连接。

为什么必须改：门户接线后 `portal.db()` 默认是**匿名 T0**会话（这是对的 ——
面向用户的只读进程就该从最低等级起）。但开发者模式是**可写的特权运维工具**：
它要 CREATE TABLE / ADD 维度 / 跑迁移，T3 等级角色也没有 DDL 权限。
把它套进 T0 的结果是 16 项断言失败、页面全打不开 —— 实测（run_all 完整档第 14 套）。

原语义：portal.py 的 `db()` 在接线前是 `readonly=False` 的普通连接（可写）。
所以这里用 `admin_db(readonly=False)` 恢复原语义，并把理由写在代码里。
它也因此仍在 ops/health.py 的 I4.1 待办清单里（写路径需要各自的受限写角色），
且**绝不允许暴露到公网**（见 docs/19）。
"""
import io

P = "code/demo/portal_dev.py"
s = io.open(P, encoding="utf-8").read()

OLD = "P.db()"
NEW = "P.admin_db(readonly=False)"
n = s.count(OLD)
if n == 0:
    raise SystemExit("没有找到 %r —— 补丁未生效" % OLD)

# 在第一次出现前插入一段说明（只插一次）
ANCHOR = "import portal as P  # noqa: E402"
if "开发者模式用特权连接" not in s:
    note = (
        "# 开发者模式（本进程，:8083）用**特权连接** P.admin_db(readonly=False)：\n"
        "#   · 它是可写的运维工具：要 CREATE TABLE / 加维度 / 跑迁移，等级角色没有 DDL 权限；\n"
        "#   · 只读门户 :8082 才是面向用户的进程，它用受限角色 mt_portal + SET LOCAL ROLE。\n"
        "# 因此本进程**绝不允许暴露到公网**（docs/19 的部署纪律），也仍在 ops/health.py\n"
        "# 的 I4.1 待办里（写路径需要各自的受限写角色，不能在只读角色上顺手放开）。\n"
    )
    if ANCHOR not in s:
        raise SystemExit("找不到锚点 %r" % ANCHOR)
    s = s.replace(ANCHOR, note + ANCHOR, 1)

s = s.replace(OLD, NEW)
io.open(P, "w", encoding="utf-8", newline="").write(s)
print("替换 %d 处 P.db() → P.admin_db(readonly=False)" % n)
