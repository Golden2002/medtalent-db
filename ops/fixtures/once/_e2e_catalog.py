# -*- coding: utf-8 -*-
"""验证数据目录与字段页（含匿名/T3 两种身份）——真实 HTTP。"""
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import psycopg

BASE = "http://127.0.0.1:8082"
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")
EMAIL, PW = "catalog_probe@local.test", "catalog-probe-pw-5d27"
sys.stdout.reconfigure(encoding="utf-8")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(path, cookie=None, data=None):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    if data is not None:
        req.data = urllib.parse.urlencode(data).encode()
        req.method = "POST"
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


out = []
with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    c.execute("SELECT mt.web_user_add(%s,%s,'T3','目录探针')", (EMAIL, PW))
    n_prof = c.execute("SELECT count(*) FROM mt.column_profile").fetchone()[0]

    # 匿名
    st, body, _ = get("/catalog")
    out.append(("匿名 /catalog", "HTTP %d, %d 字节" % (st, len(body)), st == 200))
    has_kpi = "字段名" in body and "行（精确" in body
    out.append(("目录页含 KPI 与口径说明", "字段名/精确 count 说明都存在=%s" % has_kpi, has_kpi))
    st, body, _ = get("/field/person/person_id")
    out.append(("匿名 /field/person/person_id（T0 列）",
                "HTTP %d" % st, st == 200))

    # 登录 T3
    st, _, hdr = get("/login", data={"email": EMAIL, "password": PW})
    ck = (hdr.get("Set-Cookie") or "").split(";")[0]
    st, body, _ = get("/catalog", cookie=ck)
    out.append(("T3 /catalog", "HTTP %d, %d 字节" % (st, len(body)), st == 200))
    # 检索
    st, body2, _ = get("/catalog?q=" + urllib.parse.quote("学历"), cookie=ck)
    out.append(("目录检索「学历」", "HTTP %d, %d 字节" % (st, len(body2)), st == 200))
    # 字段页：取一个真的有剖析数据的列
    fld = c.execute("""SELECT table_name, column_name, n_distinct, is_enum_like,
                              top_values IS NOT NULL AS has_top
                         FROM mt.column_profile
                        WHERE top_values IS NOT NULL AND n_distinct > 2
                        ORDER BY elapsed_ms DESC LIMIT 1""").fetchone()
    t, col, nd, isenum, htop = fld
    st, body3, _ = get("/field/%s/%s" % (t, col), cookie=ck)
    ok3 = st == 200 and "去重值个数" in body3 and ("中文标签" in body3 or "Top-K" in body3)
    out.append(("字段页 %s.%s" % (t, col),
                "HTTP %d, 有去重数=%s, 有Top-K表头=%s" %
                (st, "去重值个数" in body3, "中文标签" in body3), ok3))
    # 排序
    st, body4, _ = get("/catalog?sort=enum&dir=desc", cookie=ck)
    out.append(("目录排序（可分类列）", "HTTP %d" % st, st == 200))
    # 权限筛选
    st, body5, _ = get("/catalog?tier=T3", cookie=ck)
    out.append(("目录按权限等级筛选 T3", "HTTP %d" % st, st == 200))

    c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                   (SELECT user_id FROM mt.app_user WHERE email=%s)""", (EMAIL,))
    c.execute("DELETE FROM mt.app_user WHERE email=%s", (EMAIL,))
    c.execute("DELETE FROM mt.access_log WHERE actor=%s", (EMAIL,))
    print("剖析表中列数：%d；残留测试账号=%d" % (
        n_prof, c.execute("SELECT count(*) FROM mt.app_user WHERE email=%s",
                          (EMAIL,)).fetchone()[0]))

print("\n" + "=" * 72)
ok = True
for name, detail, passed in out:
    print("%s %-34s %s" % ("[PASS]" if passed else "[FAIL]", name, detail))
    ok = ok and passed
print("=" * 72)
sys.exit(0 if ok else 1)
