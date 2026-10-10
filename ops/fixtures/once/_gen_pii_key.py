# -*- coding: utf-8 -*-
"""生成 PII 加密密钥并落盘到既有的凭据目录（不回显密钥）。"""
import os
import secrets
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
k = secrets.token_urlsafe(48)
for d in (r"D:\wechat_summary", os.path.join(BASE, ".tools", "credentials")):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "medtalent_pii_key.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("MEDTALENT_PII_KEY=%s\n" % k)
    print("[✓] 已写入 %s（%d 字节）" % (p, os.path.getsize(p)))
print("密钥已生成（%d 字符，**不回显**）" % len(k))
