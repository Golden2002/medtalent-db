# -*- coding: utf-8 -*-
"""
ops/tests/_harness.py —— 回归测试的最小公共脚手架

为什么只抽这些、不多抽一层框架：

  · 10 套脚本里，`check()` / `PASS, FAIL` / "结果：PASS n 项，FAIL n 项" 汇总
    是**逐行相同**的样板（每套约 20 行），改一处要改十处；
  · 但这些脚本的价值之一，是**能单独 `python ops/tests/xxx_test.py` 跑、零外部依赖**。
    所以这里只放纯函数与两个计数列表：不引入注册表、不引入夹具生命周期、
    不要求测试继承任何东西——测试仍然是普通脚本，只是不再各抄一遍样板。

刻意**不**抽的东西（看起来像重复，但不是）：

  · 清理逻辑与注入数据：每套的清库顺序与范围都不同（外键顺序、TAG 前缀、
    "真实来源 vs 测试来源"的区分），合并会把"这套测试到底清了什么"藏进别的文件；
  · 各测试自己的断言组织与前置准备。

run_all.py 用正则 `PASS (\d+) 项[，,] FAIL (\d+) 项` 从 stdout 取用例数，
因此汇总行由 report() 统一产出，格式不能改。

用法：

    import os, sys
    sys.path.insert(0, os.path.join(BASE, "ops", "tests"))
    import _harness as H

    ...
    H.check(cond, "消息")
    ...
    return H.report(note="（测试数据已清理）")
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

PASS = []
FAIL = []


def check(cond, msg):
    """记一条断言并按 [PASS]/[FAIL] 打印，返回 cond 便于链式使用。"""
    (PASS if cond else FAIL).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    return cond


def ok(msg):
    PASS.append(msg)
    print("  [PASS] " + msg)


def fail(msg):
    FAIL.append(msg)
    print("  [FAIL] " + msg)


def connect(dsn):
    """统一的连接工厂。全部测试都按 dict 行取字段（psycopg3 row_factory）。"""
    return psycopg.connect(dsn, row_factory=dict_row)


def q1(c, sql, p=None):
    """首行首列；无行返回 None。"""
    with c.cursor() as cur:
        cur.execute(sql, p)
        r = cur.fetchone()
        return list(r.values())[0] if r else None


def q(c, sql, p=None):
    """全部行。"""
    with c.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchall()


def report(note=None, width=78, list_fails=False, extra=None):
    """打印统一的结果汇总行，返回进程退出码（有 FAIL 即 1）。

    note      追加到结果行末尾的说明（如"（测试数据已清理）"）
    width     分隔线宽度（各测试历史上用了 74 或 78，保持原样以免输出变化）
    list_fails 汇总后再逐条列出失败项（部分测试有，部分没有）
    extra     在结果行与收尾分隔线之间插入额外输出（如耗时统计）的可调用对象
    """
    print("\n" + "=" * width)
    print("结果：PASS %d 项，FAIL %d 项%s"
          % (len(PASS), len(FAIL), ("   " + note) if note else ""))
    if extra:
        extra()
    if list_fails:
        for f in FAIL:
            print("  [FAIL] " + f)
    print("=" * width)
    return 1 if FAIL else 0
