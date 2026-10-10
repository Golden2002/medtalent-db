# -*- coding: utf-8 -*-
"""列出 I4.1 仍未接线的服务入口（复刻指标里的判据，便于准确汇报）。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXEMPT = {"code/demo/portal.py", "ops/backup/backup.py"}
SERVING = ["code/demo/portal.py", "code/demo/portal_dev.py", "code/demo/console.py",
           "code/demo/app.py", "code/demo/static_site.py", "code/db.py",
           "code/bridge/exchange.py", "code/bridge/outbox.py",
           "code/bridge/projection.py", "ops/backup/backup.py"]
print("%-32s %-10s %s" % ("文件", "状态", "说明"))
print("-" * 78)
for rel in SERVING:
    p = os.path.join(BASE, rel)
    if not os.path.isfile(p):
        print("%-32s %-10s %s" % (rel, "不存在", "指标会跳过"))
        continue
    with open(p, encoding="utf-8") as fh:
        txt = fh.read()
    sup = "user=postgres" in txt
    role = "mt_bridge" if "user=mt_bridge" in txt else ("mt_portal" if "PORTAL_DSN" in txt else "?")
    if not sup:
        st = "已接线"
    elif rel in EXEMPT:
        st = "已登记例外"
    else:
        st = "**未接线**"
    print("%-32s %-10s 连接角色=%s" % (rel, st, role))
