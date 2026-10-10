# -*- coding: utf-8 -*-
"""跑 portal_test 并把失败项与登录相关行全部打出来。"""
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
r = subprocess.run([sys.executable, "ops/tests/portal_test.py"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
out = (r.stdout or "") + (r.stderr or "")
for ln in out.splitlines():
    if "FAIL" in ln or "登录" in ln or "会话" in ln or "结果：" in ln:
        print(ln.strip()[:170])
