# -*- coding: utf-8 -*-
"""
ops/tests/frontend_test.py —— 导航组织与移动端（评审优先级 5）

评审把基础视觉的整改范围收窄为：**导航组织、默认列、移动端、真实任务验证**。
本套件覆盖其中可机器检的三条（"真实任务验证"需要真人，见末尾说明）。

① **导航组织**：原来 21 项平铺一行，用户找不到"我想做的事在哪"。
   现在按**任务**分五组（看数据/看人才/做分析/看质量/管数据）。
   这里守住两件事：
     · 分组名单与扁平名单**完全一致**（不一致就会有页面在导航上找不到入口）；
     · 页面**真的渲染出分组标签**（不是只在代码里定义了）。
② **每个导航入口都可达**：逐个请求 `/` 开头的导航项，**不得出现 404/500**。
   这是"真实任务验证"里最容易自动化的一半：链接 404 是最典型的「用户走不到」。
   ⚠ 这一条以前没有，所以新增页面时"加了导航但路由没接"不会被发现。
③ **移动端**：`viewport` meta + 窄屏媒体查询 + 表格横向滚动。
   ⚠ 只有 viewport 而没有配套 CSS，手机上依然不可用（表格会撑破布局）——
   所以三条一起断言，避免"看起来做了移动端"。
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check

import portal                                          # noqa: E402

PORT = 8099
CRED_DIR = os.path.join(BASE, ".tools", "credentials")


def _cred(name):
    """从凭据文件读一个值 —— **绝不在源码里硬编码凭据**。

    ⚠ 这里原本把管理员邮箱写死在源码里，被 `ops/secrets_scan.py` 抓了出来
    （它是已登记的凭据值）。教训：会话里用内联命令传凭据不会被扫到，
    一旦写进**受版本管理的文件**就是泄露 —— 所以测试也必须从凭据目录读。
    """
    p = os.path.join(CRED_DIR, name)
    if not os.path.isfile(p):
        return None
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        return line.split("=", 1)[1].strip() if "=" in line else line
    return None



def fetch(path, cookie=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path))
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    import subprocess
    import time

    proc = subprocess.Popen([sys.executable, os.path.join(BASE, "code", "demo", "portal.py"),
                             "--serve", "--port", str(PORT)],
                            cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    try:
        # ---------- ① 分组名单一致性 ----------
        flat = {t for _u, t in portal.NAV}
        grouped = {t for _g, xs in portal.NAV_GROUPS for _u, t in xs}
        check(flat == grouped,
              "导航分组与扁平名单**完全一致**（差异：%s）—— 不一致就会有页面找不到入口"
              % ("、".join(sorted(flat ^ grouped)) or "无"))
        check(len(portal.NAV_GROUPS) >= 3,
              "导航按任务分成多组（%d 组：%s）"
              % (len(portal.NAV_GROUPS), "、".join(g for g, _ in portal.NAV_GROUPS)))

        # ---------- 登录拿 cookie（**凭据从文件读，不硬编码**）----------
        cookie = None
        email = _cred("medtalent_admin_email.txt")
        pw = _cred("medtalent_admin_password.txt")
        if email and pw:
            body = urllib.parse.urlencode({"email": email, "password": pw}).encode()

            class NR(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a):
                    return None
            try:
                with urllib.request.build_opener(NR()).open(
                        urllib.request.Request("http://127.0.0.1:%d/login" % PORT,
                                               data=body, method="POST"), timeout=30) as r:
                    cookie = (r.headers.get("Set-Cookie") or "").split(";")[0]
            except urllib.error.HTTPError as e:
                cookie = (e.headers.get("Set-Cookie") or "").split(";")[0]

        st, home = fetch("/", cookie)
        check(st == 200, "首页可访问（HTTP %d）" % st)

        # 分组标签必须真的渲染出来（不是只在代码里定义）
        missing = [g for g, _ in portal.NAV_GROUPS if g not in home]
        check(not missing,
              "页面上**真的渲染出分组标签**（缺：%s）—— 只在代码里定义不算做了导航组织"
              % ("、".join(missing) or "无"))

        # ---------- ② 每个导航入口都可达 ----------
        bad = []
        for path, label in portal.NAV:
            if not path.startswith("/"):
                continue
            s, _b = fetch(path, cookie)
            if s >= 400:
                bad.append("%s(%s)→%d" % (label, path, s))
        check(not bad,
              "**每个导航入口都可达**（404/500：%s）—— 链接 404 是最典型的「用户走不到」"
              % ("、".join(bad[:5]) or "无"))

        # ---------- ③ 移动端：三条一起才有用 ----------
        check("viewport" in home and "width=device-width" in home,
              "有 viewport meta（没有它手机按桌面宽度渲染再缩小，字小到看不清）")
        check("@media" in home and "max-width" in home,
              "有窄屏媒体查询（只有 viewport 而没有配套 CSS，手机上依然不可用）")
        check("overflow-x:auto" in home and ".tscroll" in home,
              "窄屏下表格**横向滚动**而不是压扁 —— 压扁会丢列，等于看不到数据")

        # ---------- 页面级"看不到 ≠ 没有"的安全网 ----------
        st2, schema = fetch("/schema", cookie)
        check(st2 == 200, "表与视图页可访问（HTTP %d）" % st2)
        # 数据里有、显示名单里没有的域必须**显式列出并警示**，不能静默丢掉
        check("不在门户的显示名单" in schema or "未归类" not in schema,
              "「数据里有、名单里没有」的域会被**显式列出并警示**，不会静默消失")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
