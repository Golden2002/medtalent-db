# -*- coding: utf-8 -*-
"""
匿名可见性预检：公网暴露前必须回答"匿名到底能看到什么"

判据要**精确**（第一版用 `'@' in body` 当"含个人数据"，结果每个页面都报 True ——
因为 CSS 里有 `@media`。粗糙的检测等于没有检测）。

这里用的标记：
  · 3 位真实公开人物的 person_id（per_real_*）与姓名
  · 人才档案里的编号形态（MT-REAL- / MT-MOCK-）
  · 手机号/邮箱形态（正则）
逐页报告：匿名拿到的是数据、是权限不足页、还是公开落地页。
"""
import re
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
# 用法：python ops/cf_preflight.py [base_url]
# 传公网地址时就是**从公网那一侧**做同样的检查 —— 隧道日志说"连上了"
# 不等于"外面看到的和本机一样"，必须实测。
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8082").rstrip("/")
PAGES = ["/", "/catalog", "/schema", "/search", "/analyze", "/viz", "/sql", "/lineage",
         "/extend", "/dev", "/talent", "/talent.csv", "/real", "/quality", "/audit",
         "/occupations", "/match", "/tree", "/login", "/t/person", "/t/person_pii",
         "/t/real_test", "/field/person/person_id", "/field/person/subject_code"]

NAME_PAT = re.compile(r"冯唐|李天天|于莺")
ID_PAT = re.compile(r"per_real_[0-9a-z_]+")
CODE_PAT = re.compile(r"MT-(REAL|MOCK|EXT)-[A-Z0-9]+")
CONTACT_PAT = re.compile(r"1[3-9]\d{9}|[\w.+-]+@[\w-]+\.[\w.]+")

print("%-34s %-6s %-10s %s" % ("页面", "状态", "形态", "泄露标记"))
print("-" * 92)
bad = []
for p in PAGES:
    try:
        with urllib.request.urlopen(BASE + p, timeout=40) as r:
            st, b = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        st, b = e.code, e.read().decode("utf-8", "replace")
    except Exception as ex:                               # noqa: BLE001
        print("%-34s %-6s 连接失败 %s" % (p, "-", ex))
        continue
    form = ("权限不足页" if "这不是错误，是访问控制在工作" in b
            else ("公开落地页" if "公开（T0）概览" in b else "数据页"))
    hits = []
    if NAME_PAT.search(b):
        hits.append("真实姓名")
    if ID_PAT.search(b):
        hits.append("per_real_ 编号")
    if CODE_PAT.search(b):
        hits.append("档案编号")
    if CONTACT_PAT.search(b):
        hits.append("联系方式形态")
    print("%-34s %-6s %-10s %s" % (p, st, form, "、".join(hits) or "无"))
    # 只有"数据页"上的泄露才算问题：权限不足页里出现"per_real_"是说明文字的一部分
    if hits and form == "数据页":
        bad.append((p, hits))

print("\n" + "=" * 92)
if bad:
    print("⚠ 需要处理：以下数据页对匿名可见且命中标记：")
    for p, h in bad:
        print("   %s → %s" % (p, "、".join(h)))
else:
    print("✓ 匿名能打开的页面里没有命中任何一个精确标记")
print("=" * 92)
