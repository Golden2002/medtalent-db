# -*- coding: utf-8 -*-
"""补丁（续）：outbox.py / projection.py 的 DSN 改为 mt_bridge。

上一版脚本只处理了 exchange.py 就退出了，因为另两个文件的 DSN 结尾写法不同
（不是以 options='...') 结束）。这里按行精确替换，并在末尾补 application_name。
"""
import io

JOBS = [("code/bridge/outbox.py", "bridge_outbox"),
        ("code/bridge/projection.py", "bridge_projection")]
for path, appname in JOBS:
    lines = io.open(path, encoding="utf-8").read().split("\n")
    hit = False
    for i, ln in enumerate(lines):
        if "DSN" in ln and "user=postgres" in ln:
            lines[i] = ln.replace("user=postgres", "user=mt_bridge")
            hit = True
            # 下一行是 DSN 的续行（client_encoding / options ...）
            if i + 1 < len(lines) and "options=" in lines[i + 1]:
                if "application_name" not in lines[i + 1]:
                    lines[i + 1] = lines[i + 1].replace(
                        "')\"", "application_name=%s')\"" % appname)
            break
    if not hit:
        print("[=] %s 未找到待替换行" % path)
        continue
    io.open(path, "w", encoding="utf-8", newline="").write("\n".join(lines))
    print("[✓] %s → mt_bridge" % path)

# 核对结果
for p, _ in JOBS:
    s = io.open(p, encoding="utf-8").read()
    print("  %s: mt_bridge=%s application_name=%s"
          % (p, "user=mt_bridge" in s, "application_name" in s))
