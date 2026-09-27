#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ops/install_postgres.py — 在无管理员权限的 Windows 上部署便携版 PostgreSQL 16

为什么用便携版：本机没有 winget/choco，且当前会话非管理员、沙箱只允许写工作区。
EnterpriseDB 提供的 windows-x64-binaries.zip 解压即用，无需安装、无需管理员。

用法：
  python ops/install_postgres.py            # 下载并解压
  python ops/install_postgres.py --check    # 只检查现有安装

产物：
  D:\\wbo-workspace\\.tools\\pgsql\\     可执行文件（bin/initdb.exe, bin/pg_ctl.exe, bin/psql.exe）
  D:\\wbo-workspace\\.tools\\downloads\\ 下载缓存（便于重装）
"""
import argparse
import os
import shutil
import sys
import time
import urllib.request
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

VERSION = "16.8-1"
URL = "https://get.enterprisedb.com/postgresql/postgresql-%s-windows-x64-binaries.zip" % VERSION
TOOLS = r"D:\wbo-workspace\.tools"
PGROOT = os.path.join(TOOLS, "pgsql")
DLDIR = os.path.join(TOOLS, "downloads")
ZIP = os.path.join(DLDIR, "postgresql-%s-windows-x64-binaries.zip" % VERSION)


def human(n):
    return "%.1f MB" % (n / 2 ** 20)


def check():
    exe = os.path.join(PGROOT, "bin", "pg_ctl.exe")
    if os.path.exists(exe):
        print("[OK] 已安装：%s" % exe)
        return 0
    print("[--] 未安装（缺少 %s）" % exe)
    return 1


def download():
    os.makedirs(DLDIR, exist_ok=True)
    if os.path.exists(ZIP):
        print("[=] 已存在下载缓存：%s（%s）" % (ZIP, human(os.path.getsize(ZIP))))
        return
    print("[↓] %s" % URL)
    t0 = time.time()
    last = [0.0]

    def hook(blocks, bs, total):
        got = blocks * bs
        now = time.time()
        if now - last[0] < 5:
            return
        last[0] = now
        pct = (got / total * 100) if total > 0 else 0
        speed = got / max(now - t0, 0.001) / 2 ** 20
        sys.stdout.write("\r    %5.1f%%  %s / %s  %.2f MB/s"
                         % (pct, human(got), human(total), speed))
        sys.stdout.flush()

    urllib.request.urlretrieve(URL, ZIP, hook)
    print("\n[✓] 下载完成：%s（%s，用时 %.0fs）"
          % (ZIP, human(os.path.getsize(ZIP)), time.time() - t0))


def extract():
    if os.path.isdir(PGROOT):
        print("[=] 已解压，跳过")
        return
    os.makedirs(TOOLS, exist_ok=True)
    print("[*] 解压到 %s ..." % TOOLS)
    with zipfile.ZipFile(ZIP) as z:
        names = z.namelist()
        top = sorted(set(n.split("/")[0] for n in names))
        print("    压缩包顶层：%s" % top)
        z.extractall(TOOLS)
    # zip 内顶层目录通常是 pgsql/
    src = os.path.join(TOOLS, "pgsql")
    if os.path.isdir(src):
        print("[✓] 解压完成：%s" % src)
    else:
        print("[!] 未找到预期的 pgsql/ 目录，顶层为 %s" % top)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check()
    download()
    extract()
    return check()


if __name__ == "__main__":
    sys.exit(main())
