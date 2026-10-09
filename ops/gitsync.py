# -*- coding: utf-8 -*-
"""
ops/gitsync.py —— 当 `git push` 走不通时，用 GitHub REST API 完成等价推送

为什么需要它（本机实测，不是设想）
--------------------------------------------------------------------------
实测连通性：
    api.github.com:443   → **通**（TLS 正常）
    github.com:443       → **不通**（`Recv failure: Connection was reset`）
而 `git push` 用的是 `github.com`。所以在这个网络环境下，
`git push` 会一直失败，但 GitHub 的 REST API 完全可用。
（前几轮 push 成功过，说明是间歇性干扰；但"间歇"意味着不能依赖它。）

本工具用 Git Data API 复现一次推送的全部语义：
    GET  /git/ref/heads/main          → 远端当前提交（等于 `git fetch`）
    POST /git/blobs                   → 为每个**有变化的文件**建 blob
    POST /git/trees  (base_tree=...)  → 基于远端树构造新树（等于提交的内容）
    POST /git/commits (parents=[远端]) → 新提交（保留本地的提交信息）
    PATCH /git/refs/heads/main        → 移动分支（等于 push 成功）

安全前提（必须先满足，否则拒绝执行）
--------------------------------------------------------------------------
远端 main **必须**是本地 HEAD 的祖先。否则说明远端有本地没有的提交，
直接改 ref 会**丢掉别人的提交** —— 那种事绝不能靠"应该没问题"来做。
不满足时本工具拒绝执行并提示先 `git pull`（或改用 git push）。

用法
    python ops/gitsync.py --dry-run          # 只看会推送哪些文件，不改任何东西
    python ops/gitsync.py                    # 推送 HEAD 到 main
    python ops/gitsync.py --branch main --remote origin

凭据：从 GITHUB_TOKEN 环境变量读（见 ops/env.py），**绝不写进命令行**。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "Golden2002/medtalent-db"
API = "https://api.github.com"


def sh(*args, **kw):
    r = subprocess.run(list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if r.returncode != 0:
        raise SystemExit("[X] 命令失败：%s\n%s" % (" ".join(args), (r.stderr or "")[:400]))
    return (r.stdout or "").strip()


def api(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "medtalent-gitsync",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except Exception:                                 # noqa: BLE001
            msg = {"raw": raw[:300]}
        return e.code, msg


def token_of():
    t = os.environ.get("GITHUB_TOKEN")
    if not t:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from env import load_into_environ
            load_into_environ()
            t = os.environ.get("GITHUB_TOKEN")
        except Exception:                                 # noqa: BLE001
            pass
    if not t:
        print("[X] 未设置 GITHUB_TOKEN。配置： python ops\\env.py set GITHUB_TOKEN --from-file <文件>")
        sys.exit(2)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="main")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tok = token_of()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)

    # 1) 远端当前提交（api.github.com 通，所以这一步在我们这里反而比 git fetch 可靠）
    st, ref = api("GET", "/repos/%s/git/ref/heads/%s" % (REPO, a.branch), tok)
    if st != 200:
        print("[X] 取远端分支失败（HTTP %d）：%s" % (st, ref))
        return 1
    remote_sha = ref["object"]["sha"]
    st, rc = api("GET", "/repos/%s/git/commits/%s" % (REPO, remote_sha), tok)
    if st != 200:
        print("[X] 取远端提交失败（HTTP %d）：%s" % (st, rc))
        return 1
    remote_tree = rc["tree"]["sha"]

    # 2) 确认远端是本地 HEAD 的祖先（安全闸）
    local_sha = sh("git", "rev-parse", a.commit)
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", remote_sha, local_sha]).returncode
    if anc != 0:
        print("[X] 拒绝执行：远端 %s（%s）**不是**本地 %s 的祖先。"
              % (a.branch, remote_sha[:10], local_sha[:10]))
        print("    直接改 ref 会丢掉远端已有的提交。请先 `git pull`，或用 git push。")
        return 3
    if remote_sha == local_sha:
        print("[=] 远端已经是本地这个提交，无需推送")
        return 0

    # 3) 本地相对远端改了什么
    raw = sh("git", "diff", "--name-status", "-z", remote_sha, local_sha)
    parts = [p for p in raw.split("\0") if p]
    changes = []
    i = 0
    while i < len(parts):
        code = parts[i]
        if code.startswith("R") or code.startswith("C"):
            changes.append((code[0], parts[i + 1], parts[i + 2]))
            i += 3
        else:
            changes.append((code[0], parts[i + 1], None))
            i += 2

    msg = sh("git", "log", "-1", "--format=%B", local_sha)
    author = sh("git", "log", "-1", "--format=%an <%ae>", local_sha)

    print("远端 %s = %s" % (a.branch, remote_sha[:10]))
    print("本地 %s = %s（%d 个文件有变化）" % (a.commit, local_sha[:10], len(changes)))
    for c, p, _ in changes[:40]:
        print("   %s  %s" % (c, p))
    if len(changes) > 40:
        print("   …还有 %d 个" % (len(changes) - 40))
    if a.dry_run:
        print("\n[dry-run] 未做任何修改。")
        return 0

    # 4) 建 blob + 构造树（base_tree 指向远端树，所以没变的文件自动保持）
    entries = []
    for code, path, newpath in changes:
        if code == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        full = newpath or path
        with open(full, "rb") as fh:
            content = fh.read()
        st, b = api("POST", "/repos/%s/git/blobs" % REPO, tok,
                    {"content": base64.b64encode(content).decode("ascii"),
                     "encoding": "base64"})
        if st not in (200, 201):
            print("[X] 建 blob 失败 %s：HTTP %d %s" % (full, st, b))
            return 1
        mode = "100755" if os.access(full, os.X_OK) else "100644"
        entries.append({"path": full, "mode": mode, "type": "blob", "sha": b["sha"]})
        if code == "R" and newpath:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    st, tree = api("POST", "/repos/%s/git/trees" % REPO, tok,
                   {"base_tree": remote_tree, "tree": entries})
    if st not in (200, 201):
        print("[X] 建树失败：HTTP %d %s" % (st, tree))
        return 1

    st, commit = api("POST", "/repos/%s/git/commits" % REPO, tok,
                     {"message": msg, "tree": tree["sha"], "parents": [remote_sha],
                      "author": {"name": author.split(" <")[0],
                                 "email": author.split(" <")[1].rstrip(">")}})
    if st not in (200, 201):
        print("[X] 建提交失败：HTTP %d %s" % (st, commit))
        return 1

    st, upd = api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, a.branch), tok,
                  {"sha": commit["sha"], "force": False})
    if st not in (200, 201):
        print("[X] 移动分支失败：HTTP %d %s" % (st, upd))
        return 1
    print("\n[✓] 已通过 REST API 推送：%s → %s" % (a.branch, commit["sha"][:10]))
    print("    提交页：https://github.com/%s/commit/%s" % (REPO, commit["sha"]))
    print("    提示：本地 git 与远端已同步内容，但本地的 reflog 不知道这次推送；"
          "下次能用 git push 时它仍然会正常工作。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
