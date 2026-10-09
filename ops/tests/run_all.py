#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ops/tests/run_all.py —— 一条命令跑完整套回归（15 套 + 2 个收尾动作）

为什么需要它：这条链路的**顺序**本身是有语义的，手工跑反复踩坑：
  · collect/backup 测试会重新解析 JD，而 jd_ingest 是"先删后插" job_requirement，
    会把 concept_id 与权重一起抹掉 → 之后必须重建派生层，否则匹配静默失效。
  · evolution_test 会拆分真实种子节点 → 跑完必须复位基线，否则职业树凭空少枝。
把顺序写进代码，就不会因为"我记得先跑哪个"而出错。

用法：
  python ops/tests/run_all.py                # 完整档（默认）：15 套 + 2 个收尾
  python ops/tests/run_all.py --fast         # 快速档：每次改动都跑的"最小可信集合"
  python ops/tests/run_all.py --tier fast    # 同上（--fast 的等价写法）
  python ops/tests/run_all.py --only 3,4     # 只跑指定步骤（依赖的收尾动作会自动补上）
  python ops/tests/run_all.py --only R1      # 只跑某个收尾动作
  python ops/tests/run_all.py --after-only   # 只跑收尾（复位基线 + 重建派生层）
  python ops/tests/run_all.py --list         # 看步骤清单（含档位与副作用）
  python ops/tests/run_all.py -v             # 打印每步完整输出

关于两个档位（分级依据是实测耗时，不是感觉；原始数字见下面的 COST 注释）：
  · fast = 每次改动都想跑的集合。挑选标准是"零持久副作用 + 覆盖三个最容易被改动打穿的面"：
    数据字典/结构一致性、数据库层不变量、动态建模（本项目"零 DDL"的旗舰机制）、
    以及两个对外可见的页面（控制台、可视化分析）。实测约 19 秒（预算 60 秒）。
  · full = 发版前才跑的完整集合，仍是默认档。备份/恢复（T10）、采集管道（T08）、
    小程序 bridge（T09）、门户全页（T13）、开发者模式（T14）、演化层（T12）
    都只在 full 里跑——它们要么贵，要么有持久副作用（必须配对收尾动作）。

  fast 档**故意不跑收尾动作**（R1/R2）：R1 服务的 evolution_test、R2 服务的
  collect/backup 测试都不在 fast 里，且 fast 选中的 9 套经实测零持久副作用
  （见 _perf_fastcand 验证：事务回滚 / 例行代码，不写基线、不重建派生层）。
  若显式用 --only 点到了有副作用的步骤，run_all 会把该步骤依赖的收尾动作自动补上。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.stdout.reconfigure(encoding="utf-8")

PY = sys.executable

# ---------------------------------------------------------------------------
# 步骤表
#
# (编号, 名称, 类型, 目标, 档位, 副作用, 实测秒数, 覆盖说明)
#   类型 kind：py=直接跑脚本；sql=经 ops/pg.py sql 执行
#   档位 tier：fast=两个档位都跑；full=只在完整档跑
#   副作用 edge：clean=零持久副作用（事务内回滚或只读）；
#               tree=会拆分真实职业树节点（需要 R1 复位）；
#               derived=会重建/追加派生层（重跑解析 JD，需要 R2 重建）；
#               both=两者都要；audit=只追加审计流水 change_log（设计上 append-only）
#   实测秒数为本机 Postgres 16.8 / 库 medtalent 上的单步实测值（见报告）。
# ---------------------------------------------------------------------------
STEPS = [
    ("1", "目录与词表静态校验", "py", "code/validate_catalog.py",
     "fast", "clean", 0.20, "49 代码表 / 302 代码值 / 字段目录登记完整性"),
    ("2", "Schema 静态校验", "py", "code/validate_schema.py",
     "fast", "clean", 0.34, "DDL 静态：BEGIN/COMMIT、外键目标、保留字、前向引用"),
    ("3", "码值一致性（存码不存标签）", "sql", "ops/tests/code_conformance.sql",
     "fast", "clean", 0.65, "CHECK 字面量必须能在 code_value 里找到"),
    ("4", "门禁回归 + 完整性不变量", "sql", "ops/tests/regression_guards.sql",
     "fast", "clean", 0.82, "schema 层的语义门禁：未登记 attrs/字段必须被拒"),
    ("5", "Schema 冒烟", "sql", "ops/tests/smoke_schema.sql",
     "fast", "clean", 0.90, "真实形状数据走一遍全链路（事务内 ROLLBACK）"),
    ("6", "动态行列增删", "py", "ops/tests/dynamic_model_test.py",
     "fast", "clean", 2.80, "零 DDL 加列建行（全程单事务，结束回滚）"),
    ("7", "动态表单服务", "py", "ops/tests/form_server_test.py",
     "fast", "clean", 5.58, "n 个选项 → 用户选择 → 入库"),
    ("8", "L0 采集与去重", "py", "ops/tests/collect_test.py",
     "full", "derived", 14.46, "采集合规门禁 + 内容哈希去重（会重跑解析 JD）"),
    ("9", "小程序 bridge", "py", "ops/tests/bridge_test.py",
     "full", "clean", 6.49, "交换包摄入 / 幂等 / 墓碑 / 授权门 / outbox"),
    ("10", "备份/保留/恢复", "py", "ops/tests/backup_test.py",
     "full", "derived", 348.47, "全库备份 + sha256 校验 + 恢复到新库核对（最贵）"),
    ("11", "控制台端到端", "py", "ops/tests/console_test.py",
     "fast", "clean", 5.70, "业务控制台 8 个页面真实读写"),
    ("12", "演化层（职业树生长）", "py", "ops/tests/evolution_test.py",
     "full", "tree", 10.07, "候选发现→提升→拆分→版本化重算（会拆分真实节点）"),
    ("13", "数据库门户（展示/检索/分析）", "py", "ops/tests/portal_test.py",
     "full", "clean", 30.85, "门户 13 个页面 + 只读证明（页面多，最慢的非备份套件）"),
    ("14", "开发者模式（可写/零 DDL）", "py", "ops/tests/devmode_test.py",
     "full", "derived", 16.22, "在线 SQL 试运行回滚 + 动态建模 + 运维工具按钮"),
    ("15", "可视化与分析层（口径一致）", "py", "ops/tests/viz_test.py",
     "fast", "clean", 8.74, "16 面板可渲染/可导 CSV + 图表口径与 SQL 一致"),
    ("16", "真实案例与维度体检", "py", "ops/tests/real_test.py",
     "full", "clean", 12.00, "画像向量组装层 + 正交/充分/有效三项体检（真实公开案例，只读）"),
    ("17", "字段级访问控制", "py", "ops/tests/access_test.py",
     "full", "clean", 12.00, "等级角色 + 列级 GRANT 真的在拦（只读：全部在事务内回滚）"),
    # 指标门禁：34 条可测量指标的记分卡，棘轮模式（基线 ops/health_baseline.json）
    ("18", "优秀数据库指标门禁", "py", "ops/health.py",
     "fast", "clean", 8.00, "35 条指标记分卡；新出现的 MUST 退化即失败（已知未达标项在基线里）"),
    # 列级剖析物化：目录页只读快照，计算在这里做。full 档跑它（实测 ~22s，fast 档放不下）
    ("19", "列级剖析物化", "py", "ops/profile.py",
     "full", "derived", 25.00, "把空值率/基数/Top-K 算进 column_profile（--all，全库 1000+ 列）"),
]

# 收尾动作：不属于任何档位的"筛选范围"，而是有副作用的步骤的依赖。
# 完整档总是执行；--only 只在点到需要它的步骤时才补上；fast 档不执行（见文件头说明）。
AFTER = [
    ("R1", "复位职业树基线", "py", "code/evolve/restore_baseline.py",
     1.46, "evolution_test 拆分真实节点后必须复位，否则职业树凭空少枝"),
    ("R2", "重建派生层并重算能力权重", "py", "code/analytics/build_competency.py",
     5.10, "重新解析 JD 会清空 concept_id 与权重，必须重建否则匹配静默失效"),
]

# 收尾动作服务于哪些副作用
AFTER_FOR = {"tree": ("R1",), "derived": ("R2",), "both": ("R1", "R2")}

RE_PY = re.compile(r"PASS\s+(\d+)\s*项[，,]\s*FAIL\s+(\d+)\s*项")
RE_STATIC = re.compile(r"错误：(\d+)\s+警告：(\d+)")
RE_PASS_NOTICE = re.compile(r"PASS[ :]")
# 独立单词 FAIL：避免把 "FAILED"/"FAIL_URL" 或说明文字里的 FAIL 误判。
# 必须排除 psql 的命令回显行（以 "$ " 开头），它带着 -f <路径> 会混进解析。
RE_BARE_FAIL = re.compile(r"(?<![A-Za-z0-9_])FAIL(?![A-Za-z0-9_])")
PSQL_ECHO = re.compile(r"^\s*\$\s")

STEPS_BY_ID = {s[0]: s for s in STEPS}
AFTER_BY_ID = {s[0]: s for s in AFTER}
STEP_IDS = [s[0] for s in STEPS]
AFTER_IDS = [s[0] for s in AFTER]


def clean_output(raw: str) -> str:
    """去掉 ops/pg.py 回显进来的 psql 命令行，避免它被当成测试输出解析。

    没有这一步时，SQL 用例的摘要是 "psql:D:/.../xxx.sql:95:"（命令行本身的尾巴），
    而不是测试真正报告的结果——正是这个缺陷让摘要看起来像在说别的事。
    """
    return "\n".join(ln for ln in raw.splitlines() if not PSQL_ECHO.match(ln))


def last_meaningful(raw: str) -> str:
    """取最后一行有意义的输出（跳过纯分隔线），用于没有计数可报的步骤。"""
    for ln in reversed(raw.splitlines()):
        s = ln.strip()
        if s and not set(s) <= set("=-—"):
            return s[:72]
    return ""


def run(kind, target, verbose):
    """执行一步，返回 (ok, 摘要, 输出, 耗时秒)。"""
    if kind == "py":
        cmd = [PY, os.path.join(BASE, target.replace("/", os.sep))]
    else:
        cmd = [PY, os.path.join(BASE, "ops", "pg.py"), "sql",
               os.path.join(BASE, target.replace("/", os.sep))]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=BASE, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=3600)
    except subprocess.TimeoutExpired as e:
        dt = time.perf_counter() - t0
        # psql/测试卡住时也要给出已经跑掉的秒数，否则"超时"是个没有量纲的词
        raw = (e.output or b"").decode("utf-8", "replace")
        if verbose:
            print(raw)
        return False, "超时（>3600s）", raw, dt
    dt = time.perf_counter() - t0
    raw = p.stdout.decode("utf-8", "replace")
    body = clean_output(raw)
    ok = p.returncode == 0

    # 摘要：优先取测试自己报的 PASS/FAIL 计数，其次静态校验的"错误：N"
    summary = ""
    m = RE_PY.search(body)
    if m:
        np_, nf = int(m.group(1)), int(m.group(2))
        ok = ok and nf == 0
        summary = "PASS %d / FAIL %d" % (np_, nf)
    else:
        m = RE_STATIC.search(body)
        if m:
            nerr, nwarn = int(m.group(1)), int(m.group(2))
            ok = ok and nerr == 0
            summary = "错误 %d / 警告 %d" % (nerr, nwarn)
        else:
            # SQL 用例：数 PASS 通知，出现独立单词 FAIL 即失败
            npass = len(RE_PASS_NOTICE.findall(body))
            bad = RE_BARE_FAIL.search(body) is not None
            ok = ok and not bad
            if npass:
                summary = "%d 条用例%s" % (npass, "，含 FAIL" if bad else "")
            else:
                # 这类脚本（复位基线、重建派生层）不自报用例数。
                # 失败时把最后一行输出带出来，否则只有一个 [FAIL] 没人知道为什么。
                tail = last_meaningful(body)
                summary = tail if tail else ("退出码 %d" % p.returncode)
            if not ok and npass and body:
                tail = last_meaningful(body)
                if tail and tail not in summary:
                    summary += "  ← %s" % tail
    if verbose:
        print(raw)
    return ok, summary, raw, dt


def select_todo(tier, want_ids, after_only, no_after):
    """按档位/--only/收尾开关算出要跑的步骤，返回 (todo, auto_added)。

    todo 里每个元素是 (编号, 名称, 类型, 目标)；auto_added 是被自动补上的收尾编号。
    """
    if after_only:
        return list(AFTER), []

    if want_ids:
        todo = [STEPS_BY_ID[i] for i in STEP_IDS if i in want_ids]
        # 显式点名收尾动作也要能跑
        todo += [AFTER_BY_ID[i] for i in AFTER_IDS if i in want_ids]
    else:
        todo = [s for s in STEPS if tier == "full" or s[4] == "fast"]

    if no_after:
        return todo, []

    # 补上"被选中步骤所依赖的收尾动作"。
    # 原版 --only 一律不做收尾，于是 `--only 12` 会把职业树留在被拆分状态；
    # `--only 10` 会把派生层留在被清空状态——定点排查反而制造了坑。
    have = {s[0] for s in todo}
    auto = []
    if want_ids:
        for s in todo:
            if s[0] in AFTER_BY_ID:
                continue
            for aid in AFTER_FOR.get(s[5], ()):
                if aid not in have and aid not in auto:
                    auto.append(aid)
        # 收尾动作之间也是有顺序的（先复位基线，再重建派生层），统一按 AFTER 的顺序排
        auto = [a for a in AFTER_IDS if a in auto]
        todo = todo + [AFTER_BY_ID[a] for a in auto]
    elif tier == "full":
        for s in AFTER:
            if s[0] not in have:
                todo = todo + [s]
    # fast 档不补收尾：fast 选中的步骤实测零持久副作用（见文件头说明）
    return todo, auto


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑这些编号（步骤号 1-15 或收尾 R1/R2），逗号分隔，如 3,4,12")
    ap.add_argument("--tier", choices=("fast", "full"), default="full",
                    help="档位：fast=每次改动都跑的最小可信集合；full=发版前的完整集合（默认）")
    ap.add_argument("--fast", action="store_true", help="等价于 --tier fast")
    ap.add_argument("--after-only", action="store_true", help="只跑收尾动作（复位基线 + 重建派生层）")
    ap.add_argument("--list", action="store_true", help="列出步骤（含档位与副作用）")
    ap.add_argument("--no-after", action="store_true", help="跳过收尾动作（调试用）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印完整输出")
    a = ap.parse_args()

    if a.fast:
        a.tier = "fast"

    if a.list:
        print("步骤（档位 fast 表示两个档位都跑；full 表示只在完整档跑）")
        print("  %-4s %-26s %-6s %-8s %-9s %s" % ("编号", "名称", "档位", "副作用", "实测秒数", "目标"))
        for n, name, kind, tgt, tier, edge, cost, _cov in STEPS:
            print("  %-4s %-26s %-6s %-8s %9.2f  %s" % (n, name, tier, edge, cost, tgt))
        print("\n收尾动作（不被档位筛选；由有副作用的步骤依赖）")
        for n, name, kind, tgt, cost, _why in AFTER:
            print("  %-4s %-26s %-6s %-8s %9.2f  %s" % (n, name, "-", "-", cost, tgt))
        print("\n副作用：clean=零持久副作用  tree=需 R1 复位职业树  derived=需 R2 重建派生层  audit=只追加审计流水")
        print("档位：full（默认）15 套 + 2 收尾；fast 只跑档位为 fast 的套件，实测合计约 19 秒")
        return 0

    # 校验 --only 的编号，避免把打错的编号静默变成"跑 0 步、报成功"
    want_ids = []
    if a.only:
        valid = set(STEP_IDS) | set(AFTER_IDS)
        raw_ids = [x.strip() for x in a.only.split(",") if x.strip()]
        # 兼容 "r1" 小写写法
        raw_ids = [("R" + x[1:]) if x[:1].lower() == "r" else x for x in raw_ids]
        unknown = [x for x in raw_ids if x not in valid]
        if unknown:
            print("[X] --only 里有不存在的编号：%s" % "、".join(unknown))
            print("    可用编号：" + ",".join(STEP_IDS) + "（步骤） / "
                  + ",".join(AFTER_IDS) + "（收尾）")
            return 2
        want_ids = raw_ids

    # 前置：数据库必须在线，否则全盘皆错（先给一句人话，别让 15 套一起报连接失败）
    probe = subprocess.run([PY, os.path.join(BASE, "ops", "pg.py"), "status"],
                           cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if b"server is running" not in probe.stdout:
        print("[X] 数据库未运行。先执行：python ops\\pg.py start")
        return 2

    todo, auto = select_todo(a.tier, want_ids, a.after_only, a.no_after)

    tier_label = "完整档" if a.tier == "full" else "快速档"
    if a.after_only:
        tier_label = "收尾"
    print("=" * 78)
    print("医学生人才信息库 · %s（共 %d 步）" % (tier_label, len(todo)))
    if a.tier == "fast" and not want_ids:
        print("只跑 fast 档套件：零持久副作用、覆盖字典/结构/不变量/动态建模/对外页面。")
        print("备份恢复、采集、bridge、门户全页、开发者模式、演化层只在完整档跑。")
    print("=" * 78)

    results = []
    for step in todo:
        n, name, kind, tgt = step[0], step[1], step[2], step[3]
        tag = ""
        if n in auto:
            tag = "  （自动补上：前面步骤有副作用）"
        print("\n[%s] %s%s" % (n, name, tag), flush=True)
        ok, summary, _raw, dt = run(kind, tgt, a.verbose)
        results.append((n, name, ok, summary, dt))
        print("     %s  %s  —— %.2fs" % ("[OK  ]" if ok else "[FAIL]", summary, dt), flush=True)

    total = sum(r[4] for r in results)
    bad = [r for r in results if not r[2]]

    print("\n" + "=" * 78)
    print("耗时排行（按单步耗时降序）")
    print("=" * 78)
    print("  %-8s %-4s %-26s %s" % ("秒数", "编号", "名称", "占比"))
    for n, name, ok, summary, dt in sorted(results, key=lambda r: r[4], reverse=True):
        share = (dt / total * 100) if total else 0.0
        print("  %8.2f %-4s %-26s %5.1f%%" % (dt, n, name, share))
    print("  %8.2f %-4s %s" % (total, "合计", "%d 步" % len(results)))

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    for n, name, ok, summary, dt in results:
        print("  %s %-4s %-26s %-22s %7.2fs"
              % ("[OK  ]" if ok else "[FAIL]", n, name, summary, dt))
    print("\n%d 步通过，%d 步失败，总耗时 %.2fs" % (len(results) - len(bad), len(bad), total))
    if bad:
        print("失败：%s" % "、".join("%s %s" % (r[0], r[1]) for r in bad))
    if a.tier == "fast" and not want_ids and not a.no_after:
        print("（快速档不跑收尾动作 R1/R2：所选套件实测零持久副作用，"
              "不需要复位基线或重建派生层）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
