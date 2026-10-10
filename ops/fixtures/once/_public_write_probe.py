# -*- coding: utf-8 -*-
"""公网侧验证：门户的 SQL 控制台在 GET 方式下也**写不进去**（只读事务在数据库层拒绝）。

为什么用 GET 测：门户的 /sql 是 GET + ?q= 的形式，所以 POST 返回 405 只证明
"HTTP 方法不对"，**没有证明"只读"**。真正的证据是 GET 一条 INSERT 被数据库拒绝。
"""
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8082"

for label, q in (
        ("INSERT", "INSERT INTO mt.person (person_id) VALUES ('per_probe_via_public')"),
        ("DELETE", "DELETE FROM mt.person WHERE person_id = 'per_nobody'"),
        ("UPDATE", "UPDATE mt.person SET status = 'active'"),
        ("DDL", "CREATE TABLE mt.probe_table (x int)")):
    url = BASE + "/sql?" + urllib.parse.urlencode({"q": q})
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            b, st = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        b, st = e.read().decode("utf-8", "replace"), e.code
    denied = ("只读" in b or "READ ONLY" in b or "read-only" in b.lower()
              or "权限不足" in b or "必须以 SELECT" in b or "不允许" in b)
    print("%-7s → HTTP %s  %s" % (label, st, "被拒（只读/权限在工作）" if denied else "**未明确拒绝，需人工看**"))
    if not denied:
        # 打印页面里最像错误的一句话，便于判断
        import re
        for m in re.finditer(r'note (?:err|warn)">(.*?)</div>', b, re.S):
            txt = re.sub(r"<[^>]+>", "", m.group(1))
            print("        提示：%s" % re.sub(r"\s+", " ", txt).strip()[:160])
            break
