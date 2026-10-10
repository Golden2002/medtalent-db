# -*- coding: utf-8 -*-
"""过载验证：并发打满时，① 数据库连接数有上界 ② 失败是 503 而不是 500。

为什么必须实测这两点：
· "加了信号量"只是代码事实；真正要证明的是**数据库侧的连接峰值受控**
  （那才是会打满 max_connections 的量）；
· 而且失败必须是明确的 503 + Retry-After —— 如果变成 500，Cloudflare 与浏览器
  都会当成"服务器坏了"并立刻重试，反而加剧过载。
"""
import concurrent.futures as cf
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

URL = "http://127.0.0.1:8082/catalog"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
PSQL = r"D:\wbo-workspace\.tools\pgsql\bin\psql.exe"
DSN = ["-h", "127.0.0.1", "-p", "55432", "-U", "postgres", "-d", "medtalent",
       "-t", "-A", "-c", "SELECT count(*) FROM pg_stat_activity"]

peak = [0]
stop = threading.Event()


def sample():
    """并发采样数据库连接数（用独立 psql 进程，避免和被压的服务共享连接）。"""
    while not stop.is_set():
        try:
            out = subprocess.run([PSQL] + DSN, capture_output=True, text=True,
                                 timeout=20).stdout.strip()
            n = int(out) if out.isdigit() else 0
            peak[0] = max(peak[0], n)
        except Exception:                                  # noqa: BLE001
            pass
        time.sleep(0.15)


def hit(_i):
    t = time.time()
    try:
        with urllib.request.urlopen(URL, timeout=180) as r:
            r.read()
            return r.status, time.time() - t
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, time.time() - t
    except Exception as e:                                 # noqa: BLE001
        # 失败必须**带原因**：只统计"URLError 1 个"没法定位
        # （可能是客户端读到连接重置，也可能是服务端抛异常后断开）
        return "%s: %s" % (type(e).__name__, str(e)[:60]), time.time() - t


limit = int(subprocess.run([PSQL] + DSN[:-1] + ["-t", "-A",
             "-c", "SELECT current_setting('max_connections')"],
             capture_output=True, text=True).stdout.strip())
th = threading.Thread(target=sample, daemon=True)
th.start()
t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    res = list(ex.map(hit, range(N)))
dt = time.time() - t0
stop.set()
time.sleep(0.3)

codes = Counter(s for s, _ in res)
print("并发 %d 个请求，总耗时 %.2f 秒" % (N, dt))
print("状态分布：%s" % dict(codes))
print("最慢 3 个：%s 秒" % sorted((round(d, 2) for _, d in res), reverse=True)[:3])
print("数据库连接峰值：%d（max_connections = %d）" % (peak[0], limit))
print()
print("① 连接峰值在限制内且有余量：%s" % (peak[0] < limit * 0.8))
print("② 没有 500（过载必须是 503 或成功）：%s" % (500 not in codes))
print("③ 每个请求都得到明确结果（无超时/异常）：%s"
      % all(isinstance(s, int) for s, _ in res))
