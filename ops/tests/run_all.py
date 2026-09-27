#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ops/tests/run_all.py —— 一条命令跑完整套回归（12 套 + 2 个收尾动作）

为什么需要它：这条链路的**顺序**本身是有语义的，手工跑反复踩坑：
  · collect/backup 测试会重新解析 JD，而 jd_ingest 是"先删后插" job_requirement，
    会把 concept_id 与权重一起抹掉 → 之后必须重建派生层，否则匹配静默失效。
  · evolution_test 会拆分真实种子节点 → 跑完必须复位基线，否则职业树凭空少枝。
把顺序写进代码，就不会因为"我记得先跑哪个"而出错。

用法：
  python ops/tests/run_all.py                # 全部
  python ops/tests/run_all.py --only 3,4     # 只跑指定步骤（定点排查，不做收尾）
  python ops/tests/run_all.py --after-only   # 只跑收尾（复位基线 + 重建派生层）
  python ops/tests/run_all.py --list         # 看步骤清单
  python ops/tests/run_all.py -v             # 打印每步完整输出
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.stdout.reconfigure(encoding="utf-8")

PY = sys.executable

# (编号, 名称, 类型, 目标)  类型：py=直接跑脚本；sql=经 ops/pg.py sql 执行
STEPS = [
    ("1",  "目录与词表静态校验",  "py",  "code/validate_catalog.py"),
    ("2",  "Schema 静态校验",   "py",  "code/validate_schema.py"),
    ("3",  "码值一致性（存码不存标签）", "sql", "ops/tests/code_conformance.sql"),
    ("4",  "门禁回归 + 完整性不变量", "sql", "ops/tests/regression_guards.sql"),
    ("5",  "Schema 冒烟",      "sql", "ops/tests/smoke_schema.sql"),
    ("6",  "动态行列增删",       "py",  "ops/tests/dynamic_model_test.py"),
    ("7",  "动态表单服务",       "py",  "ops/tests/form_server_test.py"),
    ("8",  "L0 采集与去重",      "py",  "ops/tests/collect_test.py"),
    ("9",  "小程序 bridge",    "py",  "ops/tests/bridge_test.py"),
    ("10", "备份/保留/恢复",     "py",  "ops/tests/backup_test.py"),
    ("11", "控制台端到端",       "py",  "ops/tests/console_test.py"),
    ("12", "演化层（职业树生长）",  "py",  "ops/tests/evolution_test.py"),
    ("13", "数据库门户（展示/检索/分析）", "py", "ops/tests/portal_test.py"),
    ("14", "开发者模式（可写/零 DDL）",  "py", "ops/tests/devmode_test.py"),
    ("15", "可视化与分析层（口径一致）",   "py", "ops/tests/viz_test.py"),
]

# 收尾动作：不在 --only 的筛选范围内，总是执行（除非 --no-after）
AFTER = [
    ("R1", "复位职业树基线",   "py",  "code/evolve/restore_baseline.py"),
    ("R2", "重建派生层并重算能力权重", "py", "code/analytics/build_competency.py"),
]

RE_PY = re.compile(r"PASS\s+(\d+)\s*项[，,]\s*FAIL\s+(\d+)\s*项")
RE_STATIC = re.compile(r"错误：(\d+)\s+警告：(\d+)")
RE_PASS_NOTICE = re.compile(r"PASS[ :]")


def run(kind, target, verbose):
    """执行一步，返回 (ok, 摘要, 输出)。"""
    if kind == "py":
        cmd = [PY, os.path.join(BASE, target.replace("/", os.sep))]
    else:
        cmd = [PY, os.path.join(BASE, "ops", "pg.py"), "sql",
               os.path.join(BASE, target.replace("/", os.sep))]
    p = subprocess.run(cmd, cwd=BASE, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=3600)
    raw = p.stdout.decode("utf-8", "replace")
    ok = p.returncode == 0

    # 摘要：优先取测试自己报的 PASS/FAIL 计数，其次静态校验的"错误：N"
    summary = ""
    m = RE_PY.search(raw)
    if m:
        np_, nf = int(m.group(1)), int(m.group(2))
        ok = ok and nf == 0
        summary = "PASS %d / FAIL %d" % (np_, nf)
    else:
        m = RE_STATIC.search(raw)
        if m:
            nerr, nwarn = int(m.group(1)), int(m.group(2))
            ok = ok and nerr == 0
            summary = "错误 %d / 警告 %d" % (nerr, nwarn)
        else:
            # SQL 用例：数 PASS 通知，出现 FAIL 即失败
            npass = len(RE_PASS_NOTICE.findall(raw))
            bad = "FAIL" in raw
            ok = ok and not bad
            if npass:
                summary = "%d 条用例%s" % (npass, "，含 FAIL" if bad else "")
            else:
                # 这类脚本（复位基线、重建派生层）不自报用例数：
                # 取最后一行有意义的输出当摘要，否则"0 条用例"毫无信息量。
                tail = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                summary = tail[-1][:72] if tail else ("退出码 %d" % p.returncode)
    # 有 FAIL 字样但测试仍返回 0（例如说明文字里出现 FAIL）时，以计数为准，
    # 这里只在**没有计数**时报出来，避免误报。
    if verbose:
        print(raw)
    return ok, summary, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑这些编号，逗号分隔，如 3,4,12")
    ap.add_argument("--after-only", action="store_true", help="只跑收尾动作（复位基线 + 重建派生层）")
    ap.add_argument("--list", action="store_true", help="列出步骤")
    ap.add_argument("--no-after", action="store_true", help="跳过收尾动作（调试用）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印完整输出")
    a = ap.parse_args()

    if a.list:
        for n, name, kind, tgt in STEPS:
            print("  %-4s %-28s %s" % (n, name, tgt))
        print("\n收尾：")
        for n, name, kind, tgt in AFTER:
            print("  %-4s %-28s %s" % (n, name, tgt))
        return 0

    # 前置：数据库必须在线，否则全盘皆错（先给一句人话，别让 12 套一起报连接失败）
    probe = subprocess.run([PY, os.path.join(BASE, "ops", "pg.py"), "status"],
                           cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if probe.returncode != 0 or b"server is running" not in probe.stdout:
        print("[X] 数据库未运行。先执行：python ops\\pg.py start")
        return 2

    want = set(a.only.split(",")) if a.only else None
    if a.after_only:
        todo = list(AFTER)
    else:
        todo = [s for s in STEPS if not want or s[0] in want]
        # --only 是定点排查，不做收尾（收尾会写库：复位基线 + 重算权重）。
        # 需要单独收尾时用 --after-only。
        if not a.no_after and not want:
            todo = todo + AFTER

    results = []
    print("=" * 78)
    print("医学生人才信息库 · 全套回归（共 %d 步）" % len(todo))
    print("=" * 78)
    for n, name, kind, tgt in todo:
        print("\n[%s] %s" % (n, name), flush=True)
        try:
            ok, summary, _ = run(kind, tgt, a.verbose)
        except subprocess.TimeoutExpired:
            ok, summary = False, "超时"
        results.append((n, name, ok, summary))
        print("     %s  %s" % ("[OK  ]" if ok else "[FAIL]", summary), flush=True)

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    bad = [r for r in results if not r[2]]
    for n, name, ok, summary in results:
        print("  %s %-4s %-28s %s" % ("[OK  ]" if ok else "[FAIL]", n, name, summary))
    print("\n%d 步通过，%d 步失败" % (len(results) - len(bad), len(bad)))
    if bad:
        print("失败：%s" % "、".join("%s %s" % (r[0], r[1]) for r in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
