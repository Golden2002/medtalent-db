# -*- coding: utf-8 -*-
"""把归档探针里硬编码的管理员邮箱改成**运行时读取凭据**（清理泄露，保留证据）。

背景：我在归档这批一次性探针时手工扫过凭据，扫到了 token 前缀与口令，
但**没把"登录邮箱"当凭据** —— 于是 3 个探针里的管理员邮箱被提交进了公开仓库，
第 4 个（_verify_sec_fixes.py）连手工扫都没扫到。
ops/secrets_scan.py 用"我们确实把它当凭据保存了"作判据，一次全抓出来。

（本文件自己的注释里原本也写了那个邮箱 —— 扫描器连它一起抓了，
所以这里也不再写出具体值：**扫描器的判据是"值"，不是"看起来像不像密钥"**。）

修法：改成从凭据文件读取（`_verify_admin.py` 早就是这么读口令的），
并在行内注明"此处曾硬编码，已改为运行时读取"。
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ONCE = os.path.join(BASE, "ops", "fixtures", "once")

# 从凭据文件读邮箱（与读口令同一模式）
READER = ('_load_admin_email()')
HELPER = '''

def _load_admin_email():
    """从凭据文件读管理员邮箱 —— **不要硬编码在这里**。

    这一行原来写的是明文邮箱，被 ops/secrets_scan.py 抓出来：
    它把"我们确实当凭据保存的东西"作为判据，于是登录邮箱也算凭据。
    凭据文件在仓库外/被 gitignore，仓库里只留读取逻辑。
    """
    for d in (r"D:\\wechat_summary", os.path.join(os.path.dirname(
                  os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                  ".tools", "credentials")):
        p = os.path.join(d, "medtalent_admin_email.txt")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as fh:
                return fh.read().strip().split("=", 1)[-1].strip()
    raise SystemExit("[X] 找不到 medtalent_admin_email.txt —— 本脚本不再硬编码邮箱")
'''

pat_email = re.compile(r'["\']1293869083@qq\.com["\']')
fixed = []
for fn in sorted(os.listdir(ONCE)):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(ONCE, fn)
    with io.open(p, encoding="utf-8") as fh:
        s = fh.read()
    if not pat_email.search(s):
        continue
    s2 = pat_email.sub("_load_admin_email()", s)
    if "_load_admin_email" not in s.split("def ")[0] and "def _load_admin_email" not in s:
        # 在 import 段之后插入 helper（放在首个 def 之前）
        m = re.search(r"^(?=def |class )", s2, re.M)
        s2 = (s2[:m.start()] + HELPER.lstrip("\n") + "\n\n" + s2[m.start():]
              if m else s2 + HELPER)
    if "import io" not in s2:
        s2 = s2.replace("import os", "import io\nimport os", 1)
    with io.open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(s2)
    fixed.append(fn)
    print("[✓] 已清理 %s" % fn)
print("共清理 %d 个文件" % len(fixed))
