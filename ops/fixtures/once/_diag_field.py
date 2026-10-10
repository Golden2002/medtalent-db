# -*- coding: utf-8 -*-
"""直接以 T0 会话渲染字段页，抓出 500 的真实异常。"""
import sys
import traceback

sys.path.insert(0, "code/demo")
sys.path.insert(0, "code")

import portal as P  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
P.meta()
sess = P.Session(actor="anon_probe", tier="T0", ok=False)
for t, col in (("person", "person_id"), ("code_value", "code"), ("person", "subject_code")):
    try:
        with P.db(sess) as c:
            b = P.view_field(c, t, col, {})
        print("[OK  ] %s.%s → %d 字节" % (t, col, len(b)))
    except Exception as e:                                   # noqa: BLE001
        print("[FAIL] %s.%s → %s: %s" % (t, col, type(e).__name__, e))
        traceback.print_exc()
