# -*- coding: utf-8 -*-
"""
归档一次性探针脚本（独立审查 P3-2 的建议）

审查意见："`schema/checks/` 与 `ops/fixtures/` 里有大量一次性探针脚本
（`_probe_*.sql`、`_diag_*.py`），建议归档以免与现行断言混淆。"

**混淆是真实的代价**：`ops/fixtures/` 现在混着三类东西 ——
  ① 被测试/代码引用的**现行夹具**（real_cases.json、_fk_map.sql…）；
  ② 我为了排查某一个问题写的一次性探针（_probe_*.sql、_diag_*.py…）；
  ③ 生成/复位夹具的工具。
读的人无法一眼分辨"哪些还在生效"，于是要么全信、要么全不信。

做法：把**未被任何地方引用**的一次性文件移到 `ops/fixtures/once/`，
并写一份 README 说明约定。**只移动未被引用的** —— 先在整个仓库里搜引用，
搜到就留着（例如 `_fk_map.sql` 被 portal_mockreg.py 引用、`_lab_access.py` 被 portal.py 引用，
它们看着像探针，其实是生产代码依赖）。
"""
import io
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(BASE, "ops", "fixtures")
ONCE = os.path.join(FIX, "once")

# 搜索范围：整个仓库（排除归档目录、缓存、工具目录）
SKIP_DIRS = {".git", ".tools", "__pycache__", "once", "pgdata", "archive"}


def repo_text_files():
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".py", ".sql", ".json", ".md", ".ps1", ".conf")):
                yield os.path.join(root, f)


def referenced(name):
    """该文件名在仓库里是否被引用（按文件名搜，够用且保守）。"""
    pat = re.compile(re.escape(name))
    for p in repo_text_files():
        if os.path.basename(p) == name:
            continue                                  # 文件自己不算
        try:
            with io.open(p, encoding="utf-8", errors="replace") as fh:
                if pat.search(fh.read()):
                    return os.path.relpath(p, BASE)
        except OSError:
            pass
    return None


def main():
    os.makedirs(ONCE, exist_ok=True)
    moved, kept = [], []
    for fn in sorted(os.listdir(FIX)):
        p = os.path.join(FIX, fn)
        if not os.path.isfile(p) or not fn.startswith("_"):
            continue
        ref = referenced(fn)
        if ref:
            kept.append((fn, ref))
            continue
        shutil.move(p, os.path.join(ONCE, fn))
        moved.append(fn)

    with io.open(os.path.join(ONCE, "README.md"), "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write("""# 归档的一次性探针脚本

这些文件是**排查某一个问题时写下的一次性脚本**，不是现行断言的一部分。
保留它们是为了让当时的证据可复核（每条结论都能追溯"我是怎么查出来的"），
但**不要在它们之上做判断** —— 它们可能引用了早已变化的表/列/接口。

## 约定

| 目录 | 含义 |
|---|---|
| `ops/fixtures/` | **现行夹具**：被测试或生产代码引用的（`real_cases.json`、`_fk_map.sql`…） |
| `ops/fixtures/once/`（本目录） | 一次性探针的历史存档：`_probe_*` / `_diag_*` / `_verify_*` / `_recon_*` 等 |
| `ops/tests/` | **现行断言**：回归套件，每次 `run_all.py` 都跑 |

判断"某个东西是否还在生效"只看 `ops/tests/` 与代码里的引用 ——
本目录里的东西**默认不生效**。

## 为什么归档而不是删除

删除会让"当时的结论是怎么得出的"无从复核；而把探针和现行断言混在一起，
会让读的人分不清哪些还在生效（要么全信、要么全不信）。
归档是"保留证据 + 明确边界"。
""")
    print("[✓] 归档 %d 个一次性探针 → ops/fixtures/once/" % len(moved))
    for m in moved[:12]:
        print("    %s" % m)
    if len(moved) > 12:
        print("    …还有 %d 个" % (len(moved) - 12))
    print("\n[=] 保留 %d 个（被引用，看着像探针其实是现行依赖）：" % len(kept))
    for n, r in kept:
        print("    %-28s ← %s" % (n, r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
