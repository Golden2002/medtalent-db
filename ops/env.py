# -*- coding: utf-8 -*-
"""
ops/env.py —— 凭据存进 Windows 用户级环境变量（**永不回显密文**）

为什么要有这个工具，而不是直接 `setx`
--------------------------------------------------------------------------
1. **不回显**。`setx GITHUB_TOKEN ghp_xxx` 会把密文写进命令历史、也会出现在进程列表里。
   这个工具从文件或标准输入读，全程只打印"名称 + 长度 + 写到哪一层"。
2. **不落进仓库**。密文只在环境变量里，不写进任何被 git 跟踪的文件 ——
   凭据进仓库是最常见的泄露方式，且一旦推送就无法撤回。
3. **可核对**。`status` 能回答"哪些凭据配好了、来自哪一层"，而不必把值打出来。

用法
    python ops/env.py status                       # 只看有没有配、多长（不打值）
    python ops/env.py set NAME --from-file PATH    # 从文件读入（推荐）
    python ops/env.py set NAME --stdin             # 从标准输入读
    python ops/env.py unset NAME
    python ops/env.py load                          # 把注册表里的值注入当前进程环境（供其它脚本用）

安全边界（必须说清楚，否则"存进环境变量"会被当成万无一失）
    · **用户级环境变量对该用户的所有进程可读**。同账号下任何程序都能取到它。
      这比把密文写进文件好（不会被 git 跟踪、不会被打包），但不等于加密存储。
    · 需要更强隔离时用 Windows 凭据管理器（DPAPI）或只在 CI 里注入。
    · 本工具**不打印值**；但也**不能阻止别人**用 `echo $env:NAME` 读出来。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys

try:
    import winreg
except ImportError:                                   # 非 Windows
    winreg = None

REG_PATH = "Environment"
# 本项目需要的凭据（名称 → 用途说明）。只登记用途，不登记值。
KNOWN = {
    "GITHUB_TOKEN": "推送代码到 GitHub（ops/release.py 也从这里读）",
    "CLOUDFLARE_API_TOKEN": "调用 Cloudflare API 建隧道/配 DNS（**最小权限范围**）",
    "CLOUDFLARE_ACCOUNT_ID": "Cloudflare 账号 ID（不是密文，但配对使用）",
    "CLOUDFLARE_TUNNEL_TOKEN": "已建隧道的运行令牌（cloudflared tunnel run --token-file）",
    "MEDTALENT_DSN": "覆盖默认数据库连接串（可选）",
}


def _broadcast():
    """通知 Windows 环境变量变了，让**新**启动的进程能拿到（不必注销）。"""
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, None)
    except Exception:                                 # noqa: BLE001
        pass


def _read_reg(name, root=winreg.HKEY_CURRENT_USER):
    try:
        with winreg.OpenKey(root, REG_PATH, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v
    except OSError:
        return None


def _write_reg(name, value, root=winreg.HKEY_CURRENT_USER, sub=REG_PATH):
    with winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _delete_reg(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
            return True
    except OSError:
        return False


def effective(name):
    """当前有效值：进程环境优先，其次用户级注册表。"""
    v = os.environ.get(name)
    if v:
        return v, "进程环境"
    if winreg:
        v = _read_reg(name)
        if v:
            return v, "用户级环境变量"
    return None, ""


def cmd_status(a):
    print("%-26s %-14s %-10s %s" % ("名称", "状态", "长度", "来源 / 用途"))
    print("-" * 100)
    for n, why in KNOWN.items():
        v, src = effective(n)
        print("%-26s %-14s %-10s %s"
              % (n, "已配置" if v else "未配置",
                 ("%d 字符" % len(v)) if v else "—",
                 (src + " · " if v else "") + why))
    print("\n提示：只显示名称与长度，**从不打印值**。")
    return 0


def cmd_set(a):
    if a.from_file:
        if not os.path.isfile(a.from_file):
            print("[X] 找不到文件：%s" % a.from_file)
            return 1
        with open(a.from_file, encoding="utf-8") as fh:
            raw = fh.read()
    elif a.stdin:
        raw = sys.stdin.read()
    else:
        print("[X] 必须给 --from-file 或 --stdin（本工具不接受命令行明文参数，"
              "那样会进命令历史与进程列表）")
        return 1
    val = raw.strip()
    # 常见的 KEY=VALUE 包装：从 github_access_token.txt 那种文件里直接读也能用
    if "=" in val and "\n" not in val:
        val = val.split("=", 1)[1].strip()
    if not val:
        print("[X] 读到的值是空的，未做任何修改")
        return 1
    if not winreg:
        print("[X] 本工具只在 Windows 上写用户级环境变量")
        return 1
    _write_reg(a.name, val)
    _broadcast()
    print("[✓] 已写入用户级环境变量 %s（%d 字符，未回显）" % (a.name, len(val)))
    print("    新启动的进程即可读到；已运行的进程需要重启才能拿到。")
    return 0


def cmd_unset(a):
    ok = _delete_reg(a.name)
    _broadcast()
    print("[✓] 已删除用户级环境变量 %s" % a.name if ok else "[=] 本来就没有 %s" % a.name)
    return 0


def cmd_load(a):
    """把注册表里的凭据注入**当前进程**环境。

    为什么需要它：本会话里每个 pwsh 都是新进程，而用户级环境变量的变更
    不一定会传播到由其它父进程启动的子进程。其它脚本可以调用
    `from ops.env import load_into_environ; load_into_environ()` 拿到凭据，
    而不必依赖 shell 是否刷新过环境。
    """
    n = 0
    for name in KNOWN:
        if os.environ.get(name):
            continue
        v = _read_reg(name) if winreg else None
        if v:
            os.environ[name] = v
            n += 1
    if a.quiet:
        return 0
    print("[✓] 已从注册表注入 %d 个凭据到当前进程环境（值不回显）" % n)
    return 0


def load_into_environ():
    """给其它脚本调用：确保凭据在 os.environ 里可用。"""
    for name in KNOWN:
        if not os.environ.get(name) and winreg:
            v = _read_reg(name)
            if v:
                os.environ[name] = v


def main():
    ap = argparse.ArgumentParser(description="凭据 → 用户级环境变量（不回显密文）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("set")
    p.add_argument("name")
    p.add_argument("--from-file", metavar="PATH")
    p.add_argument("--stdin", action="store_true")
    p.set_defaults(fn=cmd_set)
    q = sub.add_parser("unset")
    q.add_argument("name")
    q.set_defaults(fn=cmd_unset)
    r = sub.add_parser("load")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_load)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
