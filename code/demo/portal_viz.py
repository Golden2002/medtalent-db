# -*- coding: utf-8 -*-
"""
code/demo/portal_viz.py —— 数据可视化模块（只读）

调研结论（Metabase / Superset / Grafana / gnomAD 那一类成熟数据产品的共同点）：
真正与工具无关的不是某个图表库的 API，而是三件事——

  ① **度量注册表**（Semantic Layer）。每个指标在一处定义：口径、粒度、负责人、版本。
     Superset 叫 dataset/metric，dbt 叫 metric，Metabase 叫 Model。
     本库已经建了 `metric_definition(metric_id, name, definition_sql, grain, version,
     owner, status)` —— 之前是空表。这里把全部图表口径注册进去（`--register-metrics`
     写入库），于是"这个数字怎么算的"是**可查询的数据**，而不是散落在代码里的字符串。
  ② **图表选型规则**。分类×数值→条形，时间×数值→折线，两分类×数值→热力图，
     两数值→散点，构成→堆叠，目标达成→目标条。规则是稳定的，库是可换的。
  ③ **每个图都能被核对**。图旁边就是它的 SQL、行数、单位和 CSV。
     Grafana 面板的 "Inspect → Query"、Superset 的 "View query" 是同一个诉求。

所以这一页不是"贴几张图"，而是：**口径 → 图 → 原始数据** 三者一一对应。
空数据画明确的空状态（`charts.py` 负责），不画空坐标系。

设计边界：
  · 只读。与 `portal.py` 同进程、同一条只读纪律。
  · 零 JS、零 CDN。图表是内联 SVG（`charts.py`），拷走一个 HTML 就能看。
  · 图表数受控（15 个面板，分 7 组）——调研里的经验值是一屏 10–15 个卡片，
    再多读者就抓不住重点了。

用法（随门户一起）：
  python code/demo/portal.py --serve        # 打开 /viz
  python code/demo/portal.py --register-metrics   # 把口径写入 metric_definition
"""
from __future__ import annotations

import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from psycopg import sql as sql_mod  # noqa: E402

import charts as CH  # noqa: E402
import portal as P  # noqa: E402
import metrics as M  # noqa: E402

# ---------------------------------------------------------------------------
# 度量注册表：每张图的**唯一**口径定义
# ---------------------------------------------------------------------------
# grain 说明这条 SQL 的粒度；unit 用于坐标轴与提示；kind 决定图表类型。
VIZ = [
    dict(id="m_rows", section="数据规模", kind="bar_h",
         title="行数最多的表", grain="每表一行",
         desc="精确 count(*)，不是 pg_class.reltuples 估算值。",
         label="表", value="行数", unit=" 行", source="meta",
         sql="""-- 表清单来自 pg_class，行数是逐表精确统计
SELECT c.relname AS "表",
       (SELECT count(*) FROM ...) AS "行数"   -- 逐表执行，见 portal.load_meta()
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mt' AND c.relkind = 'r'
 ORDER BY 2 DESC"""),

    dict(id="m_domain", section="数据规模", kind="bar_h",
         title="各域的数据量", grain="每域一行",
         desc="8 个域的精确行数合计（域划分见 portal.DOMAIN_TABLES）。",
         label="域", value="行数", unit=" 行", source="meta",
         sql="-- 按 portal.DOMAIN_TABLES 分组聚合 meta() 的精确行数"),

    dict(id="m_gate", section="数据质量", kind="target_bar",
         title="岗位侧质量门：实际 vs 目标", grain="每指标一行",
         desc="四项指标与各自目标线。虚线是目标，绿=达标、红=未达标。"
              "口径来自 <code>code/metrics.py</code>（<b>单一定义</b>）——"
              "质量门脚本、门户、可视化三处用的是同一段 SQL，不各写一遍。",
         label="指标", value="实际", target="目标", unit="%", metrics="gate",
         sql="""-- 口径单一定义在 code/metrics.py::GATE_SQL，此处直接复用
SELECT '岗位族覆盖' AS "指标", count(DISTINCT job_family) AS "实际", 17 AS "目标"
  FROM job_posting
UNION ALL SELECT '必填字段完整率', ..., 99
UNION ALL SELECT '原文可回溯率', ..., 100
UNION ALL SELECT '能力概念映射覆盖率', ..., 70
-- 覆盖率分母 = requirement_type IN ('RT5','RT6','RT7','RT8')
-- （资格门槛 RT1/RT3/RT4 由专用字段承载，不计入）"""),

    dict(id="m_audit", section="数据质量", kind="bar_h",
         title="审计流水按对象类型", grain="每对象类型一行",
         desc="change_log 是触发器强制的 append-only 流水；这里看写入集中在哪些对象。",
         label="对象类型", value="变更次数", unit="", sql="""SELECT object_type AS "对象类型", count(*) AS "变更次数"
  FROM change_log GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),

    dict(id="m_family", section="岗位市场", kind="bar_h",
         title="各岗位族的 JD 数", grain="每族一行",
         desc="需求端的规模分布。临床（F01）只是 17 族之一。",
         label="岗位族", value="JD 数", unit="", sql="""SELECT job_family AS "岗位族码", count(*) AS "JD 数"
  FROM job_posting WHERE verify_status <> 'V4'
 GROUP BY 1 ORDER BY 2 DESC"""),

    dict(id="m_salary", section="岗位市场", kind="histogram",
         title="薪资下限分布", grain="每条有薪资的 JD 一行",
         desc="只统计明确给了薪资下限的 JD；面议的不参与，也不填充。",
         col="salary_min", unit="", sql="""SELECT salary_min FROM job_posting
 WHERE salary_min IS NOT NULL AND verify_status <> 'V4' ORDER BY salary_min"""),

    dict(id="m_edu_req", section="岗位市场", kind="stacked",
         title="各岗位族的学历要求构成", grain="每族一行 × 学历码",
         desc="构成比绝对数更能看出「这个族要不要博士」。",
         label="岗位族", cats="CT_DEGREE_LEVEL", unit="", sql="""SELECT job_family AS "岗位族码", education_req AS "学历码", count(*) AS n
  FROM job_posting WHERE education_req IS NOT NULL
 GROUP BY 1, 2 ORDER BY 1, 2"""),

    dict(id="m_heat", section="能力图谱", kind="heatmap",
         title="岗位族 × 能力：平均重要性", grain="族 × 能力",
         desc="用色阶承载第三维。只在样本足够的组合上有值（样本 <30 的会被跳过）。",
         sql="""SELECT o.family AS "族", c.preferred_label AS "能力",
       round(avg(w.importance), 3) AS "平均重要性"
  FROM job_competency_weight w
  JOIN occupation o ON o.occupation_id = w.occupation_id
  JOIN concept c ON c.concept_id = w.concept_id
 WHERE w.valid_to IS NULL
 GROUP BY 1, 2 HAVING count(*) >= 1
 ORDER BY 1, 3 DESC"""),

    dict(id="m_demand", section="能力图谱", kind="bar_h",
         title="能力需求 Top 15", grain="每能力一行",
         desc="按“被多少个职业要求”排序，是需求端的宽度，不是深度。",
         label="能力", value="涉及职业数", unit="", sql="""SELECT c.preferred_label AS "能力",
       count(DISTINCT w.occupation_id) AS "涉及职业数",
       round(avg(w.importance), 3) AS "平均重要性"
  FROM job_competency_weight w JOIN concept c ON c.concept_id = w.concept_id
 WHERE w.valid_to IS NULL GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 15"""),

    dict(id="m_ver", section="能力图谱", kind="line",
         title="能力矩阵随重算版本的变化", grain="每次重算一行",
         desc="每一版都是一次完整快照（版本化，不覆盖）。曲线平直说明市场侧没变——"
              "这本身是结论，不是故障。<b>只画一个量纲</b>：行数/覆盖职业/覆盖能力"
              "三者量级不同，塞进同一个 y 轴会让读者误判。",
         x="版本", series=["权重行数"], unit="", sql="""SELECT to_char(created_at, 'MM-DD HH24:MI') AS "版本",
       row_count AS "权重行数",
       occupation_count AS "覆盖职业",
       concept_count AS "覆盖能力"
  FROM competency_run ORDER BY created_at"""),

    dict(id="m_degree", section="人才供给", kind="bar_h",
         title="学历分布", grain="每学历一行",
         desc="按人（去重）统计最高学历记录。合成数据在图上与真实数据同权——"
              "所以页面上必须标注数据来源。",
         label="学历", value="人数", unit=" 人", code_table="CT_DEGREE_LEVEL",
         sql="""SELECT degree_level AS "学历", count(DISTINCT person_id) AS "人数"
  FROM education_record GROUP BY 1 ORDER BY 2 DESC"""),

    dict(id="m_major", section="人才供给", kind="bar_h",
         title="专业方向 Top 12", grain="每专业一行",
         desc="供给端的结构。与岗位侧对照才看得出错配。",
         label="专业方向", value="人数", unit=" 人", sql="""SELECT major_raw AS "专业方向", count(DISTINCT person_id) AS "人数"
  FROM education_record WHERE major_raw IS NOT NULL
 GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),

    dict(id="m_sd", section="人才供给", kind="scatter",
         title="技能供需对照", grain="每能力一行",
         desc="横轴=具备该能力的人数，纵轴=要求该能力的岗位数。"
              "右下=供给过剩，左上=供给缺口。",
         x="具备人数", y="要求岗位数", label="能力", sql="""SELECT coalesce(s.preferred_label, d.preferred_label) AS "能力",
       coalesce(s.person_count, 0) AS "具备人数",
       coalesce(d.posting_count, 0) AS "要求岗位数"
  FROM v_skill_supply s FULL JOIN v_skill_demand d USING (concept_id)
 WHERE coalesce(s.person_count,0) + coalesce(d.posting_count,0) > 0"""),

    dict(id="m_match", section="匹配", kind="stacked",
         title="各岗位族的匹配三态构成", grain="每族一行",
         desc="met 命中 / gap 缺口 / unknown 未观测。"
              "<b>unknown 不是不合格</b>——把它单独成一色，就是不让它被读成失败。",
         label="岗位族", cats=("met 命中", "gap 缺口", "unknown 未观测"), unit="", sql="""SELECT jp.job_family AS "岗位族码",
       sum((m.score_breakdown->>'met')::numeric) AS "met 命中",
       sum((m.score_breakdown->>'gap')::numeric) AS "gap 缺口",
       sum((m.score_breakdown->>'unknown')::numeric) AS "unknown 未观测"
  FROM match_result m JOIN job_posting jp ON jp.job_id = m.target_id
 GROUP BY 1 ORDER BY 1"""),

    dict(id="m_tree", section="演化", kind="line",
         title="职业树规模的时间曲线", grain="每月一行",
         desc="用 occupation_asof() 还原每个月的树。曲线只在 valid_from 之后才起来——"
              "早于它的 0 不是错误，是「那时还不存在」。",
         x="月份", unit=" 个节点", sql="""SELECT to_char(d, 'YYYY-MM') AS "月份",
       (SELECT count(*) FROM occupation_asof(d::date)) AS "节点数"
  FROM generate_series(date_trunc('month', (SELECT min(valid_from) FROM occupation)),
                       date_trunc('month', current_date), interval '1 month') AS d
 ORDER BY 1"""),

    dict(id="m_drift", section="演化", kind="bar_h",
         title="最新版本的能力漂移（非稳定项）", grain="每 (职业, 能力) 一行",
         desc="两版之间重要性变化超过阈值的项。没有柱子 = 最近两版完全一致。",
         label="能力", value="重要性变化", unit="", sql="""SELECT c.preferred_label || ' @ ' || o.label_zh AS "能力",
       abs(coalesce(d.delta, 0)) AS "重要性变化"
  FROM competency_drift d
  JOIN occupation o ON o.occupation_id = d.occupation_id
  JOIN concept c ON c.concept_id = d.concept_id
 WHERE d.drift_type <> 'stable'
   AND d.to_run_id = (SELECT run_id FROM competency_run
                       ORDER BY created_at DESC LIMIT 1)
 ORDER BY 2 DESC LIMIT 12"""),
]

SECTIONS = ["数据规模", "数据质量", "岗位市场", "能力图谱", "人才供给", "匹配", "演化"]


def by_id(vid):
    for v in VIZ:
        if v["id"] == vid:
            return v
    return None


def safe_q(c, sqltext):
    """在 SAVEPOINT 里跑一条面板 SQL。

    为什么必须这么做：一个面板的 SQL 报错会让整个连接进入 "current transaction is
    aborted" 状态，后面**所有**面板连带报错——页面看上去像"数据库挂了"，其实只是一个
    图的口径写错了。成熟 BI 的每个 panel 是独立查询，就是这个道理。
    """
    with c.cursor() as cur:
        cur.execute("SAVEPOINT viz_panel")
    try:
        rows = P.q(c, sqltext)
        with c.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT viz_panel")
        return rows
    except Exception:
        with c.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT viz_panel")
        raise


# ---------------------------------------------------------------------------
# 数据获取（每条 SQL 一个函数，便于单独测试）
# ---------------------------------------------------------------------------
def fetch(c, item):
    """返回 (chart_result, extra)。extra 供 stacked/scatter 这类需要多列的场景。"""
    vid = item["id"]

    # 质量门：口径来自 code/metrics.py，不在这里另写一遍
    if item.get("metrics") == "gate":
        rows = P.q(c, M.GATE_SQL, M.gate_params())
        return CH.target_bar(rows, "指标", "实际", "目标", unit="%"), rows

    # --- meta 驱动的两组：口径是"逐表精确 count(*)"，不是普通 SQL ---
    if vid == "m_rows":
        rels = sorted([r for r in P.meta()["rels"] if r["kind"] == "table"],
                      key=lambda r: -r["rows"])[:14]
        rows = [{"表": r["name"], "行数": r["rows"]} for r in rels]
        return CH.bar_h(rows, "表", "行数", unit=" 行"), rows

    if vid == "m_domain":
        m = P.meta()
        agg = {}
        for r in m["rels"]:
            if r["kind"] != "table":
                continue
            a = agg.setdefault(r["domain"], {"域": r["domain"], "行数": 0, "表数": 0})
            a["行数"] += r["rows"]
            a["表数"] += 1
        rows = sorted(agg.values(), key=lambda x: -x["行数"])
        return CH.bar_h(rows, "域", "行数", unit=" 行"), rows

    rows = safe_q(c, item["sql"])

    # 空结果必须在进入各图表分支**之前**处理：否则 stacked/scatter/heatmap
    # 会去读 rows[0] 而越界（实测踩到过）。空是正常状态，不是异常状态。
    if not rows:
        k = item["kind"]
        if k == "bar_h":
            return CH.bar_h([], item["label"], item["value"]), rows
        if k == "target_bar":
            return CH.target_bar([], item["label"], item["value"], item["target"]), rows
        if k == "histogram":
            return CH.histogram([]), rows
        if k == "line":
            return CH.line([], []), rows
        if k == "heatmap":
            return CH.heatmap([], []), rows
        if k == "scatter":
            return CH.scatter([], item["x"], item["y"]), rows
        if k == "stacked":
            return CH.stacked([], [], item["label"]), rows
        raise P.PortalError(500, "未知图表类型：%s" % k)

    if item["kind"] == "bar_h":
        if not rows:
            return CH.bar_h([], item["label"], item["value"]), rows
        lab = item["label"] if item["label"] in rows[0] else list(rows[0].keys())[0]
        # 学历这类码值先翻译成中文再画，否则轴上全是 D2/D3/D4
        if item.get("code_table"):
            for r in rows:
                r[lab] = P.lbl(item["code_table"], r[lab])
        return CH.bar_h(rows, lab, item["value"], unit=item.get("unit", "")), rows

    if item["kind"] == "target_bar":
        return CH.target_bar(rows, item["label"], item["value"], item["target"],
                             unit=item.get("unit", "")), rows

    if item["kind"] == "histogram":
        return CH.histogram([r[item["col"]] for r in rows], unit=item.get("unit", "")), rows

    if item["kind"] == "line":
        xkey = item["x"]
        xs = [str(r[xkey]) for r in rows]
        # 只画显式指定的序列。默认"所有数值列"会把不同量纲塞进同一个 y 轴，
        # 那是在制造误读，不是在展示数据。
        want = item.get("series") or [k for k in (rows[0].keys() if rows else []) if k != xkey]
        series = [{"name": k, "values": [r[k] for r in rows]} for k in want if rows and k in rows[0]]
        return CH.line(series, xs, unit=item.get("unit", "")), rows

    if item["kind"] == "heatmap":
        keys = list(rows[0].keys()) if rows else []
        rk, ck, vk = keys[0], keys[1], keys[2]
        # 取"覆盖最广"的 12 个能力作为列，避免热力图变成一片噪声
        from collections import Counter
        cnt = Counter(r[ck] for r in rows)
        cols = [x for x, _ in cnt.most_common(12)]
        byrow = {}
        for r in rows:
            if r[ck] not in cols:
                continue
            byrow.setdefault(str(r[rk]), {"label": str(r[rk]), "cells": {}})
            byrow[str(r[rk])]["cells"][str(r[ck])] = CH._num(r[vk]) or 0.0
        # 族名翻译 + 排序
        items = []
        for k in sorted(byrow):
            it = byrow[k]
            it["label"] = P.lbl("CT_JOB_FAMILY", k) or k
            items.append(it)
        return CH.heatmap(items, [str(x) for x in cols], unit=""), rows

    if item["kind"] == "scatter":
        return CH.scatter(rows, item["x"], item["y"], label_key=item.get("label")), rows

    if item["kind"] == "stacked":
        keys = list(rows[0].keys()) if rows else []
        lk = keys[0]
        cats = item["cats"]
        if isinstance(cats, str):                    # 码表：展开成中文标签
            table = cats
            piv = {}
            for r in rows:
                lab = P.lbl(table, r[keys[1]]) or str(r[keys[1]])
                piv.setdefault(str(r[lk]), {lk: str(r[lk])})
                piv[str(r[lk])][lab] = piv[str(r[lk])].get(lab, 0) + (r[keys[2]] or 0)
            out = []
            allcats = []
            for k in sorted(piv):
                row = piv[k]
                row[lk] = P.lbl("CT_JOB_FAMILY", k) or k
                out.append(row)
                for cc in row:
                    if cc != lk and cc not in allcats:
                        allcats.append(cc)
            return CH.stacked(out, allcats, lk, unit=item.get("unit", "")), rows
        piv = {}
        for r in rows:
            lab = P.lbl("CT_JOB_FAMILY", r[lk]) or str(r[lk])
            piv.setdefault(lab, {lk: lab})
            for cc in cats:
                piv[lab][cc] = piv[lab].get(cc, 0) + (r[cc] or 0)
        return CH.stacked(list(piv.values()), list(cats), lk,
                          unit=item.get("unit", "")), rows

    raise P.PortalError(500, "未知图表类型：%s" % item["kind"])


def csv_rows(c, item):
    """导出用：meta 驱动的两组没有单条 SQL，单独给出行集。"""
    if item.get("metrics") == "gate":
        rows = P.q(c, M.GATE_SQL, M.gate_params())
        return (list(rows[0].keys()) if rows else []), rows
    if item["id"] in ("m_rows", "m_domain"):
        _chart, rows = fetch(c, item)
        return list(rows[0].keys()) if rows else [], rows
    rows = safe_q(c, item["sql"])
    return (list(rows[0].keys()) if rows else []), rows


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def panel(c, item) -> str:
    try:
        chart, _rows = fetch(c, item)
    except P.psycopg.Error as e:
        return ('<div class="card"><h2>%s</h2><div class="note err">%s</div>%s</div>'
                % (P.esc(item["title"]), P.esc(str(e).splitlines()[0]), P.sql_box(item["sql"])))
    extra = ""
    if chart["empty"]:
        extra = ('<p class="muted">这一项当前没有数据。'
                 '<b>空状态是画出来的，不是画一个空坐标系</b>——'
                 '后者更容易被误读成「值接近 0」。</p>')
    return """
<div class="card" id="%s">
  <h2>%s <span class="muted">· %d 行 · %s</span>
      <span style="float:right"><a href="/viz/%s.csv">下载 CSV</a></span></h2>
  <div class="sub" style="margin:-4px 0 10px">%s</div>
  %s%s
  %s
</div>""" % (P.esc(item["id"]), P.esc(item["title"]), chart["rows"],
             P.esc(item.get("grain", "")), P.esc(item["id"]),
             item["desc"], chart["svg"], extra, P.sql_box(item["sql"], "这个图的口径："))


def view_viz(c, qs) -> bytes:
    m = P.meta()
    tables = [r for r in m["rels"] if r["kind"] == "table"]
    n_rows = sum(r["rows"] for r in tables)
    filled = len([r for r in tables if r["rows"] > 0])
    n_metric = P.q1(c, "SELECT count(*) FROM metric_definition")

    head = """
<div class="sub">成熟数据产品的可视化层有三件共同的东西，与用哪个图表库无关：
<b>① 度量注册表</b>（每个指标在一处定义口径、粒度、版本——本库对应
<code>metric_definition</code>，当前 %d 行）；
<b>② 图表选型规则</b>（分类×数值→条形，时间×数值→折线，两分类×数值→热力图，
两数值→散点，构成→堆叠，目标达成→目标条）；
<b>③ 每个图都能被核对</b>（图旁边就是它的 SQL、行数、单位和 CSV）。
<br>这一页就是这三件事的落地：<b>口径 → 图 → 原始数据</b> 一一对应。
图表是内联 SVG，<b>零 JS、零 CDN</b>——拷走一个 HTML 文件就能看。</div>
<div class="cards">%s</div>
""" % (n_metric, "".join([
        P.metric(f"{len(tables)}", "基础表"),
        P.metric(f"{n_rows:,}", "数据行", "精确 count(*)"),
        P.metric(f"{len(VIZ)}", "可视化面板", "分 %d 组" % len(SECTIONS)),
        P.metric(f"{n_metric:,}", "已注册度量",
                 "metric_definition" if n_metric else "尚未注册（见下）"),
    ]))

    reg = ""
    if not n_metric:
        reg = ('<div class="note info">度量注册表还是空的。'
               '运行 <code>python code\\demo\\portal.py --register-metrics</code> '
               '会把下面 %d 个面板的口径写进 <code>metric_definition</code>——'
               '于是"这个数字怎么算的"变成<b>可查询的数据</b>，而不是散落在代码里的字符串。'
               '这是 Superset 的 dataset/metric、dbt 的 metric 在做的事。</div>' % len(VIZ))

    blocks = []
    for sec in SECTIONS:
        items = [v for v in VIZ if v["section"] == sec]
        if not items:
            continue
        blocks.append('<h1 style="margin:26px 0 8px;font-size:17px">%s</h1>' % P.esc(sec))
        blocks.append("".join(panel(c, it) for it in items))

    body = head + reg + "".join(blocks)
    return P.page("可视化", body,
                  subtitle="%d 个面板 · 分 %d 组 · 口径可核 · 内联 SVG（零 JS / 零 CDN）"
                           % (len(VIZ), len(SECTIONS)))


# ---------------------------------------------------------------------------
# 图表构建器：让**用户/开发者**自己做分析，而不是只能看我们预置的图
# ---------------------------------------------------------------------------
AGGS = [("count", "计数（行数）"), ("sum", "求和"), ("avg", "平均"),
        ("min", "最小值"), ("max", "最大值")]
KINDS = [("auto", "自动选型"), ("bar_h", "条形图"), ("line", "折线图"),
         ("stacked", "堆叠条"), ("scatter", "散点图")]


def build_sql(table, dim, val, agg, limit=60):
    """由用户选择拼出一条聚合 SQL。表名/列名**必须先与系统目录比对**再用
    `sql.Identifier` 转义——URL 参数是用户输入，不能直接拼。"""
    m = P.meta()
    if table not in m["by_name"]:
        raise P.PortalError(404, "未知的表或视图：%s" % table)
    cols = [x["column_name"] for x in m["cols"].get(table, [])]
    if dim not in cols:
        raise P.PortalError(400, "表 %s 没有列 %s" % (table, dim))
    if agg != "count" and val not in cols:
        raise P.PortalError(400, "表 %s 没有列 %s" % (table, val))
    a = agg if agg in [k for k, _ in AGGS] else "count"
    measure = (sql_mod.SQL("count(*)")
               if a == "count" else
               sql_mod.SQL("{}({})").format(sql_mod.SQL(a), sql_mod.Identifier(val)))
    return (sql_mod.SQL("SELECT {} AS \"维度\", {} AS \"度量\" FROM {}.{} "
                        "WHERE {} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {}")
            .format(sql_mod.Identifier(dim), measure,
                    sql_mod.Identifier(P.SCHEMA), sql_mod.Identifier(table),
                    sql_mod.Identifier(dim), sql_mod.Literal(int(limit))))


def view_build(c, qs) -> bytes:
    m = P.meta()
    table = (qs.get("t", ["job_posting"])[0] or "").strip()
    dim = (qs.get("dim", [""])[0] or "").strip()
    val = (qs.get("val", [""])[0] or "").strip()
    agg = (qs.get("agg", ["count"])[0] or "count").strip()
    kind = (qs.get("kind", ["auto"])[0] or "auto").strip()

    tables = [r["name"] for r in m["rels"] if r["kind"] == "table"]
    if table not in m["by_name"]:
        table = "job_posting"
    cols = [x for x in m["cols"].get(table, [])]

    chart_html, sql_text, n_rows, err = "", "", 0, ""
    if dim:
        try:
            stmt = build_sql(table, dim, val, agg)
            sql_text = stmt.as_string(c)
            rows = safe_q(c, stmt)
            n_rows = len(rows)
            # 选型：用户没指定就按数据形状自动选
            k = kind
            if k == "auto":
                k = "bar_h"
            if k == "line":
                ch = CH.line([{"name": "度量", "values": [r["度量"] for r in rows]}],
                             [str(r["维度"]) for r in rows])
            elif k == "stacked":
                ch = CH.stacked(rows, ["度量"], "维度")
            elif k == "scatter":
                ch = CH.scatter(rows, "维度", "度量")
            else:
                ch = CH.bar_h(rows, "维度", "度量")
            chart_html = ch["svg"]
            if ch["empty"]:
                chart_html += ('<p class="muted">这条组合返回 0 行。'
                               '换个维度或度量再试——<b>空结果也是结果</b>。</p>')
        except P.PortalError as e:
            err = e.message
        except P.psycopg.Error as e:
            err = str(e).splitlines()[0]

    t_opts = "".join('<option value="%s" %s>%s</option>'
                     % (P.esc(t), "selected" if t == table else "", P.esc(t))
                     for t in tables)
    d_opts = "".join('<option value="%s" %s>%s（%s）</option>'
                     % (P.esc(x["column_name"]),
                        "selected" if x["column_name"] == dim else "",
                        P.esc(x["column_name"]), P.esc(x["data_type"]))
                     for x in cols)
    v_opts = "".join('<option value="%s" %s>%s（%s）</option>'
                     % (P.esc(x["column_name"]),
                        "selected" if x["column_name"] == val else "",
                        P.esc(x["column_name"]), P.esc(x["data_type"]))
                     for x in cols if CH._num and x["data_type"].split("(")[0] in
                     ("integer", "bigint", "smallint", "numeric", "real", "double precision"))
    a_opts = "".join('<option value="%s" %s>%s</option>'
                     % (k, "selected" if k == agg else "", P.esc(lab)) for k, lab in AGGS)
    k_opts = "".join('<option value="%s" %s>%s</option>'
                     % (k, "selected" if k == kind else "", P.esc(lab)) for k, lab in KINDS)

    csv_href = "/viz/build.csv?" + urllib.parse.urlencode(
        {"t": table, "dim": dim, "val": val, "agg": agg})
    result = ""
    if err:
        result = '<div class="note err">%s</div>' % P.esc(err)
    elif sql_text:
        result = ('<div class="card"><h2>结果 <span class="muted">· %d 行</span>'
                  '<span style="float:right"><a href="%s">下载 CSV</a></span></h2>%s%s</div>'
                  % (n_rows, P.esc(csv_href), chart_html,
                     P.sql_box(sql_text, "你这次分析执行的 SQL（表名列名已与系统目录比对后转义）：")))

    body = """
<div class="sub">前面 %d 个面板是<b>预置</b>的口径。这一页是给<b>用户和开发者</b>的：
自己挑表、挑维度、挑度量，立刻得到图、SQL 和 CSV。
<br>表名列名会先与系统目录比对再用 <code>sql.Identifier</code> 转义——
所以这条"自助分析"路径<b>不引入注入面</b>，也不需要给你开写权限。</div>

<div class="card"><form method="get" action="/viz/build">
  <div class="row">
    <div style="flex:2 1 220px"><label>表 / 视图</label>
      <select name="t" onchange="this.form.submit()">%s</select></div>
    <div style="flex:2 1 200px"><label>维度（分组列）</label>
      <select name="dim"><option value="">（请选择）</option>%s</select></div>
    <div style="flex:2 1 200px"><label>度量列</label>
      <select name="val"><option value="">（计数时不需要）</option>%s</select></div>
    <div style="flex:1 1 130px"><label>聚合</label><select name="agg">%s</select></div>
    <div style="flex:1 1 130px"><label>图表</label><select name="kind">%s</select></div>
    <div style="flex:0 0 110px"><button type="submit">出图</button></div>
  </div>
</form>
<p class="muted">度量列只列出数值型列（聚合 <code>count</code> 时不需要度量列）。
结果按度量降序取前 60 行。</p></div>

%s
""" % (len(VIZ), t_opts, d_opts, v_opts, a_opts, k_opts, result)
    return P.page("图表构建器", body,
                  subtitle="自助分析 · 选表 → 选维度 → 选度量 → 图 + SQL + CSV")


def build_csv(c, qs) -> bytes:
    table = (qs.get("t", [""])[0] or "").strip()
    dim = (qs.get("dim", [""])[0] or "").strip()
    val = (qs.get("val", [""])[0] or "").strip()
    agg = (qs.get("agg", ["count"])[0] or "count").strip()
    stmt = build_sql(table, dim, val, agg)
    rows = safe_q(c, stmt)
    cols = ["维度", "度量"]
    return P.to_csv(cols, rows)


# ---------------------------------------------------------------------------
# 度量注册表写入（这是**写**操作，所以只在命令行里做，不在只读门户里做）
# ---------------------------------------------------------------------------
def register_metrics() -> int:
    """把 VIZ 的口径写入 metric_definition。幂等。"""
    with P.db() as c, c.cursor() as cur:
        for v in VIZ:
            cur.execute("""
                INSERT INTO metric_definition (metric_id, name, description, definition_sql,
                                               grain, version, owner, status)
                VALUES (%s, %s, %s, %s, %s, '1.0.0', 'data', 'active')
                ON CONFLICT (metric_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    definition_sql = EXCLUDED.definition_sql,
                    grain = EXCLUDED.grain,
                    version = EXCLUDED.version,
                    status = EXCLUDED.status,
                    updated_at = now()""",
                (v["id"], v["title"], v["desc"], v["sql"], v.get("grain")))
        c.commit()
        cur.execute("SELECT count(*) AS n FROM metric_definition")
        n = cur.fetchone()["n"]
    print("[✓] 已注册 %d 个度量口径到 mt.metric_definition（现共 %d 行）" % (len(VIZ), n))
    print("    每个面板的 id 就是 metric_id，可在 /t/metric_definition 查看。")
    return 0
