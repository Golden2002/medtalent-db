# -*- coding: utf-8 -*-
"""一次性修正已生成的 postgresql.conf 里的 log_line_prefix（%% → %）。

为什么单独修：ops/pg.py 的配置写入是**一次性**的（靠 marker 判断"已写入"），
所以修了源码不会影响已经生成的配置。这份配置在 .tools/pgdata 下、属于生成物，
但它是**当前正在用的**那份，必须与实际源码一致，否则"修了源码但库还在用旧配置"。
"""
import io
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
CONF = r"D:\wbo-workspace\.tools\pgdata\postgresql.conf"
OLD = "log_line_prefix = '%%m [%%p] user=%%u db=%%d app=%%a from %%r '"
NEW = "log_line_prefix = '%m [%p] user=%u db=%d app=%a from %r '"

if not os.path.exists(CONF):
    raise SystemExit("找不到 %s" % CONF)
s = io.open(CONF, encoding="utf-8").read()
if NEW in s:
    print("[=] 已是正确写法")
elif OLD in s:
    io.open(CONF, "w", encoding="utf-8", newline="").write(s.replace(OLD, NEW))
    print("[✓] 已修正 log_line_prefix（%% → %%）".replace("%%", "%"))
else:
    raise SystemExit("配置里没有找到待修正的那一行，未改动任何内容")

r = subprocess.run([r"D:\wbo-workspace\.tools\pgsql\bin\psql.exe",
                    "-h", "127.0.0.1", "-p", "55432", "-U", "postgres", "-d", "postgres",
                    "-tAc", "SELECT pg_reload_conf()"],
                   capture_output=True, text=True, encoding="utf-8")
print("reload → %s" % (r.stdout.strip() or r.stderr.strip()[:100]))
r2 = subprocess.run([r"D:\wbo-workspace\.tools\pgsql\bin\psql.exe",
                     "-h", "127.0.0.1", "-p", "55432", "-U", "postgres", "-d", "postgres",
                     "-tAc", "SHOW log_line_prefix"],
                    capture_output=True, text=True, encoding="utf-8")
print("当前 log_line_prefix = %r" % r2.stdout.strip())
