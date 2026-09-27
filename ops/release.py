# -*- coding: utf-8 -*-
"""
ops/release.py —— 通过 GitHub API 打 tag 并发布 Release（本机专用，凭据只从环境读）

为什么单独写一个脚本而不是手敲 curl：
  ① 本机 curl.exe / Invoke-WebRequest 会因凭据问题失败（SEC_E_NO_CREDENTIALS），
     只能走 Python urllib + 显式代理；
  ② token 必须**只从环境变量读**，绝不写进命令行参数或文件 —— 命令行参数会进进程列表
     与 shell history；
  ③ 发布说明很长且含中文，用 JSON body 提交比拼 shell 引号可靠。

用法（PowerShell）：
    $env:GITHUB_TOKEN = (Get-Content D:\\wechat_summary\\github_access_token.txt -Raw).Split('=')[1].Trim()
    $env:HTTPS_PROXY  = "http://127.0.0.1:7892"
    python ops\\release.py --tag v1.0.0 --name "..." --notes-file .tools\\release.md
    Remove-Item Env:GITHUB_TOKEN
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "Golden2002/medtalent-db"


def call(method, path, token, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "medtalent-release")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:500]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--notes-file", required=True)
    ap.add_argument("--target", default="main")
    ap.add_argument("--draft", action="store_true")
    a = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[!] 未设置 GITHUB_TOKEN（只从环境变量读，绝不写进命令行）")
        return 2
    with open(a.notes_file, encoding="utf-8") as fh:
        notes = fh.read()

    # 先看有没有同名 release；有就不重复创建（幂等）
    st, existing = call("GET", "/repos/%s/releases/tags/%s" % (REPO, a.tag), token)
    if st == 200:
        print("[=] release %s 已存在：%s" % (a.tag, existing.get("html_url")))
        return 0
    if st not in (404,):
        print("[!] 查询 release 失败 HTTP %s：%s" % (st, existing))
        return 1

    st, res = call("POST", "/repos/%s/releases" % REPO, token, {
        "tag_name": a.tag,
        "target_commitish": a.target,
        "name": a.name or a.tag,
        "body": notes,
        "draft": bool(a.draft),
        "prerelease": False,
    })
    if st not in (200, 201):
        print("[!] 创建 release 失败 HTTP %s：%s" % (st, res))
        return 1
    print("[✓] release %s：%s" % (a.tag, res.get("html_url")))
    print("    tag → %s" % res.get("target_commitish"))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
