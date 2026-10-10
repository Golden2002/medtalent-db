# -*- coding: utf-8 -*-
"""一次性补丁：把 do_GET 里逐路由的 log_visit 去掉，改为在 _send 单点统一记。

为什么：逐路由记漏掉了 /talent、/search、/analyze 等一大批页面 ——
审计"在跑但覆盖面残缺"，而残缺的审计比没有更危险（你会以为"没记录=没发生"）。
保留的例外：
  · /logout  —— 语义是 logout，不是 view，且它自己在退出时记（路径已在 _send 里排除）
  · /login 的 login / login_failed —— 由 do_POST 自己记（要区分成功与失败）
  · 403 的 denied —— 由 InsufficientPrivilege 处理器把**原因**放进 self._audit_detail，
    再由 _send 统一记（这样既有覆盖面，又不丢"缺哪个等级、数据库原话"这些细节）
"""
import io

P = "code/demo/portal.py"
s = io.open(P, encoding="utf-8").read()

# 1) 删掉 do_GET 里逐路由的三条 view 记录（/audit、/、/catalog、/field）
drops = [
    '                    log_visit(sess, "view", "audit")\n',
    '                    log_visit(sess, "view", "home")\n',
    '                    log_visit(sess, "view", "catalog")\n',
    '                    log_visit(sess, "view", "field", detail={"t": mm.group(1), "c": mm.group(2)})\n',
]
removed = 0
for d in drops:
    if d in s:
        s = s.replace(d, "", 1)
        removed += 1
    else:
        print("[!] 没找到（跳过）：%r" % d.strip()[:60])

# 2) 403 处理器：把原因放进实例属性，交给 _send 统一记
old_denied = '''            log_visit(sess, "denied", path, tier=sess.tier,
                      detail={"need": need, "db_error": str(e).splitlines()[0][:200]})'''
new_denied = '''            # 原因交给 _send 统一记（覆盖面单点化，细节不丢）
            self._audit_detail = {"need": need,
                                  "db_error": str(e).splitlines()[0][:200]}'''
if old_denied in s:
    s = s.replace(old_denied, new_denied, 1)
    removed += 1
else:
    print("[!] 没找到 403 的 log_visit")

io.open(P, "w", encoding="utf-8", newline="").write(s)
print("处理 %d 处" % removed)
