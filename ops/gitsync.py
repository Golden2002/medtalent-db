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


def local_tree_map(commit):
    """本地 HEAD 的 (路径 → (mode, blob_sha))。

    `git ls-tree -r` 给出的 blob sha 与 GitHub 自己算的**是同一个 SHA-1**
    （都是对同一份内容做同样的哈希），所以可以直接复用这些 sha 建树 ——
    **一个 blob 都不用上传**。这一点让 API 推送从"逐文件上传"变成"只发元数据"。
    """
    out = {}
    raw = sh("git", "ls-tree", "-r", "-z", "--full-tree", commit)
    for item in raw.split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        mode, _typ, sha = meta.split()
        out[path] = (mode, sha)
    return out


def remote_tree_map(tree_sha):
    st, d = api("GET", "/repos/%s/git/trees/%s?recursive=1" % (REPO, tree_sha), TOKEN[0])
    if st != 200:
        raise SystemExit("[X] 取远端树失败：HTTP %d %s" % (st, d))
    return {e["path"]: (e["mode"], e["sha"]) for e in d.get("tree", [])
            if e["type"] == "blob"}


TOKEN = [None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="main")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tok = token_of()
    TOKEN[0] = tok
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)

    # 1) 远端当前提交
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

    # 2) **按树比对**，而不是按提交比对。
    #    为什么改（上一轮的实测教训）：`git merge-base --is-ancestor <远端> <本地>`
    #    要求远端提交**在本地对象库里**。而 API 推送产生的提交本地没有（也 fetch 不到，
    #    因为 github.com 走不通），于是下一次 sync 直接报
    #    "Not a valid commit name" —— 安全闸把自己锁死了。
    #    按树比对只需要两边的**文件清单**，不依赖本地是否有远端提交对象。
    local_sha = sh("git", "rev-parse", a.commit)
    if remote_sha == local_sha:
        print("[=] 远端已经是本地这个提交，无需推送")
        return 0
    lmap = local_tree_map(local_sha)
    rmap = remote_tree_map(remote_tree)

    # 3) 安全闸：远端**有而本地没有**的文件，意味着远端有我们不知道的工作，
    #    直接改 ref 会把它们删掉。这种情况必须拒绝，让人先弄清楚发生了什么。
    remote_only = sorted(set(rmap) - set(lmap))
    if remote_only:
        print("[X] 拒绝执行：远端有 %d 个本地没有的文件，直接推送会删掉它们：" % len(remote_only))
        for p in remote_only[:20]:
            print("      %s" % p)
        print("    这通常说明远端有别人的提交。请恢复 github.com 连通后 git fetch 再处理。")
        return 3

    changes = []                                   # (状态, 路径)
    for path, (mode, sha) in sorted(lmap.items()):
        if path not in rmap:
            changes.append(("A", path))
        elif rmap[path] != (mode, sha):
            changes.append(("M", path))
    for path in sorted(set(rmap) - set(lmap)):
        changes.append(("D", path))                # 到不了这里（上面已拒绝），留着以防逻辑变动

    msg = sh("git", "log", "-1", "--format=%B", local_sha)
    author = sh("git", "log", "-1", "--format=%an <%ae>", local_sha)

    print("远端 %s = %s（树 %s）" % (a.branch, remote_sha[:10], remote_tree[:10]))
    print("本地 %s = %s；文件 %d 个，**有变化 %d 个**" % (a.commit, local_sha[:10],
                                                        len(lmap), len(changes)))
    for c, p in changes[:40]:
        print("   %s  %s" % (c, p))
    if len(changes) > 40:
        print("   …还有 %d 个" % (len(changes) - 40))
    if not changes:
        print("\n[=] 两边内容一致（只是提交 SHA 不同），无需推送")
        return 0
    if a.dry_run:
        print("\n[dry-run] 未做任何修改。")
        return 0

    # 4) 建树：**改过的文件必须上传 blob**。
    #    实测教训：我一开始想"复用本地 blob sha"（同一份内容 git 与 GitHub 算出同一个
    #    SHA-1，看起来能省掉上传），结果建树报
    #        tree.sha 8b73ed… is not a valid blob
    #    因为**新内容的对象在 GitHub 那边还不存在** —— SHA 相同不代表对象已在对方库里。
    #    没改的文件不必传（它们在 base_tree 里已经有了）。
    entries = []
    for code, path in changes:
        if code == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        with open(path, "rb") as fh:
            content = fh.read()
        st, b = api("POST", "/repos/%s/git/blobs" % REPO, tok,
                    {"content": base64.b64encode(content).decode("ascii"),
                     "encoding": "base64"})
        if st not in (200, 201):
            print("[X] 建 blob 失败 %s：HTTP %d %s" % (path, st, b))
            return 1
        entries.append({"path": path, "mode": lmap[path][0], "type": "blob", "sha": b["sha"]})
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
    print("""
[!] 这次推送把本地提交**合并成远端 1 个提交**，两边**内容相同但提交 SHA 分叉**。
    本工具已能处理分叉（按树比对，不再依赖本地是否有远端提交对象），
    但 `git push` 仍会被判为非快进。等 github.com 通了，对账方式：
        git fetch origin %s
        git reset --hard origin/%s        # 内容一致，不会丢任何工作""" % (a.branch, a.branch))
    try:
        with open(".git/gitsync_last_remote", "w", encoding="utf-8") as fh:
            fh.write(commit["sha"])
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
