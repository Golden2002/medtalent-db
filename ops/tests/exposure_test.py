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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def code_dir():
    return os.path.join(BASE, "code")


def _fetch_with(path, cookie):
    """带会话 cookie 取页面/CSV，返回 (状态, 文本, 响应头字典)。"""
    req = urllib.request.Request(ROOT + path)
    req.add_header("Cookie", cookie)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def _login_t3():
    """用一个临时 T3 账号登录，拿会话 cookie（导出禁令对 T3 才有意义：
    低等级本来就看不到那些列，测不出"能看不能导"）。

    账号自建自删，前缀 exposure_test@，不污染库。
    """
    import psycopg
    from psycopg.rows import dict_row
    email, pw = "exposure_test@local.test", "exposure-test-pw-3a71"
    admin = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
             "options='-c search_path=mt,public'")
    try:
        with psycopg.connect(admin, row_factory=dict_row, autocommit=True) as c:
            c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                           (SELECT user_id FROM mt.app_user WHERE email=%s)""", (email,))
            c.execute("DELETE FROM mt.app_user WHERE email=%s", (email,))
            c.execute("SELECT mt.web_user_add(%s,%s,'T3','暴露面测试')", (email, pw))
        req = urllib.request.Request(ROOT + "/login")
        req.data = urllib.parse.urlencode({"email": email, "password": pw}).encode()
        req.method = "POST"
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
                sc = r.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as e:
            sc = e.headers.get("Set-Cookie") or ""
        return sc.split(";")[0]
    except Exception:                                     # noqa: BLE001
        return None


def _cleanup_t3():
    import psycopg
    admin = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
             "options='-c search_path=mt,public'")
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                           (SELECT user_id FROM mt.app_user
                             WHERE email='exposure_test@local.test')""")
            c.execute("DELETE FROM mt.app_user WHERE email='exposure_test@local.test'")
            c.execute("DELETE FROM mt.access_log WHERE actor='exposure_test@local.test'")
    except Exception:                                     # noqa: BLE001
        pass


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
        # 【D2】每个服务入口必须**声明自己的连接身份**，不许导入别的模块的 DSN。
        # 为什么断言这个（实测教训）：`console.py` 原来写的是 `DSN = ex.DSN`，
        # 于是把 exchange.py 的 DSN 改成最小权限角色时，**控制台跟着换了身份**，
        # 它的 DELETE 立刻 permission denied —— 一个文件的安全改动弄坏了另一个无关进程。
        # 而且"这个进程用什么身份连库"变得说不清楚，而 I4.1 恰恰要逐个进程回答它。
        import re as _re
        shared = []
        for root, _dirs, files in os.walk(code_dir()):
            if "__pycache__" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                with open(p, encoding="utf-8") as fh:
                    txt = fh.read()
                # 形如 `DSN = <模块>.DSN` 的赋值
                for m in _re.finditer(r"^\s*DSN\s*=\s*(\w+)\.DSN\s*$", txt, _re.M):
                    shared.append("%s → %s.DSN" % (os.path.relpath(p, BASE), m.group(1)))
        check(not shared,
              "没有模块导入别人的 DSN（各自声明连接身份）；违规：%s"
              % ("、".join(shared) or "无"))

        # ===============================================================
        print("\n【F】年龄口径：年龄段公开、出生年 T2（用户决策 037）")
        ok, _ = as_t0("SELECT age_band FROM mt.person_demographics LIMIT 1")
        check(ok, "匿名能读 person_demographics.age_band（年龄段 = 统计属性，公开）")
        ok, _ = as_t0("SELECT birth_year FROM mt.person_demographics LIMIT 1")
        check(not ok, "匿名读不到 person_demographics.birth_year（出生年 = 个人信息，T2）")
        ok, _ = as_t0("SELECT birth_year FROM mt.person_demographics LIMIT 1", role="mt_t2")
        check(ok, "T2 员工能读 birth_year（收得刚好，没伤及正当使用）")
        ok, rows = as_t0("SELECT count(*) AS n FROM mt.v_age_band_public")
        check(ok and rows and rows > 0,
              "匿名能读**年龄段分布**视图（%s 个年龄段）—— 这是「年龄公开」的落地形式" % rows)
        # 分布视图**不含主体标识**：否则它就成了绕过披露控制的旁路
        with psycopg.connect(portal.PORTAL_DSN, row_factory=dict_row, autocommit=True) as c:
            cols = [r["column_name"] for r in c.execute("""
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema='mt' AND table_name='v_age_band_public'""")]
        check(not any(x in cols for x in ("person_id", "subject_code")),
              "年龄段分布视图不含主体标识（列：%s）—— 否则它是披露控制的旁路" % "、".join(cols))
        # 页面层：匿名落地页必须**看得见**年龄段，且**看不到**具体出生年
        st, body = fetch("/")
        check(st == 200 and "年龄段分布" in body,
              "匿名落地页展示年龄段分布（让「年龄公开」真的可见，而不只是数据库里写着）")
        years = re.findall(r"\b(19[5-9]\d|20[0-2]\d)\b", body)
        check(not years, "匿名落地页里没有出现任何具体出生年（出现的：%s）"
              % (sorted(set(years))[:6] or "无"))

        # ===============================================================
        print("\n【E】导出控制：动作级禁令必须真的拦住「带走」，但不影响「看」")
        # 背景：mt.access_policy 有 5 条 action='export_row'、min_tier='X' 的禁令
        # （体检受限项/姓名密文/联系方式/证件哈希/JD 原文），但此前**列级策略里 X 数为 0**
        # —— 也就是"禁止导出"只是文档，CSV 照样导得出去。
        # 迁移 032 把它落成显式表 export_denied，并在 to_csv()（CSV 唯一出口）强制。
        import psycopg as _pg2
        from psycopg.rows import dict_row as _dr2
        admin = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
                 "options='-c search_path=mt,public'")
        with _pg2.connect(admin, row_factory=_dr2) as c:
            # E1 收敛性：每条 export_row=X 策略都必须有物理落点（这是 I4.4 的修复）
            gap = c.execute("""
                SELECT ap.policy_id FROM access_policy ap
                 WHERE ap.action='export_row' AND ap.min_tier='X'
                   AND NOT EXISTS (SELECT 1 FROM export_denied ed
                                    WHERE ed.policy_id = ap.policy_id)""").fetchall()
            n_pol = c.execute("""SELECT count(*) AS n FROM access_policy
                                  WHERE action='export_row' AND min_tier='X'""").fetchone()["n"]
            n_col = c.execute("SELECT count(*) AS n FROM export_denied").fetchone()["n"]
        check(not gap, "%d 条「禁止导出」策略全部有物理落点（共 %d 列）；空转的：%s"
              % (n_pol, n_col, "、".join(g["policy_id"] for g in gap) or "无"))
        check(n_col >= n_pol, "落点列数（%d）不少于策略数（%d）" % (n_col, n_pol))

        # E2 CSV 里必须没有禁导列，且响应头要告知（不能静默删列）
        t3_cookie = _login_t3()
        if t3_cookie:
            for path, needle in (("/t/person_demographics.csv", "health_limits"),
                                 ("/t/person_pii.csv", "full_name_enc"),
                                 ("/t/job_posting.csv", "raw_text_ref")):
                st, b, hdr = _fetch_with(path, t3_cookie)
                head = b.splitlines()[0] if b else ""
                check(st == 200 and needle not in head,
                      "%s 的表头里没有禁导列 %s" % (path, needle))
                check("X-Export-Denied" in dict(hdr) and needle in dict(hdr)["X-Export-Denied"],
                      "  响应头 X-Export-Denied 明确列出被移除的列（不静默删列）")
            # E3 反证：**页面里仍然看得到**该列 —— "能看"与"能导"是两件事，
            # 砍掉"看"不是这些策略要表达的意思。
            st, b, _ = _fetch_with("/t/person_demographics", t3_cookie)
            check(st == 200 and "health_limits" in b,
                  "但页面上仍然看得到 health_limits（能看 != 可以带走）")

    finally:
        srv.shutdown()
        srv.server_close()
        _cleanup_t3()

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
