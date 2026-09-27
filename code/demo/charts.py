# -*- coding: utf-8 -*-
"""
code/demo/charts.py —— 纯 Python 内联 SVG 图表库（零 JS / 零 CDN / 零依赖）

为什么自己画，而不是引入 ECharts / Chart.js / Plotly：

  1. **没有前端构建链**。这个项目的所有界面都是标准库 `http.server` 直出的 HTML。
     引一个图表库就意味着 CDN、离线失效、版本漂移、以及"演示机上打不开"。
     内联 SVG 是纯文本，跟着 HTML 一起产出，拷走一个文件就能看。
  2. **可断言**。"图上画了 7 根柱子""最大值那根最长"在 SVG 里是可检查的事实，
     而在 canvas 里只能截图比对。`ops/tests/viz_test.py` 就是靠这个做回归的。
  3. **口径可核**。每个图都带它执行的 SQL、行数和单位——这是本项目的纪律，
     第三方图表库不会替你保证。

成熟 BI 的形态（Metabase / Superset / Grafana / gnomAD 那一类）里，
真正与工具无关的部分是**图表选型规则**和**数据到视觉通道的映射**，
不是某个库的 API。这四条规则实现了它们：

  分类 × 数值      → 横条图（`bar_h`）    标签可读、天然排序
  时间 × 数值      → 折线图（`line`）     比较趋势，不比较面积
  两分类 × 数值    → 热力图（`heatmap`）  用色阶承载第三个维度
  两个数值         → 散点图（`scatter`）  看相关与离群
  构成占比         → 堆叠条（`stacked`）  比饼图好读，且能并排比较
  目标达成         → 目标条（`target_bar`）实际值 vs 目标线（质量门用）
  分布             → 直方图（`histogram`）看形状，不看个体

空数据一律画**明确的空状态**，不画空坐标系 —— 一个没有数据的图比没有图更误导。

所有图表函数返回 `dict(svg=..., rows=..., empty=...)`。
"""
from __future__ import annotations

import html
import math

# 配色：主序列用蓝色阶承载"量"，状态色只用于达标/警示，避免把"大"和"坏"混为一谈。
PALETTE = {
    "blue": "#2f81f7", "green": "#2da44e", "amber": "#bf8700", "red": "#cf222e",
    "purple": "#8250df", "teal": "#1b7c83", "gray": "#8c959f", "axis": "#d0d7de",
    "ink": "#1f2328", "muted": "#656d76", "bg": "#ffffff", "grid": "#eaeef2",
}
RAMP = ["#dbeafe", "#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8"]
DIVERGE = ["#cf222e", "#f6b0b0", "#f6f8fa", "#a5d6ff", "#0969da"]  # 低→高（含负值场景）

FONT = '-apple-system,"Segoe UI","Microsoft YaHei",sans-serif'


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def text_w(s, size=12.0) -> float:
    """估算文本像素宽度：CJK 按 ~1.9 个西文字宽算。

    这个估算决定左边距。踩过的坑：早先固定左边距，中文标签被裁掉——
    "看起来像图表 bug，其实是排版没算字符宽度"。
    """
    s = "" if s is None else str(s)
    units = sum(1.0 if ord(c) < 128 else 1.9 for c in s)
    return units * size * 0.55


def _num(v):
    """把数据库返回的数值转成 float。

    psycopg 对 `numeric` 列返回 `decimal.Decimal`，对 `bigint` 返回 int。
    图表库不该假设"数值就是 float"——本项目在备份模块已经因为 Decimal
    不可 JSON 序列化踩过一次，这里是同一类问题的第二次露面。
    """
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v, unit="") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 2)
    if isinstance(v, int) or (isinstance(v, float) and float(v).is_integer()):
        return f"{int(v):,}{unit}"
    return f"{v:,}{unit}"


def _svg(w, h, body, extra="") -> str:
    return ('<svg viewBox="0 0 %d %d" width="100%%" height="%d" '
            'preserveAspectRatio="xMinYMin meet" role="img" '
            'style="font-family:%s;max-width:100%%" %s>%s</svg>'
            % (w, h, h, FONT, extra, body))


def _empty(w, h, msg="没有数据") -> str:
    return ('<div class="chart-empty" style="height:%dpx">%s</div>' % (h, esc(msg)))


def _result(svg, rows, empty=False) -> dict:
    return {"svg": svg, "rows": rows, "empty": empty}


def _title_tag(text) -> str:
    return "<title>%s</title>" % esc(text)


# ---------------------------------------------------------------------------
# 横条图：分类 × 数值（最常用）
# ---------------------------------------------------------------------------
def bar_h(rows, label_key, value_key, unit="", width=760, row_h=22,
          color=None, max_rows=18, value_fmt=None, href_key=None) -> dict:
    """rows: [dict]；取 label_key 作标签、value_key 作长度。"""
    data = [r for r in rows if r.get(value_key) is not None][:max_rows]
    if not data:
        return _result(_empty(width, 90), 0, True)
    labels = [str(r[label_key]) for r in data]
    vals = [_num(r[value_key]) or 0.0 for r in data]
    vmax = max(vals) or 1.0
    lw = min(320, max(90, max(text_w(l) for l in labels) + 12))
    pad_r, pad_t = 74, 8
    plot_w = width - lw - pad_r
    h = pad_t + row_h * len(data) + 8
    color = color or PALETTE["blue"]
    body = []
    for i, (lab, v, r) in enumerate(zip(labels, vals, data)):
        y = pad_t + i * row_h
        bw = max(1.5, plot_w * v / vmax)
        tip = "%s：%s" % (lab, value_fmt(r) if value_fmt else _fmt(v, unit))
        body.append('<g>%s' % _title_tag(tip))
        body.append('<text x="%d" y="%d" font-size="12" fill="%s" text-anchor="end">%s</text>'
                    % (lw - 8, y + row_h * 0.68, PALETTE["ink"], esc(lab)))
        body.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="3" fill="%s">%s</rect>'
                    % (lw, y + 4, bw, row_h - 9, color, _title_tag(tip)))
        body.append('<text x="%.1f" y="%d" font-size="11.5" fill="%s">%s</text>'
                    % (lw + bw + 6, y + row_h * 0.68, PALETTE["muted"],
                       esc(value_fmt(r) if value_fmt else _fmt(v, unit))))
        body.append("</g>")
    return _result(_svg(width, h, "".join(body)), len(data))


# ---------------------------------------------------------------------------
# 折线图：时间 / 版本 × 数值
# ---------------------------------------------------------------------------
def line(series, x_labels, y_label="", unit="", width=760, height=210,
         colors=None, fill=True, markers=True) -> dict:
    """series: [{"name": str, "values": [num|None]}]，与 x_labels 等长。"""
    series = [s for s in series if s.get("values")]
    if not series or not x_labels:
        return _result(_empty(width, 90), 0, True)
    n = len(x_labels)
    allv = [_num(v) for s in series for v in s["values"] if _num(v) is not None]
    if not allv:
        return _result(_empty(width, 90), 0, True)
    vmin, vmax = min(allv), max(allv)
    if vmax == vmin:
        vmax = vmin + 1
    pad_l, pad_r, pad_t, pad_b = 48, 12, 12, 34
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    colors = colors or [PALETTE["blue"], PALETTE["green"], PALETTE["purple"],
                        PALETTE["amber"], PALETTE["teal"]]

    def X(i):
        return pad_l + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v):
        return pad_t + ph - ph * (v - vmin) / (vmax - vmin)

    body = []
    # 网格 + y 轴刻度（4 条）
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = Y(v)
        body.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                    % (pad_l, y, width - pad_r, y, PALETTE["grid"]))
        body.append('<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="end">%s</text>'
                    % (pad_l - 6, y + 3.5, PALETTE["muted"], esc(_fmt(round(v, 2), unit))))
    for i, lab in enumerate(x_labels):
        step = max(1, n // 8)
        if i % step == 0 or i == n - 1:
            body.append('<text x="%.1f" y="%d" font-size="10.5" fill="%s" text-anchor="middle">%s</text>'
                        % (X(i), height - pad_b + 15, PALETTE["muted"], esc(lab)))
    for si, s in enumerate(series):
        col = colors[si % len(colors)]
        pts = [(X(i), Y(v)) for i, v in enumerate(s["values"]) if v is not None]
        if not pts:
            continue
        d = "M " + " L ".join("%.1f %.1f" % p for p in pts)
        if fill and len(series) == 1 and len(pts) > 1:
            area = (d + " L %.1f %.1f L %.1f %.1f Z"
                    % (pts[-1][0], pad_t + ph, pts[0][0], pad_t + ph))
            body.append('<path d="%s" fill="%s" opacity="0.12"/>' % (area, col))
        body.append('<path d="%s" fill="none" stroke="%s" stroke-width="2"/>' % (d, col))
        if markers:
            for i, v in enumerate(s["values"]):
                v = _num(v)
                if v is None:
                    continue
                body.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s">%s</circle>'
                            % (X(i), Y(v), col,
                               _title_tag("%s · %s：%s" % (s["name"], x_labels[i], _fmt(v, unit)))))
    return _result(_svg(width, height, "".join(body)), sum(len(s["values"]) for s in series))


# ---------------------------------------------------------------------------
# 热力图：两个分类 × 数值
# ---------------------------------------------------------------------------
def heatmap(rows, col_labels, row_key="label", width=760, cell_h=20, unit="",
            fmt=None) -> dict:
    if not rows or not col_labels:
        return _result(_empty(width, 90), 0, True)
    grid = [[r["cells"].get(c) if isinstance(r.get("cells"), dict) else None
             for c in col_labels] for r in rows]
    flat = [v for row in grid for v in row if v is not None]
    if not flat:
        return _result(_empty(width, 90), 0, True)
    vmin, vmax = min(flat), max(flat)
    if vmax == vmin:
        vmax = vmin + 1e-9
    lw = min(260, max(90, max(text_w(r[row_key]) for r in rows) + 12))
    cw = (width - lw - 8) / max(1, len(col_labels))
    h = 40 + cell_h * len(rows) + 8
    body = []
    for j, cl in enumerate(col_labels):
        x = lw + cw * (j + 0.5)
        body.append('<text x="%.1f" y="18" font-size="10.5" fill="%s" text-anchor="middle" '
                    'transform="rotate(-0)">%s</text>'
                    % (x, PALETTE["muted"], esc(str(cl)[:12])))
    for i, r in enumerate(rows):
        y = 26 + i * cell_h
        body.append('<text x="%d" y="%.1f" font-size="11" fill="%s" text-anchor="end">%s</text>'
                    % (lw - 8, y + cell_h * 0.7, PALETTE["ink"], esc(r[row_key])))
        for j, cl in enumerate(col_labels):
            v = r["cells"].get(cl) if isinstance(r.get("cells"), dict) else None
            if v is None:
                body.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="#f6f8fa"/>'
                            % (lw + cw * j + 1, y, cw - 2, cell_h - 2))
                continue
            t = (float(v) - vmin) / (vmax - vmin)
            body.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="2" fill="%s">%s</rect>'
                        % (lw + cw * j + 1, y, cw - 2, cell_h - 2, RAMP[int(t * (len(RAMP) - 1))],
                           _title_tag("%s × %s：%s"
                                      % (r[row_key], cl,
                                         fmt(v) if fmt else _fmt(v, unit)))))
    body.append('<text x="%d" y="%d" font-size="10.5" fill="%s">低</text>'
                % (lw, 26 + cell_h * len(rows) + 18, PALETTE["muted"]))
    for k, c in enumerate(RAMP):
        body.append('<rect x="%d" y="%d" width="18" height="9" fill="%s"/>'
                    % (lw + 22 + k * 20, 26 + cell_h * len(rows) + 10, c))
    body.append('<text x="%d" y="%d" font-size="10.5" fill="%s">高</text>'
                % (lw + 22 + len(RAMP) * 20 + 6, 26 + cell_h * len(rows) + 18,
                   PALETTE["muted"]))
    return _result(_svg(width, h, "".join(body)), len(flat))


# ---------------------------------------------------------------------------
# 散点图：两个数值
# ---------------------------------------------------------------------------
def scatter(rows, x_key, y_key, label_key=None, unit_x="", unit_y="",
            width=760, height=240, color=None) -> dict:
    data = [r for r in rows if r.get(x_key) is not None and r.get(y_key) is not None]
    if not data:
        return _result(_empty(width, 90), 0, True)
    xs = [_num(r[x_key]) or 0.0 for r in data]
    ys = [_num(r[y_key]) or 0.0 for r in data]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax == xmin:
        xmax = xmin + 1
    if ymax == ymin:
        ymax = ymin + 1
    pad_l, pad_r, pad_t, pad_b = 52, 14, 12, 34
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    X = lambda v: pad_l + pw * (v - xmin) / (xmax - xmin)          # noqa: E731
    Y = lambda v: pad_t + ph - ph * (v - ymin) / (ymax - ymin)     # noqa: E731
    body = []
    for k in range(5):
        y = pad_t + ph * k / 4
        body.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
                    % (pad_l, y, width - pad_r, y, PALETTE["grid"]))
    for k in range(5):
        v = ymin + (ymax - ymin) * (4 - k) / 4
        body.append('<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="end">%s</text>'
                    % (pad_l - 6, pad_t + ph * k / 4 + 3.5, PALETTE["muted"],
                       esc(_fmt(round(v, 1), unit_y))))
    for k in range(5):
        v = xmin + (xmax - xmin) * k / 4
        body.append('<text x="%.1f" y="%d" font-size="10.5" fill="%s" text-anchor="middle">%s</text>'
                    % (X(v), height - pad_b + 16, PALETTE["muted"], esc(_fmt(round(v, 1), unit_x))))
    col = color or PALETTE["blue"]
    for r in data:
        tip = "%s：%s=%s，%s=%s" % (r.get(label_key, ""), x_key, _fmt(r[x_key], unit_x),
                                    y_key, _fmt(r[y_key], unit_y))
        body.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" fill-opacity="0.72" '
                    'stroke="#fff" stroke-width="0.7">%s</circle>'
                    % (X(_num(r[x_key]) or 0.0), Y(_num(r[y_key]) or 0.0), col, _title_tag(tip)))
    return _result(_svg(width, height, "".join(body)), len(data))


# ---------------------------------------------------------------------------
# 堆叠条：构成占比（比饼图好读，且可并排比较）
# ---------------------------------------------------------------------------
def stacked(rows, cats, label_key, width=760, bar_h=20, colors=None,
            unit="", show_legend=True) -> dict:
    """rows: [{label_key: 名, **{cat: 值}}]"""
    data = [r for r in rows if any((r.get(c) or 0) for c in cats)]
    if not data or not cats:
        return _result(_empty(width, 90), 0, True)
    colors = colors or [PALETTE["blue"], PALETTE["green"], PALETTE["amber"],
                        PALETTE["purple"], PALETTE["teal"], PALETTE["gray"]]
    lw = min(240, max(80, max(text_w(str(r[label_key])) for r in data) + 12))
    pad_r = 56
    pw = width - lw - pad_r
    h = 30 + bar_h * len(data) + (22 if show_legend else 0)
    body = []
    if show_legend:
        x = lw
        for i, c in enumerate(cats):
            body.append('<rect x="%d" y="4" width="10" height="10" rx="2" fill="%s"/>'
                        % (x, colors[i % len(colors)]))
            body.append('<text x="%d" y="13" font-size="11" fill="%s">%s</text>'
                        % (x + 14, PALETTE["muted"], esc(c)))
            x += 14 + text_w(c) + 18
    for i, r in enumerate(data):
        total = sum((_num(r.get(c)) or 0.0) for c in cats) or 1.0
        y = 26 + i * bar_h
        body.append('<text x="%d" y="%.1f" font-size="11.5" fill="%s" text-anchor="end">%s</text>'
                    % (lw - 8, y + bar_h * 0.7, PALETTE["ink"], esc(str(r[label_key]))))
        x = lw
        for j, c in enumerate(cats):
            v = _num(r.get(c)) or 0.0
            w = pw * v / total
            if w <= 0:
                continue
            body.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s">%s</rect>'
                        % (x, y + 3, w, bar_h - 8, colors[j % len(colors)],
                           _title_tag("%s · %s：%s（%.0f%%）"
                                      % (r[label_key], c, _fmt(v, unit), 100.0 * v / total))))
            x += w
        body.append('<text x="%.1f" y="%.1f" font-size="11" fill="%s">%s</text>'
                    % (lw + pw + 6, y + bar_h * 0.7, PALETTE["muted"], esc(_fmt(total, unit))))
    return _result(_svg(width, h, "".join(body)), len(data))


# ---------------------------------------------------------------------------
# 目标条：实际值 vs 目标（质量门用）
# ---------------------------------------------------------------------------
def target_bar(rows, label_key, value_key, target_key, unit="%",
               width=760, row_h=30, higher_is_better=True) -> dict:
    """带目标线的条形图：达标绿色、未达标红色。成熟 BI 里对应 Grafana 的 threshold。"""
    data = [r for r in rows if r.get(value_key) is not None]
    if not data:
        return _result(_empty(width, 90), 0, True)
    vmax = max(max(float(r[value_key]) for r in data),
               max(float(r[target_key]) for r in data if r.get(target_key) is not None) or 0)
    vmax = vmax or 1
    lw = min(260, max(80, max(text_w(str(r[label_key])) for r in data) + 12))
    pad_r = 76
    pw = width - lw - pad_r
    h = 12 + row_h * len(data) + 6
    body = []
    for i, r in enumerate(data):
        v = _num(r[value_key]) or 0.0
        t = _num(r.get(target_key))
        y = 8 + i * row_h
        ok = (v >= t) if (t is not None and higher_is_better) else \
             (v <= t) if t is not None else True
        col = PALETTE["green"] if ok else PALETTE["red"]
        bw = max(1.5, pw * v / vmax)
        body.append('<text x="%d" y="%.1f" font-size="11.5" fill="%s" text-anchor="end">%s</text>'
                    % (lw - 8, y + row_h * 0.62, PALETTE["ink"], esc(str(r[label_key]))))
        body.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="3" fill="%s">%s</rect>'
                    % (lw, y + 4, bw, row_h - 14, col,
                       _title_tag("%s：%s" % (r[label_key], _fmt(v, unit)))))
        if t is not None:
            tx = lw + pw * t / vmax
            body.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" '
                        'stroke-width="2" stroke-dasharray="3 2">%s</line>'
                        % (tx, y + 1, tx, y + row_h - 9, PALETTE["ink"],
                           _title_tag("目标 %s" % _fmt(t, unit))))
        body.append('<text x="%.1f" y="%.1f" font-size="11" fill="%s">%s / 目标 %s</text>'
                    % (lw + bw + 6, y + row_h * 0.62, PALETTE["muted"],
                       esc(_fmt(v, unit)), esc(_fmt(t, unit))))
    return _result(_svg(width, h, "".join(body)), len(data))


# ---------------------------------------------------------------------------
# 直方图：单变量分布
# ---------------------------------------------------------------------------
def histogram(values, bins=10, unit="", width=760, height=180, color=None) -> dict:
    vals = [_num(v) for v in values if _num(v) is not None]
    if not vals:
        return _result(_empty(width, 90), 0, True)
    vmin, vmax = min(vals), max(vals)
    if vmax == vmin:
        vmax = vmin + 1
    step = (vmax - vmin) / bins
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - vmin) / step))] += 1
    cmax = max(counts) or 1
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 34
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    bw = pw / bins
    col = color or PALETTE["blue"]
    body = []
    for k in range(len(counts)):
        hgt = ph * counts[k] / cmax
        x = pad_l + bw * k
        body.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s">%s</rect>'
                    % (x + 1, pad_t + ph - hgt, bw - 2, max(1, hgt), col,
                       _title_tag("%s–%s：%d 条"
                                  % (_fmt(round(vmin + step * k, 1)),
                                     _fmt(round(vmin + step * (k + 1), 1)), counts[k]))))
    for k in range(0, bins + 1, max(1, bins // 5)):
        v = vmin + step * k
        body.append('<text x="%.1f" y="%d" font-size="10.5" fill="%s" text-anchor="middle">%s</text>'
                    % (pad_l + bw * k, height - pad_b + 16, PALETTE["muted"],
                       esc(_fmt(round(v, 1), unit))))
    return _result(_svg(width, height, "".join(body)), len(vals))


# ---------------------------------------------------------------------------
# 迷你趋势线（列表行内嵌）
# ---------------------------------------------------------------------------
def sparkline(values, width=90, height=20, color=None) -> str:
    vals = [_num(v) for v in values if _num(v) is not None]
    if len(vals) < 2:
        return '<span class="muted">—</span>'
    vmin, vmax = min(vals), max(vals)
    rng = (vmax - vmin) or 1
    col = color or PALETTE["blue"]
    pts = [(width * i / (len(vals) - 1), height - 2 - (height - 4) * (v - vmin) / rng)
           for i, v in enumerate(vals)]
    d = "M " + " L ".join("%.1f %.1f" % p for p in pts)
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" style="vertical-align:middle">'
            '<path d="%s" fill="none" stroke="%s" stroke-width="1.6">%s</path></svg>'
            % (width, height, width, height, d, col,
               _title_tag("趋势：%s → %s" % (_fmt(vals[0]), _fmt(vals[-1])))))


# ---------------------------------------------------------------------------
# 仪表条：单值 vs 目标（顶部 KPI 用）
# ---------------------------------------------------------------------------
def gauge(value, target, label="", unit="%", width=210, height=64) -> str:
    v = 0 if value is None else float(value)
    ok = v >= float(target)
    col = PALETTE["green"] if ok else (PALETTE["amber"] if v >= float(target) * 0.8
                                       else PALETTE["red"])
    pw = width - 16
    body = ['<text x="0" y="16" font-size="11.5" fill="%s">%s</text>'
            % (PALETTE["muted"], esc(label)),
            '<text x="0" y="40" font-size="20" font-weight="600" fill="%s">%s</text>'
            % (col, esc(_fmt(v, unit))),
            '<rect x="0" y="48" width="%d" height="8" rx="4" fill="#eaeef2"/>' % pw,
            '<rect x="0" y="48" width="%.1f" height="8" rx="4" fill="%s"/>'
            % (max(2, pw * min(1.0, v / max(float(target), 1e-9))), col),
            '<line x1="%d" y1="45" x2="%d" y2="59" stroke="%s" stroke-width="2"/>'
            % (pw, pw, PALETTE["ink"]),
            '<text x="0" y="%d" font-size="10.5" fill="%s">目标 %s</text>'
            % (height - 2, PALETTE["muted"], esc(_fmt(target, unit)))]
    return _svg(width, height, "".join(body))


# ---------------------------------------------------------------------------
# 自检：图表库的回归入口
# ---------------------------------------------------------------------------
def self_check() -> list:
    """返回问题列表；空列表表示通过。纯函数，不连数据库。"""
    bad = []
    r = bar_h([{"k": "甲", "v": 10}, {"k": "乙", "v": 5}], "k", "v")
    if r["empty"] or r["rows"] != 2:
        bad.append("bar_h 基本渲染失败")
    if r["svg"].count("<rect") < 2:
        bad.append("bar_h 未画出柱子")
    if bar_h([], "k", "v")["empty"] is not True:
        bad.append("bar_h 空数据应返回 empty")

    # 最长的那根柱子必须最宽——这是"图表是否忠实"的最小断言
    r2 = bar_h([{"k": "a", "v": 100}, {"k": "b", "v": 50}], "k", "v")
    import re
    ws = [float(x) for x in re.findall(r'<rect x="\d+" y="[\d.]+" width="([\d.]+)"', r2["svg"])]
    if len(ws) < 2 or not (ws[0] > ws[1]):
        bad.append("bar_h 宽度未随数值单调（最大值应最宽）")

    if line([{"name": "s", "values": [1, 2, 3]}], ["v1", "v2", "v3"])["empty"]:
        bad.append("line 基本渲染失败")
    if line([], [])["empty"] is not True:
        bad.append("line 空数据应返回 empty")
    if line([{"name": "s", "values": [None, None]}], ["a", "b"])["empty"] is not True:
        bad.append("line 全 None 应返回 empty")

    if heatmap([{"label": "r1", "cells": {"c1": 1, "c2": 2}}], ["c1", "c2"])["rows"] != 2:
        bad.append("heatmap 计数不对")
    if scatter([{"x": 1, "y": 2}], "x", "y")["rows"] != 1:
        bad.append("scatter 计数不对")
    if scatter([{"x": 1, "y": None}], "x", "y")["empty"] is not True:
        bad.append("scatter 缺值时应为空状态")
    if stacked([{"l": "a", "p": 1, "q": 2}], ["p", "q"], "l")["rows"] != 1:
        bad.append("stacked 计数不对")
    if target_bar([{"k": "甲", "v": 90, "t": 70}], "k", "v", "t")["empty"]:
        bad.append("target_bar 基本渲染失败")
    if histogram([1, 2, 3, 4, 5], bins=5)["rows"] != 5:
        bad.append("histogram 计数不对")
    if histogram([])["empty"] is not True:
        bad.append("histogram 空数据应返回 empty")
    if "svg" not in sparkline([1, 2, 3]) or sparkline([1]) != '<span class="muted">—</span>':
        bad.append("sparkline 行为不符")
    if "<svg" not in gauge(90, 70):
        bad.append("gauge 未产出 svg")

    # 特殊字符必须转义（标签来自数据库）
    r3 = bar_h([{"k": "<script>x</script>", "v": 1}], "k", "v")
    if "<script>" in r3["svg"]:
        bad.append("bar_h 未转义标签中的 HTML")
    return bad
