# -*- coding: utf-8 -*-
"""打印 M3 那次提交的真实响应，找出为什么没写进去。"""
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ext = "regmock_diag_%d" % int(time.time())
data = {"act": "submit", "ext_id": ext, "stage": "D3", "degree": "D3",
        "city": "CTY01", "major": "临床医学",
        "org": "某医科大学附属医院", "role": "住院医师",
        "start_ym": "2024-09", "is_current": "1",
        "skill_lit": "1", "skill_team": "1",
        "occupation": "F02", "consent_cp1": "1"}

req = urllib.request.Request("http://127.0.0.1:8083/mockreg")
req.data = urllib.parse.urlencode(data, doseq=True).encode()
req.method = "POST"
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        st, body = r.status, r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    st, body = e.code, e.read().decode("utf-8", "replace")

print("HTTP %s，%d 字节" % (st, len(body)))
# 注意：开发者模式的错误块用的是**单引号** HTML 属性（<div class='note err'>），
# 一开始用双引号正则什么都没匹配到 —— 断言/诊断也要按真实的 HTML 写，不能想当然。
for cls in ("note err", "note ok", "note warn", "note info"):
    for m in re.finditer(r"""<div class=['"]%s['"]>(.*?)</div>""" % cls, body, re.S):
        txt = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if txt:
            print("\n[%s] %s" % (cls, txt[:700]))
if "note err" not in body:
    i = body.find("错误")
    print("\n原始片段：%s" % re.sub(r"<[^>]+>", " ", body[max(0, i - 200):i + 600])[:700])

# 概要行
m = re.search(r"写入成功.*?</div>", body, re.S)
print("\n写入成功标记：%s" % bool(m))
print("外部编号出现在页面里：%s" % (ext in body))
