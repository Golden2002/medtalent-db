# -*- coding: utf-8 -*-
"""
验证 Cloudflare API token：它**到底能做什么**（不是只看"存进去了"）

为什么必须验证：token 存进环境变量只说明"我拿到了一个字符串"，
不代表它能用、更不代表它能做我们需要的三件事（隧道 / Access / DNS）。
所以这里逐个调真实端点，把"能/不能"变成事实：
  1. /user/tokens/verify        —— token 本身是否有效（拿到 status: active）
  2. /accounts                  —— 能看到哪些账号（核对 CLOUDFLARE_ACCOUNT_ID）
  3. /accounts/{id}/cfd_tunnel  —— 能不能管隧道（需求②的核心能力）
  4. /accounts/{id}/access/apps —— 能不能管 Access 应用（加身份验证）
  5. /zones                     —— 能不能看到域名（配 DNS 的前提）
全程只读（GET），不创建任何东西。
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "ops")
sys.stdout.reconfigure(encoding="utf-8")
import env as E  # noqa: E402

E.load_into_environ()
tok = os.environ.get("CLOUDFLARE_API_TOKEN")
acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
print("token 长度 %d；账号 ID 末尾 %s" % (len(tok or ""), (acct or "")[-6:]))
API = "https://api.cloudflare.com/client/v4"


def cf(path):
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + (tok or ""),
        "Content-Type": "application/json",
        "User-Agent": "medtalent-cfcheck"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:                                 # noqa: BLE001
            return e.code, {"raw": raw[:300]}
    except Exception as e:                                # noqa: BLE001
        return 0, {"error": str(e)[:200]}


print("\n=== 1) token 本身是否有效 ===")
st, d = cf("/user/tokens/verify")
print("  HTTP %s  success=%s  status=%s" % (st, d.get("success"), (d.get("result") or {}).get("status")))
if not d.get("success"):
    print("  错误：%s" % json.dumps(d.get("errors") or d, ensure_ascii=False)[:300])

print("\n=== 2) 账号可见性 ===")
st, d = cf("/accounts")
accs = d.get("result") or []
print("  HTTP %s，可见账号 %d 个" % (st, len(accs)))
for a in accs[:6]:
    mark = " ← 与 CLOUDFLARE_ACCOUNT_ID 一致" if a.get("id") == acct else ""
    print("    %s  %s%s" % (a.get("id"), a.get("name"), mark))

print("\n=== 3) 隧道（需求②的核心能力）===")
st, d = cf("/accounts/%s/cfd_tunnel?is_deleted=false" % acct)
print("  HTTP %s  success=%s" % (st, d.get("success")))
if d.get("success"):
    tun = d.get("result") or []
    print("    现有隧道 %d 个" % len(tun))
    for t in tun[:8]:
        print("      %-36s %s  status=%s" % (t.get("name"), t.get("id"), t.get("status")))
else:
    print("    错误：%s" % json.dumps(d.get("errors") or d, ensure_ascii=False)[:300])

print("\n=== 4) Access 应用（加身份验证）===")
st, d = cf("/accounts/%s/access/apps" % acct)
print("  HTTP %s  success=%s" % (st, d.get("success")))
if d.get("success"):
    apps = d.get("result") or []
    print("    现有 Access 应用 %d 个" % len(apps))
    for x in apps[:6]:
        print("      %s  domain=%s" % (x.get("name"), x.get("domain")))
else:
    print("    错误：%s" % json.dumps(d.get("errors") or d, ensure_ascii=False)[:300])

print("\n=== 5) 域名（配 DNS 的前提）===")
st, d = cf("/zones")
zs = d.get("result") or []
print("  HTTP %s  success=%s，可见域名 %d 个" % (st, d.get("success"), len(zs)))
for z in zs[:8]:
    print("    %-28s status=%-8s zone_id=%s" % (z.get("name"), z.get("status"), z.get("id")))
if not zs:
    print("    （没有域名 —— 要走 Named Tunnel 需要先在 Cloudflare 托管一个域名；")
    print("      否则只能用 Quick Tunnel，而它**不能挂 Access 策略**，不适合挂私有数据）")
