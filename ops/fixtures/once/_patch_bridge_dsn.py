# -*- coding: utf-8 -*-
"""一次性补丁：bridge 三个模块的 DSN 从 postgres 改为最小权限角色 mt_bridge。

为什么要改：`code/bridge/` 是唯一往库里写人才数据的路径，而它一直用超级用户连接 ——
写操作绕过全部权限设计（没有列级限制、没有"不能删人"的约束）。
迁移 033 建了 mt_bridge（非超级、无 DDL、不能删人、读不到加密身份表），这里接上。

同时给每个模块加上不同的 `application_name`，这样数据库连接日志能区分
是 bridge 的哪个部件连的（排查"谁写坏了数据"时很有用）。
"""
import io
import re

FILES = {
    "code/bridge/exchange.py": "bridge_exchange",
    "code/bridge/outbox.py": "bridge_outbox",
    "code/bridge/projection.py": "bridge_projection",
}
changed = 0
for path, appname in FILES.items():
    s = io.open(path, encoding="utf-8").read()
    if "user=mt_bridge" in s:
        print("[=] %s 已是 mt_bridge" % path)
        continue
    # 匹配 DSN 定义行（可能跨行），把 user=postgres 换掉并补上 application_name
    new, n = re.subn(r'user=postgres', 'user=mt_bridge', s, count=1)
    if n == 0:
        raise SystemExit("找不到 user=postgres：%s" % path)
    # 在 options='...' 后补 application_name（若还没有）
    if "application_name" not in new:
        new, n2 = re.subn(r"options='-c search_path=mt,public'\)",
                          "options='-c search_path=mt,public' "
                          "application_name=%s)" % appname, new, count=1)
        if n2 == 0:
            raise SystemExit("找不到 options 结尾：%s" % path)
    io.open(path, "w", encoding="utf-8", newline="").write(new)
    changed += 1
    print("[✓] %s → user=mt_bridge（application_name=%s）" % (path, appname))
print("改了 %d 个文件" % changed)
