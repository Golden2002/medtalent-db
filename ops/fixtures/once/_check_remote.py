# -*- coding: utf-8 -*-
"""验证 API 推送真的生效：从 api.github.com 读回远端内容核对（不依赖 git）。"""
import base64
import json
import os
import sys
import urllib.request

sys.path.insert(0, "ops")
sys.stdout.reconfigure(encoding="utf-8")
import env as E  # noqa: E402

E.load_into_environ()
tok = os.environ["GITHUB_TOKEN"]
REPO = "Golden2002/medtalent-db"


def api(path):
    req = urllib.request.Request("https://api.github.com" + path,
                                 headers={"Authorization": "Bearer " + tok,
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "medtalent-check"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


ref = api("/repos/%s/git/ref/heads/main" % REPO)
sha = ref["object"]["sha"]
c = api("/repos/%s/git/commits/%s" % (REPO, sha))
print("远端 main = %s" % sha[:10])
print("提交信息首行 = %s" % (c["message"].splitlines()[0]))
print("提交时间 = %s" % c["committer"]["date"])

# 核对本轮新增的关键文件在远端确实存在
tree = api("/repos/%s/git/trees/%s?recursive=1" % (REPO, c["tree"]["sha"]))
paths = {e["path"] for e in tree["tree"]}
for p in ("ops/env.py", "ops/gitsync.py", "code/demo/portal_mockreg.py", ".gitignore",
          "ops/cf_tunnel.py", "ops/cf_preflight.py", "ops/cf_check.py",
          "ops/cf_get_cloudflared.py", "ops/tests/mockreg_test.py",
          "schema/sql/030_identifier_disclosure.sql"):
    print("  远端含 %-42s %s" % (p, p in paths))

# 核对 .gitignore 里确实有凭据规则（内容级验证，不只是"文件在"）
blob = api("/repos/%s/contents/.gitignore?ref=%s" % (REPO, sha))
content = base64.b64decode(blob["content"]).decode("utf-8")
print("\n.gitignore 凭据规则已推送：%s" % ("*.token" in content and ".env" in content))
print("远端文件总数：%d" % len(paths))

# 顺带确认：远端**没有**任何凭据文件
bad = [p for p in paths if p.endswith((".token", ".pem", ".key")) or p == ".env"]
print("远端是否含凭据类文件：%s" % (bad or "否（干净）"))
