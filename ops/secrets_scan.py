# -*- coding: utf-8 -*-
"""
ops/secrets_scan.py —— 凭据泄露扫描（把"别把凭据提交上去"从纪律变成可执行的检查）

为什么要做成工具，而不是继续"提交前手工扫一遍"
--------------------------------------------------------------------------
实测教训：我在提交一批归档文件时**手工扫了**，扫到 4 处命中，其中 3 处是
我自己写的探针脚本里**硬编码了管理员登录邮箱**。手工扫之所以能发现，是运气 ——
更常见的情况是扫的模式不全（我当时扫的是 token 前缀与口令，没把"登录邮箱"当凭据），
于是漏掉。**凡是要靠"每次记得做"的检查，迟早会漏。**

设计：不硬编码任何凭据值，而是**把我们真正当作凭据保存的东西，拿去仓库里搜**
--------------------------------------------------------------------------
三步，全部不打印命中内容（否则扫描器自己成了泄露源）：
  ① **已知凭据值**：环境变量 / 注册表里的（`ops/env.py` 的 KNOWN 与 `MEDTALENT_*`）
     + `.tools/credentials/*.txt` 里保存的值 —— 它们**一个都不该出现在仓库里**；
  ② **通用形态**：token 前缀（`cfat_`/`ghp_`/`sk-`/`AKIA`…）、私钥头
     （`BEGIN ... PRIVATE KEY`）、明文口令赋值；
  ③ 命中的文件与行号要报出来，**但值只显示前 4 位 + 长度**，够定位、不够利用。

为什么①能覆盖"邮箱也算凭据"这种情况：因为我们**确实把它当凭据存了**
（`medtalent_admin_email.txt`）。判据不是"这东西看起来像不像密钥"，
而是"**我们是不是把它当凭据保管**" —— 后者是事实，前者是感觉。

用法
    python ops/secrets_scan.py            # 扫全仓（返回非 0 = 有命中）
    python ops/secrets_scan.py --staged   # 只扫已暂存的改动（提交前用）
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "ops"))

SKIP_DIRS = {".git", "__pycache__", "pgdata", "node_modules", "archive"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".exe", ".dll",
           ".pyc", ".woff", ".woff2", ".ico"}
TEXT_EXT = {".py", ".sql", ".json", ".md", ".txt", ".ps1", ".conf", ".csv", ".yml",
            ".yaml", ".toml", ".ini", ".sh", ".bat", ".html", ".js"}

# 通用形态：这些前缀/头部本身就是"这是凭据"的强信号
GENERIC = [
    (re.compile(r"\bcfat_[A-Za-z0-9_-]{20,}"), "Cloudflare API token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI 风格密钥"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥文件内容"),
    (re.compile(r"""(?i)\b(password|passwd|pwd|secret|token)\s*[:=]\s*["'][^"'\s]{12,}["']"""),
     "明文口令/密钥赋值"),
]


def cred_values():
    """我们从环境/注册表/凭据文件里保存的**真实值**（绝不打印）。"""
    vals = {}
    try:
        import env as E
        E.load_into_environ()
        names = set(getattr(E, "KNOWN", {})) | set(E._list_reg())
        for n in names:
            v = os.environ.get(n)
            if v and len(v) >= 8:
                vals["环境变量 %s" % n] = v
    except Exception:                                       # noqa: BLE001
        pass
    cdir = os.path.join(BASE, ".tools", "credentials")
    if os.path.isdir(cdir):
        for fn in os.listdir(cdir):
            p = os.path.join(cdir, fn)
            if not os.path.isfile(p):
                continue
            try:
                with io.open(p, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if "=" in line:
                            k, v = line.split("=", 1)
                            v = v.strip()
                            if len(v) >= 6:
                                vals["凭据文件 %s" % fn] = v
            except OSError:
                pass
    return vals


def mask(v: str) -> str:
    """只显示前 4 位 + 长度：够定位，不够利用。"""
    return "%s…（%d 字符）" % (v[:4], len(v))


def scan_text(path, text, secrets):
    """返回 [(行号, 说明, 命中值的掩码)]。**不返回命中值本身。**"""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for label, val in secrets.items():
            if val and val in line:
                hits.append((i, "已知凭据（%s）" % label, mask(val)))
        for rx, label in GENERIC:
            m = rx.search(line)
            if m:
                hits.append((i, "通用形态：%s" % label, mask(m.group(0))))
    return hits


def iter_files():
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".tools")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in SKIP_EXT or (ext and ext not in TEXT_EXT):
                continue
            yield os.path.join(root, f)


def main():
    ap = argparse.ArgumentParser(description="凭据泄露扫描（不打印命中内容）")
    ap.add_argument("--staged", action="store_true", help="只扫已暂存的改动")
    a = ap.parse_args()

    secrets = cred_values()
    print("用于比对的凭据：%d 项（%s）—— **只比对，不回显**"
          % (len(secrets), "、".join(sorted(secrets)) or "无"))

    files = []
    if a.staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only"],
                             cwd=BASE, capture_output=True, text=True,
                             encoding="utf-8").stdout
        for rel in (out or "").split("\n"):
            rel = rel.strip()
            if rel and os.path.isfile(os.path.join(BASE, rel)):
                files.append(os.path.join(BASE, rel))
    else:
        files = list(iter_files())

    total = 0
    for p in files:
        try:
            with io.open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if os.path.basename(p) == "secrets_scan.py":
            continue                                   # 扫描器自己含模式串
        hits = scan_text(p, text, secrets)
        for ln, label, m in hits:
            total += 1
            print("  [命中] %s:%d  %s  → %s"
                  % (os.path.relpath(p, BASE), ln, label, m))

    print()
    if total:
        print("[X] 共 %d 处命中。**不要提交** —— 先生成值、再把文件里的值改成占位符，"
              "或把该文件加进 .gitignore。" % total)
        print("    注意：git 历史里已有的值不会被本次清理移除，需要改写历史或换掉该凭据。")
        return 1
    print("[✓] 未发现凭据泄露（比对 %d 个文件）" % len(files))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
