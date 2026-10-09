# -*- coding: utf-8 -*-
"""
ops/tests/exposure_test.py —— 公网暴露面的**可断言**性质（需求①②的安全底线）

为什么要有这一套
--------------------------------------------------------------------------
"只暴露公开内容"这句话如果只写在文档里、只在部署那天人工检查过一次，
那它和本项目此前吃过的亏是同一类：`docs/06` 早就写全了分级模型，
而实测 RLS 启用 0 张表、access_log 0 行 —— **写在文档里的能力不等于具备的能力**。
所以这里把暴露面变成每一次回归都会跑的断言。

断言分四层
--------------------------------------------------------------------------
A. **HTTP 层**：公开页 200、个人数据页 403、状态码与页面内容必须一致
B. **内容层**：任何匿名可打开的页面里，**不许出现**精确的识别标记
   （真实姓名 / per_real_ 编号 / MT-REAL 档案号 / 手机号邮箱形态）
C. **数据库层**：匿名角色读不到主体标识列与列级剖析快照（绕过网页也拿不到）
D. **部署层**：隧道的端口白名单只有只读门户；门户只绑回环地址
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.join(BASE, "ops"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import portal                                         # noqa: E402
import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check

PORT = 8103
ROOT = "http://127.0.0.1:%d" % PORT

# 公开页：匿名**应该**能看（都是聚合、字典、结构，不含个人数据）
PUBLIC = ["/", "/catalog", "/schema", "/search", "/analyze", "/viz", "/sql", "/login"]
# 受限页：必须 403（含个人数据或内部运营数据）
DENIED = ["/talent", "/talent.csv", "/real", "/quality", "/audit", "/occupations",
          "/match", "/tree", "/lineage", "/extend", "/t/person", "/t/person_pii",
          "/field/person/person_id", "/field/person/subject_code"]

# 内容层的精确标记（**必须精确**：第一版用 `'@' in body` 判"含个人数据"，
# 结果每个页面都报 True —— 因为 CSS 里有 @media。粗糙的检测等于没有检测）
NAME_PAT = re.compile(r"冯唐|李天天|于莺")
ID_PAT = re.compile(r"per_real_[0-9a-z_]+")
CODE_PAT = re.compile(r"MT-(REAL)-[A-Z0-9_]+")
CONTACT_PAT = re.compile(r"1[3-9]\d{9}|[\w.+-]+@[\w-]+\.[a-z]{2,}")
# 页面里的"权限不足"标识（用于校验状态码与内容一致）
DENIED_MARK = "这不是错误，是访问控制在工作"


def fetch(path):
    try:
        with urllib.request.urlopen(ROOT + path, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    portal.meta()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), portal.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # ===============================================================
        print("\n【A】HTTP 层：公开页 200、个人数据页 403，且状态码与内容一致")
        for p in PUBLIC:
            st, b = fetch(p)
            check(st == 200, "匿名 %-28s → 200（实得 %d）" % (p, st))
            check(DENIED_MARK not in b or p == "/", "（%s 不该是权限不足页）" % p)
        for p in DENIED:
            st, b = fetch(p)
            check(st == 403, "匿名 %-28s → 403（实得 %d）" % (p, st))
            check(DENIED_MARK in b, "  %-28s 返回的是「权限不足」页而不是空白/500" % p)

        # ===============================================================
        print("\n【B】内容层：匿名能打开的页面里不许出现识别标记")
        leaks = []
        for p in PUBLIC:
            st, b = fetch(p)
            hits = []
            if NAME_PAT.search(b):
                hits.append("真实姓名")
            if ID_PAT.search(b):
                hits.append("per_real_ 编号")
            if CODE_PAT.search(b):
                hits.append("MT-REAL 档案号")
            if CONTACT_PAT.search(b):
                hits.append("联系方式形态")
            if hits:
                leaks.append("%s→%s" % (p, "、".join(hits)))
        check(not leaks, "公开页里没有任何识别标记（异常：%s）" % ("；".join(leaks) or "无"))

        # ===============================================================
        print("\n【C】数据库层：绕过网页也拿不到（匿名角色读不到标识列与剖析快照）")

        def as_t0(sql, role="mt_t0"):
            """以指定等级角色试一条查询，返回 (能否执行, 值或错误)。

            每条用**独立连接**：PostgreSQL 里一条语句失败会让整个事务 aborted，
            复用一个连接试探会让第二次之后的结果全部失真
            （本项目在 ops/health.py、字段页、公开概览三处都踩过这个坑）。
            """
            with psycopg.connect(portal.PORTAL_DSN, row_factory=dict_row,
                                 autocommit=True) as c:
                c.execute("BEGIN")
                c.execute("SET LOCAL ROLE " + role)
                try:
                    row = c.execute(sql).fetchone()
                    val = list(row.values())[0] if row else None
                    c.execute("ROLLBACK")
                    return True, val
                except psycopg.Error as e:
                    c.execute("ROLLBACK")
                    return False, str(e).splitlines()[0]

        for tbl, col in (("person", "person_id"), ("person", "subject_code")):
            ok, info = as_t0("SELECT %s FROM mt.%s LIMIT 1" % (col, tbl))
            check(not ok, "匿名角色读不到 %s.%s（披露控制：标识列的取值会 identify 个体）"
                  % (tbl, col))
        ok, _ = as_t0("SELECT * FROM mt.column_profile LIMIT 1")
        check(not ok, "匿名角色读不到 column_profile（列级 Top-K 快照只给 T3）")
        ok, _ = as_t0("SELECT * FROM mt.person_pii LIMIT 1")
        check(not ok, "匿名角色读不到 person_pii（加密身份信息）")
        ok, _ = as_t0("SELECT * FROM mt.access_log LIMIT 1")
        check(not ok, "匿名角色读不到 access_log（审计流水是 T2）")
        # 反向：T1（注册用户）**应该**能读标识列 —— 收得太紧同样是错
        ok, _ = as_t0("SELECT person_id FROM mt.person LIMIT 1", role="mt_t1")
        check(ok, "T1 注册用户能读 person.person_id（披露控制收得刚好，没伤及正当使用）")
        # 数量公开：这是用户"编号、年龄可以公开"与"限权"之间的落点。
        # **必须走聚合闸门 public_counts()**（迁移 031）：
        # 直接 `count(*)` 在 PostgreSQL 里要求表级/列级 SELECT，而 030 之后 person 表
        # 对匿名一列可读都没有 → 会被拒。这不是缺陷，而是"聚合与列访问必须分开授权"
        # 的直接证据 —— 靠"恰好有一列公开"来顺带满足数量公开，是脆弱的副作用。
        ok, n = as_t0("SELECT n_rows FROM mt.public_counts() WHERE table_name='person'")
        check(ok and n and n > 0,
              "匿名经聚合闸门拿到数量（%s 人）—— 与列权限无关" % n)
        ok, _ = as_t0("SELECT count(*) FROM mt.person")
        check(not ok,
              "匿名直接 count(person) 被拒（该表对匿名无一列可读）"
              "—— 所以数量必须走闸门，不能靠副作用")

        # ===============================================================
        print("\n【D】部署层：端口白名单只有只读门户；门户只绑回环地址")
        import cf_tunnel
        check(set(cf_tunnel.ALLOWED) == {8082},
              "隧道端口白名单只有 8082（实得 %s）—— 可写进程/控制台/数据库绝不允许暴露"
              % sorted(cf_tunnel.ALLOWED))
        src = inspect.getsource(portal.serve)
        check("127.0.0.1" in src and "0.0.0.0" not in src,
              "门户绑定写死 127.0.0.1（TLS 由 Cloudflare 边缘终止，源站不出回环）")
        # 只读这个性质本身也要在暴露面上成立
        check("READ ONLY" in inspect.getsource(portal.db),
              "请求路径仍在 READ ONLY 事务里（写不进去是被数据库拒绝的，不是靠自觉）")

    finally:
        srv.shutdown()
        srv.server_close()

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
