# -*- coding: utf-8 -*-
"""
ops/tests/access_test.py —— 字段级访问控制是否**真的在拦**

这一套的存在理由：访问分级在本项目里长期"是标签不是拦截"（README 自己写过）。
所以这里的每一条断言都必须**让数据库来回答**，而不是问页面或问配置表：
  · 断言"读不到"，就真的去读一次，必须收到 permission denied；
  · 断言"读得到"，就真的把值取回来；
  · 断言"数量公开但字段受限"，就必须同时验证 `count(*)` 成功、`count(受限列)` 失败。

反面用例（"如果实现坏了，这条一定会失败"）是刻意设计的：
  · 一个没有任何表权限的登录角色，**不设等级就应该什么都读不到** ——
    如果哪天有人图省事给 mt_portal 直接 GRANT SELECT，这条会立刻红；
  · `SELECT *` 必须被拒 —— 它挡住的是"应用层忘了裁剪列"这类最常见的事故；
  · 策略刷新必须**先收回再授权** —— 删掉一条策略后权限必须真的消失。

用法：python ops/tests/access_test.py
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")
PORTAL = ("host=127.0.0.1 port=55432 dbname=medtalent user=mt_portal "
          "connect_timeout=5 options='-c search_path=mt,public'")


def try_read(role, sql, actor="test@access_test", tier=None):
    """以 mt_portal 身份、可选 SET LOCAL ROLE，执行一条查询。
    返回 (ok, payload_or_error)。全程在一个事务里，结束回滚 —— 不留痕。"""
    try:
        with psycopg.connect(PORTAL, row_factory=dict_row) as c:
            with c.cursor() as cur:
                cur.execute("BEGIN")
                if actor:
                    cur.execute("SELECT set_config('mt.actor', %s, true)", (actor,))
                if tier:
                    cur.execute("SELECT set_config('mt.tier', %s, true)", (tier,))
                if role:
                    cur.execute("SET LOCAL ROLE " + role)
                cur.execute(sql)
                rows = cur.fetchall()
                cur.execute("ROLLBACK")
                return True, rows
    except psycopg.Error as e:
        return False, str(e).splitlines()[0]


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        # ===============================================================
        print("\n【A1】角色与阶梯：这是「权限」的载体，不是命名")
        roles = {r["rolname"]: r for r in c.execute(
            """SELECT rolname, rolsuper, rolcanlogin, rolbypassrls, rolinherit
                 FROM pg_roles WHERE rolname IN ('mt_portal','mt_t0','mt_t1','mt_t2','mt_t3')""")}
        check(set(roles) == {"mt_portal", "mt_t0", "mt_t1", "mt_t2", "mt_t3"},
              "四个等级角色 + 一个登录角色齐备")
        check(not roles["mt_portal"]["rolsuper"] and not roles["mt_portal"]["rolbypassrls"],
              "mt_portal 不是超级用户、不绕过 RLS —— 否则权限设计全是装饰")
        check(not roles["mt_portal"]["rolinherit"],
              "mt_portal 是 NOINHERIT：必须显式 SET ROLE 才拿到权限（默认拒绝）")
        tiers = c.execute("SELECT tier, rank, is_public FROM access_tier ORDER BY rank").fetchall()
        check([t["tier"] for t in tiers] == ["T0", "T1", "T2", "T3", "X"],
              "等级阶梯有序且含 X（禁止）：%s" % [t["tier"] for t in tiers])
        check(c.execute("SELECT access_rank('T2') > access_rank('T0') AS r").fetchone()["r"],
              "等级比较用 rank（T2 > T0）")
        check(c.execute("SELECT access_rank('不认识') AS r").fetchone()["r"] == -1,
              "未知等级返回 -1（比 T0 还低）→ 写错的等级是拒绝而不是放行")

        # ===============================================================
        print("\n【A2】不设等级：什么都读不到（默认拒绝）")
        ok, err = try_read(None, "SELECT person_id FROM mt.person LIMIT 1")
        check(not ok, "未 SET ROLE 时读 person 被拒：%s" % err[:60])
        ok, err = try_read(None, "SELECT count(*) AS n FROM mt.person")
        check(not ok, "未 SET ROLE 时连 count(*) 也被拒")
        ok, rows = try_read("mt_t0", "SELECT current_user AS u")
        check(ok and rows[0]["u"] == "mt_t0", "SET LOCAL ROLE 生效（current_user = mt_t0）")

        # ===============================================================
        print("\n【A3】数量公开、字段受限（需求原话：编号、年龄可公开）")
        # 期望值**从策略本身推导**，不写死"哪个列应该是 T0"：
        # 策略是数据，测试对着数据断言，才不会因为策略调整而变成假失败。
        def one_col(tier):
            return c.execute("""SELECT table_name, column_name FROM column_policy
                                 WHERE min_tier = %s AND table_name = 'person'
                                 ORDER BY column_name LIMIT 1""", (tier,)).fetchone()

        ok, rows = try_read("mt_t0", "SELECT count(*) AS n FROM mt.person")
        check(ok and rows[0]["n"] > 0, "T0 能算总人数（不用读任何受限列）：%s"
              % (rows[0]["n"] if ok else "-"))
        c0 = one_col("T0")
        if c0:
            ok, _ = try_read("mt_t0", "SELECT %s FROM mt.person ORDER BY 1 LIMIT 2"
                             % c0["column_name"])
            check(ok, "T0 能读策略标为 T0 的列 person.%s（编号类应在此级）"
                  % c0["column_name"])
        ok, err = try_read("mt_t0", "SELECT * FROM mt.person LIMIT 1")
        check(not ok, "T0 读 `SELECT *` 被拒 —— 应用忘了裁剪列也漏不出去：%s" % err[:52])
        # 找一个 T3 列（姓名/证件哈希一类）验证低等级读不到、高等级读得到
        t3 = c.execute("""SELECT table_name, column_name FROM column_policy
                           WHERE min_tier='T3' AND table_name IN ('person_pii','person')
                           ORDER BY table_name, column_name LIMIT 1""").fetchone()
        if t3:
            ok, err = try_read("mt_t0", "SELECT %s FROM mt.%s LIMIT 1"
                               % (t3["column_name"], t3["table_name"]))
            check(not ok, "T0 读 T3 列 %s.%s 被拒" % (t3["table_name"], t3["column_name"]))
            ok, err = try_read("mt_t3", "SELECT %s FROM mt.%s LIMIT 1"
                               % (t3["column_name"], t3["table_name"]))
            check(ok, "T3 读同一列成功（等级确实在起作用）%s" % ("" if ok else err[:60]))
        else:
            check(False, "column_policy 里没有 T3 列 —— 策略生成可能失败")

        # ===============================================================
        print("\n【A4】等级阶梯是单调的：高等级看得到低等级的一切")
        # 逐等级验证：策略标为 Tn 的列，只有 mt_tn 及以上能读。
        # 这条断言把"阶梯"从声明变成可验证的性质 —— 第一版这里抓到过
        # "mt_t1 列权限掉到 180"的严重 bug（授权被一段 REVOKE 清空）。
        for tier, role in (("T0", "mt_t0"), ("T1", "mt_t1"),
                           ("T2", "mt_t2"), ("T3", "mt_t3")):
            col = c.execute("""SELECT table_name, column_name FROM column_policy
                                WHERE min_tier = %s ORDER BY table_name, column_name
                                LIMIT 1""", (tier,)).fetchone()
            if not col:
                continue
            ok_hi, err_hi = try_read(role, "SELECT %s FROM mt.%s LIMIT 1"
                                     % (col["column_name"], col["table_name"]))
            check(ok_hi, "%s 能读策略标为 %s 的列 %s.%s%s"
                  % (role, tier, col["table_name"], col["column_name"],
                     "" if ok_hi else " → " + err_hi[:56]))
        # 低等级读不到更高等级的列
        c1 = one_col("T1")
        if c1:
            ok, _ = try_read("mt_t0", "SELECT %s FROM mt.person LIMIT 1" % c1["column_name"])
            check(not ok, "T0 读不到 T1 列 person.%s（阶梯确实在挡）" % c1["column_name"])
        cnt = {}
        for t in ("mt_t0", "mt_t1", "mt_t2", "mt_t3"):
            cnt[t] = c.execute("""SELECT count(*) AS n FROM information_schema.column_privileges
                                   WHERE grantee = %s AND privilege_type = 'SELECT'""",
                               (t,)).fetchone()["n"]
        check(cnt["mt_t0"] < cnt["mt_t1"] < cnt["mt_t2"] < cnt["mt_t3"],
              "四个等级的列权限数量严格递增：%s" % cnt)

        # ===============================================================
        print("\n【A5】访问日志：读也要留痕，且身份取「实际生效的角色」")
        n0 = c.execute("SELECT count(*) AS n FROM access_log").fetchone()["n"]
        try:
            with psycopg.connect(PORTAL, row_factory=dict_row) as pc, pc.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SELECT set_config('mt.actor', %s, true)", ("u_1001@mini",))
                cur.execute("SELECT set_config('mt.tier', %s, true)", ("T1",))
                cur.execute("SET LOCAL ROLE mt_t1")
                cur.execute("SELECT count(*) AS n FROM mt.person")
                n = cur.fetchone()["n"]
                cur.execute("SELECT mt.log_access('view', 'talent_list', 'T1', %s, '注册用户浏览人才库')", (n,))
                cur.execute("COMMIT")
            check(True, "以 mt_portal + T1 身份读并记了一条日志")
        except psycopg.Error as e:
            check(False, "记日志失败：%s" % str(e).splitlines()[0][:70])
        n1 = c.execute("SELECT count(*) AS n FROM access_log").fetchone()["n"]
        check(n1 == n0 + 1, "access_log 从 %d 增到 %d（真的写进去了）" % (n0, n1))
        row = c.execute("""SELECT actor, actor_role, action, target, access_tier, row_count
                             FROM access_log ORDER BY access_id DESC LIMIT 1""").fetchone()
        if row:
            check(row["actor"] == "u_1001@mini",
                  "日志记下了操作者（actor=%s）" % row["actor"])
            check(row["actor_role"] == "mt_t1",
                  "日志记的是**实际生效的角色**（actor_role=%s）——声明与实权不一致时看得出来"
                  % row["actor_role"])
            check(row["row_count"] and row["row_count"] > 0,
                  "日志记下了读到多少行（row_count=%s）" % row["row_count"])
        # 清理这条测试日志
        c.execute("DELETE FROM access_log WHERE actor = 'u_1001@mini'")
        c.commit()

        # ===============================================================
        print("\n【A6】反面用例：把授权收掉，权限必须真的消失（只加不减是常见配置错误）")
        before = c.execute("""SELECT count(*) AS n FROM information_schema.column_privileges
                               WHERE grantee='mt_t3' AND privilege_type='SELECT'""").fetchone()["n"]
        c.execute("REVOKE ALL ON mt.occupation FROM mt_t3")
        c.commit()
        ok, err = try_read("mt_t3", "SELECT * FROM mt.occupation LIMIT 1")
        check(not ok, "手工收回后 T3 立刻读不到（证明拦截来自数据库授权本身）")
        c.execute("SELECT mt.apply_column_grants()")
        c.commit()
        after = c.execute("""SELECT count(*) AS n FROM information_schema.column_privileges
                              WHERE grantee='mt_t3' AND privilege_type='SELECT'""").fetchone()["n"]
        check(after == before, "apply_column_grants() 重放后权限恢复原样（%d → %d）"
              % (before, after))
        ok, err = try_read("mt_t3", "SELECT * FROM mt.occupation LIMIT 1")
        check(ok, "重放后又能读（授权确实由策略重新算出来）")

        # ===============================================================
        print("\n【A7】策略的成色：多少有字典依据，多少只是列名约定")
        cov = c.execute("""SELECT sum(by_field_catalog) AS fc, sum(by_access_policy) AS ap,
                                  sum(by_pattern) AS pat, sum(by_default) AS def,
                                  count(*) AS tables, sum(n_columns) AS cols
                             FROM v_column_policy_coverage""").fetchone()
        # psycopg 把 SUM() 返回成 Decimal —— 算术前必须显式转 float
        # （这个项目此前已经在图表层踩过一次 Decimal 的坑）
        fc, ap_, pat, dfn = (int(cov["fc"]), int(cov["ap"]), int(cov["pat"]), int(cov["def"]))
        cols = int(cov["cols"])
        check(cols == fc + ap_ + pat + dfn,
              "每一列都有来源归属（%d 列 = 字典 %d + 策略 %d + 模式 %d + 默认 %d）"
              % (cols, fc, ap_, pat, dfn))
        check(fc + ap_ > 0,
              "至少有一部分策略有字典/人工依据（字典 %d、人工策略 %d）" % (fc, ap_))
        # 诚实指标：约定占比过高说明字典没跟上，这是要补的信号而不是可忽略的细节
        pct = 100.0 * (pat + dfn) / max(cols, 1)
        print("  [信息] 非字典依据占比 %.1f%%（%d/%d 列）—— 这个数字高说明该补字典了"
              % (pct, pat + dfn, cols))

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
