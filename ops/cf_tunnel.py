# -*- coding: utf-8 -*-
"""
ops/cf_tunnel.py —— 把只读门户挂到公网的隧道管理（启 / 停 / 看状态 / 取地址）

为什么要有这个工具而不是让你记命令
--------------------------------------------------------------------------
1. **一键下线**。把连着真实数据库的页面挂到公网，出问题时必须能立刻停掉，
   而不是"去翻笔记找命令"。`stop` 会杀掉所有 cloudflared 进程。
2. **地址会变**。Quick Tunnel（免账号）**每次重启都换域名**，
   所以地址必须由工具现取现报，写死在文档里一定会过期。
3. **暴露面是硬约束**。本工具**只允许**把端口指向 127.0.0.1 上的允许清单，
   并且默认只允许 8082（只读门户）。可写进程（8083）、控制台（8081）、
   数据库（55432）**不允许**经此工具暴露 —— 这是 docs/19 的部署纪律，
   写在代码里比写在文档里更可靠。

实测过的关键事实（写下来，避免重复踩）
--------------------------------------------------------------------------
· Quick Tunnel **无法挂 Cloudflare Access 策略**（官方明确：Access 应用要求域名属于
  你账号里的 zone，而 trycloudflare.com 不属于你）。所以 Quick Tunnel 下**唯一**的门
  是应用自己的登录与列级权限。本项目恰好有这一层（T0 匿名只读公开内容），因此可用；
  但要挂**私有**数据，必须走 Named Tunnel + 自有域名 + Access。
· 该账号当前 **Access 未启用**（API 返回 `access.api.error.not_enabled`），
  且**名下没有域名** —— 所以现阶段只能用 Quick Tunnel。
· 隧道没有可用性保证（官方原文：account-less tunnels have no uptime guarantee）。

用法
    python ops/cf_tunnel.py start            # 启动隧道（默认指向 8082）
    python ops/cf_tunnel.py url              # 打印当前公网地址
    python ops/cf_tunnel.py status           # 进程 + 连通性自检
    python ops/cf_tunnel.py stop             # **紧急下线**
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CF = r"D:\wbo-workspace\.tools\cloudflared.exe"
URLFILE = r"D:\wbo-workspace\.tools\tunnel_url.txt"

# 允许经隧道暴露的端口白名单。只读门户在 8082。
# 8083 可写 / 8081 控制台 / 55432 数据库 —— 绝不允许（docs/19）。
ALLOWED = {8082: "只读门户（唯一允许暴露的服务）"}


def procs():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-Process cloudflared -ErrorAction SilentlyContinue).Id -join ','"],
                       capture_output=True, text=True, encoding="utf-8")
    ids = [x for x in (r.stdout or "").strip().split(",") if x.strip()]
    return ids


def read_url():
    if os.path.isfile(URLFILE):
        with open(URLFILE, encoding="utf-8") as fh:
            return fh.read().strip()
    return None


def cmd_start(a):
    if not os.path.isfile(CF):
        print("[X] 找不到 cloudflared：%s" % CF)
        print("    先跑：python ops\\cf_get_cloudflared.py")
        return 1
    if a.port not in ALLOWED:
        print("[X] 拒绝：端口 %d 不在允许清单里。允许的只有：%s"
              % (a.port, "、".join("%d（%s）" % (k, v) for k, v in ALLOWED.items())))
        return 3
    if procs():
        print("[!] 已有 cloudflared 在跑（pid %s）。先 stop 再 start。" % ",".join(procs()))
        return 1
    print("[1] 启动隧道 → http://127.0.0.1:%d（%s）" % (a.port, ALLOWED[a.port]))
    # stderr 里既有人看的横幅也有日志，所以直接重定向到文件，再从文件里抓 URL
    log = r"D:\wbo-workspace\.tools\tunnel.log"
    with open(log, "w", encoding="utf-8") as lf:
        p = subprocess.Popen([CF, "tunnel", "--url", "http://127.0.0.1:%d" % a.port,
                              "--no-autoupdate"], stdout=lf, stderr=lf,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    print("    进程 pid %d，日志 %s" % (p.pid, log))
    url = None
    for _ in range(40):
        time.sleep(2)
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", fh.read())
            if m:
                url = m.group(0)
                break
        except OSError:
            pass
    if not url:
        print("[X] 60 秒内没拿到公网地址，看日志：%s" % log)
        return 1
    with open(URLFILE, "w", encoding="utf-8") as fh:
        fh.write(url)
    print("\n[✓] 公网地址：%s" % url)
    print("    验证（从公网那一侧）：python ops\\cf_preflight.py %s" % url)
    print("    ⚠ 免账号的 Quick Tunnel **每次重启换域名**，且无法挂 Access 策略。")
    print("    ⚠ 紧急下线：python ops\\cf_tunnel.py stop")
    return 0


def cmd_url(a):
    u = read_url()
    print(u or "（还没有记录地址；先跑 start）")
    return 0 if u else 1


def cmd_status(a):
    ps_ = procs()
    print("cloudflared 进程：%s" % ("、".join(ps_) or "未运行"))
    u = read_url()
    print("记录的地址：%s" % (u or "无"))
    if u:
        try:
            req = urllib.request.Request(u + "/", headers={"User-Agent": "medtalent-check"})
            with urllib.request.urlopen(req, timeout=30) as r:
                b = r.read().decode("utf-8", "replace")
            print("公网可达：HTTP %s，%d 字节，形态=%s"
                  % (r.status, len(b),
                     "公开落地页" if "公开（T0）概览" in b else "未知"))
        except Exception as e:                            # noqa: BLE001
            print("公网不可达：%s" % str(e)[:120])
    return 0


def cmd_stop(a):
    ps_ = procs()
    if not ps_:
        print("[=] 没有 cloudflared 在跑")
        return 0
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force"],
                   capture_output=True, text=True)
    time.sleep(1)
    left = procs()
    print("[✓] 已下线（停掉 %s）" % "、".join(ps_))
    if left:
        print("[!] 仍有残留：%s" % "、".join(left))
    print("    提醒：隧道下线只切断公网入口；本机 8082 仍在监听（那是对的）。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--port", type=int, default=8082)
    s.set_defaults(fn=cmd_start)
    sub.add_parser("url").set_defaults(fn=cmd_url)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
