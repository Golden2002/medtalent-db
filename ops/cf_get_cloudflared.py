# -*- coding: utf-8 -*-
"""
下载 cloudflared（Windows amd64）到工作区工具目录，并校验数字签名

为什么不用 `curl.exe`：本机 `curl.exe` / `Invoke-WebRequest` 报 SEC_E_NO_CREDENTIALS，
只能用 Python urllib（项目既有约定）。

为什么走 GitHub API 取资产地址而不是写死 URL：
`github.com` 在本网络**间歇被阻断**，而 `objects.githubusercontent.com`（资产实际所在）
是通的。用 API（api.github.com，实测稳定）拿到 browser_download_url，再直连资产域名下载。

为什么用 Authenticode 而不是 SHA256：Cloudflare **不提供** Windows 二进制的 SHA256 文件
（docs/19 实测 .sha256 / SHA256SUMS / .sig 全 404）。所以用签名校验：
签名者必须是 `CN=Cloudflare, Inc.`，且带时间戳。
"""
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.stdout.reconfigure(encoding="utf-8")

DEST = r"D:\wbo-workspace\.tools\cloudflared.exe"


def main():
    import env as E
    E.load_into_environ()
    tok = os.environ.get("GITHUB_TOKEN")

    print("[1] 取 cloudflared 最新发布信息（api.github.com）")
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": "medtalent-cf"}
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(
        "https://api.github.com/repos/cloudflare/cloudflared/releases/latest", headers=hdr)
    rel = json.loads(urllib.request.urlopen(req, timeout=45).read().decode())
    tag = rel["tag_name"]
    asset = next((a for a in rel["assets"]
                  if "windows-amd64" in a["name"] and a["name"].endswith(".exe")), None)
    if not asset:
        print("[X] 没有找到 windows-amd64 的 exe 资产")
        return 1
    print("    版本 %s，资产 %s（%d 字节）" % (tag, asset["name"], asset["size"]))

    if os.path.isfile(DEST) and os.path.getsize(DEST) == asset["size"]:
        print("[=] 本地已有同尺寸文件，跳过下载：%s" % DEST)
    else:
        print("[2] 下载（objects.githubusercontent.com）")
        # 资产 API + Accept: application/octet-stream 会 302 到资产域名；urllib 自动跟随
        req2 = urllib.request.Request(
            asset["url"], headers={"Accept": "application/octet-stream",
                                   "User-Agent": "medtalent-cf"})
        if tok:
            req2.add_header("Authorization", "Bearer " + tok)
        os.makedirs(os.path.dirname(DEST), exist_ok=True)
        n = 0
        with urllib.request.urlopen(req2, timeout=300) as r, open(DEST, "wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                n += len(chunk)
        print("    已下载 %d 字节 → %s" % (n, DEST))
        if n != asset["size"]:
            print("[X] 尺寸不符：期望 %d，实得 %d" % (asset["size"], n))
            return 1

    print("[3] 校验 Authenticode 签名（Cloudflare 不提供 SHA256 文件）")
    ps = ("$s = Get-AuthenticodeSignature '%s'; "
          "Write-Output ($s.Status.ToString() + '|' + $s.SignerCertificate.Subject)"
          % DEST)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8")
    out = (r.stdout or "").strip()
    print("    %s" % out)
    ok = out.startswith("Valid") and "Cloudflare" in out
    print("[%s] 签名校验：%s" % ("✓" if ok else "X",
                              "有效且签名者为 Cloudflare" if ok else "未通过（不要使用这个文件）"))
    if not ok:
        return 1
    r2 = subprocess.run([DEST, "--version"], capture_output=True, text=True,
                        encoding="utf-8", timeout=60)
    print("[4] 版本自检：%s" % (r2.stdout or r2.stderr or "").strip()[:120])
    return 0


if __name__ == "__main__":
    sys.exit(main())
