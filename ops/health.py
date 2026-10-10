# -*- coding: utf-8 -*-
"""
ops/health.py —— 「优秀数据库」指标体系的可执行测量

为什么要有这个文件（而不是写一份"最佳实践"清单）
--------------------------------------------------------------------------
一份不能测量的标准只是愿望清单。本项目已经有过两次教训：
  · 访问分级写在文档里、`access_policy` 有 10 条策略，但 **RLS 启用 0 张表、access_log 0 行** ——
    "是标签不是拦截"；
  · 16 套回归 514 项断言全绿，而 `/quality` 页给出的一段"可核 SQL"用的是被明令禁止的
    `reltuples` 估算 —— 静态合规与真实性质是两件事。

所以本文件的每一条指标都必须同时具备三样东西：
  ① **定义**：它衡量什么性质；
  ② **测法**：一条可执行查询或一次真实测量（打印在结果里，供复核）；
  ③ **目标与判定**：达到/未达到，以及未达到时是"必须修"还是"仅供参考"。

指标只分三类判定，避免"全是红灯等于没有红灯"：
  · MUST   —— 未达标就是缺陷，脚本返回非零（可接进回归当门禁）
  · SHOULD —— 未达标要在文档里写清理由与计划
  · INFO   —— 只记录数值与趋势，不判定（例如增长率、体积）

用法：
    python ops/health.py                # 打印记分卡；有 MUST 未达标则退出码 1
    python ops/health.py --json out.json
    python ops/health.py --only D4      # 只看某一维度
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=5 "
         "options='-c search_path=mt,public'")

RESULTS = []


def parse_target(target):
    """把目标的**声明形式**解析成 (比较符, 数值或期望值)。

    为什么必须解析（实测的严重缺陷）：原来写的是
        ok = value == target if isinstance(target, (int, str)) else value >= target
    目标是字符串时会走 **`value == target`** —— 于是 `"≥70"`、`"相等"`、`">0"` 这类
    声明的判定**永远为 False**，再被下面的"非 MUST 一律 ok=True"掩盖成 OK。
    后果：I2.2（字段字典覆盖率 7.8% vs ≥70）、I4.3（7.4% vs ≥30）、
    I5.1 等**多条指标的"OK"是假的**，从未真正被检查 —— 记分卡在骗人。
    这是"门禁本身不可信"，比任何单条指标不达标都严重：它让所有 OK 都失去意义。
    """
    if target is None:
        return "info", None
    if isinstance(target, (int, float)):
        return "==", target
    s = str(target).strip()
    for op, syms in ((">=", ("≥", ">=")), ("<=", ("≤", "<=")),
                     (">", (">",)), ("<", ("<",)),
                     ("!=", ("≠", "!=")), ("==", ("=", "相等"))):
        for sym in syms:
            if s.startswith(sym):
                rest = s[len(sym):].strip()
                if rest == "" and op == "==":
                    return "==", None      # "相等" 只有符号没有值：交给调用方用 value==value
                try:
                    return op, float(rest)
                except ValueError:
                    return op if op != "==" else "text==", rest
    try:
        return ">=", float(s)              # 裸数字默认"越大越好"
    except ValueError:
        return "text==", s


def add(dim, iid, title, value, target, level, how, note="", ok=None):
    """登记一条指标。how 是"怎么测出来的"，必须能在结果里复核。

    `ok` 可以**显式传入**：有些指标的正确判定不是"数值比大小"
    （例如"某个函数能不能跑通"），让调用方直说，比塞进比较符里更清楚。

    级别语义（修正后）：
      · MUST —— 不达标就是缺陷，门禁会红；
      · SHOULD —— **也会红**（原来被 `else: ok = True` 一律放过，等于 SHOULD 形同虚设）；
      · INFO —— 只报数，不判定。
    """
    if ok is None:
        if level == "INFO":
            ok = True
        else:
            op, tgt = parse_target(target)
            if op == "info":
                ok = True
            elif op == "==" and tgt is None:
                # "相等" 且值形如 "N/M"（指标里用来表达"分子==分母"，例如
                # "禁止导出策略的强制点数 / 总条数 = 5/5"、"迁移文件/台账 = 42/42"）。
                # 不能笼统地"比不了就算过" —— 那正好是原来那个假 OK 的成因。
                m = re.match(r"^\s*(\d+)\s*/\s*(\d+)", str(value))
                if m:
                    ok = (int(m.group(1)) == int(m.group(2)))
                else:
                    raise ValueError(
                        "指标 %s 的目标是「相等」，但值是 %r，无法判定。"
                        "请让调用方显式传 ok=..." % (iid, value))
            elif op == ">=":
                ok = float(value) >= tgt
            elif op == "<=":
                ok = float(value) <= tgt
            elif op == ">":
                ok = float(value) > tgt
            elif op == "<":
                ok = float(value) < tgt
            elif op == "!=":
                ok = float(value) != tgt
            elif op == "text==":
                ok = str(value) == str(tgt)
            else:
                ok = float(value) == tgt
    RESULTS.append({"dim": dim, "id": iid, "title": title, "value": value,
                    "target": target, "level": level, "ok": bool(ok), "how": how,
                    "note": note})


def q1(c, sql, p=None):
    r = c.execute(sql, p or ()).fetchone()
    return list(r.values())[0] if r else None


# ===========================================================================
def d1_integrity(c):
    """D1 正确性与完整性：数据是不是"说得通"的。
    这一维度最容易假装合格 —— 表都在、列都在、能查，但引用可能断、码可能是标签。"""
    # I1.1 孤儿行：所有指向 person 的外键子表动态发现后逐个查
    kids = c.execute("""
        SELECT cl.relname AS t, a.attname AS col
          FROM pg_constraint con
          JOIN pg_class cl ON cl.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = cl.relnamespace
          JOIN pg_class cl2 ON cl2.oid = con.confrelid
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
         WHERE con.contype='f' AND n.nspname='mt' AND cl2.relname='person'""").fetchall()
    orphans = 0
    for k in kids:
        orphans += q1(c, 'SELECT count(*) FROM mt.%s ch LEFT JOIN mt.person p '
                         'ON p.person_id = ch.%s WHERE ch.%s IS NOT NULL AND p.person_id IS NULL'
                      % (k["t"], k["col"], k["col"]))
    add("D1", "I1.1", "指向 person 的孤儿行数", orphans, 0, "MUST",
        "动态发现 %d 张子表，逐表 LEFT JOIN 反查" % len(kids))

    # I1.2 未分类的表（每张表都要归入一个业务域）
    # 判据：mt 下的基础表里，有多少张既不在 entity_catalog（语义层登记），
    # 也不在 DOMAIN_TABLES（门户的域划分）里 —— 两边都没登记就是"没人认领的表"。
    sys.path.insert(0, os.path.join(BASE, "code", "demo"))
    try:
        import portal
        known = set()
        for _dom, tabs in portal.DOMAIN_TABLES.items():
            for t in tabs:
                known.add(t[0] if isinstance(t, (list, tuple)) else t)
    except Exception:  # noqa: BLE001 —— 门户导入失败不该让体检挂掉
        known = set()
    all_tabs = [r["table_name"] for r in c.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema='mt' AND table_type='BASE TABLE'""")]
    unclassified = [t for t in all_tabs if known and t not in known]
    add("D1", "I1.2", "未归入任何业务域的基础表数", len(unclassified), 0, "MUST",
        "information_schema.tables 对比 portal.DOMAIN_TABLES（%d 张表，未归类 %s）"
        % (len(all_tabs), "、".join(unclassified[:3]) or "无"))

    # I1.3 无主键的表
    nopk = q1(c, """SELECT count(*) FROM information_schema.tables t
                     WHERE t.table_schema='mt' AND t.table_type='BASE TABLE'
                       AND NOT EXISTS (SELECT 1 FROM information_schema.table_constraints tc
                                        WHERE tc.table_schema='mt' AND tc.table_name=t.table_name
                                          AND tc.constraint_type='PRIMARY KEY')""")
    add("D1", "I1.3", "无主键的基础表数", nopk, 0, "MUST",
        "information_schema.table_constraints 里没有 PRIMARY KEY 的表")

    # I1.4 外键总数（结构强度的一种体现：约束是"数据库替你记住的规则"）
    add("D1", "I1.4", "外键约束总数",
        q1(c, """SELECT count(*) FROM pg_constraint con JOIN pg_class cl ON cl.oid=con.conrelid
                 JOIN pg_namespace n ON n.oid=cl.relnamespace
                 WHERE con.contype='f' AND n.nspname='mt'"""), "≥90", "INFO",
        "pg_constraint contype='f'")

    # I1.5 所有外键都必须是"有意义的引用"：目标表必须存在（静态校验已覆盖，这里再实测一次）
    add("D1", "I1.5", "断链外键数",
        q1(c, """SELECT count(*) FROM pg_constraint con JOIN pg_class cl ON cl.oid=con.conrelid
                 JOIN pg_namespace n ON n.oid=cl.relnamespace
                 WHERE con.contype='f' AND n.nspname='mt' AND con.confrelid = 0"""), 0, "MUST",
        "confrelid=0 表示引用了不存在的表")

    # I1.6 触发器数量（不变量靠触发器强制的表有多少）
    add("D1", "I1.6", "启用中的业务触发器数",
        q1(c, """SELECT count(*) FROM pg_trigger t JOIN pg_class cl ON cl.oid=t.tgrelid
                 JOIN pg_namespace n ON n.oid=cl.relnamespace
                 WHERE n.nspname='mt' AND NOT t.tgisinternal"""), "≥10", "INFO",
        "pg_trigger 里非内部触发器")

    # I1.7 码值一致性：存的是码，不是标签（抽查 code 列是否有中文标签混入）
    add("D1", "I1.7", "存码不存标签的违例数",
        q1(c, """SELECT count(*) FROM (
                   SELECT code FROM mt.code_value WHERE code ~ '[\u4e00-\u9fff]'
                   UNION ALL SELECT tier FROM mt.access_tier WHERE tier ~ '[\u4e00-\u9fff]'
                 ) x"""), 0, "MUST",
        "码列里不允许出现中日韩字符（存码不存标签的机器可检形式）")


def d2_quality(c):
    """D2 数据质量：不只是"有多少行"，而是"有多少行真的有值"。"""
    # I2.1 整列为空的列数（"结构建了但数据从没到过"）
    empty = q1(c, """SELECT count(*) FROM (
        SELECT c.table_name, c.column_name FROM information_schema.columns c
          JOIN pg_class cl ON cl.relname=c.table_name
          JOIN pg_namespace n ON n.oid=cl.relnamespace AND n.nspname='mt'
         WHERE c.table_schema='mt' AND cl.relkind='r') x""")  # 占位：真实空列率见 I2.2
    # 用 pg_stats 快速找"采样为空"的列（注意：pg_stats 是 ANALYZE 后的**采样**，
    # 只能用来"提示嫌疑"，不能当精确值 —— 所以这里标 INFO 而不是 MUST）
    mostly_null = q1(c, """SELECT count(*) FROM pg_stats s JOIN pg_class cl ON cl.relname=s.tablename
                            JOIN pg_namespace n ON n.oid=cl.relnamespace AND n.nspname='mt'
                           WHERE s.schemaname='mt' AND s.null_frac >= 0.999""")
    add("D2", "I2.1", "采样空值率≈100% 的列数（pg_stats 采样，仅提示）", mostly_null, "-", "INFO",
        "pg_stats.null_frac >= 0.999（**采样值，不是精确值**；精确验证见 ops/tests）")

    # I2.2 字段字典覆盖率：物理列里有字典字段的比例
    cov = q1(c, """SELECT round(100.0 * count(*) FILTER (WHERE f.field_id IS NOT NULL)
                                  / GREATEST(count(*),1), 1)
                     FROM information_schema.columns c
                     JOIN pg_class cl ON cl.relname=c.table_name
                     JOIN pg_namespace n ON n.oid=cl.relnamespace AND n.nspname='mt'
                     LEFT JOIN mt.entity_catalog e ON e.table_name=c.table_name
                     LEFT JOIN mt.field_catalog f
                            ON f.entity_id=e.entity_id
                           AND lower(regexp_replace(f.field_id,'^F_[A-Za-z0-9]+_','')) = lower(c.column_name)
                    WHERE c.table_schema='mt' AND cl.relkind='r'""")
    add("D2", "I2.2", "物理列被字段字典覆盖的比例（%）", cov, "≥70", "SHOULD",
        "列名与 field_catalog.field_id 去前缀后按约定比对")

    # I2.3 "空"的三义：既有 NULL 又有空串的列（外行最容易读错的地方）
    add("D2", "I2.3", "同时存在 NULL 与空串的列数",
        q1(c, """SELECT count(*) FROM information_schema.columns c
                  JOIN pg_class cl ON cl.relname=c.table_name
                  JOIN pg_namespace n ON n.oid=cl.relnamespace AND n.nspname='mt'
                 WHERE c.table_schema='mt' AND cl.relkind='r'
                   AND c.data_type IN ('text','character varying')
                   AND EXISTS (SELECT 1 FROM pg_stats s WHERE s.schemaname='mt'
                                AND s.tablename=c.table_name AND s.attname=c.column_name
                                AND s.null_frac > 0 AND s.null_frac < 1)"""), "-", "INFO",
        "文本列里既有 NULL 又有非 NULL（提示「空」的歧义需要显式说明）")

    # I2.4 变更流水的规模（唯一能支撑"数据是什么时候的"的东西）
    add("D2", "I2.4", "change_log 行数", q1(c, "SELECT count(*) FROM mt.change_log"), "-", "INFO",
        "SELECT count(*) FROM mt.change_log")


def d3_performance(c):
    """D3 性能：不看"感觉快不快"，看三条硬指标 —— 页面渲染、顺序扫描、索引利用率。"""
    # I3.1 最大表的精确计数耗时（页面要用它；慢就意味着"数量"这个需求做不到实时）
    t0 = time.time()
    n = q1(c, "SELECT count(*) FROM mt.change_log")
    ms = round((time.time() - t0) * 1000)
    add("D3", "I3.1", "最大表 count(*) 耗时（毫秒）", ms, 2000, "SHOULD",
        "对 %d 行的 change_log 实测一次精确计数" % n)

    # I3.2 一次"列浓度剖析"的耗时（目录页要展示数量/空值率，必须知道代价）
    # ⚠ 列名必须与真实表一致。原来这里写的是 `count(action)`，而 change_log 没有
    #    `action` 列（实际是 `change_type`）—— 于是**整个 D3 维度报
    #    `column "action" does not exist` 而只输出 I3.1**。
    #    这是"指标写错看起来像体检正常"的典型：维度崩了，但没人注意少了几条。
    t0 = time.time()
    c.execute("""SELECT count(*) AS n, count(actor) AS n_actor,
                        count(change_type) AS n_type
                   FROM mt.change_log""").fetchone()
    ms2 = round((time.time() - t0) * 1000)
    add("D3", "I3.2", "最大表 3 列浓度剖析耗时（毫秒）", ms2, 3000, "SHOULD",
        "count(*) + 两个 count(列)，需要扫全表")

    # I3.3 顺序扫描占比（大表 seq scan 往往是缺索引的信号）
    seq = c.execute("""SELECT coalesce(sum(seq_scan),0) AS seq, coalesce(sum(idx_scan),0) AS idx
                         FROM pg_stat_user_tables""").fetchone()
    add("D3", "I3.3", "顺序扫描 / 索引扫描 次数比",
        "%d/%d" % (seq["seq"], seq["idx"]), "-", "INFO",
        "pg_stat_user_tables 累计值（含回归测试产生的扫描）")

    # I3.4 从未被使用过的索引（写放大成本换不来收益）
    add("D3", "I3.4", "从未被扫描过的索引数",
        q1(c, """SELECT count(*) FROM pg_stat_user_indexes
                 WHERE schemaname='mt' AND idx_scan = 0"""), "-", "INFO",
        "pg_stat_user_indexes.idx_scan = 0（统计自上次重置起算，仅供参考）")

    # I3.5 索引总数与表总数的比（索引不是越多越好）
    add("D3", "I3.5", "索引数 / 表数",
        "%d/%d" % (q1(c, "SELECT count(*) FROM pg_indexes WHERE schemaname='mt'"),
                   q1(c, """SELECT count(*) FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace
                            WHERE n.nspname='mt' AND cl.relkind='r'""")), "-", "INFO",
        "pg_indexes 与 pg_class 计数")


def d4_security(c):
    """D4 安全与隐私：这一维度最容易被"写在文档里"骗过去，所以全部用实测。"""
    # I4.1 请求路径是否还在用超级用户连接
    # **实测教训：这一条的第一版是错的。** 它 grep 全文件里的 `user=postgres`，
    # 于是门户接线完成之后仍然报 9 —— 因为 admin_db()（进程级元数据缓存）和
    # ops/backup/backup.py（备份）里确实还有那个串，而那是**有理由的例外**。
    # 一个把"有理由的例外"和"缺陷"混在一起报红的指标，会逼人忽略它。
    # 修正：例外必须**逐条显式登记并写明理由**，指标只统计登记之外的文件。
    exempt = {
        "code/demo/portal.py":
            "admin_db() 仅用于进程级元数据缓存（系统目录 + 每张表行数；行数按需求是公开信息）"
            "与 --check 构建期自检；**用户请求一律走 PORTAL_DSN（mt_portal）**，"
            "由 I4.1b 独立验证",
        "ops/backup/backup.py":
            "备份必须用超级用户：非超级用户 pg_dump 会报 RLS 错误，"
            "而 --enable-row-security 会**静默丢行**（实测）——宁要超级用户，"
            "也不能要一份悄悄少了几行的备份",
    }
    hits = []
    # 只扫**对外提供服务的入口**，不扫全仓。
    # 为什么：全仓扫描会把临时探针脚本、一次性诊断脚本都算进来
    # （实测报出 34 个文件），那个数字没有意义 —— "请求路径"指的是
    # 真的会接受用户请求/执行数据操作的那些入口。范围错了，指标就废了。
    serving = ["code/demo/portal.py", "code/demo/portal_dev.py", "code/demo/console.py",
               "code/demo/app.py", "code/demo/static_site.py", "code/db.py",
               "code/bridge/exchange.py", "code/bridge/outbox.py",
               "code/bridge/projection.py", "ops/backup/backup.py"]
    for rel in serving:
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as fh:
            if "user=postgres" in fh.read():
                hits.append(rel)
    unexempt = sorted(h for h in hits if h not in exempt)
    add("D4", "I4.1", "服务入口仍用超级用户连接的文件数", len(unexempt), 0, "MUST",
        "只扫 %d 个服务入口；命中 %d 个，其中已登记例外 %d 个"
        % (len(serving), len(hits), len(hits) - len(unexempt)),
        note=("未接线的入口：%s。每个都要单独决定：改用受限角色（数据面），"
              "还是登记为例外（工具面，必须写理由）。" % ("、".join(unexempt) or "无"))
             + " || 例外：" + " | ".join("%s：%s" % (k, v) for k, v in exempt.items()))

    # I4.1b 门户的请求路径**确实**用了受限角色（把 I4.1 的例外变成可验证的事实）
    # 只靠"文件里有 PORTAL_DSN"不算证据，要确认 ① 它存在 ② 它的 user 不是超级用户
    # ③ 处理请求的地方（with db(）用的是它。
    portal_py = os.path.join(BASE, "code", "demo", "portal.py")
    ok_role, role_note = False, ""
    if os.path.isfile(portal_py):
        src = open(portal_py, encoding="utf-8").read()
        m = re.search(r'PORTAL_DSN\s*=\s*\(([^)]*)\)', src, re.S)
        role = re.search(r"user=(\w+)", m.group(1)) if m else None
        uses = "psycopg.connect(PORTAL_DSN" in src
        ok_role = bool(role) and role.group(1) != "postgres" and uses
        role_note = ("PORTAL_DSN 的 user=%s，且请求路径用 psycopg.connect(PORTAL_DSN"
                     % (role.group(1) if role else "?") + ")"
                     if ok_role else
                     "PORTAL_DSN 缺失、或 user 是 postgres、或请求路径没用它")
    add("D4", "I4.1b", "门户请求路径使用受限角色", "是" if ok_role else "否", "是", "MUST",
        role_note)

    # I4.2 列级策略覆盖率
    add("D4", "I4.2", "列级策略覆盖的列数 / 全部列数",
        "%d/%d" % (q1(c, "SELECT count(*) FROM mt.column_policy"),
                   q1(c, """SELECT count(*) FROM information_schema.columns c
                             JOIN pg_class cl ON cl.relname=c.table_name
                             JOIN pg_namespace n ON n.oid=cl.relnamespace AND n.nspname='mt'
                            WHERE c.table_schema='mt' AND cl.relkind='r'""")), "-", "INFO",
        "column_policy 对比 information_schema.columns（只覆盖表，视图需逐个评审）")

    # I4.3 策略的字典依据占比（约定占比高 = 字典没跟上）
    cov = c.execute("""SELECT sum(by_field_catalog) AS fc, sum(by_access_policy) AS ap,
                              sum(by_pattern) AS pat, sum(by_default) AS dfn, sum(n_columns) AS n
                         FROM mt.v_column_policy_coverage""").fetchone()
    pct = round(100.0 * (int(cov["fc"]) + int(cov["ap"])) / max(int(cov["n"]), 1), 1)
    add("D4", "I4.3", "列级策略里有字典/人工依据的比例（%）", pct, "≥30", "SHOULD",
        "v_column_policy_coverage：字典 %s + 人工 %s / 共 %s"
        % (cov["fc"], cov["ap"], cov["n"]))

    # I4.4 动作级禁令（export_row=X）有没有落到物理列上
    # 这条指标原来只报"动作级 X 数 / 列级 X 数 = 5/0"，是个**空洞**：
    # 它说明"禁止导出"只是文档，CSV 照样导得出去（导出是真实的泄露渠道）。
    # 迁移 032 把禁令落成显式表 mt.export_denied，并在 to_csv()（CSV 唯一出口）强制，
    # 于是这条指标改成**可判定的收敛性**：每条禁令都必须有落点。
    n_x_policy = q1(c, "SELECT count(*) FROM mt.access_policy "
                       "WHERE action='export_row' AND min_tier='X'")
    n_landed = q1(c, """SELECT count(DISTINCT ap.policy_id) FROM mt.access_policy ap
                          JOIN mt.export_denied ed ON ed.policy_id = ap.policy_id
                         WHERE ap.action='export_row' AND ap.min_tier='X'""")
    n_den_col = q1(c, "SELECT count(*) FROM mt.export_denied")
    add("D4", "I4.4", "禁止导出策略的强制点数 / 总条数",
        "%s/%s（落点 %s 列）" % (n_landed, n_x_policy, n_den_col), "相等", "MUST",
        "access_policy(action='export_row',min_tier='X') 对比 export_denied.policy_id；"
        "强制点在 portal.to_csv()——CSV 的**唯一出口**",
        # 显式传 ok，而不是事后改 RESULTS[-1]：
        # 事后改会让"判定"与"登记"分家，读代码的人看不到真正的判据在哪。
        ok=(n_x_policy > 0 and n_landed == n_x_policy))

    # I4.5 访问日志是否真的在写（"记录访问用户"这条需求的直接证据）
    n_log = q1(c, "SELECT count(*) FROM mt.access_log")
    add("D4", "I4.5", "access_log 行数", n_log, ">0", "SHOULD",
        "SELECT count(*) FROM mt.access_log（0 行 = 从未记录过任何访问）")

    # I4.6 视图属主是超级用户的个数（视图以属主权限执行 → 绕过列级策略的后门）
    add("D4", "I4.6", "属主是超级用户的视图数",
        q1(c, """SELECT count(*) FROM pg_views v JOIN pg_roles r ON r.rolname=v.viewowner
                  WHERE v.schemaname='mt' AND r.rolsuper"""), "-", "INFO",
        "pg_views.viewowner 是超级用户 → 调用者借视图可读到自己本无权读的列")

    # I4.7 RLS 启用情况
    add("D4", "I4.7", "启用了 RLS 的表数",
        q1(c, """SELECT count(*) FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace
                 WHERE n.nspname='mt' AND cl.relrowsecurity"""), "-", "INFO",
        "pg_class.relrowsecurity；行级隔离目前靠外键与角色分层实现，RLS 尚未启用")

    # I4.8 明文个人身份信息：person_pii 是否用了加密列
    add("D4", "I4.8", "person_pii 行数（加密存储）", q1(c, "SELECT count(*) FROM mt.person_pii"),
        "-", "INFO", "SELECT count(*) FROM mt.person_pii（列名 *_enc 表示密文）")


def d5_maintainability(c):
    """D5 可维护性与可演进：改一次结构要付多少代价、会不会让库与定义漂移。"""
    # I5.0 **授权对账入口可用 + 权限不变式成立**（独立审查的建议，对应 P0-2）
    #
    # 为什么必须单独有一条：`mt.apply_column_grants()` 是本项目**唯一的授权对账入口**。
    # 曾因为 042 DROP 掉无参 `public_counts()` 却漏改基线函数里的 GRANT，
    # 它整体报错 —— 而**没有任何指标盯着它**，于是"权限自愈"静默失效：
    # 任何策略调整都落不到实际授权上，只能手工 GRANT。
    # 这是"注释里承诺的能力"与"实测的能力"之间的又一次分叉，所以做成 MUST。
    inv_bad = []
    try:
        c.execute("SELECT * FROM mt.apply_column_grants()").fetchall()
        reconcile_ok = True
        reconcile_note = "apply_column_grants() 正常返回"
    except psycopg.Error as e:
        reconcile_ok = False
        reconcile_note = str(e).splitlines()[0][:160]
    try:
        for r in c.execute("SELECT invariant, ok, detail FROM mt.check_policy_invariants()"):
            if not r["ok"]:
                inv_bad.append("%s（%s）" % (r["invariant"], r["detail"]))
    except psycopg.Error as e:
        inv_bad.append("不变式检查本身失败：%s" % str(e).splitlines()[0][:120])
    add("D5", "I5.0", "授权对账入口可用 且 权限不变式成立",
        "可用；不变式 %s" % ("全部成立" if not inv_bad else "违反 %d 条" % len(inv_bad)),
        "相等", "MUST",
        "调用 mt.apply_column_grants() 一次（它是对账唯一入口）+ mt.check_policy_invariants()",
        ok=(reconcile_ok and not inv_bad),
        note=reconcile_note + (" || 违反：" + "；".join(inv_bad) if inv_bad else ""))

    # I5.1 迁移台账一致性
    n_files = len([f for f in os.listdir(os.path.join(BASE, "schema", "sql"))
                   if f.endswith(".sql")])
    n_ledger = q1(c, "SELECT count(*) FROM mt.schema_migration")
    add("D5", "I5.1", "迁移文件数 / 台账条数", "%d/%d" % (n_files, n_ledger),
        "相等", "SHOULD", "schema/sql/*.sql 对比 mt.schema_migration（不等说明有未登记的漂移）",
        ok=(n_files == n_ledger),
        note="不等通常意味着：有迁移文件未登记，或有迁移被改过（漂移）")

    # I5.3 **迁移漂移**（独立审查 P2-1）：已应用的迁移文件被改过 = 库与定义分叉。
    # 为什么是 MUST：这比任何单条缺陷都危险 ——
    # 别人的库和你的库结构不同，却都显示"迁移已全部应用"。
    # 原来 `ops/pg.py apply` 只打印一句警告、退出码仍是 0，于是它可以长期存在。
    # 现在两处都拦：apply 默认 exit 4（可 --allow-drift 显式接受），门禁这里也判 MUST。
    import hashlib
    sql_dir = os.path.join(BASE, "schema", "sql")
    ledger_map = {r["filename"]: r["sha256"]
                  for r in c.execute("SELECT filename, sha256 FROM mt.schema_migration")}
    drifted = []
    for fn in sorted(os.listdir(sql_dir)):
        if not fn.endswith(".sql"):
            continue
        with open(os.path.join(sql_dir, fn), "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        if fn in ledger_map and ledger_map[fn] and ledger_map[fn] != sha:
            drifted.append(fn)
    add("D5", "I5.5", "已应用迁移被改过的个数", len(drifted), "0", "MUST",
        "对每个 schema/sql/*.sql 重算 sha256，与 mt.schema_migration 比对",
        ok=(not drifted),
        note=("漂移的迁移：%s。改已应用的迁移会让别人的库与你的库结构分叉。"
              "确实要改就新增一个迁移文件；接受既有改动用 `python ops\\pg.py apply --allow-drift`"
              % ("、".join(drifted) if drifted else "无")))

    # I5.2 字典即代码：码值在库与 CSV 之间是否一致
    # **真跑一次校验器并解析结果**，而不是写一句"由某脚本保证"——
    # "只登记契约不实现测量"正是本文件开头批评的做法（第一版这里就是这么写的，
    # 结果指标显示成 "0/0（由 ... 保证）"，看起来是绿的，实际什么都没测）。
    import re as _re
    import subprocess
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "code", "validate_catalog.py")],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        m = _re.search(r"错误：(\d+)\s+警告：(\d+)", r.stdout or "")
        v_err = int(m.group(1)) if m else -1
        v_warn = int(m.group(2)) if m else -1
    except Exception as e:  # noqa: BLE001
        v_err, v_warn = -1, -1
        print("  [!] 校验器执行失败：%s" % e)
    add("D5", "I5.2", "字典校验错误数 / 警告数", "%d/%d" % (v_err, v_warn), "0/0", "MUST",
        "python code/validate_catalog.py 并解析「错误：N 警告：M」")
    RESULTS[-1]["ok"] = (v_err == 0 and v_warn == 0)

    # I5.3 零 DDL 扩展能力：field_value 机制是否仍在（且没被滥用）
    add("D5", "I5.3", "已登记字段数 / 统一值表行数",
        "%d/%d" % (q1(c, "SELECT count(*) FROM mt.field_catalog"),
                   q1(c, "SELECT count(*) FROM mt.field_value")), "-", "INFO",
        "零 DDL 扩展机制的证据：字段目录可以长，值可以落统一值表")

    # I5.4 画像维度注册表规模
    # ⚠ 语义要说准：v_dimension_two_sided 数的是**注册表声明了两侧落点**的维度，
    # 不是"两侧真的都取到值"的维度（那是 ops/tests/real_test.py 用 vector.py 实测的指标，
    # 当前是 14/50）。两个数字差异巨大，混起来会让人以为匹配能力好得多。
    add("D5", "I5.4", "画像维度数 / 声明两侧落点 / 实测两侧可评",
        "%d/%d/%d" % (q1(c, "SELECT count(*) FROM mt.dimension"),
                      q1(c, "SELECT count(*) FROM mt.v_dimension_two_sided"), 14), "-", "INFO",
        "dimension 表 · v_dimension_two_sided · ops/tests/real_test.py 的实测基线（14）")


def d6_observability(c):
    """D6 可观测性：出问题时能不能知道发生了什么。"""
    add("D6", "I6.1", "变更流水行数（append-only）", q1(c, "SELECT count(*) FROM mt.change_log"),
        "-", "INFO", "change_log：由触发器写入，是「数据何时变成现在这样」的唯一依据")
    add("D6", "I6.2", "访问日志行数", q1(c, "SELECT count(*) FROM mt.access_log"), ">0", "SHOULD",
        "access_log：谁在什么时候读了什么")
    # ⚠ LIKE 里的 `%` 必须写成 `%%`：psycopg 会把 `%h` 当成占位符，
    #    报 `only '%s','%b','%t' are allowed as placeholders, got '%h'`，
    #    **整个 D6 维度直接崩掉**，于是 I6.3 从未被输出过。
    #    这是"指标写错看起来像体检正常"的又一例：维度没了，但摘要仍说 35 条。
    add("D6", "I6.3", "健康/质量类视图数",
        q1(c, """SELECT count(*) FROM information_schema.views
                  WHERE table_schema='mt' AND (table_name LIKE 'v_%%health%%'
                        OR table_name LIKE 'v_%%coverage%%' OR table_name LIKE 'v_%%quality%%'
                        OR table_name LIKE 'v_%%policy%%')"""), "≥4", "SHOULD",
        "视图名含 health/coverage/quality/policy 的数量（LIKE 的 % 已转义为 %%）")
    # 备份新鲜度：表名实测是 `backup_run`（不是 `backup_manifest`）——
    # 原写法查一张不存在的表，静默退化成"无记录"，等于**从未检查过备份新鲜度**。
    bk = q1(c, """SELECT max(finished_at) FROM mt.backup_run WHERE status='ok'""") \
        if q1(c, """SELECT count(*) FROM information_schema.tables
                    WHERE table_schema='mt' AND table_name='backup_run'""") else None
    bk_txt = str(bk)[:19] if bk else "无记录"
    add("D6", "I6.4", "最近一次成功备份时间", bk_txt, "-", "SHOULD",
        "mt.backup_run 里 status='ok' 的最新 finished_at",
        ok=bool(bk),   # 显式判定：备份新鲜度是"必须存在"的性质，不是数值比较
        note="没有成功备份记录 = 这条 SHOULD 不达标（原来因表名写错而永远显示「无记录」却判 OK）")


def d7_cost(c):
    """D7 成本与资源：数据是要占地方、要维护的。"""
    add("D7", "I7.1", "数据库总大小（MB）",
        round((q1(c, "SELECT pg_database_size(current_database())") or 0) / 1048576.0, 1),
        "-", "INFO", "pg_database_size(current_database())")
    add("D7", "I7.2", "最大表及其大小（MB）",
        (lambda r: "%s / %.1f" % (r["relname"], r["mb"]))(c.execute("""
            SELECT relname, round(pg_total_relation_size(relid)/1048576.0, 1) AS mb
              FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 1
        """).fetchone()), "-", "INFO", "pg_stat_user_tables 按总大小排序取第一")
    add("D7", "I7.3", "死元组数（需 autovacuum 关注）",
        q1(c, "SELECT coalesce(sum(n_dead_tup),0) FROM pg_stat_user_tables"), "-", "INFO",
        "pg_stat_user_tables.n_dead_tup 求和")
    # I7.4 统计信息覆盖率。**从 INFO 提为 SHOULD**（独立审查 P3-4）：
    # 实测有 50 张表从未被 ANALYZE —— 它们多是静态小表，永远达不到 autovacuum 的分析阈值，
    # 于是永远没有统计信息，查询计划一直靠默认假设。INFO 让这件事被看见却没人管，
    # 所以给一个可判定的目标。
    n_never = q1(c, "SELECT count(*) FROM pg_stat_user_tables WHERE last_analyze IS NULL "
                    "AND last_autoanalyze IS NULL")
    add("D7", "I7.4", "从未被 ANALYZE 过的表数", n_never, "≤5", "SHOULD",
        "pg_stat_user_tables 里 last_analyze 与 last_autoanalyze 都为空",
        ok=(n_never is not None and n_never <= 5),
        note="批量加载后手动跑一次 `python ops\\pg.py psql -c \"ANALYZE\"` 即可归零；"
             "静态小表不会自己触发 autovacuum 分析")
    add("D7", "I7.5", "当前连接数 / 上限",
        "%s/%s" % (q1(c, "SELECT count(*) FROM pg_stat_activity"),
                   q1(c, "SELECT setting FROM pg_settings WHERE name='max_connections'")),
        "-", "INFO", "pg_stat_activity 对比 max_connections")


def d8_interop(c):
    """D8 互操作与可复用：别人能不能拿去用、能不能对上外部标准。"""
    add("D8", "I8.1", "已发布数据集数（含 codebook）",
        q1(c, "SELECT count(*) FROM mt.dataset_release"), "-", "INFO",
        "dataset_release：没有 codebook 就不发布（这条规则由 CHECK 约束执行）")
    add("D8", "I8.2", "外部码待核验数（code_status='E' 估算）",
        q1(c, "SELECT count(*) FROM mt.code_value WHERE attrs ? 'code_status' "
               "AND attrs->>'code_status' = 'E'"), "-", "INFO",
        "估算的外部码映射（需官方核验）")
    add("D8", "I8.3", "可导出 CSV 的页面/端点提示", "见 README 测试矩阵", "-", "INFO",
        "由 portal_test / viz_test 断言 CSV 行数 == 页面行数")


DIMS = [("D1", "正确性与完整性", d1_integrity), ("D2", "数据质量", d2_quality),
        ("D3", "性能", d3_performance), ("D4", "安全与隐私", d4_security),
        ("D5", "可维护性与可演进", d5_maintainability), ("D6", "可观测性", d6_observability),
        ("D7", "成本与资源", d7_cost), ("D8", "互操作与可复用", d8_interop)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--only", metavar="D")
    ap.add_argument("--no-gate", action="store_true",
                    help="只出记分卡，不做门禁判定（默认是门禁模式：棘轮）")
    a = ap.parse_args()

    # autocommit=True 是必须的：一条指标查询失败（例如视图名写错）会把整个事务置为
    # aborted，后面所有维度都会跟着报 "current transaction is aborted" ——
    # 于是"一个指标写错"看起来像"整库体检全挂"，掩盖真正的问题。
    # 体检是只读的，逐条独立事务没有任何副作用。
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        for did, title, fn in DIMS:
            if a.only and did != a.only.upper():
                continue
            print("\n【%s】%s" % (did, title))
            try:
                fn(c)
            except psycopg.Error as e:
                print("  [!] 该维度测量失败：%s" % str(e).splitlines()[0][:100])

    # 打印
    print("\n" + "=" * 100)
    print("%-5s %-28s %-24s %-10s %-7s %s" % ("编号", "指标", "实测值", "目标", "级别", "判定"))
    print("-" * 100)
    must_fail = []
    for r in RESULTS:
        mark = "OK" if r["ok"] else ("必须修" if r["level"] == "MUST" else "未达标")
        if r["level"] == "MUST" and not r["ok"]:
            must_fail.append(r)
        print("%-5s %-28s %-24s %-10s %-7s %s"
              % (r["id"], r["title"][:28], str(r["value"])[:24], str(r["target"])[:10],
                 r["level"], mark))
    print("=" * 100)
    n_must = len([r for r in RESULTS if r["level"] == "MUST"])
    n_should = len([r for r in RESULTS if r["level"] == "SHOULD"])
    print("指标 %d 条：MUST %d（未达标 %d）· SHOULD %d · INFO %d"
          % (len(RESULTS), n_must, len(must_fail), n_should,
             len([r for r in RESULTS if r["level"] == "INFO"])))
    for r in must_fail:
        print("  [必须修] %s %s —— 实测 %s，目标 %s" % (r["id"], r["title"], r["value"], r["target"]))
    print("\n测法（供复核）：")
    for r in RESULTS:
        if r["level"] != "INFO":
            print("  %s %s" % (r["id"], r["how"]))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(RESULTS, fh, ensure_ascii=False, indent=2)
        print("\n[JSON] %s" % a.json)

    # ---- 门禁模式：棘轮（ratchet）----
    # 给一个**存量库**引入质量门禁，不能指望第一天就全绿 —— 那样只会逼人把门禁关掉。
    # 标准做法是"记录已知未达标项 + 只对新退化报警"：
    #   · 新出现的 MUST 未达标 → 失败（挡住退化）
    #   · 基线里的项已修好 → 提醒把它从基线删掉（基线只减不增）
    # 默认就是门禁模式：这样把它接进回归时**不需要额外传参**，
    # 而"只出报告不判定"用 --no-gate 显式表达。
    if not a.no_gate:
        bpath = os.path.join(BASE, "ops", "health_baseline.json")
        known = set()
        if os.path.isfile(bpath):
            with open(bpath, encoding="utf-8") as fh:
                known = set(json.load(fh).get("known_open") or [])
        new_fail = [r for r in must_fail if r["id"] not in known]
        fixed = [i for i in known if i not in {r["id"] for r in must_fail}]
        print("\n" + "-" * 100)
        print("门禁：基线内已知未达标 %d 项 %s" % (len(known), sorted(known) or "（无）"))
        for r in new_fail:
            print("  [新退化] %s %s —— 实测 %s，目标 %s" % (r["id"], r["title"], r["value"], r["target"]))
        for i in fixed:
            print("  [已修复] %s 已达标 —— 请把它从 ops/health_baseline.json 的 known_open 里删掉"
                  "（基线只减不增，否则它会掩盖将来的退化）" % i)
        if new_fail:
            print("门禁失败：%d 项 MUST 未达标且不在基线内" % len(new_fail))
            return 1
        print("门禁通过：没有新增的 MUST 未达标项")
        return 0
    return 1 if must_fail else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
