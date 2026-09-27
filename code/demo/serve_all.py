# -*- coding: utf-8 -*-
"""
code/demo/serve_all.py —— 一条命令把两个站点都起起来（给人工校验用）

为什么需要它：门户（只读，:8082）与开发者模式（可写，:8083）是**两个进程**，
这是有意的信任边界（见 `portal_dev.py` 开头）。但校验时逐个启动很烦，且容易忘了
先确认数据库在跑、或者忘了先自检。

这个脚本按顺序做四件事，任何一步失败就明确报错并停下：

  ① 探活数据库（没起就调用 `ops/pg.py start` 起一次，再探）
  ② 对两个站点各跑一次**渲染自检**（不开服务，把每个页面都渲染一遍）
     —— 这一步能挡住 90% 的"改了代码但页面坏了"
  ③ 并发启动两个 HTTP 服务（都在 127.0.0.1）
  ④ 打印可点的 URL 与"该看什么"，Ctrl+C 一起停

用法：
  python code/demo/serve_all.py                 # 自检 + 起两个站点
  python code/demo/serve_all.py --no-check      # 跳过自检，直接起（快）
  python code/demo/serve_all.py --check-only    # 只自检，不起服务（CI/收尾用）
  python code/demo/serve_all.py --portal-only   # 只起只读门户
  python code/demo/serve_all.py --dev-only      # 只起开发者模式
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
for p in (os.path.join(BASE, "code"), HERE):
    sys.path.insert(0, p)

sys.stdout.reconfigure(encoding="utf-8")

PORTAL_PORT = 8082
DEV_PORT = 8083
PY = sys.executable


def hr(title=""):
    print("\n" + "=" * 74)
    if title:
        print(title)
        print("=" * 74)


def db_up() -> bool:
    r = subprocess.run([PY, os.path.join(BASE, "ops", "pg.py"), "status"],
                       cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return b"server is running" in r.stdout


def ensure_db() -> bool:
    hr("① 数据库")
    if db_up():
        print("  [OK  ] PostgreSQL 已在运行（127.0.0.1:55432 / medtalent）")
        return True
    print("  [    ] 未运行，尝试启动 …")
    subprocess.run([PY, os.path.join(BASE, "ops", "pg.py"), "start"], cwd=BASE)
    time.sleep(2)
    ok = db_up()
    print("  [%s] 启动%s" % ("OK  " if ok else "FAIL", "成功" if ok else "失败，请手动检查 ops\\README-database.md"))
    return ok


def port_free(port) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_owner(port) -> tuple:
    """返回占用端口的 (pid, 进程名, 命令行)。用 netstat + tasklist，都是系统自带且输出为 ASCII。"""
    pid = None
    try:
        out = subprocess.run(["netstat", "-ano"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=30
                             ).stdout.decode("ascii", "replace")
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(":" + str(port)):
                    pid = parts[4]
                    break
    except Exception:                                          # noqa: BLE001
        pass
    if not pid:
        return None, "?", ""
    name = "?"
    try:
        t = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30
                           ).stdout.decode("utf-8", "replace")
        if t.strip().startswith('"'):
            name = t.strip().split('","')[0].strip('"')
    except Exception:                                          # noqa: BLE001
        pass
    cmd = ""
    try:
        c = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Process -Filter \"ProcessId=%s\")"
                            ".CommandLine" % pid],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60
                           ).stdout.decode("utf-8", "replace").strip()
        cmd = c[:200]
    except Exception:                                          # noqa: BLE001
        pass
    return pid, name, cmd


def preflight(pairs) -> list:
    """启动前检查端口。占用时返回 [(名称, 端口, pid, 进程名, 命令行)]。

    为什么必须做这一步（真实踩过）：上一轮演示留下的开发者模式进程占着 8083，
    新服务 bind 失败直接抛异常；而使用者看到的是"服务好像起来了、但界面还是旧的"
    ——因为浏览器连的是那个旧进程。端口冲突必须**在启动前说清楚是谁占的**。
    """
    busy = []
    for name, port in pairs:
        if not port_free(port):
            pid, pname, cmd = port_owner(port)
            busy.append((name, port, pid, pname, cmd))
    return busy


def kill_ours(pid, pname, cmd) -> bool:
    """只杀"确定是我们自己的演示服务"的进程——宁可不动，也不要误杀别人的东西。"""
    if not pid:
        return False
    if not str(pname).lower().startswith("python"):
        return False
    low = (cmd or "").lower()
    if not any(k in low for k in ("portal.py", "portal_dev.py", "portal_viz",
                                  "serve_all.py", "console.py", "app.py")):
        return False
    r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def ensure_ports(portal=True, dev=True, force=False) -> bool:
    pairs = []
    if portal:
        pairs.append(("只读门户", PORTAL_PORT))
    if dev:
        pairs.append(("开发者模式", DEV_PORT))
    busy = preflight(pairs)
    if not busy:
        return True
    hr("② 端口预检")
    for name, port, pid, pname, cmd in busy:
        print("  [占用] %s :%d  被 PID %s (%s) 占用" % (name, port, pid, pname))
        if cmd:
            print("         %s" % cmd)
    if force:
        for name, port, pid, pname, cmd in busy:
            if kill_ours(pid, pname, cmd):
                print("  [已终止] PID %s（--force）" % pid)
            else:
                print("  [跳过]   PID %s 不是本项目的演示服务，不杀" % pid)
        time.sleep(1)
        return not preflight(pairs)
    print("""
  处理办法（三选一）：
    1) 复用已在跑的旧服务：直接打开上面的 URL —— 但注意它可能是**旧代码**，
       页面不会更新。要看到最新界面请先把旧进程停掉。
    2) 停掉占用者：上面那行 PID，执行  taskkill /PID <pid> /F
    3) 让本脚本自动处理（只杀"确认是本项目演示服务"的进程）：
       python code\\demo\\serve_all.py --force
  也可以换端口：--portal-port N / --dev-port N""")
    return False


def run_check(name, argv) -> bool:
    t0 = time.time()
    r = subprocess.run([PY] + argv, cwd=BASE, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=900)
    out = r.stdout.decode("utf-8", "replace")
    ok = r.returncode == 0
    # 只回显自检的关键行，避免刷屏
    keep = [ln for ln in out.splitlines()
            if any(k in ln for k in ("[PASS]", "[FAIL]", "[X]", "[✓]", "对象：", "自检"))]
    print("  [%s] %-22s %5.1f 秒" % ("OK  " if ok else "FAIL", name, time.time() - t0))
    for ln in keep[-8:]:
        print("        " + ln.strip())
    return ok


def check_all() -> bool:
    hr("② 渲染自检（不开服务，把每个页面都渲染一遍）")
    ok = True
    ok &= run_check("只读门户 portal.py", [os.path.join(BASE, "code", "demo", "portal.py"), "--check"])
    ok &= run_check("开发者模式 portal_dev.py", [os.path.join(BASE, "code", "demo", "portal_dev.py"), "--check"])
    return ok


def probe(url, timeout=20) -> tuple:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, len(r.read())
    except Exception as e:                                    # noqa: BLE001
        return None, str(e)


def serve(portal=True, dev=True, portal_port=PORTAL_PORT, dev_port=DEV_PORT):
    hr("④ 启动服务")
    servers = []
    if portal:
        import portal
        portal.meta()
        s = ThreadingHTTPServer(("127.0.0.1", portal_port), portal.Handler)
        servers.append(("只读门户", s, portal_port))
        threading.Thread(target=s.serve_forever, daemon=True).start()
    if dev:
        import portal_dev
        s = ThreadingHTTPServer(("127.0.0.1", dev_port), portal_dev.DevHandler)
        servers.append(("开发者模式（可写）", s, dev_port))
        threading.Thread(target=s.serve_forever, daemon=True).start()

    for name, _s, port in servers:
        st, n = probe("http://127.0.0.1:%d/" % port)
        print("  [%s] %-22s http://127.0.0.1:%d  %s"
              % ("OK  " if st == 200 else "FAIL", name, port,
                 "%d bytes" % n if st == 200 else n))

    hr("⑤ 该看什么")
    if portal:
        print("""  只读门户 http://127.0.0.1:%d
    /            库的名片：78 表 / 1,120 列 / 精确行数 / 8 个域
    /viz         可视化仪表盘（16 个面板，口径可核，内联 SVG 零 JS）
    /viz/build   图表构建器：自己选表→维度→度量，出图 + SQL + CSV
    /talent      人才库 + 6 个在线分析       /occupations  职业库 + 6 个在线分析
    /tree        职业树 + as-of 时间旅行      /match        能力-职业匹配三态
    /extend      三条扩展机制 + 演化痕迹       /quality      成熟度仪表盘
    /schema /t/<表> /e/<表> /search /analyze /sql /lineage
    只读证明：/dev 页说明为什么写操作在另一个进程""" % portal_port)
    if dev:
        print("""
  开发者模式 http://127.0.0.1:%d  ← 可写，独立进程/端口
    /sql         多语句、单事务、默认试运行后回滚（写走 POST）
    /model       动态建模：加维度 / 建行 / 写值 / 废弃，操作后给"零 DDL"证据
    /tools       一键驱动既有 CLI（重建派生层 / 发现候选 / 重算 / 质量门 / 备份）
    /audit       change_log 审计流水         /migrate  迁移清单与结构统计""" % dev_port)
    print("\n  浏览器如果显示的是旧页面，按 Ctrl+F5 强制刷新（HTTP 缓存）。")
    print("  Ctrl+C 停止两个服务。\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[✓] 已停止")
    finally:
        for _n, s, _p in servers:
            s.shutdown()
            s.server_close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-check", action="store_true", help="跳过渲染自检")
    ap.add_argument("--check-only", action="store_true", help="只自检，不起服务")
    ap.add_argument("--portal-only", action="store_true")
    ap.add_argument("--dev-only", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="端口被占时，自动终止「确认是本项目演示服务」的旧进程")
    ap.add_argument("--portal-port", type=int, default=PORTAL_PORT)
    ap.add_argument("--dev-port", type=int, default=DEV_PORT)
    a = ap.parse_args()

    want_portal = not a.dev_only
    want_dev = not a.portal_only

    hr("医学生人才信息库 · 本地校验")
    if not ensure_db():
        return 2
    if not ensure_ports(want_portal, want_dev, a.force):
        return 3
    if not a.no_check:
        if not check_all():
            print("\n[X] 自检未通过，服务未启动。修好上面的 [FAIL] 再试。")
            return 1
        print("\n  [OK  ] 两个站点的渲染自检都通过")
    if a.check_only:
        hr("完成（--check-only，未启动服务）")
        return 0
    return serve(want_portal, want_dev, a.portal_port, a.dev_port)


if __name__ == "__main__":
    sys.exit(main())
