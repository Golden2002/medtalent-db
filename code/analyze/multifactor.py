# -*- coding: utf-8 -*-
"""
code/analyze/multifactor.py —— 多因子分析（3 个及以上因子）

为什么需要它（用户提问："那我应该可以纳入 3 个及以上因子进行分析"）
--------------------------------------------------------------------------
两个因子只能看**两两关系**：A 与结果有没有关系、B 与结果有没有关系。
**第三个因子才是"控制混杂"的起点** ——
例如"有海外经历的人就业率更高"，可能只是因为**有海外经历的人学历也更高**。
把学历放进模型一起看，海外经历的效应若消失，那它就是被学历带出来的。

本模块做两件事，并**明确区分**它们：
  ① **粗关联**（crude）：单个因子与结果的关系，不控制任何东西。
     这是大多数"数据发现"的来源，也是大多数错误结论的来源。
  ② **调整后效应**（adjusted）：多个因子**同时**进逻辑回归，各自得到
     调整后的优势比（OR）与 95% 置信区间。看"控制其他因子后还剩多少"。

**它仍然不是因果推断**（必须说清楚）
--------------------------------------------------------------------------
逻辑回归只能回答"在观测到的这些变量条件下，还剩下多少关联"。
要谈因果，至少还需要：时间先后、无未测混杂、无反向因果、正确的模型设定。
本模块把这句话直接印在结果页上，而不是让人以为跑了个回归就得到了因果。

**数据不达标时宁可说"不能算"，也不给一个漂亮数字**
--------------------------------------------------------------------------
小样本下逻辑回归会给出看起来很确定的系数（完全分离时甚至给出无穷大的 OR）。
所以内置四道护栏，任何一道触发都会**显著提示**（有的直接拒绝出 OR）：
  · 事件数/变量数（EPV）< 10 —— 经验法则，低于它回归系数极不稳定
  · 完全分离（separation）—— 某个水平下全是/全不是事件，OR 数学上无穷
  · 稀疏格子 —— 某水平的事件数或非事件数过少
  · 完全共线 —— 设计矩阵不满秩（例如两个因子是同一件事的两种说法）

依赖：只用 numpy（不引入 sklearn/statsmodels）—— 这样"拟合的是什么模型"
完全可解释、可复核，生产环境也不多背一个重依赖。
"""
from __future__ import annotations

import math

import numpy as np

MISSING = "（未填）"
# EPV 经验阈值：每个自变量至少要有这么多个"事件"（少数类），系数才勉强稳定
EPV_MIN = 10
# 某个水平上事件数或非事件数低于它，就算稀疏
SPARSE_MIN = 5


def _norm(v):
    return MISSING if v is None or str(v).strip() == "" else str(v)


def build_design(rows, factors, y_key="y"):
    """把 (因子值…, y) 的行集合转成设计矩阵。

    返回 dict：
      X        设计矩阵（含截距列）
      y        0/1
      names    每列对应的可读名字（用于展示）
      levels   每个因子的水平（第一个水平作参照）
      dropped  被丢掉的因子（水平数 < 2，没有信息量）
    每个因子做 one-hot 并**丢掉一个参照水平**（否则与截距完全共线）。
    参照水平取**样本量最大**的那个：这样每个 OR 都是"相对最常见的那一类"，
    比"相对第一个字母序"好解释得多。
    """
    y = np.array([1 if r[y_key] == 1 else 0 for r in rows], dtype=float)
    cols, names, levels, dropped = [np.ones(len(rows))], ["截距"], {}, []
    for f in factors:
        vals = [_norm(r[f]) for r in rows]
        uniq = {}
        for v in vals:
            uniq[v] = uniq.get(v, 0) + 1
        if len(uniq) < 2:
            dropped.append((f, len(uniq), uniq))
            continue
        order = sorted(uniq, key=lambda k: -uniq[k])      # 最多的作参照
        levels[f] = order
        for lv in order[1:]:
            cols.append(np.array([1.0 if v == lv else 0.0 for v in vals]))
            names.append("%s = %s" % (f, lv))
    X = np.column_stack(cols) if cols else np.ones((len(rows), 1))
    return {"X": X, "y": y, "names": names, "levels": levels, "dropped": dropped}


def _singular_reason(X):
    """区分两种"矩阵不可解" —— 它们该做的事完全不同，不能给同一句提示。

    · **真共线**：秩 < 列数。说明两个因子表达的是同一件事
      （例如"海外经历"同时从教育记录和自填字段取来），该去掉一个。
    · **稀疏导致的数值奇异**：秩正常，但某些 one-hot 列只有 1–2 个人，
      权重极小、与截距几乎共线。这时该做的是**合并稀疏水平**，
      而不是去查因子定义（查也查不出问题）。
    第一版对两者都报"因子之间可能完全共线"，会把人带去错误的方向（实测踩过）。
    """
    p = X.shape[1]
    r = int(np.linalg.matrix_rank(X))
    if r < p:
        return ("设计矩阵**真共线**（秩 %d < 列数 %d）：有两个因子表达的可能是同一件事，"
                "请去掉其中一个" % (r, p))
    tiny = [int((np.abs(X[:, j]) > 1e-9).sum()) for j in range(p)]
    few = sum(1 for t in tiny[1:] if t < SPARSE_MIN)
    return ("设计矩阵数值奇异：秩正常（%d），但**有 %d 个水平的人数少于 %d** —— "
            "它们让加权矩阵几乎不可逆。**这不是因子定义的问题，是数据太稀**；"
            "解决办法是合并稀疏水平（本页默认已自动合并，若仍失败说明连合并都不够）"
            % (r, few, SPARSE_MIN))


def fit_logit(X, y, max_iter=60, tol=1e-9):
    """IRLS（迭代重加权最小二乘）拟合逻辑回归。纯 numpy。

    返回 (beta, se, converged, note)；拟合失败返回 (None, None, False, 原因)。
    """
    n, p = X.shape
    if n <= p:
        return None, None, False, "样本数(%d) 不多于参数个数(%d)，无法拟合" % (n, p)
    beta = np.zeros(p)
    for it in range(max_iter):
        eta = np.clip(X @ beta, -500, 500)
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = np.maximum(mu * (1.0 - mu), 1e-10)
        z = eta + (y - mu) / w
        XtW = X.T * w
        H = XtW @ X
        try:
            step = np.linalg.solve(H, XtW @ z)
        except np.linalg.LinAlgError:
            return None, None, False, _singular_reason(X)
        if not np.all(np.isfinite(step)):
            return None, None, False, "出现非有限值（通常是完全分离）"
        if np.max(np.abs(step - beta)) < tol:
            beta = step
            break
        beta = step
    else:
        return None, None, False, "迭代 %d 次未收敛" % max_iter
    try:
        cov = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None, None, False, _singular_reason(X)
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    note = ""
    if np.max(np.abs(beta)) > 12 or np.max(se) > 10:
        note = ("系数或标准误异常大 —— 典型的**完全分离**"
                "（某个水平下结果全是一个值），此时 OR 在数学上趋于无穷，"
                "**不要解读它**")
    return beta, se, True, note


def _wald_p(z):
    """Wald 检验的 p 值：用 math.erfc（标准库），不依赖 scipy。"""
    return math.erfc(abs(z) / math.sqrt(2.0))


def crude_or(rows, f, ref, lv, y_key="y"):
    """单个因子的**粗**优势比（不控制任何变量），2×2 表。

    零格加 0.5 修正（并标注），否则 OR 会是 0 或无穷。
    """
    a = b = c = d = 0
    for r in rows:
        v = _norm(r[f])
        yv = r[y_key]
        if v == lv:
            a += yv
            b += (1 - yv)
        elif v == ref:
            c += yv
            d += (1 - yv)
    corr = ""
    if 0 in (a, b, c, d):
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        corr = "（含 0 格，已加 0.5 修正）"
    return (a * d) / (b * c) if b * c else float("inf"), corr


def merge_sparse_levels(rows, factors, min_n=SPARSE_MIN, y_key="y"):
    """把人数过少的水平合并成「其他（合并）」，返回 (新 rows, 合并报告)。

    为什么必须做这一步（实测撞出来的）
    ---------------------------------------------------------------------------
    拿 4 个因子跑，模型报"设计矩阵奇异（因子之间可能完全共线）"。
    **但那个诊断是错的**：真正的原因是**稀疏水平** ——
    年龄段里有 2 人/1 人的类别、性别里有 2 人/1 人的类别。
    这些 one-hot 列在加权最小二乘里权重极小、与截距几乎共线，
    于是矩阵数值奇异。它**不是**"两个因子说的是同一件事"。
    诊断说错会把人带去查因子定义（白费功夫），而真正该做的是合并稀疏水平。

    合并是标准做法（不是妥协）：一个只有 1 个人的类别，
    它贡献不了任何可估计的效应，却会让整个模型崩掉。
    合并的代价是"这个类别不再单独看"，所以要**如实报告合并了什么**。
    """
    report = []
    out = [dict(r) for r in rows]
    for f in factors:
        cnt = {}
        for r in out:
            cnt[_norm(r[f])] = cnt.get(_norm(r[f]), 0) + 1
        small = {k for k, v in cnt.items() if v < min_n}
        if not small or len(small) == len(cnt):
            if small and len(small) == len(cnt):
                report.append((f, sorted(small), 0,
                               "所有水平人数都不足 %d，无法合并（该因子整体不可用）" % min_n))
            continue
        for r in out:
            if _norm(r[f]) in small:
                r[f] = "其他（合并）"
        report.append((f, sorted(small), len(small),
                       "把人数 < %d 的 %d 个水平合并成「其他（合并）」" % (min_n, len(small))))
    return out, report


def analyze(rows, factors, labels=None, y_key="y", merge=True, min_n=SPARSE_MIN):
    """跑多因子分析：粗关联 + 调整后 OR + 四道护栏。

    rows: [{factor: value, ..., 'y': 0/1}]
    labels: {因子名: 展示标题}
    merge: 是否先合并稀疏水平（默认是 —— 见 merge_sparse_levels 的说明）
    返回结构化结果，供页面渲染（**不在这一层做 HTML**）。
    """
    labels = labels or {}
    merges = []
    excluded = 0
    if merge:
        rows, merges = merge_sparse_levels(rows, factors, min_n, y_key)
        # ⚠ 合并可能**制造共线**（实测撞到的陷阱）：
        # 如果"在多个因子上都稀疏"的是**同一批人**，那么合并出来的
        # 「其他（合并）」列在各个因子里**完全相同** → 设计矩阵真共线、无法估计。
        # 例：年龄段稀疏的是 AG6+AG9、性别稀疏的是 F+M，而恰好都是同样那 3 个人。
        # 这时"合并"就不是解法。退回更朴素的办法：**排除这些稀疏个体**，
        # 用"所有因子都有足够样本的人"来估计 —— 代价是样本变少，但模型可识别。
        # 两条路都要**如实报告**（少了几个人、为什么），不能悄悄换口径。
        _d = build_design(rows, factors, y_key)
        if _d["X"].shape[1] - 1 > 0 and \
                int(np.linalg.matrix_rank(_d["X"])) < _d["X"].shape[1]:
            keep = []
            for r in rows:
                ok = True
                for f in factors:
                    same = sum(1 for x in rows if _norm(x[f]) == _norm(r[f]))
                    if same < min_n:
                        ok = False
                        break
                if ok:
                    keep.append(r)
            excluded = len(rows) - len(keep)
            if keep and excluded:
                rows = keep
                merges.append(("__rows__", [], excluded,
                               "合并后仍共线（同一批人在多个因子上都稀疏）→ "
                               "改为**排除 %d 个稀疏个体**，用 %d 人估计"
                               % (excluded, len(rows))))
    n = len(rows)
    n_events = int(sum(r[y_key] for r in rows))
    n_nonevents = n - n_events
    des = build_design(rows, factors, y_key)
    X, y, names = des["X"], des["y"], des["names"]
    n_params = X.shape[1] - 1                     # 不含截距

    warn = []
    # 护栏① EPV
    epv = (min(n_events, n_nonevents) / n_params) if n_params else float("inf")
    if n_params and epv < EPV_MIN:
        warn.append(("EPV 过低", "每个自变量只有 %.1f 个事件（经验下限 %d）。"
                     "低于它时回归系数极不稳定，**OR 的数值不可信**；"
                     "这里只应当作方法演示，不能当结论。" % (epv, EPV_MIN)))
    # 护栏② 稀疏格子
    sparse = []
    for f in factors:
        for lv in des["levels"].get(f, []):
            sub = [r for r in rows if _norm(r[f]) == lv]
            ev = sum(r[y_key] for r in sub)
            if len(sub) < SPARSE_MIN or ev < SPARSE_MIN or (len(sub) - ev) < SPARSE_MIN:
                sparse.append("%s=%s（%d 人，其中事件 %d）" % (labels.get(f, f), lv,
                                                              len(sub), ev))
    if sparse:
        warn.append(("稀疏格子", "这些水平的人数或事件数过少：" + "、".join(sparse[:8])
                     + "。它们的 OR 会非常不稳定（甚至无穷大）。"))

    beta = se = None
    converged = False
    note = ""
    if n_params == 0:
        note = "所选因子没有任何一个有 2 个以上水平，没有可估计的效应。"
    else:
        beta, se, converged, note = fit_logit(X, y)
    if note:
        warn.append(("模型问题", note))

    # 每个因子的结果
    out = []
    for f in factors:
        lvs = des["levels"].get(f)
        title = labels.get(f, f)
        if not lvs:
            # dropped 的元素是三元组 (因子, 水平数, 各水平计数) —— 第一版按二元组解包，
            # 于是"因子只有一个取值"这条分支一走到就 ValueError（实测被测试抓到）。
            d = next((v for k, *v in [(x[0], x[1], x[2]) for x in des["dropped"]]
                      if k == f), None)
            detail = ""
            if d:
                detail = "、".join(list(d[1])[:3])
            out.append({"factor": f, "title": title, "levels": [],
                        "skip": "该因子在这个样本里只有一个取值%s，没有可比性"
                                % (("（%s）" % detail) if detail else "")})
            continue
        ref = lvs[0]
        items = []
        for lv in lvs[1:]:
            cor, corr_note = crude_or(rows, f, ref, lv, y_key)
            item = {"level": lv, "n": sum(1 for r in rows if _norm(r[f]) == lv),
                    "events": int(sum(r[y_key] for r in rows if _norm(r[f]) == lv)),
                    "crude_or": cor, "crude_note": corr_note,
                    "adj_or": None, "lo": None, "hi": None, "p": None}
            if converged and beta is not None:
                key = "%s = %s" % (f, lv)
                if key in names:
                    i = names.index(key)
                    z = beta[i] / se[i] if se[i] > 0 else float("nan")
                    item["adj_or"] = float(math.exp(beta[i])) if abs(beta[i]) < 50 else float("inf")
                    item["lo"] = float(math.exp(beta[i] - 1.96 * se[i])) if abs(beta[i]) < 50 else None
                    item["hi"] = float(math.exp(beta[i] + 1.96 * se[i])) if abs(beta[i]) < 50 else None
                    item["p"] = _wald_p(z)
            items.append(item)
        out.append({"factor": f, "title": title, "ref": ref, "levels": items})

    # 混杂提示：粗关联显著、调整后不显著（或反向）→ 效应很可能是别的因子带出来的
    confound = []
    for blk in out:
        for it in blk.get("levels", []):
            if it["adj_or"] is None or it["crude_or"] is None:
                continue
            if it["adj_or"] == 0 or it["crude_or"] == 0:
                continue
            ratio = it["adj_or"] / it["crude_or"] if it["crude_or"] else None
            if ratio and (ratio < 0.6 or ratio > 1.7):
                confound.append("%s：%s 相对「%s」的 OR 从 **%.2f（粗）** 变成 "
                                "**%.2f（调整后）**"
                                % (blk["title"], it["level"], blk.get("ref", "参照"),
                                   it["crude_or"], it["adj_or"]))
    return {"n": n, "n_events": n_events, "n_nonevents": n_nonevents,
            "n_params": n_params, "epv": epv if np.isfinite(epv) else None,
            "warn": warn, "factors": out, "confound": confound,
            "converged": converged, "dropped": des["dropped"],
            "merges": merges, "excluded": excluded,
            "outcome_rate": (100.0 * n_events / n) if n else None}
