# -*- coding: utf-8 -*-
"""验证凭据通道真的通了（不回显任何密文）。

要证明三件事：
  1. 用户级环境变量确实写进去了（新开的终端能看到）；
  2. 由别的父进程拉起的子进程看不到（这是 Windows 环境变量继承的真实行为，不是 bug）；
  3. `ops/env.py` 的注册表兜底能拿到，并**真的能用**（拿 GITHUB_TOKEN 打一次 GitHub API）。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "ops")
sys.stdout.reconfigure(encoding="utf-8")

import env as E  # noqa: E402

print("=== 1) 用户级环境变量（注册表）===")
for n in ("GITHUB_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
    v, src = E.effective(n)
    print("  %-24s %s" % (n, ("已配置 %d 字符（来源：%s）" % (len(v), src)) if v else "未配置"))

print("\n=== 2) 子进程默认看不到（继承的是父进程的旧环境块）===")
r = subprocess.run([sys.executable, "-c",
                    "import os;print(bool(os.environ.get('GITHUB_TOKEN')))"],
                   capture_output=True, text=True)
print("  子进程里 os.environ 有 GITHUB_TOKEN：%s" % r.stdout.strip())

print("\n=== 3) 注册表兜底 + 真的调用一次 API ===")
E.load_into_environ()
tok = os.environ.get("GITHUB_TOKEN")
print("  load_into_environ() 之后：%s" % ("已注入" if tok else "仍然没有"))
if tok:
    req = urllib.request.Request("https://api.github.com/user",
                                 headers={"Authorization": "Bearer " + tok,
                                          "User-Agent": "medtalent-env-check",
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            print("  GitHub API 返回 200，登录账号 = %s（token 有效）" % d.get("login"))
    except urllib.error.HTTPError as e:
        print("  GitHub API %d —— token 无效或权限不足" % e.code)
    except Exception as e:                                # noqa: BLE001
        print("  网络不可达：%s（凭据本身已就位，等网络恢复即可用）" % str(e)[:80])
