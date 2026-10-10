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

import concurrent.futures
import csv
import inspect
import io
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
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

# 运维连接（清理、以及"验证门户读不到的东西确实存在"这类反向断言）
_ADMIN_DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
              "options='-c search_path=mt,public'")

# 公开页：匿名**应该**能看（都是聚合、字典、结构，不含个人数据）
PUBLIC = ["/", "/catalog", "/schema", "/search", "/analyze", "/viz", "/sql", "/login"]
# 受限页：必须 403（含个人数据或内部运营数据）
DENIED = ["/talent", "/talent.csv", "/real", "/quality", "/audit", "/occupations",
          "/match", "/tree", "/lineage", "/extend", "/t/person", "/t/person_pii",
          "/field/person/person_id", "/field/person/subject_code",
          # ⚠ 独立审查指出的漏检：原来这里**只列了 person 表**的两个字段页。
          # 而主体标识在 29 张子表里都存在 —— 匿名因此能通过
          # /field/evidence/person_id 拿到"真实人物 id + 精确条数"。
          # 教训：清单式断言必须覆盖**同一类对象的全部**，否则它守的是"这一个"而不是"这一类"。
          "/field/evidence/person_id", "/field/award_honor/person_id",
          "/field/education_record/person_id", "/field/research_output/person_id",
          "/field/person_pii/full_name_enc", "/t/app_user", "/t/login_attempt"]

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


def _ensure_t3():
    """建（或重建）一个临时 T3 账号，返回 (邮箱, 口令)。账号自建自删，不污染库。"""
    import psycopg
    from psycopg.rows import dict_row
    email, pw = "exposure_test@local.test", "exposure-test-pw-3a71"
    admin = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
             "options='-c search_path=mt,public'")
    with psycopg.connect(admin, row_factory=dict_row, autocommit=True) as c:
        c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                       (SELECT user_id FROM mt.app_user WHERE email=%s)""", (email,))
        c.execute("DELETE FROM mt.app_user WHERE email=%s", (email,))
        c.execute("SELECT mt.web_user_add(%s,%s,'T3','暴露面测试')", (email, pw))
    return email, pw


def _login_t3():
    """用一个临时 T3 账号登录，拿会话 cookie（导出禁令对 T3 才有意义：
    低等级本来就看不到那些列，测不出"能看不能导"）。
    """
    email, pw = _ensure_t3()
    req = urllib.request.Request(ROOT + "/login")
    req.data = urllib.parse.urlencode({"email": email, "password": pw}).encode()
    req.method = "POST"
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as e:
        sc = e.headers.get("Set-Cookie") or ""
    return sc.split(";")[0]


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
            # 限流探测留下的记录：**必须清掉**，否则它会污染限流状态
            # （虽然邮箱是随机的，但"测试留下的登录痕迹"本身就是不该长期存在的数据）
            c.execute("DELETE FROM mt.login_attempt WHERE email LIKE 'throttle_probe_%@local.test'")
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
        # 【D3】用户决策「公网只读、管理只在本机」——必须**真的写不进去**，
        # 而不是"我们没提供写入口"。证据分两层：
        #   ① HTTP 方法层：门户不接受 POST（405）；
        #   ② **数据库层**：GET 一条 INSERT/DELETE/UPDATE/DDL 被只读事务拒绝，
        #      并且事后库里没有任何探测行 —— 后者才是"没写进去"的证据
        #      （只看页面提示可能是渲染出来的文案）。
        import urllib.parse as _up
        for label, stmt in (
                ("INSERT", "INSERT INTO mt.person (person_id) VALUES ('per_probe_exposure')"),
                ("DELETE", "DELETE FROM mt.person WHERE person_id='per_nobody'"),
                ("UPDATE", "UPDATE mt.person SET status='active'"),
                ("DDL", "CREATE TABLE mt.probe_table (x int)")):
            st, body = fetch("/sql?" + _up.urlencode({"q": stmt}))
            check(st < 500 and ("只读" in body or "READ ONLY" in body
                                or "权限不足" in body or "必须以 SELECT" in body
                                or "不允许" in body),
                  "门户拒绝 %s（数据库层的只读/权限在拦）" % label)
        with psycopg.connect(_ADMIN_DSN, row_factory=dict_row, autocommit=True) as c:
            n = c.execute("SELECT count(*) AS n FROM mt.person "
                          "WHERE person_id LIKE 'per_probe%'").fetchone()["n"]
            t = c.execute("SELECT count(*) AS n FROM information_schema.tables "
                          "WHERE table_schema='mt' AND table_name='probe_table'").fetchone()["n"]
        check(n == 0 and t == 0,
              "四个写尝试**没有留下任何痕迹**（探测行 %d、probe_table %d）—— "
              "这是「公网只读」的实证，不是文案" % (n, t))

        # ===============================================================
        print("\n【J】口径一致性：闸门、/schema、/catalog 必须同一个答案")
        # 背景（独立审查 P1-1）：041 给聚合闸门加了排除，但公开的 /schema 用 meta()
        # 照样把 change_log 的真实行数显示给匿名 —— **同一问题两套实现、两个答案**，
        # 而且两边看起来都对。048 把判据统一到 mt.v_count_visibility。
        with psycopg.connect(_ADMIN_DSN, row_factory=dict_row, autocommit=True) as c:
            hidden = [r["table_name"] for r in c.execute(
                "SELECT table_name FROM mt.v_count_visibility WHERE NOT count_is_public")]
            gate = [r["table_name"] for r in c.execute("SELECT * FROM mt.public_counts()")]
        check(bool(hidden), "登记了「行数不公开」的表（%d 张：%s）"
              % (len(hidden), "、".join(hidden)))
        check(not (set(hidden) & set(gate)),
              "聚合闸门**确实不再返回**这些表的行数（一致的第一个答案）")

        # 页面层：两个公开页都要按同一判据隐藏（结构精确匹配，避免"列数"被误当行数——
        # 我第一版就是用松散的子串匹配，把 n_cols=9 误判成行数泄露）
        cell_pat = re.compile(
            r'<code>([a-z_]+)</code></a></td><td class="n">(\d+)</td>'
            r'<td class="n">(.*?)</td>', re.S)
        for page_path in ("/schema", "/catalog"):
            st, body = fetch(page_path)
            cells = {m.group(1): re.sub(r"<[^>]+>", "", m.group(3)).strip()
                     for m in cell_pat.finditer(body)}
            leaked = [t for t in hidden
                      if cells.get(t) and cells[t].replace(",", "").isdigit()]
            check(st == 200 and not leaked,
                  "匿名 %s 不显示活动表的真实行数（泄露的：%s）"
                  % (page_path, "、".join(leaked) or "无"))
        # 数据表的行数必须**仍然公开**（需求原文"数量可以公开"不能被误伤）。
        # 只在 /schema 上做这条断言：它的行模板是「表|列|行数|…」，
        # 而 /catalog 是「表|域|行数|…」（中间多一列），
        # 用同一个正则匹配两个页面会把 /catalog 的 null 当成失败 —— 断言要贴着实际结构写。
        st_s, body_s = fetch("/schema")
        cells_s = {m.group(1): re.sub(r"<[^>]+>", "", m.group(3)).strip()
                   for m in cell_pat.finditer(body_s)}
        check(cells_s.get("person", "").replace(",", "").isdigit(),
              "数据表行数仍然公开（/schema 上 person=%s）—— 「数量可以公开」没被误伤"
              % cells_s.get("person"))
        # /catalog 也必须有 person 这一行（只是列位置不同）：确认它没被整页隐藏
        st_c, body_c = fetch("/catalog")
        check("person" in body_c and "不公开" in body_c,
              "/catalog 同时包含公开行（person）与隐藏行（活动表）")
        # 同一判据的反面：登录后（T2+）必须看得到真实数字，否则"收过头了"
        t3ck = _login_t3()
        if t3ck:
            st, body = _fetch_with("/schema", t3ck)[:2]
            cells = {m.group(1): re.sub(r"<[^>]+>", "", m.group(3)).strip()
                     for m in cell_pat.finditer(body)}
            shown = [t for t in hidden
                     if cells.get(t, "").replace(",", "").isdigit()]
            check(len(shown) == len(hidden),
                  "T3 登录后看得到全部 %d 张活动表的真实行数（收得刚好，没伤运维）"
                  % len(hidden))

        # 错误信息按身份分流（P2-6）：匿名只给追踪号，登录后给数据库原话
        fake = "permission denied for function occupation_asof"
        anon_block = portal.db_error_block(portal.ANON, fake)
        auth_block = portal.db_error_block(
            portal.Session(actor="x@y.z", tier="T3", session_id="s", ok=True), fake)
        check(fake not in anon_block and "追踪号" in anon_block,
              "匿名页不给数据库原话，只给追踪号（内部表名/函数名不外泄）")
        check(fake in auth_block and "追踪号" in auth_block,
              "登录后给数据库原话 + 追踪号（运维可诊断，这是刻意保留的能力）")
        check(portal.target_label(portal.ANON, "mt.access_log") != "mt.access_log",
              "匿名页的「被拒绝的对象」不暴露内部表名")

        # 参数化：db.call 用 Identifier 拼函数名（P2-4）。含引号的"函数名"必须被当作
        # 标识符处理（→ 不存在），而不是拼进 SQL 里形成注入。
        import sys as _sys
        _sys.path.insert(0, os.path.join(BASE, "code"))
        import db as _db
        try:
            _db.call("access_rank'; DROP TABLE mt.person; --", ["T2"])
            bad_call = True
        except Exception:                                  # noqa: BLE001
            bad_call = False
        check(not bad_call, "db.call 把函数名当标识符处理，注入式输入被拒（P2-4）")
        with psycopg.connect(_ADMIN_DSN, row_factory=dict_row, autocommit=True) as c:
            alive = c.execute("SELECT count(*) AS n FROM information_schema.tables "
                              "WHERE table_schema='mt' AND table_name='person'").fetchone()["n"]
        check(alive == 1, "mt.person 仍然存在（注入尝试没有生效）")

        # ===============================================================
        print("\n【I】权限不变式（独立审查发现的 P0/P1 都在这里变成断言）")
        with psycopg.connect(_ADMIN_DSN, row_factory=dict_row, autocommit=True) as c:
            # I-1 主体标识：**全部表**都不许对匿名开放（P0-1）
            #     030 曾按表名只保护 person/person_pii 两张，于是另外 29 张子表的
            #     person_id 全是 T0，匿名能枚举全库主体、并从公开剖析视图拿到
            #     取值+精确频次（per_real_fengtang 等编码姓名的 id）。
            n_t0 = c.execute("""SELECT count(*) AS n FROM mt.column_policy
                                 WHERE column_name IN ('person_id','subject_code')
                                   AND mt.access_rank(min_tier) <= mt.access_rank('T0')"""
                             ).fetchone()["n"]
            check(n_t0 == 0, "没有任何表的主体标识列 ≤ T0（实测 %d 个）" % n_t0)
            # I-2 公开剖析视图不得交出主体标识的取值（P0-1 的第二层）
            n_top = c.execute("""SELECT count(*) AS n FROM mt.v_column_profile_public
                                  WHERE column_name IN ('person_id','subject_code')
                                    AND top_values IS NOT NULL""").fetchone()["n"]
            check(n_top == 0, "公开剖析视图没有交出主体标识的 Top-K 取值（%d 条）" % n_top)
            # I-3 凭据列不得对任何等级角色外授（P1-2）
            cred = [r for r in c.execute("""SELECT unnest(ARRAY['mt_portal','mt_t0','mt_t1',
                                                'mt_t2','mt_t3']) AS r""")
                    if c.execute("SELECT has_column_privilege(%s,'mt.app_user',"
                                 "'password_hash','SELECT') AS x", (r["r"],)).fetchone()["x"]]
            check(not cred, "口令哈希对任何等级角色都不可读（可读的：%s）"
                  % ("、".join(x["r"] for x in cred) or "无"))
            # I-4 本库自有函数不得对 PUBLIC 可执行（P1-3：原来 82 个）
            n_pub = c.execute("SELECT count(*) AS n FROM mt.v_function_public_exposure"
                              ).fetchone()["n"]
            check(n_pub == 0, "本库自有函数对 PUBLIC 的可执行数 = %d（期望 0）" % n_pub)
            # I-5 不变式检查函数本身必须全部成立（P0-2 的守卫）
            inv = c.execute("SELECT invariant, ok, detail FROM mt.check_policy_invariants()"
                            ).fetchall()
            bad = ["%s（%s）" % (r["invariant"], r["detail"]) for r in inv if not r["ok"]]
            check(not bad and len(inv) >= 4,
                  "权限不变式全部成立（%d 条）；违反：%s" % (len(inv), "；".join(bad) or "无"))
            # I-6 授权对账入口必须可跑（P0-2：曾因 public_counts 签名不一致整体报错）
            try:
                c.execute("SELECT * FROM mt.apply_column_grants()").fetchall()
                recon = True
            except psycopg.Error:
                recon = False
            check(recon, "授权对账入口 apply_column_grants() 可正常执行（唯一自愈路径）")
            # I-9 只读辅助函数的"自动放行"规则（047）
            #     045 的"一刀切拒绝"曾把门户要用的 occupation_asof 也锁死 → /tree 对
            #     T3 管理员 403。判据必须是函数属性（非 DEFINER + 只读），不是名字清单。
            n_definer = c.execute("""SELECT count(*) AS n FROM mt.read_only_helpers() h
                                       JOIN pg_proc p ON p.proname = h.proname
                                       JOIN pg_namespace ns ON ns.oid = p.pronamespace
                                                            AND ns.nspname = 'mt'
                                      WHERE p.prosecdef""").fetchone()["n"]
            check(n_definer == 0,
                  "自动放行的只读函数里没有 SECURITY DEFINER（%d 个）—— "
                  "DEFINER 会绕过调用者权限，绝不能自动放行" % n_definer)
            asof = c.execute("SELECT has_function_privilege('mt_t3',"
                             "'mt.occupation_asof(date)','EXECUTE') AS t3,"
                             " has_function_privilege('mt_t1',"
                             "'mt.occupation_asof(date)','EXECUTE') AS t1").fetchone()
            check(asof["t3"] and asof["t1"],
                  "页面要用的只读辅助函数 occupation_asof 对 T1/T3 可执行（否则 /tree 会 403）")
            write_ok = c.execute("""SELECT has_function_privilege('mt_t3',
                     'mt.set_value(text,text,text,text,text[],numeric,text,date,text,'
                     'smallint,numeric)','EXECUTE') AS x""").fetchone()["x"]
            check(not write_ok, "写函数 set_value 仍不可被等级角色执行（收得紧）")

            # I-10 审计粒度（迁移 050）：机械重算按"操作"记，但必须有缺口检测兜底
            #      背景：实测 change_log 89 万行里 job_requirement 一项占 70%，
            #      全是 JD 解析"按岗位先删后插"的机械重写 —— 噪声把真正的变更淹没了。
            mode = c.execute("SELECT mode FROM mt.audit_mode WHERE table_name='job_requirement'"
                             ).fetchone()
            check(mode and mode["mode"] == "summary",
                  "job_requirement 登记为 summary 粒度（按操作记，不逐行记）")
            before = c.execute("SELECT count(*) AS n FROM mt.change_log "
                               "WHERE object_type='job_requirement'").fetchone()["n"]
            with psycopg.connect(_ADMIN_DSN, autocommit=True) as w:
                w.execute("UPDATE mt.job_requirement SET raw_text = raw_text "
                          "WHERE requirement_id = (SELECT requirement_id "
                          "FROM mt.job_requirement LIMIT 1)")
            after = c.execute("SELECT count(*) AS n FROM mt.change_log "
                              "WHERE object_type='job_requirement'").fetchone()["n"]
            check(before == after,
                  "写 summary 粒度的表**不再产生逐行审计**（%d → %d）" % (before, after))

            # **检测必须真的能检出**（这是"换粒度"与"关审计"的分界线）：
            # 上面那次写没有写汇总行 → 缺口视图应当立刻报出来。
            # 只断言"缺口=0"是不够的 —— 那可能只是视图恒返回空。
            gap_now = c.execute("SELECT 未覆盖增量 AS g FROM mt.v_audit_gap "
                                "WHERE table_name='job_requirement'").fetchone()["g"]
            check(gap_now > 0,
                  "缺口检测**有效**：不写汇总行的改动被检出（job_requirement +%s）" % gap_now)
            # 写一条汇总行 → 缺口闭合（这正是重算工具该做的事）
            with psycopg.connect(_ADMIN_DSN, autocommit=True) as w:
                w.execute("SELECT mt.audit_bulk('job_requirement','rebuild',%s,%s)",
                          (1, "测试：闭合缺口"))
            gap_after = c.execute("SELECT 未覆盖增量 AS g FROM mt.v_audit_gap "
                                  "WHERE table_name='job_requirement'").fetchone()["g"]
            check(gap_after == 0,
                  "写汇总行后缺口归零（+%s → %s）—— 所以「按操作记」是可验证的粒度，"
                  "而不是关掉了审计" % (gap_now, gap_after))
            # 汇总行必须带计数器快照（否则缺口检测无从比较）
            snap = c.execute("""SELECT count(*) AS n FROM mt.change_log
                                 WHERE detail->>'mode'='summary'
                                   AND detail ? 'counter_snapshot'""").fetchone()["n"]
            check(snap > 0, "汇总审计行带行计数器快照（%d 条）—— 缺口检测的依据" % snap)

        # I-11 保留策略执行器：既有策略是权威，工具只执行不发明
        import subprocess as _sp
        r = _sp.run([sys.executable, os.path.join(BASE, "ops", "retention.py"), "plan"],
                    capture_output=True, text=True, encoding="utf-8", cwd=BASE)
        out = (r.stdout or "") + (r.stderr or "")
        check(r.returncode == 0 and "rp_change_log" in out,
              "保留策略执行器可读既有策略（含 rp_change_log）")
        check("1825" in out,
              "沿用项目**既有**的保留期（change_log 60 个月 = 1825 天），"
              "而不是工具自己发明一个")
        r2 = _sp.run([sys.executable, os.path.join(BASE, "ops", "retention.py"),
                      "apply", "--table", "change_log"],
                     capture_output=True, text=True, encoding="utf-8", cwd=BASE)
        check(r2.returncode != 0 and "max-rows" in ((r2.stdout or "") + (r2.stderr or "")),
              "执行器拒绝「不给上限就删」—— 删除审计必须由人给出具体数字")
        # I-7 密码强度：新口令必须是 cost ≥ 12（原来 pgcrypto 默认 6）
        with psycopg.connect(_ADMIN_DSN, row_factory=dict_row, autocommit=True) as c:
            weak = c.execute("""SELECT count(*) AS n FROM mt.app_user
                                 WHERE mt.password_hash_cost(password_hash) < 12""").fetchone()["n"]
        check(weak == 0, "没有 bcrypt cost < 12 的账号（实测 %d 个）" % weak)

        # ===============================================================
        print("\n【H】运行健壮性：性能上界、导出安全、过载降级")
        # H1 聚合闸门必须能"只数一张表"。实测过的事故：不带参数时闸门会把全部
        #    89 张表数一遍（含 change_log 这类十万行级大表），而外层 WHERE **不会下推**
        #    —— 取 person 一张表的行数要 689ms，而直接数它只要 5ms。
        #    公开落地页要为 7 个指标各查一次 → 匿名打开一次首页约 4.8 秒数据库开销，
        #    而且是**公网**页面。所以这里把"代价"本身做成断言。
        import time as _t
        with psycopg.connect(portal.PORTAL_DSN, row_factory=dict_row, autocommit=True) as c:
            c.execute("SET ROLE mt_t0")
            t0 = _t.time()
            one = c.execute("SELECT n_rows FROM mt.public_counts('person')").fetchone()["n_rows"]
            ms_one = (_t.time() - t0) * 1000
            t0 = _t.time()
            n_all = len(c.execute("SELECT * FROM mt.public_counts()").fetchall())
            ms_all = (_t.time() - t0) * 1000
            c.execute("RESET ROLE")
        check(one == 123, "闸门按表查询返回正确的行数（person=%s）" % one)
        check(ms_one < 250 and ms_all > ms_one,
              "按表查询明显快于全量（单表 %.0fms vs 全量 %.0fms，共 %d 张表）—— "
              "这才让公开页敢用它" % (ms_one, ms_all, n_all))
        # 首页端到端时间上界（含渲染）：改前约 4.8 秒数据库开销
        t0 = _t.time()
        st, body = fetch("/")
        ms_page = (_t.time() - t0) * 1000
        check(st == 200 and ms_page < 3000,
              "匿名首页端到端 %.0fms（上界 3000ms）—— 数量走闸门后不再拖慢公网页面" % ms_page)

        # H2 CSV 公式注入：以 = + - @ 制表符开头的单元格必须变成纯文本
        #    （导出的是外部来源的原始文本，不由我们控制；Excel 打开会当公式执行）
        sample = [{"a": "=HYPERLINK(\"http://evil\",\"点我\")", "b": "@SUM(1:2)"},
                  {"a": "-2+3", "b": 12345}]
        out = portal.to_csv(["a", "b"], sample).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(out)))
        check(all(not str(v).startswith(("=", "+", "@", "\t", "\r"))
                  for v in rows[1]), "CSV 里没有以公式字符开头的单元格（%s）" % rows[1])
        check(str(rows[2][1]) == "12345" and "-2+3" in out,
              "数字未被误改、内容未被删除（只加文本前缀）")

        # H3 会话 cookie：本机 http 不加 Secure（加了浏览器不回传，登录会"成功但没生效"）；
        #    经反向代理走 https 时必须加（否则公网 cookie 可经明文泄露）
        #    用**真实账号**登录：第一版拿假账号测，登录失败 → 根本没有 Set-Cookie，
        #    于是断言在测"空字符串"（实测踩到：一条看起来在测安全的断言其实啥也没测）。
        email3, pw3 = _ensure_t3()

        def _login_headers(extra=None):
            req = urllib.request.Request(ROOT + "/login")
            req.data = urllib.parse.urlencode({"email": email3, "password": pw3}).encode()
            req.method = "POST"
            for k, v in (extra or {}).items():
                req.add_header(k, v)
            try:
                with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
                    return r.headers.get("Set-Cookie") or ""
            except urllib.error.HTTPError as e:
                return e.headers.get("Set-Cookie") or ""

        local_ck = _login_headers()
        proxied_ck = _login_headers({"X-Forwarded-Proto": "https",
                                     "Host": "example.trycloudflare.com"})
        check(bool(local_ck), "真实账号登录确实拿到了 Set-Cookie（否则下面的断言在测空串）")
        check("Secure" not in local_ck,
              "本机 http 的会话 cookie 不带 Secure（否则浏览器不回传）")
        check("Secure" in proxied_ck and "HttpOnly" in proxied_ck
              and "SameSite" in proxied_ck,
              "经 https 反向代理时会话 cookie 三个标记齐全（HttpOnly+SameSite+Secure）")

        # H4 过载降级：把并发闸临时缩小，用慢查询占满它，确认多出来的请求拿到
        #    **503 + Retry-After**，而不是 500、也不是挂死。
        #    为什么要测这条：并发闸的价值不在"正常时照旧"，而在**压力最大时**
        #    给出可退避的信号 —— 那条路径如果从没被触发过，就等于不存在。
        #    背景：max_connections=50，而每个请求最多开 3 条连接；
        #    ThreadingHTTPServer 是一请求一线程且无上限，不加闸就会被自己打满。
        old_sem, old_wait = portal._REQ_SEM, portal._REQ_WAIT_SECONDS
        portal._REQ_SEM = threading.BoundedSemaphore(2)
        portal._REQ_WAIT_SECONDS = 0.3
        try:
            slow = ROOT + "/sql?" + urllib.parse.urlencode({"q": "SELECT pg_sleep(1)"})

            def _hit(_i):
                try:
                    with urllib.request.urlopen(slow, timeout=60) as r:
                        r.read()
                        return r.status, r.headers.get("Retry-After")
                except urllib.error.HTTPError as e:
                    ra = e.headers.get("Retry-After")
                    e.read()
                    return e.code, ra
                except Exception as e:                     # noqa: BLE001
                    return type(e).__name__, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                codes = list(ex.map(_hit, range(8)))
            got = [c for c, _ in codes]
            check(503 in got, "并发闸触发过载通道（状态分布 %s）" % got)
            check(500 not in got, "过载时没有 500（过载不等于「服务器坏了」）")
            check(all(isinstance(c, int) for c in got),
                  "每个请求都拿到明确的 HTTP 结果，没有挂死或连接重置")
            check(all(ra for c, ra in codes if c == 503),
                  "所有 503 都带 Retry-After（调用方知道该退避多久）")
        finally:
            portal._REQ_SEM, portal._REQ_WAIT_SECONDS = old_sem, old_wait

        # H5 元数据缓存**必须有失效机制**。原来是无条件永久缓存，而门户是长时间
        #    运行的服务、**另一个进程**（开发者模式/迁移）会改结构 ——
        #    "结构页显示的是门户启动那一刻的样子"会让目录页的卖点（看到真实的样子）失效。
        m1 = portal.meta()
        check(portal.meta() is m1, "元数据在有效期内复用缓存（不为每个请求重读系统目录）")
        old_at = portal.META_AT
        try:
            portal.META_AT = _t.monotonic() - (portal.META_TTL_SECONDS + 60)
            m2 = portal.meta()
            check(m2 is not m1, "缓存过期后会重建（TTL=%ds）—— 结构变化不会永远看不到"
                  % portal.META_TTL_SECONDS)
            check(len(m2["rels"]) == len(m1["rels"]), "重建结果与原来一致（表数 %d）"
                  % len(m2["rels"]))
        finally:
            portal.META_AT = old_at
        # 显式刷新：必须是"真的重建"，所以断言返回的是**另一个对象**。
        # （第一版写成 `... or True` —— 那是恒真断言，等于没测；
        #   本项目把"测了个寂寞"列为要避免的反模式，不能自己犯。）
        before = portal.meta()
        after = portal.refresh_meta()
        check(after is not before and len(after["rels"]) == len(before["rels"]),
              "显式刷新 refresh_meta() 真的重建了元数据（%d 张表/视图）" % len(after["rels"]))

        # ===============================================================
        print("\n【G】登录限流：公开的登录入口必须防在线爆破（迁移 039）")
        probe = "throttle_probe_%s@local.test" % os.urandom(3).hex()
        codes = []
        for i in range(7):
            req = urllib.request.Request(ROOT + "/login")
            req.data = urllib.parse.urlencode({"email": probe,
                                               "password": "wrong-%d" % i}).encode()
            req.method = "POST"
            try:
                with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
                    codes.append(r.status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        check(429 not in codes[:4], "前几次错误口令不会被过早锁定（实得 %s）" % codes[:4])
        check(429 in codes, "连续错误口令最终触发锁定（HTTP 429，实得 %s）" % codes)
        # 锁定后即使换口令也必须被拒 —— 证明锁定期内**不比对口令**
        req = urllib.request.Request(ROOT + "/login")
        req.data = urllib.parse.urlencode({"email": probe, "password": "x"}).encode()
        req.method = "POST"
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
                last = r.status
        except urllib.error.HTTPError as e:
            last = e.code
        check(last == 429, "锁定后用任意口令仍被拒（HTTP %s）—— 不给计时侧信道" % last)
        # 数据层：门户读不到尝试流水（那是个人信息），但读得到锁定状态
        ok, _ = as_t0("SELECT * FROM mt.login_attempt LIMIT 1")
        check(not ok, "匿名读不到 login_attempt（登录痕迹属个人信息）")
        with psycopg.connect(portal.PORTAL_DSN, row_factory=dict_row, autocommit=True) as c:
            can = c.execute("SELECT has_table_privilege('mt_portal','mt.login_attempt',"
                            "'SELECT') AS x").fetchone()["x"]
            can_view = c.execute("SELECT has_table_privilege('mt_portal','mt.v_login_lockout',"
                                 "'SELECT') AS x").fetchone()["x"]
        # 计数必须用**运维连接** —— 门户角色读不到 login_attempt（这正是上面断言要证明的），
        # 用门户连接去 count 会直接 permission denied（第一版就是这么写的，实测报错）
        with psycopg.connect(_ADMIN_DSN, row_factory=dict_row, autocommit=True) as c:
            n = c.execute("SELECT count(*) AS n FROM mt.login_attempt WHERE email=%s",
                          (probe,)).fetchone()["n"]
        check(not can, "门户角色读不到 login_attempt（只存邮箱与时间，不存口令/IP）")
        check(can_view, "门户读得到 v_login_lockout（否则登录页没法告诉用户被锁了）")
        check(n >= 5, "限流尝试已留痕（%d 条）—— 但只存邮箱与时间" % n)

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
