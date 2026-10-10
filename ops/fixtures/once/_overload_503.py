# -*- coding: utf-8 -*-
"""验证过载通道：故意用慢查询占满并发闸，确认超出的请求拿到 **503 + Retry-After**。

为什么必须单独验证 503：并发闸的价值不是"正常情况下一切照旧"，
而是**过载时给出明确、可退避的信号**。如果这条路径从没被触发过，
那它就和不存在一样 —— 而它恰恰是压力最大时唯一在工作的东西。
"""
import concurrent.futures as cf
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
SLOW = "http://127.0.0.1:8082/sql?" + urllib.parse.urlencode({"q": "SELECT pg_sleep(3)"})
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40


def hit(_i):
    t = time.time()
    try:
        with urllib.request.urlopen(SLOW, timeout=300) as r:
            r.read()
            return r.status, r.headers.get("Retry-After"), time.time() - t
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After")
        e.read()
        return e.code, ra, time.time() - t
    except Exception as e:                                 # noqa: BLE001
        return "%s: %s" % (type(e).__name__, str(e)[:50]), None, time.time() - t


t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    res = list(ex.map(hit, range(N)))
print("并发 %d 个**慢查询**（每个占用 3 秒），总耗时 %.1f 秒" % (N, time.time() - t0))
print("状态分布：%s" % dict(Counter(str(s) for s, _, _ in res)))
ra = {r for c, r, _ in res if c == 503}
print("503 响应带的 Retry-After：%s" % (ra or "（没有 503）"))
print()
print("① 出现过 503（过载通道被真实触发）：%s" % (503 in [c for c, _, _ in res]))
print("② 503 都带 Retry-After（可退避，不是让调用方瞎猜）：%s"
      % (bool(ra) and all(x for x in ra)))
print("③ 没有 500（过载不是「服务器坏了」）：%s" % (500 not in [c for c, _, _ in res]))
print("④ 没有客户端异常（每个请求都拿到明确的 HTTP 结果）：%s"
      % all(isinstance(c, int) for c, _, _ in res))
