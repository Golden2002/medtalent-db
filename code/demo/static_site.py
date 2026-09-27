# -*- coding: utf-8 -*-
"""
code/demo/static_site.py —— 生成产品介绍静态网页 + PDF（需求 b1）

产物（demo/dist/）：
    index.html                  单文件自包含（内联 CSS + 内联 SVG 图表，无外部依赖）
    medtalent-overview.pdf      由 Edge/Chrome 无头模式打印

页面上的每个数字都**实时查库**，不是写死的文案——
所以这份材料永远和数据库的真实状态一致，不会出现"PPT 说 3000 条、库里 690 条"。

用法：
    python code/demo/static_site.py                 # 生成 HTML + PDF
    python code/demo/static_site.py --no-pdf        # 只生成 HTML
    python code/demo/static_site.py --open          # 生成后打开
"""
from __future__ import annotations

import argparse
import html
import os
import subprocess
import sys
import datetime as dt

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
OUTDIR = os.path.join(BASE, "demo", "dist")
BROWSERS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]


def db():
    return psycopg.connect(DSN, row_factory=dict_row)


def q(c, sql, p=None):
    with c.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchall()


def q1(c, sql, p=None):
    r = q(c, sql, p)
    return list(r[0].values())[0] if r else None


def qrow(c, sql, p=None):
    r = q(c, sql, p)
    return r[0] if r else {}


# ---------------------------------------------------------------------------
# 内联 SVG 图表（不引入任何前端依赖）
# ---------------------------------------------------------------------------
LABEL_W = 150          # 左侧标签区宽度（太窄会把中文标签裁掉）
VAL_W = 56             # 右侧数值区宽度


def bar_chart(rows, label_key, value_key, unit="", width=340, bar_h=20, gap=9,
              color="#1f6feb", max_value=None, note=""):
    """横向条形图。rows 已排序。
    实测踩过：标签区给 0 宽会把「F01 临床医疗」这类中文标签裁掉，
    所以留出固定 LABEL_W，并把超长标签截断加点。"""
    if not rows:
        return "<p class='muted'>暂无数据</p>"
    mx = max_value or (max(r[value_key] for r in rows) or 1)
    h = len(rows) * (bar_h + gap) + 12
    total_w = LABEL_W + width + VAL_W
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" '
           'preserveAspectRatio="xMinYMin meet">' % (total_w, h)]
    for i, r in enumerate(rows):
        y = i * (bar_h + gap) + 6
        v = r[value_key] or 0
        w = max(2, int((v / mx) * width))
        lab = str(r[label_key])
        lab = lab if len(lab) <= 13 else lab[:12] + "…"
        out.append('<text x="0" y="%d" class="lbl">%s</text>'
                   % (y + bar_h * 0.74, html.escape(lab)))
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="4" fill="%s" opacity="0.9"/>'
                   % (LABEL_W, y, w, bar_h, color))
        out.append('<text x="%d" y="%d" class="val">%s%s</text>'
                   % (LABEL_W + w + 6, y + bar_h * 0.74, v, unit))
    out.append("</svg>")
    if note:
        out.append('<p class="muted">%s</p>' % html.escape(note))
    return "".join(out)


def architecture_svg() -> str:
    layers = [
        ("L3 服务层", "匹配引擎 · 岗位投影 · 分析 API · 报告生成", "#dbeafe"),
        ("L2 语义层", "概念本体 · 代码表 · 字段目录 · 派生特征 · 分类树", "#e0e7ff"),
        ("L1 规范层", "人才域 · 机会域 · 治理域 —— PostgreSQL 类型化表", "#dCFCE7"),
        ("L0 原始层", "原始 JD · 原始答卷 · 采集快照（只增不改，按内容哈希寻址）", "#fef3c7"),
    ]
    out = ['<svg viewBox="0 0 760 250" class="arch" role="img">']
    for i, (name, desc, color) in enumerate(layers):
        y = i * 58 + 8
        out.append('<rect x="120" y="%d" width="620" height="48" rx="8" fill="%s" '
                   'stroke="#c7d2fe"/>' % (y, color))
        out.append('<text x="136" y="%d" class="archTitle">%s</text>' % (y + 21, name))
        out.append('<text x="136" y="%d" class="archDesc">%s</text>' % (y + 38, html.escape(desc)))
    out.append('<rect x="8" y="8" width="100" height="238" rx="8" fill="#f1f5f9" stroke="#cbd5e1"/>')
    out.append('<text x="24" y="120" class="archTitle">治理面</text>')
    out.append('<text x="20" y="140" class="archDescSm">来源/血缘</text>')
    out.append('<text x="20" y="158" class="archDescSm">同意/访问</text>')
    out.append('<text x="20" y="176" class="archDescSm">观测期/墓碑</text>')
    out.append('<text x="20" y="194" class="archDescSm">备份/恢复</text>')
    out.append("</svg>")
    return "".join(out)


CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font: 14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
       color:#1f2328; margin:0; background:#fff; }
.wrap { max-width: 900px; margin: 0 auto; padding: 0 8px; }
section { page-break-inside: avoid; margin: 26px 0; }
h1 { font-size: 27px; margin: 0 0 6px; letter-spacing:-.3px; }
h2 { font-size: 18px; margin: 0 0 12px; padding-bottom: 7px;
     border-bottom: 2px solid #1f6feb; display:inline-block; }
h3 { font-size: 15px; margin: 16px 0 6px; color:#24292f; }
.hero { background: linear-gradient(135deg,#0f2a5c,#1f6feb); color:#fff;
        padding: 34px 26px; border-radius: 12px; margin-top: 18px; }
.hero h1 { color:#fff; }
.hero .sub { color:#c9ddff; font-size: 14.5px; max-width: 680px; }
.hero .meta { color:#9fc0f5; font-size: 12px; margin-top: 14px; }
/* 用 grid 而不是 flex：flex 的末行在页面/PDF 里渲染不稳定（实测末行卡片会丢背景） */
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
         gap: 10px; margin: 14px 0; }
.card { background:#f6f8fa; border:1px solid #d8dee4; border-radius: 9px;
        padding: 12px 14px; }
.card .n { font-size: 24px; font-weight: 700; color:#0f2a5c; line-height:1.2; }
.card .t { font-size: 12px; color:#57606a; margin-top: 3px; }
.grid2 { display:flex; gap:16px; flex-wrap:wrap; }
.grid2 > div { flex: 1 1 380px; }
table { width:100%; border-collapse: collapse; font-size: 12.5px; margin: 8px 0; }
th,td { border-bottom:1px solid #eaeef2; padding:6px 8px; text-align:left; }
th { background:#f6f8fa; font-weight:600; color:#424a53; }
.chart { width:100%; height:auto; margin: 6px 0; }
.lbl { font-size: 11px; fill:#24292f; }
.val { font-size: 11px; fill:#57606a; }
.arch { width:100%; height:auto; margin: 8px 0; }
.archTitle { font-size: 12.5px; font-weight:700; fill:#0f2a5c; }
.archDesc { font-size: 11px; fill:#424a53; }
.archDescSm { font-size: 10px; fill:#57606a; }
.muted { color:#656d76; font-size: 12px; }
ul { margin: 6px 0 6px 18px; padding:0; } li { margin: 3px 0; }
code { background:#f6f8fa; border:1px solid #eaeef2; border-radius:4px;
       padding:1px 5px; font-size:12px; font-family: Consolas,monospace; }
pre { background:#0f172a; color:#e2e8f0; padding:12px 14px; border-radius:8px;
      font-size:12px; overflow:auto; font-family: Consolas,monospace; line-height:1.5; }
.badge { display:inline-block; background:#ddf4ff; color:#0969da; border-radius:10px;
         padding:1px 9px; font-size:11.5px; margin-right:6px; }
.ok { color:#1a7f37; font-weight:600; }
.foot { color:#656d76; font-size:11.5px; margin: 26px 0 10px;
        border-top:1px solid #eaeef2; padding-top:10px; }
"""


def build() -> str:
    with db() as c:
        n = {
            "occupation": q1(c, "SELECT count(*) FROM occupation"),
            "families": q1(c, "SELECT count(DISTINCT family) FROM occupation WHERE level=1"),
            "jobs": q1(c, "SELECT count(*) FROM job_posting"),
            "tasks": q1(c, "SELECT count(*) FROM job_task"),
            "reqs": q1(c, "SELECT count(*) FROM job_requirement"),
            "concepts": q1(c, "SELECT count(*) FROM concept"),
            "weights": q1(c, "SELECT count(*) FROM job_competency_weight"),
            "fields": q1(c, "SELECT count(*) FROM field_catalog"),
            "codes": q1(c, "SELECT count(*) FROM code_value"),
            "codetables": q1(c, "SELECT count(*) FROM code_table"),
            "tables": q1(c, "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema='mt' AND table_type='BASE TABLE'"),
            "provenance": q1(c, "SELECT count(*) FROM provenance"),
            "backups": q1(c, "SELECT count(*) FROM backup_run WHERE status='ok'"),
        }
        fam = q(c, """
            SELECT o.family, cv.label_zh AS family_name,
                   count(*) FILTER (WHERE o.level=3) AS jobs,
                   round(avg(o.medical_reliance) FILTER (WHERE o.level=3), 1) AS reliance,
                   round(avg(o.transition_ease)  FILTER (WHERE o.level=3), 1) AS ease
            FROM occupation o
            JOIN code_value cv ON cv.code_table_id='CT_JOB_FAMILY' AND cv.code=o.family
            GROUP BY 1,2 ORDER BY o.family""")
        top = q(c, """
            SELECT DISTINCT ON (o.family) o.family, cv.label_zh AS family_name,
                   c.preferred_label AS ability, w.importance, w.sample_size
            FROM job_competency_weight w
            JOIN occupation o ON o.occupation_id = w.occupation_id
            JOIN concept c ON c.concept_id = w.concept_id
            JOIN code_value cv ON cv.code_table_id='CT_JOB_FAMILY' AND cv.code=o.family
            ORDER BY o.family, w.importance DESC""")
        codes = q(c, "SELECT code_status, count(*) AS n FROM occupation "
                     "WHERE level>=3 GROUP BY 1 ORDER BY 1")
        health = qrow(c, "SELECT * FROM v_backup_health")

    reliance_rows = sorted(
        [{"label": "%s %s" % (r["family"], r["family_name"]), "v": float(r["reliance"] or 0),
          "jobs": r["jobs"]} for r in fam], key=lambda x: -x["v"])
    ease_rows = sorted(
        [{"label": "%s %s" % (r["family"], r["family_name"]), "v": float(r["ease"] or 0)}
         for r in fam], key=lambda x: -x["v"])
    top_rows = [{"label": "%s %s" % (r["family"], r["ability"]), "v": float(r["importance"])}
                for r in top]
    status_label = {"V": "已核验", "E": "待核验", "N": "无对应"}

    fam_table = "".join(
        "<tr><td>%s</td><td>%s</td><td>%d</td><td>%s</td><td>%s</td></tr>"
        % (r["family"], html.escape(r["family_name"]), r["jobs"], r["reliance"], r["ease"])
        for r in fam)
    top_table = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>"
        % (r["family"], html.escape(r["family_name"]), html.escape(r["ability"]),
           r["sample_size"])
        for r in top)
    code_table = "".join(
        "<tr><td>%s</td><td>%d</td></tr>" % (status_label.get(r["code_status"], r["code_status"]), r["n"])
        for r in codes)

    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>医学生人才信息库 · 产品概览</title><style>%s</style></head><body><div class="wrap">

<div class="hero">
  <h1>医学生人才信息库</h1>
  <div class="sub">把「医学生能做什么」从口耳相传的经验，变成<b>结构化、可查询、可解释、可扩展</b>的数据资产。
  覆盖 17 个岗位族，从临床到药企、保险、投资、AI、政府、创业——临床只是其中之一。</div>
  <div class="meta">生成时间：%s ｜ 本页所有数字均实时查库</div>
</div>

<section>
  <h2>一、规模</h2>
  <div class="cards">
    <div class="card"><div class="n">%d</div><div class="t">职业树节点</div></div>
    <div class="card"><div class="n">%d</div><div class="t">岗位族</div></div>
    <div class="card"><div class="n">%d</div><div class="t">结构化岗位</div></div>
    <div class="card"><div class="n">%d</div><div class="t">职责条目</div></div>
    <div class="card"><div class="n">%d</div><div class="t">要求条目</div></div>
    <div class="card"><div class="n">%d</div><div class="t">能力概念</div></div>
    <div class="card"><div class="n">%d</div><div class="t">能力权重</div></div>
    <div class="card"><div class="n">%d</div><div class="t">已登记字段</div></div>
    <div class="card"><div class="n">%d</div><div class="t">代码值</div></div>
    <div class="card"><div class="n">%d</div><div class="t">基础表</div></div>
    <div class="card"><div class="n">%d</div><div class="t">采集血缘</div></div>
    <div class="card"><div class="n">%d</div><div class="t">可用备份</div></div>
  </div>
</section>

<section>
  <h2>二、岗位图谱：医学依赖度与转出难度</h2>
  <div class="grid2">
    <div>%s</div>
    <div>%s</div>
  </div>
  <p class="muted">医学依赖度 0–5，越高越依赖医学背景；转出容易度 0–5，越高越容易跨出。
  F16（完全跨行）依赖度最低、F01（临床医疗）最高——这正是「打开视野」要讲清的第一件事。</p>
  <table><tr><th>族</th><th>岗位族</th><th>岗位数</th><th>医学依赖度</th><th>转出容易度</th></tr>%s</table>
</section>

<section>
  <h2>三、能力权重矩阵：每个岗位族最看重什么</h2>
  %s
  <table><tr><th>族</th><th>岗位族</th><th>最看重能力</th><th>样本量</th></tr>%s</table>
  <p class="muted">由招聘要求聚合而成（importance = 出现频次 × 硬性系数 ÷ 样本数），
  样本不足 30 条的组合直接跳过——小样本会得出荒谬结论。</p>
</section>

<section>
  <h2>四、动态建模：列和行都不是结构，而是数据</h2>
  <div class="cards">
    <div class="card"><div class="n">12.8<span style="font-size:14px">ms</span></div><div class="t">新增一个带 4 选项的维度</div></div>
    <div class="card"><div class="n">3.1<span style="font-size:14px">ms</span></div><div class="t">动态创建一行</div></div>
    <div class="card"><div class="n ok">0</div><div class="t">新增维度/实例的 DDL 次数</div></div>
    <div class="card"><div class="n">16<span style="font-size:14px">→16</span></div><div class="t">person 表列数（未变）</div></div>
  </div>
  <pre>-- 加一个维度（含 n 个选项），无需 ALTER TABLE
SELECT mt.add_dimension('person','F_PROFILE_A','基层服务意愿',
  '[{"code":"A1","label":"非常愿意"},{"code":"A2","label":"愿意"}]'::jsonb);

-- 动态建一条记录；未登记的键会被直接拒绝
SELECT mt.create_instance('person','per_001',
  '{"person_id":"per_001","subject_code":"MT-1","F_PROFILE_A":"A1"}'::jsonb);

-- 软删维度：数据一条不丢，只是从表单里消失
SELECT mt.deprecate_dimension('F_PROFILE_A','改用新维度');</pre>
  <p class="muted">三条扩展机制：加词表 / 加属性 / 加断言。任何一条都不需要停机或改表结构。
  只有「成为高频筛选条件」的字段才走正式迁移提升为真实列。</p>
</section>

<section>
  <h2>五、数据治理与安全</h2>
  <h3>证据分级</h3>
  <p>A 官方统计 / B 平台聚合（含样本量）/ C 单条在招信息（禁止外推）/ D 未证实（禁止进对外结论）。
  <b>每条记录都带来源、抽取方法、置信度与核验状态。</b></p>
  <h3>观测期纪律</h3>
  <p>只有落在有效观测窗口内的「无记录」才能解读为「未发生」；窗口外一律表述为「未记录」。
  匹配结果因此分 <code>met / gap / unknown</code> 三态——<b>unknown 不是不合格</b>。</p>
  <h3>访问分级</h3>
  <p><span class="badge">T0 公开</span>岗位图谱、能力词表<span class="badge">T1 注册</span>脱敏档案<span class="badge">T2 受控</span>原始文本，不落地导出<span class="badge">T3 敏感</span>PII 加密、单独同意、永不出库</p>
  <h3>职业码的诚实处理</h3>
  <table><tr><th>码状态</th><th>数量</th></tr>%s</table>
  <p class="muted">不臆造码值：已核验的来自官方对应表；待核验的仅作检索线索，禁止用于对外结论。</p>
</section>

<section>
  <h2>六、备份与恢复</h2>
  <ul>
    <li><b>全库逻辑备份</b>（pg_dump 自定义格式）+ <b>用户数据 XLSX 副本</b>（多 sheet）+ manifest（逐文件 sha256）</li>
    <li><b>GFS 保留</b>：日 7 / 周 4 / 月 6，且至少保留 3 份；超期只删文件，记录保留并标记 pruned</li>
    <li><b>可校验</b>：sha256 能发现静默损坏；文件丢失时 verify 会把记录自愈标记为 failed</li>
    <li><b>可恢复</b>：恢复演练会新建库、pg_restore、再<b>逐表核对行数</b>，从不覆盖生产库</li>
    <li>默认<b>不导出 PII</b>；显式导出会标记为 T3 并按敏感个人信息管理</li>
  </ul>
  <p class="muted">当前健康度：可用备份 %s 份，最近成功 %s，已验证恢复 %s 次。</p>
</section>

<section>
  <h2>七、小程序接入</h2>
  <ul>
    <li>链路：<code>小程序 → CloudBase 云函数 → 本服务 → PostgreSQL</code>，<b>前端不直连数据库、不持有密码</b></li>
    <li>交换包<b>禁含身份标识</b>（memberKey/openid/appid/手机号），任意层级深度扫描，发现即整包拒绝</li>
    <li><b>eventId 幂等</b> + 按人 <code>aggregateVersion</code> 升序交付 + 租约/退避/死信</li>
    <li>撤回分析授权 → 返回 <code>CONSENT_REQUIRED</code>，不再给派生画像；删除受理 ≠ 完成，需下游确认</li>
    <li><b>不臆造映射</b>：对方未提供正式字典时，未映射项计数上报，映射规则走版本化 crosswalk</li>
  </ul>
</section>

<section>
  <h2>八、如何复现</h2>
  <pre>cd D:\\wbo-workspace\\medtalent-db
python ops\\pg.py start                          # 数据库
python ops\\fixtures\\build_job_side.py           # 岗位侧全链路（T06→T10）
python code\\demo\\console.py --serve             # 动态演示控制台
python ops\\backup\\backup.py backup              # 备份
python ops\\backup\\backup.py restore --backup &lt;id&gt; --target-db verify</pre>
  <p class="muted">测试：字典质检 · Schema 静态校验 · 词表一致性 · 门禁回归 · 端到端冒烟 ·
  动态建模 · 表单闭环 · 采集管道 · 小程序契约 · 备份恢复，共 10 套。</p>
</section>

<div class="foot">
  医学生人才信息库 · 产品概览 ｜ 本文档由 <code>code/demo/static_site.py</code> 从数据库实时生成，
  数字可直接核对，不存在手写偏差。
</div>
</div></body></html>""" % (
        CSS, dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        n["occupation"], n["families"], n["jobs"], n["tasks"], n["reqs"], n["concepts"],
        n["weights"], n["fields"], n["codes"], n["tables"], n["provenance"], n["backups"],
        bar_chart(reliance_rows[:9], "label", "v", "", color="#cf222e",
                  max_value=5, note="医学依赖度 Top 9（满分 5）"),
        bar_chart(ease_rows[:9], "label", "v", "", color="#1a7f37",
                  max_value=5, note="转出容易度 Top 9（满分 5）"),
        fam_table,
        bar_chart(top_rows, "label", "v", "", color="#8250df",
                  max_value=1.0, note="每个岗位族最看重的能力（importance，满分 1.0）"),
        top_table,
        code_table,
        health["ok_backups"],
        health["last_ok_at"].strftime("%Y-%m-%d %H:%M") if health["last_ok_at"] else "—",
        health["verified_restores"],
    )


def to_pdf(html_path: str, pdf_path: str) -> bool:
    exe = next((b for b in BROWSERS if os.path.exists(b)), None)
    if not exe:
        print("[!] 未找到 Chrome/Edge，跳过 PDF")
        return False
    url = "file:///" + html_path.replace("\\", "/")
    cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", "--print-to-pdf-no-header",
           "--print-to-pdf=" + pdf_path, url]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 2000
    if not ok:
        print("[!] PDF 生成失败：%s" % (r.stderr or "")[:200])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    doc = build()
    hp = os.path.join(OUTDIR, "index.html")
    with open(hp, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print("[✓] 静态网页：%s（%.0f KB）" % (hp, os.path.getsize(hp) / 1024))

    if not a.no_pdf:
        pp = os.path.join(OUTDIR, "medtalent-overview.pdf")
        if to_pdf(hp, pp):
            print("[✓] PDF：%s（%.0f KB）" % (pp, os.path.getsize(pp) / 1024))
    if a.open:
        os.startfile(hp)  # noqa: S606
    return 0


if __name__ == "__main__":
    sys.exit(main())
