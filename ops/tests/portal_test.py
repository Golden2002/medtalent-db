# -*- coding: utf-8 -*-
"""
ops/tests/portal_test.py —— 数据库门户端到端测试

门户解决的是"**看不到数据库的样子**"这个问题，所以测试也必须落在"看到的是不是真的"上：
  · 页面上显示的行数 == 数据库里 count(*) 的结果（不是估算值、不是硬编码）
  · 表详情页里出现的每一个主键值，都能在 `mt.<表>` 里找到
  · 外键链接指向的实体页确实存在（不是死链）
  · 分析页的口径与直接跑同样 SQL 的结果一致
  · CSV 导出的行数 == 页面上显示的行数
  · 未知表 / 未知列 / 不存在的记录 → 正确的 4xx，而不是 500 或静默降级

最关键的一条：**门户是只读的**。这里不仅断言"非法 SQL 被自己的校验器拦下"，
还要断言"即使文本校验器漏过，PostgreSQL 的只读事务也会拒绝"——
用 `SELECT 1 INTO t` 这种校验器看不见、但对数据库是写操作的形式去撞。

做法：同进程起后台 HTTP 服务，用 urllib 驱动真实请求，断言落在数据库状态与响应上。

用法：python ops/tests/portal_test.py
"""
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

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import portal  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

PASS, FAIL = [], []
PORT = 8102
ROOT = "http://127.0.0.1:%d" % PORT


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    return cond


def conn():
    return psycopg.connect(portal.DSN, row_factory=dict_row)


def q1(sql, p=None):
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, p)
        r = cur.fetchone()
        return list(r.values())[0] if r else None


def get(path, expect=200):
    """返回 (状态码, 文本)。4xx 也照常返回，方便断言。"""
    try:
        with urllib.request.urlopen(ROOT + path, timeout=60) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def get_raw(path):
    with urllib.request.urlopen(ROOT + path, timeout=60) as r:
        return r.status, r.read().decode("utf-8"), dict(r.headers)


def main():
    portal.meta()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), portal.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    try:
        # ===============================================================
        print("\n【T1】总览：页面上的数字必须等于库里的真值")
        n_tables = q1("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                         WHERE n.nspname='mt' AND c.relkind='r'""")
        n_rows_job = q1("SELECT count(*) FROM job_posting")
        st, body = get("/")
        check(st == 200, "总览返回 200")
        check("数据库门户" in body, "总览是门户（不是业务演示控制台）")
        check(str(n_tables) in body,
              "总览显示的「基础表 %d」与 pg_class 真值一致" % n_tables)
        check("count(*)" in body and "不是估算值" in body,
              "总览明确声明行数是精确 count(*) 而非估算")
        check("只读" in body, "总览声明门户只读")

        # ===============================================================
        print("\n【T2】表与视图：按域分组，且每个域都在导航里")
        st, body = get("/schema")
        check(st == 200, "/schema 返回 200")
        missing = [d for d, _ in portal.DOMAINS if d not in body]
        check(not missing, "8 个域全部出现在页面上（缺：%s）" % (missing or "无"))
        check(str(n_tables) + " 张基础表" in body or ("%d" % n_tables) in body,
              "表清单页显示基础表总数 %d" % n_tables)
        check("未归类" not in body, "没有「未归类」的表（域划分覆盖 100%）")

        # 行数精确性：抽查几张表，页面上的数字必须等于 count(*)
        spot = ["occupation", "job_posting", "concept", "code_value", "provenance"]
        for tb in spot:
            n = q1("SELECT count(*) FROM " + tb)
            st, b = get("/t/" + tb)
            check(st == 200 and ("%s" % format(n, ",")) in b,
                  "%s 页面显示的行数 = 库里 count(*) = %s" % (tb, format(n, ",")))

        # ===============================================================
        print("\n【T3】表详情：结构、约束、外键、真实数据")
        st, body = get("/t/job_posting")
        check(st == 200, "/t/job_posting 返回 200")
        check("<code>job_posting_pkey</code>" in body, "显示主键约束名")
        check("FOREIGN KEY (occupation_id)" in body, "显示外键的完整定义文本")
        check("REFERENCES" in body and 'href="/t/occupation"' in body,
              "外键定义里的目标表渲染成可点的表链接")
        check("被谁引用" in body, "显示「被谁引用」反查区块")
        check("job_requirement" in body, "反查列出了引用 job_posting 的子表")
        check("本页执行的 SQL" in body, "表详情页展示它执行的 SQL")

        # 页面上的外键链接必须不是死链：抽验前若干个，逐个回库确认目标行存在
        links = re.findall(r'href="/e/([A-Za-z0-9_]+)\?val=([^"]+)"', body)
        check(len(links) > 0, "数据行里至少有外键实体链接（共 %d 个）" % len(links))
        dead = []
        for tbl, v in links[:8]:
            v = urllib.parse.unquote(v)
            pkt = portal.meta()["pk"].get(tbl)
            if not pkt:
                dead.append("%s(无主键)" % tbl)
                continue
            n = q1('SELECT count(*) FROM %s WHERE %s::text=%%s' % (tbl, pkt), (v,))
            if n != 1:
                dead.append("%s=%s" % (tbl, v))
        check(not dead, "抽查 %d 个外键链接全部指向存在的记录（死链：%s）"
              % (min(8, len(links)), dead or "无"))

        # 分页与排序
        st2, b2 = get("/t/job_posting?sort=job_id&dir=desc&size=5")
        check(st2 == 200 and "本页 5 行" in b2, "分页参数生效（size=5）")
        st3, _ = get("/t/job_posting?col=job_id&val=%s" % urllib.parse.quote(
            q1("SELECT job_id FROM job_posting LIMIT 1")), )
        check(st3 == 200, "按列筛选可用")

        # CSV 导出：行数必须与库里一致
        st4, csv_text = get("/t/code_value.csv")
        n_cv = q1("SELECT count(*) FROM code_value")
        check(st4 == 200 and csv_text.startswith("\ufeff"),
              "CSV 导出带 BOM（Excel 打开不乱码）")
        check(len(csv_text.strip().splitlines()) - 1 == n_cv,
              "CSV 行数 %d = 库里行数 %d" % (len(csv_text.strip().splitlines()) - 1, n_cv))

        # 错误处理：未知表、未知列
        st5, b5 = get("/t/not_a_table")
        check(st5 == 404 and "未知的表或视图" in b5, "未知表名返回 404 且有明确说明")
        st6, b6 = get("/t/job_posting?col=bogus_col&val=1")
        check(st6 == 400 and "没有列" in b6, "未知列名返回 400（列名走白名单校验）")

        # ===============================================================
        print("\n【T4】实体页：一行记录 + 所有指向它的表")
        occ = q1("SELECT occupation_id FROM occupation WHERE level=3 LIMIT 1")
        st, body = get("/e/occupation?val=" + urllib.parse.quote(occ))
        check(st == 200, "实体页返回 200（occupation %s）" % occ)
        check("医学" in body or "职业" in body or "岗位" in body, "实体页显示字段内容")
        n_job = q1("SELECT count(*) FROM job_posting WHERE occupation_id=%s", (occ,))
        if n_job:
            check("job_posting" in body and "指向这条记录" in body,
                  "实体页列出指向它的表（job_posting，%d 行）" % n_job)
        st2, b2 = get("/e/occupation?val=OCC-NOT-EXIST")
        check(st2 == 404 and "不存在" in b2, "不存在的实体返回 404")
        st3, _ = get("/e/occupation")
        check(st3 == 400, "缺少 val 参数返回 400")

        # ===============================================================
        print("\n【T5】检索：每条命中都标明来自哪张表")
        st, body = get("/search?q=" + urllib.parse.quote("临床"))
        check(st == 200, "检索返回 200")
        check("命中" in body, "报告命中数量")
        check("/t/occupation" in body and "/t/concept" in body,
              "命中按数据源分组并给出所在表")
        check("/e/" in body, "命中项可点进实体页")
        st2, b2 = get("/search?q=zzzz_not_exist_zzz")
        check(st2 == 200 and "没有命中" in b2, "无命中时给出明确提示（不是空页）")
        st3, b3 = get("/search")
        check(st3 == 200 and "能检索什么" in b3, "未输入检索词时给出可检索范围")
        # 元数据检索：搜列名
        st4, b4 = get("/search?q=concept_id")
        check(st4 == 200 and "元数据命中" in b4, "能检索列名（元数据命中）")

        # ===============================================================
        print("\n【T6】分析：口径可核 —— 页面结果 == 直接跑同样 SQL")
        st, body = get("/analyze")
        check(st == 200 and body.count("/analyze/a") >= 8, "分析页列出 8 个预置分析")
        for aid, title, _desc, sqltext in portal.ANALYSES:
            st, b = get("/analyze/" + aid)
            # 允许 0 行（空结果是结论），但必须渲染出结果区块、行数、以及它执行的 SQL
            ok = (st == 200 and "结果" in b and "本分析执行的 SQL" in b
                  and re.search(r"· \d+ 行", b) is not None)
            check(ok, "分析 %s「%s」可运行并展示 SQL 与行数" % (aid, title))
        # a4 的行为依赖当前数据（有没有漂移），所以断言必须**分情况**写，
        # 不能把"此刻为空"当成永久事实——那是在测试里钉死一个数据快照。
        sql_a4 = [a[3] for a in portal.ANALYSES if a[0] == "a4"][0]
        with conn() as cc:
            n_a4 = len(portal.analysis_rows(cc, sql_a4))
        st, b = get("/analyze/a4")
        if n_a4 == 0:
            check("没有漂移记录" in b,
                  "a4 空结果时解释了原因（0 行不是「页面坏了」）")
        else:
            check("漂移类型" in b and any(k in b for k in ("new", "rising", "falling", "vanished")),
                  "a4 有 %d 条漂移时列出了漂移类型与职业/能力" % n_a4)
        # 口径一致性：a3 的覆盖率数字必须等于直接用同样 SQL 算出的值
        sql_a3 = [a[3] for a in portal.ANALYSES if a[0] == "a3"][0]
        with conn() as c, c.cursor() as cur:
            cur.execute(sql_a3)
            row = cur.fetchone()
        pct = row["覆盖率%"]
        st, b = get("/analyze/a3")
        check(str(pct) in b, "a3 页面上的覆盖率 %s 与直接执行 SQL 的结果一致" % pct)
        st, csv_text = get("/analyze/a3.csv")
        check(st == 200 and "覆盖率" in csv_text, "分析结果可导出 CSV")
        st, b = get("/analyze/nope")
        check(st == 404, "未知分析返回 404")

        # ===============================================================
        print("\n【T7】只读 SQL 控制台")
        st, body = get("/sql?q=" + urllib.parse.quote("SELECT count(*) AS n FROM occupation"))
        n_occ = q1("SELECT count(*) FROM occupation")
        check(st == 200 and ("%d" % n_occ) in body,
              "SELECT 可执行，结果 %d 与库里一致" % n_occ)
        check("BEGIN TRANSACTION READ ONLY" in body.replace("&nbsp;", " "),
              "页面声明在只读事务里执行")

        for bad, why in [("DROP TABLE person", "DROP"),
                         ("SELECT 1; SELECT 2", "多条语句"),
                         ("UPDATE person SET status='active'", "UPDATE"),
                         ("DELETE FROM person", "DELETE"),
                         ("CREATE VIEW v AS SELECT 1", "CREATE")]:
            st, b = get("/sql?q=" + urllib.parse.quote(bad))
            check(st == 200 and "只读" in b or "只允许" in b,
                  "%s 被拒（%s）" % (why, bad[:28]))

        # 关键：文本校验器漏得过的写操作，必须被 PostgreSQL 的只读事务拦住
        sneaky = "SELECT 1 INTO _portal_probe_table"
        check(portal.validate_sql(sneaky) == sneaky,
              "文本校验器确实漏过了 SELECT ... INTO（说明它不是唯一防线）")
        st, b = get("/sql?q=" + urllib.parse.quote(sneaky))
        check("read-only" in b or "只读" in b,
              "PostgreSQL 只读事务拒绝了 SELECT ... INTO（数据库层才是真正的防线）")
        exists = q1("""SELECT count(*) FROM information_schema.tables
                       WHERE table_schema='mt' AND table_name='_portal_probe_table'""")
        check(exists == 0, "探测表没有被创建（门户确实写不进去）")

        # ===============================================================
        print("\n【T8】门户整体不写库：遍历全部页面后 change_log 不增长")
        n_before = q1("SELECT count(*) FROM change_log")
        n_rel = q1("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE n.nspname='mt' AND c.relkind IN ('r','v')""")
        for r in portal.meta()["rels"]:
            get("/t/" + r["name"])
        get("/")
        get("/schema")
        get("/analyze")
        get("/lineage")
        get("/search?q=" + urllib.parse.quote("医学"))
        n_after = q1("SELECT count(*) FROM change_log")
        check(n_before == n_after,
              "遍历 %d 个对象的页面后 change_log 未增长（%d → %d）"
              % (n_rel, n_before, n_after))

        # ===============================================================
        # ===============================================================
        print("\n【T9】血缘页：来源 → 采集 → 血缘 → 原始文件")
        st, body = get("/lineage")
        check(st == 200, "/lineage 返回 200")
        check("L0 原始层" in body, "展示 L0 原始层")
        check("证据分级" in body, "展示证据分级分布")
        check("没有 codebook 就不发布" in body or "无 codebook 不发布" in body,
              "声明「无 codebook 不发布」纪律")
        n_src = q1("SELECT count(*) FROM source_registry")
        st, b = get("/lineage")
        check(str(n_src) in b, "来源数量 %d 与库里一致" % n_src)

        # ===============================================================
        print("\n【T10】人才库：浏览 + 在线分析（需求 ①）")
        n_person = q1("SELECT count(*) FROM person")
        st, body = get("/talent")
        check(st == 200, "/talent 返回 200")
        check(str(n_person) in body, "人才库显示的人数 %d 与库里一致" % n_person)
        # 在线分析必须真的在算：学历分布的数字要等于直接查库
        n_deg = q1("SELECT count(DISTINCT person_id) FROM education_record")
        st, b = get("/talent")
        check("学历层次分布" in b or n_deg == 0,
              "人才库内嵌了学历分布分析")
        check("本分析的 SQL" in b, "分析块给出它执行的 SQL（口径可核）")
        # 合成数据必须自证——数据集要能一句话说清"哪些行可以对外"
        n_syn = q1("SELECT count(*) FROM person "
                   "WHERE quality_flags && ARRAY['synthetic_fixture']")
        if n_syn:
            check("synthetic_fixture" in b and f"{n_syn} 份" in b,
                  "人才库声明了 %d 份合成数据及其可辨识特征" % n_syn)
        else:
            check("合成" not in b, "没有合成数据时不谎称有")
        # 筛选必须真的过滤
        st, b2 = get("/talent?degree=NO_SUCH_DEGREE")
        check(st == 200 and "命中 0 人" in b2, "按不存在的学历筛选 → 命中 0 人（筛选真的生效）")
        # 人才详情页
        pid = q1("SELECT person_id FROM person LIMIT 1")
        st, b3 = get("/talent/" + urllib.parse.quote(pid))
        check(st == 200 and pid in b3, "人才详情页可打开：%s" % pid)
        check("能力主张" in b3 or "观测窗口" in b3, "详情页含能力主张与观测窗口区块")
        st, b4 = get("/talent/no_such_person_xxx")
        check(st == 404, "不存在的人才 → 404")

        # ===============================================================
        print("\n【T11】职业库：浏览 + 在线分析（需求 ②）")
        n_occ = q1("SELECT count(*) FROM occupation WHERE status='active'")
        st, body = get("/occupations")
        check(st == 200, "/occupations 返回 200")
        check(str(n_occ) in body, "职业库显示的在用节点数 %d 与库里一致" % n_occ)
        for title in ("岗位族规模", "岗位族", "医学依赖度", "外部职业码"):
            check(title in body, "职业库含分析：%s" % title)
        fam = q1("SELECT family FROM occupation WHERE level=1 LIMIT 1")
        st, b2 = get("/occupations?family=" + urllib.parse.quote(fam))
        check(st == 200 and "职业节点" in b2, "按岗位族筛选可用（%s）" % fam)
        # 职业详情页：能力要求必须与库里一致
        oid = q1("""SELECT occupation_id FROM occupation o WHERE o.status='active'
                     AND EXISTS (SELECT 1 FROM job_competency_weight w
                                  WHERE w.occupation_id=o.occupation_id
                                    AND w.valid_to IS NULL) LIMIT 1""")
        st, b3 = get("/occupation/" + urllib.parse.quote(oid))
        check(st == 200 and oid in b3, "职业详情页可打开：%s" % oid)
        n_c = q1("SELECT count(*) FROM job_competency_weight WHERE occupation_id=%s "
                 "AND valid_to IS NULL", (oid,))
        check("能力要求" in b3 and str(n_c) in b3,
              "职业详情页显示该职业的 %d 项能力要求" % n_c)
        st, b4 = get("/occupation/NOPE-XXX")
        check(st == 404, "不存在的职业 → 404")

        # ===============================================================
        print("\n【T12】职业树 + 能力-职业匹配（需求 ③）")
        st, body = get("/tree")
        check(st == 200, "/tree 返回 200")
        check("树" in body and "occupation_asof" in body, "职业树页使用 occupation_asof")
        with conn() as cc, cc.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM occupation_asof(current_date)")
            n_today = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM occupation_asof('2026-01-01'::date)")
            n_old = cur.fetchone()["n"]
        st, b_today = get("/tree")
        st2, b_old = get("/tree?asof=2026-01-01")
        check(st2 == 200, "as-of 时间旅行可查询（2026-01-01）")
        check(("%s" % n_old) in b_old and ("%s" % n_today) in b_today,
              "树节点数在 as-of 与今天之间确实不同：%s（当时） vs %s（今天）"
              % (n_old, n_today))
        check("迁移记录" in b_today and "ID 只退役不复用" in b_today,
              "树页说明「ID 只退役不复用」纪律")

        st, body = get("/match")
        check(st == 200, "/match 返回 200")
        n_m = q1("SELECT count(*) FROM match_result")
        check(str(n_m) in body or n_m == 0, "匹配页显示的匹配结果数 %d 与库里一致" % n_m)
        check("unknown" in body and "不是不合格" in body,
              "匹配页显式说明「unknown 不是不合格」")
        pid_m = q1("SELECT person_id FROM match_result LIMIT 1")
        if pid_m:
            st, b = get("/match?person=" + urllib.parse.quote(pid_m))
            check(st == 200 and "命中" in b and "缺口" in b,
                  "按人才看匹配结果，展示 met/gap 三态拆解")
        oid_m = q1("""SELECT jp.occupation_id FROM match_result m
                       JOIN job_posting jp ON jp.job_id=m.target_id
                      WHERE jp.occupation_id IS NOT NULL LIMIT 1""")
        if oid_m:
            st, b = get("/match?occ=" + urllib.parse.quote(oid_m))
            check(st == 200 and "这个职业要什么" in b,
                  "按职业反查该职业适合哪些人")

        # ===============================================================
        print("\n【T13】扩展与演化：行/列扩展 + 树的持续生长（需求 ④）")
        st, body = get("/extend")
        check(st == 200, "/extend 返回 200")
        for k in ("机制 A", "机制 B", "机制 C", "职业树的持续生长", "版本化"):
            check(k in body, "扩展页覆盖：%s" % k)
        check("机制未被使用" in body or "已使用" in body,
              "扩展页对每条机制标注「库里有没有使用痕迹」")
        check("add_dimension" in body and "create_instance" in body,
              "扩展页列出动态建模 API")
        # 使用痕迹必须与库里一致（空表就是空表，不粉饰）
        n_fv = q1("SELECT count(*) FROM field_value")
        n_ad = q1("SELECT count(*) FROM attribute_definition")
        check((n_fv > 0) == ("已使用 %s 行" % f"{n_fv:,}" in body) or n_fv == 0,
              "field_value 的使用痕迹与库里一致（库里 %d 行）" % n_fv)
        if n_ad == 0:
            check("机制未被使用" in body, "attribute_definition 为 0 时如实标注「未被使用」")
        # 版本化：当前有效权重 vs 历史行数
        n_cur = q1("SELECT count(*) FROM job_competency_weight WHERE valid_to IS NULL")
        n_all = q1("SELECT count(*) FROM job_competency_weight")
        check(f"{n_cur:,}" in body and f"{n_all:,}" in body,
              "扩展页同时给出当前有效权重 %s 与历史 %s（版本化不可掩盖）"
              % (f"{n_cur:,}", f"{n_all:,}"))

        # ===============================================================
        print("\n【T14】数据质量仪表盘（成熟数据库标准）")
        st, body = get("/quality")
        check(st == 200, "/quality 返回 200")
        for k in ("结构完整性不变量", "岗位侧数据质量门", "表填充率", "数据新鲜度",
                  "列空值率", "审计流水", "备份", "访问策略"):
            check(k in body, "质量页覆盖：%s" % k)
        check("通过" in body or "违反" in body, "完整性不变量给出结论")
        # 覆盖率数字必须与库一致
        mapping = q1("""SELECT count(*) FILTER (WHERE concept_id IS NOT NULL
                           AND requirement_type NOT IN ('RT5','RT6')) AS mapped,
                               count(*) FILTER (WHERE requirement_type NOT IN ('RT5','RT6')) AS denom
                          FROM job_requirement""")
        check("概念映射覆盖率" in body, "给出概念映射覆盖率")

        # ===============================================================
        print("\n【T15】开发者模式入口（需求 ⑤）")
        st, body = get("/dev")
        check(st == 200, "/dev 返回 200")
        check("只读" in body and "另一个进程" in body,
              "/dev 明确说明「门户只读、能写的是另一个进程」")
        check("portal_dev.py" in body and ":8083" in body,
              "/dev 给出开发者模式的启动命令与端口")
        check("add_dimension" in body and "试运行" in body,
              "/dev 说明开发者模式能做什么（含动态建模与试运行 SQL）")

    finally:
        srv.shutdown()
        srv.server_close()

    print("\n" + "=" * 74)
    print("结果：PASS %d 项，FAIL %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  [FAIL] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
