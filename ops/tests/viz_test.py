# -*- coding: utf-8 -*-
"""
ops/tests/viz_test.py —— 可视化与分析层测试

这一层最容易犯的错不是"图画不出来"，而是**图在说谎**。所以断言集中在四件事：

  ① **口径一致**：同一个指标，质量门脚本、门户 /quality、可视化面板三处算出的
     数字必须完全相同。实测踩过——质量门 90.2%、可视化页 61.7%，
     差别只在分母（一个用 `IN (RT5..RT8)`，一个用 `NOT IN (RT5,RT6)`）。
     一致性必须由测试守住，而不是靠人去比对两个页面。
  ② **图忠实于数据**：最长的那根柱子必须最宽；计数必须等于行数。
  ③ **空数据画空状态**，不画空坐标系（后者会被读成"值接近 0"）。
  ④ **自助分析不引入注入面**：图表构建器的表名/列名走系统目录白名单 + Identifier 转义。

另有一条工程性断言：**页面里不能出现外部 JS/CDN 引用**——
图表是内联 SVG，拷走一个 HTML 就能看，这是本项目一贯的选择。

用法：python ops/tests/viz_test.py
"""
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))

import portal as P  # noqa: E402
import portal_viz as V  # noqa: E402
import charts as CH  # noqa: E402
import metrics as M  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

PASS, FAIL = H.PASS, H.FAIL
PORT = 8104
ROOT = "http://127.0.0.1:%d" % PORT
check = H.check


# 面板渲染用**明确的高等级会话**：本套测试回答的是「面板口径对不对」，
# 不是「某个等级能不能看」（后者由 ops/tests/access_test.py 负责）。
# 用匿名会话渲染会得到 403，那会把权限问题伪装成口径问题。
TEST_SESSION = P.Session(actor="viz_test", tier="T3", ok=True)


def conn():
    return H.connect(P.DSN)


COOKIE = None
TEST_EMAIL = "viz_test@local.test"
TEST_PW = "viz-test-password-4c19"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302：登录成功返回 302 + Set-Cookie，跟随会把 cookie 丢掉。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def login():
    """以 T3 账号登录，取回会话 cookie（账号自建自删）。"""
    global COOKIE
    with H.connect(P.DSN) as c:
        c.execute("SELECT mt.web_user_add(%s, %s, 'T3', '可视化测试账号')",
                  (TEST_EMAIL, TEST_PW))
        c.commit()
    req = urllib.request.Request(ROOT + "/login")
    req.data = urllib.parse.urlencode({"email": TEST_EMAIL, "password": TEST_PW}).encode()
    req.method = "POST"
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as e:
        sc = e.headers.get("Set-Cookie") or ""
    COOKIE = sc.split(";")[0]
    return COOKIE


def cleanup_login():
    """先子后父：web_session 引用 app_user。审计行也清掉，避免影响其它套件的计数。"""
    with H.connect(P.DSN) as c:
        c.execute("""DELETE FROM mt.web_session WHERE user_id IN
                       (SELECT user_id FROM mt.app_user WHERE email = %s)""", (TEST_EMAIL,))
        c.execute("DELETE FROM mt.app_user WHERE email = %s", (TEST_EMAIL,))
        c.execute("DELETE FROM mt.access_log WHERE actor = %s", (TEST_EMAIL,))
        c.commit()


def get(path, timeout=180):
    try:
        req = urllib.request.Request(ROOT + path)
        if COOKIE:
            req.add_header("Cookie", COOKIE)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main():
    P.meta()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), P.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 先登录：门户跑在受限角色上，匿名只能读 T0（这是**正确**行为）。
    # 本套测试要回答"图与口径对不对"，所以用一个明确的 T3 会话抓页面 ——
    # 否则权限不足会被误读成"图没画出来"。
    try:
        cleanup_login()
    except Exception:                                    # noqa: BLE001
        pass
    check(bool(login()), "可视化测试账号登录成功并拿到会话 cookie")

    try:
        # ===============================================================
        print("\n【T1】图表库自身")
        bad = CH.self_check()
        check(not bad, "charts.self_check() 通过（%d 个问题）" % len(bad))
        for b in bad:
            print("        - %s" % b)
        # 柱宽必须随数值单调——图不能骗人
        r = CH.bar_h([{"k": "a", "v": 100}, {"k": "b", "v": 10}], "k", "v")
        ws = [float(x) for x in re.findall(r'<rect x="\d+" y="[\d.]+" width="([\d.]+)"', r["svg"])]
        check(len(ws) >= 2 and ws[0] > ws[1] * 5, "柱宽忠实于数值（100 → %.0f，10 → %.0f）"
              % (ws[0], ws[1]) if len(ws) >= 2 else "柱宽断言")
        # Decimal 不能让它崩（psycopg 对 numeric 返回 Decimal）
        import decimal
        r2 = CH.stacked([{"l": "x", "p": decimal.Decimal("3"), "q": decimal.Decimal("7")}],
                        ["p", "q"], "l")
        check(not r2["empty"], "图表库对 Decimal 免疫（numeric 列的返回值）")

        # ===============================================================
        print("\n【T2】16 个可视化面板全部可渲染、可导 CSV")
        with P.db(TEST_SESSION) as c:
            ok, problems = 0, []
            for it in V.VIZ:
                try:
                    ch, rows = V.fetch(c, it)
                    cols, rws = V.csv_rows(c, it)
                    # 不变量（而不是我猜的数字）：非空图必须有计数，空图计数必须为 0
                    if bool(ch["rows"]) == bool(ch["empty"]):
                        problems.append("%s：rows=%s empty=%s 不自洽"
                                        % (it["id"], ch["rows"], ch["empty"]))
                    if cols and len(rws) != len(rows):
                        problems.append("%s：CSV 行数 %d ≠ 查询行数 %d"
                                        % (it["id"], len(rws), len(rows)))
                    ok += 1
                except Exception as e:                       # noqa: BLE001
                    problems.append("面板 %s 失败：%s" % (it["id"], e))
            check(not problems, "%d/%d 个面板渲染 + 导出成功" % (ok, len(V.VIZ)))
            for p in problems:
                print("        - %s" % p)
            # 每个面板都必须给出 SQL（口径可核）
            no_sql = [i["id"] for i in V.VIZ if not i.get("sql")]
            check(not no_sql, "每个面板都带 SQL（缺：%s）" % (no_sql or "无"))
            # 注册表完整性：id 唯一、分组有效
            ids = [i["id"] for i in V.VIZ]
            check(len(ids) == len(set(ids)), "面板 id 唯一（%d 个）" % len(ids))
            check(all(i["section"] in V.SECTIONS for i in V.VIZ),
                  "所有面板的分组都在 SECTIONS 里")

        # ===============================================================
        print("\n【T3】跨组件口径一致（这一层最要紧的断言）")
        with P.db(TEST_SESSION) as c:
            row = P.q(c, M.CONCEPT_COVERAGE_SQL, M.coverage_params())[0]
            cov_spec = M.coverage_pct(row)
            page = P.view_quality(c, {}).decode("utf-8")
            m_page = re.search(r"能力概念映射覆盖率</td><td class=\"n\"><b>([\d.]+)%", page)
            gate_item = [i for i in V.VIZ if i["id"] == "m_gate"][0]
            _ch, grows = V.fetch(c, gate_item)
            g = [x for x in grows if "覆盖率" in str(x["指标"])]
        out = subprocess.run([sys.executable,
                              os.path.join(BASE, "code", "gates", "jd_quality_gate.py")],
                             cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=300)
        txt = out.stdout.decode("utf-8", "replace")
        m_gate = re.search(r"能力概念映射覆盖率\s+实际=([\d.]+)%", txt)

        vals = {"规范口径(metrics.py)": cov_spec}
        if m_page:
            vals["门户 /quality 页面"] = float(m_page.group(1))
        if g:
            vals["可视化面板 m_gate"] = float(g[0]["实际"])
        if m_gate:
            vals["质量门脚本输出"] = float(m_gate.group(1))
        check(len(vals) >= 3, "采集到 %d 处取值：%s" % (len(vals), vals))
        check(len(set(vals.values())) == 1,
              "四处口径完全一致：%s" % vals)
        check(row["denom"] == 1530 and row["numer"] == 1380,
              "分母 %d = 要求总数 %d − 资格门槛 %d（口径正确，不是把所有要求都算进去）"
              % (row["denom"], row["total"], row["qualification"]))

        # ===============================================================
        print("\n【T4】/viz 页面（HTTP）")
        st, body = get("/viz")
        check(st == 200, "/viz 返回 200")
        check(body.count("<svg") >= len(V.VIZ) - 2,
              "页面内联了 %d 个 SVG（面板 %d 个）" % (body.count("<svg"), len(V.VIZ)))
        check("metric_definition" in body, "页面说明度量注册表（语义层）")
        check("零 JS" in body and "零 CDN" in body, "声明零 JS / 零 CDN")
        # 不能有外部脚本/样式/CDN 引用
        ext = re.findall(r'(?:src|href)="(https?://[^"]+)"', body)
        check(not ext, "页面无任何外部资源引用（发现：%s）" % (ext[:3] or "无"))
        n_metric = P.q1(conn(), "SELECT count(*) FROM metric_definition")
        check(n_metric >= len(V.VIZ),
              "度量注册表已写入 %d 行（≥ 面板数 %d）" % (n_metric, len(V.VIZ)))

        # ===============================================================
        print("\n【T5】图表构建器：用户/开发者自助分析")
        st, body = get("/viz/build")
        check(st == 200 and "图表构建器" in body, "/viz/build 返回 200")
        check("job_posting" in body and "维度" in body, "给出表与列的选择项")

        st, body = get("/viz/build?" + urllib.parse.urlencode(
            {"t": "job_posting", "dim": "job_family", "val": "", "agg": "count",
             "kind": "bar_h"}))
        check(st == 200 and "<svg" in body, "按岗位族计数 → 出图")
        check("你这次分析执行的 SQL" in body, "给出这次分析执行的 SQL")
        n_fam = P.q1(conn(), "SELECT count(DISTINCT job_family) FROM job_posting "
                             "WHERE job_family IS NOT NULL")
        check(body.count("<rect") >= n_fam, "柱子数 ≥ 不同岗位族数（%d）" % n_fam)

        st, body = get("/viz/build?" + urllib.parse.urlencode(
            {"t": "occupation", "dim": "family", "val": "medical_reliance",
             "agg": "avg"}))
        check(st == 200 and "<svg" in body, "按岗位族求医学依赖度均值 → 出图")

        # 注入面：非法表名/列名必须被拒
        st, body = get("/viz/build?t=" + urllib.parse.quote("person; DROP TABLE x") +
                       "&dim=person_id")
        check(st == 200 or st == 404, "非法表名被拒（状态 %s）" % st)
        check("DROP" not in body or "未知的表" in body,
              "非法表名没有进入 SQL")
        st, body = get("/viz/build?t=job_posting&dim=" +
                       urllib.parse.quote("job_id) ; DROP TABLE person --"))
        check("没有列" in body or "未知" in body, "非法列名被拒")
        alive = P.q1(conn(), "SELECT count(*) FROM information_schema.tables "
                             "WHERE table_schema='mt' AND table_name='person'")
        check(alive == 1, "注入尝试后 person 表仍在")

        st, csv_text = get("/viz/build.csv?" + urllib.parse.urlencode(
            {"t": "job_posting", "dim": "job_family", "agg": "count"}))
        check(st == 200 and "维度" in csv_text and "度量" in csv_text,
              "构建器结果可导出 CSV")

        # ===============================================================
        print("\n【T6】分析页自动出图（分析与可视化是同一件事）")
        with P.db(TEST_SESSION) as c:
            for aid, _t, _d, sql in P.ANALYSES:
                rows = P.analysis_rows(c, sql)
                cols = list(rows[0].keys()) if rows else []
                ch = P.auto_chart(cols, rows)
                if len(cols) >= 2:
                    assert ch == "" or "<svg" in ch, aid
            # 至少有几个分析能自动出图，否则这个能力等于没有
            n_chart = 0
            for aid, _t, _d, sql in P.ANALYSES:
                rows = P.analysis_rows(c, sql)
                cols = list(rows[0].keys()) if rows else []
                if "<svg" in P.auto_chart(cols, rows):
                    n_chart += 1
        check(n_chart >= 4, "%d/%d 个预置分析能自动出图" % (n_chart, len(P.ANALYSES)))
        st, body = get("/analyze/a1")
        check(st == 200 and "<svg" in body and "这张表长什么样" in body,
              "/analyze/a1 页面上有自动图")
        check("图是入口，下面的表是依据" in body,
              "页面说明图与表的关系（图是入口，表是依据）")

        # ===============================================================
        print("\n【T7】空数据画空状态，不画空坐标系")
        with P.db(TEST_SESSION) as c:
            for f, args in ((CH.bar_h, ([], "k", "v")),
                            (CH.line, ([], [])),
                            (CH.heatmap, ([], [])),
                            (CH.scatter, ([], "x", "y")),
                            (CH.stacked, ([], [], "l")),
                            (CH.target_bar, ([], "k", "v", "t")),
                            (CH.histogram, ([],))):
                r = f(*args)
                if not r.get("empty"):
                    check(False, "%s 空数据应返回 empty" % f.__name__)
        check(True, "7 种图表在空数据下都返回明确的空状态")

    finally:
        srv.shutdown()
        srv.server_close()
        # 构造了多少就清多少（账号 + 会话 + 本次自产的审计行）
        cleanup_login()

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
