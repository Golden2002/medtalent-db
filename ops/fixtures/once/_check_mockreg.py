# -*- coding: utf-8 -*-
"""进程内自检：mock 注册窗口能不能渲染（不必起服务）。"""
import sys
import traceback

sys.path.insert(0, "code/demo")
sys.path.insert(0, "code")
sys.stdout.reconfigure(encoding="utf-8")

import portal as P            # noqa: E402
import portal_mockreg as MR   # noqa: E402

P.meta()
with P.admin_db(readonly=False) as c:
    for label, fn in (("GET /mockreg（表单）", lambda: MR.view(c, {})),):
        try:
            b = fn()
            print("[OK  ] %s → %d 字节" % (label, len(b)))
            txt = b.decode("utf-8")
            for probe in ("mock 小程序注册窗口", "外部编号", "个人分析", "source_system",
                          "三层证据", "CONSENT_REQUIRED"):
                print("        含「%s」：%s" % (probe, probe in txt))
        except Exception:                                  # noqa: BLE001
            print("[FAIL] %s" % label)
            traceback.print_exc()

# 孤儿检测 + 快照本身也要能跑（它们是证据层的地基）
try:
    s = MR.snapshot()
    print("[OK  ] snapshot() → %s" % {k: v for k, v in list(s.items())[:4]})
except Exception:                                          # noqa: BLE001
    print("[FAIL] snapshot()")
    traceback.print_exc()
try:
    o = MR.orphan_check()
    print("[OK  ] orphan_check() → 检查了 %d 张外键子表，问题：%s"
          % (o["n_fk_children"], o["problems"] or "无"))
except Exception:                                          # noqa: BLE001
    print("[FAIL] orphan_check()")
    traceback.print_exc()
