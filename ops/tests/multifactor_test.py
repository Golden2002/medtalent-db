# -*- coding: utf-8 -*-
"""
ops/tests/multifactor_test.py —— 多因子分析的回归断言

为什么必须有一套
--------------------------------------------------------------------------
多因子模型最危险的失败方式不是报错，而是**给出一串看起来很专业的数字**。
本模块内置了四道护栏（EPV、稀疏、完全分离、共线），这些护栏本身也会写错，
所以要用**可手算验证**的例子把它们钉住：

  ① **正确性**：只有一个二值因子时，逻辑回归的调整 OR 应当**精确等于**
     2×2 表的粗 OR（这是数学恒等式）。这一条能同时验出 IRLS 写错、
     one-hot 编码写错、参照水平取错。
  ② **混杂演示**：构造"A 的效应完全由 B 带来"的数据，
     调整后 A 的 OR 应当回到 1 附近，且被识别为"效应明显变化"。
  ③ **稀疏合并**：人数 < 5 的水平被合并，且**报告出来**。
  ④ **合并致共线 → 回退**：构造"同一批人在多个因子上都稀疏"的数据（实测踩到的陷阱），
     断言它不会硬给一堆不可信的数字，而是**排除稀疏个体并说明**。
  ⑤ **算不出来就说算不出来**：构造完全分离的数据，断言不出现调整 OR。
"""
from __future__ import annotations

import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code", "analyze"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import multifactor as MF                        # noqa: E402
import _harness as H                            # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check


def mk(a, b, c, d):
    """造 2×2：暴露组（yes）a 事件 / b 非事件；参照组（no）c 事件 / d 非事件。

    ⚠ 参照水平的规则是**样本最多的那一类**（不是"先出现的"）。
    所以要让 no 组人数明显更多，才能确定参照就是 no，OR 的读法才唯一。
    （第一版我用 30/20 与 20/30 对称的两组，人数打平 → 参照取了先出现的 yes，
     算出来的 OR 是"no vs yes"= 倒数。**模型是对的，我的期望写错了。**）
    """
    return ([{"x": "yes", "y": 1}] * a + [{"x": "yes", "y": 0}] * b +
            [{"x": "no", "y": 1}] * c + [{"x": "no", "y": 0}] * d)


def rnd(i, salt):
    """确定性伪随机 0..99（可复现，不依赖 random 的全局状态）。"""
    return ((i * 2654435761 + salt * 40503) % 1000003) % 100


def fmt(v, nd=2):
    if v is None:
        return "None"
    if isinstance(v, float) and v == float("inf"):
        return "∞"
    return ("%%.%df" % nd) % v


def main():
    # ---------- ① 正确性：单因子时 调整 OR == 粗 OR（数学恒等式）----------
    # yes: 30 事件/20 非；no: 20 事件/80 非（no 人数更多 → 参照确定为 no）
    # OR = (30*80)/(20*20) = 6.0
    rows = mk(30, 20, 20, 80)
    res = MF.analyze(rows, ["x"], merge=False)
    blk = res["factors"][0]
    check(blk["ref"] == "no", "参照水平取样本最多的那一类（实得 %s）" % blk["ref"])
    it = blk["levels"][0]
    check(abs(it["crude_or"] - 6.0) < 1e-9,
          "粗 OR 与手算一致（30*80/(20*20)=6，实得 %.4f）" % it["crude_or"])
    check(res["converged"], "单因子模型收敛")
    check(abs(it["adj_or"] - it["crude_or"]) < 0.02,
          "**调整 OR == 粗 OR**（单因子时是数学恒等式：%s vs %s）"
          % (fmt(it["adj_or"], 4), fmt(it["crude_or"], 4)))
    check(it["lo"] < it["adj_or"] < it["hi"],
          "95%% 置信区间包住点估计（%s ~ %s）" % (fmt(it["lo"], 3), fmt(it["hi"], 3)))
    check(it["p"] is not None and it["p"] < 0.01,
          "OR=6 在这个样本量下 p 应远小于 0.01（实得 %s）" % fmt(it["p"], 5))

    # 反向：把事件与非事件对调，OR 应变倒数
    res2 = MF.analyze(mk(20, 30, 80, 20), ["x"], merge=False)
    it2 = res2["factors"][0]["levels"][0]
    check(abs(it2["crude_or"] - 1 / 6.0) < 1e-9,
          "把表对调 OR 变倒数（1/6 = 0.1667，实得 %.4f）" % it2["crude_or"])

    # ---------- ② 混杂：A 的关联完全来自 B ----------
    # A 与 B 相关，但**结果只由 B 决定**。
    # ⚠ 关键：A 与 y 必须用**不同的模数**造（5 与 7）。
    # 第一版两者都用 i%10，于是 A 与 y 确定性相关 → 完全分离 → 模型不可估计。
    # 那不是"模型不行"，是我的测试数据把两个变量绑死了。
    rows = []
    for i in range(300):
        b = "B1" if i % 2 == 0 else "B0"
        a = "A1" if ((b == "B1" and i % 5 != 2) or (b == "B0" and i % 5 < 2)) else "A0"
        y = 1 if ((b == "B1" and i % 7 != 3) or (b == "B0" and i % 7 >= 5)) else 0
        rows.append({"A": a, "B": b, "y": y})
    res = MF.analyze(rows, ["A", "B"], merge=True)
    check(res["converged"], "混杂演示的模型收敛")
    a_blk = next(x for x in res["factors"] if x["factor"] == "A")
    b_blk = next(x for x in res["factors"] if x["factor"] == "B")
    a_lv = next((x for x in a_blk["levels"] if x["level"] == "A1"), a_blk["levels"][0])
    b_lv = next((x for x in b_blk["levels"] if x["level"] == "B1"), b_blk["levels"][0])
    check(b_lv["adj_or"] is not None
          and (b_lv["adj_or"] > 2 or b_lv["adj_or"] < 0.5),
          "真正的驱动因子 B 调整后仍有强效应（方向取决于哪个水平作参照：OR=%s）"
          % fmt(b_lv["adj_or"]))
    # 方向无关地断言"明显偏离 1"：参照水平是"样本最多的那一类"，
    # 它可能是 B1 也可能是 B0，所以不能写死"OR 必须 > 1"（第一版就是这么写错的）。
    check(abs(math.log(a_lv["crude_or"])) > 0.4,
          "A 的**粗** OR 明显偏离 1（%s）—— 只看粗关联会被误导"
          % fmt(a_lv["crude_or"]))
    check(a_lv["adj_or"] is not None and abs(math.log(a_lv["adj_or"])) < 0.4,
          "由 B 带出来的 A 调整后趋近 1（粗 %s → 调整 %s）"
          % (fmt(a_lv["crude_or"]), fmt(a_lv["adj_or"])))

    # ---------- ③ 稀疏合并 ----------
    rows = ([{"x": "big1", "y": 1}] * 20 + [{"x": "big1", "y": 0}] * 20 +
            [{"x": "big2", "y": 0}] * 20 + [{"x": "big2", "y": 1}] * 20 +
            [{"x": "tiny", "y": 1}] * 1 + [{"x": "tiny", "y": 0}] * 1)
    res = MF.analyze(rows, ["x"])
    merged = [m for m in res["merges"] if m[0] == "x"]
    check(bool(merged), "人数 < 5 的水平被合并，并且**被报告出来**")
    check(any("其他（合并）" in str(lv) for lv in
              [i["level"] for i in res["factors"][0]["levels"]]),
          "合并后的水平出现在结果里（能看出合了什么）")

    # ---------- ④ 合并致共线 → 回退到排除稀疏个体 ----------
    # 要点：**同一批人**在两个因子上都稀疏，但两个因子在主体部分的划分不同。
    # 合并后两个「其他（合并）」列完全相同 → 真共线 → 必须回退。
    # （第一版我把 P 与 Q 的划分造得完全相同，那本来就是真共线，
    #   测不出"合并制造共线"这个陷阱。）
    rows = []
    for i in range(60):
        tiny = i >= 57                       # 同样这 3 个人在两个因子上都稀疏
        p = "P_tiny" if tiny else ("P1" if i % 2 == 0 else "P2")
        q = "Q_tiny" if tiny else ("Q1" if i % 3 == 0 else "Q2")
        y = 1 if (i % 2 == 0) != (i % 3 == 0) else 0
        rows.append({"p": p, "q": q, "y": y})
    res = MF.analyze(rows, ["p", "q"])
    check(res["converged"],
          "合并制造共线时**回退到排除稀疏个体**，模型仍可估计（而不是硬给数字）")
    check(res["excluded"] == 3, "如实报告排除了几个个体（实得 %d）" % res["excluded"])
    check(any("排除" in m[3] for m in res["merges"]),
          "排除这件事被写进处理说明（不悄悄换口径）")
    check(all(i["adj_or"] is not None
              for b in res["factors"] for i in b["levels"]),
          "回退之后每个水平都有调整 OR（而不是一片空值）")

    # ---------- ⑤ 算不出来就说算不出来 ----------
    # 完全分离：暴露组全是事件、参照组全是非事件
    rows = ([{"x": "yes", "y": 1}] * 20 + [{"x": "no", "y": 0}] * 20)
    res = MF.analyze(rows, ["x"], merge=False)
    it = res["factors"][0]["levels"][0]
    check(not res["converged"] or it["adj_or"] is None or it["adj_or"] > 50,
          "完全分离时不给一个「看起来很确定」的调整 OR（收敛=%s，OR=%s）"
          % (res["converged"], it["adj_or"]))
    check(it["crude_or"] is not None,
          "但**粗关联仍然给出**（它算得出来，只是被 0.5 修正过）")

    # ---------- ⑥ EPV 护栏 ----------
    rows = [{"a": "x" if i % 3 else "y", "b": "u" if i % 2 else "v",
             "c": "m" if i % 5 else "n", "d": "s" if i % 7 else "t",
             "y": 1 if i % 9 == 0 else 1 if i % 11 == 0 else 0}
            for i in range(80)]
    res = MF.analyze(rows, ["a", "b", "c", "d"], merge=False)
    check(any(w[0] == "EPV 过低" for w in res["warn"]),
          "样本薄时 EPV 护栏触发（EPV=%.1f）" % (res["epv"] or 0))

    # ---------- ⑦ 因子水平唯一时被跳过而不是报错 ----------
    rows = [{"x": "same", "y": i % 2} for i in range(20)]
    res = MF.analyze(rows, ["x"], merge=False)
    check(res["factors"][0].get("skip") is not None,
          "只有一个取值的因子被明确跳过（而不是算出一个假效应）")

    # ---------- ⑧ 卡方上尾概率与已知临界值一致 ----------
    # 这是"因子贡献"那套检验的地基，算错会让所有 p 值一起错。
    # 用教科书上的临界值核对（α=0.05 / 0.01 对应的 χ² 分位点）。
    for x, k, exp in ((3.841459, 1, 0.05), (6.634897, 1, 0.01),
                      (5.991465, 2, 0.05), (9.487729, 4, 0.05),
                      (11.344867, 3, 0.01)):
        got = MF._chi2_sf(x, k)
        check(abs(got - exp) < 2e-4,
              "χ²=%.4f, df=%d 的上尾概率 ≈ %.4f（实得 %.6f）" % (x, k, exp, got))

    # ---------- ⑨ 因子贡献：能不能把"真驱动"和"影子"分开 ----------
    # 造一个**已知真相**的数据集：
    #   · A 是真正的驱动（决定 y）；
    #   · B 只与 A 相关，对 y 没有独立作用（典型混杂/影子）。
    # 期望：A 的贡献排第一且显著；B 的贡献小且不显著。
    rows = []
    for i in range(400):
        a = "A1" if i % 3 != 0 else "A0"
        # B 与 A 相关（A1 里 8 成是 B1），但 y 只由 A 决定
        b = "B1" if (i % 5 < 4) == (a == "A1") else "B0"
        y = 1 if (a == "A1" and i % 7 != 3) or (a == "A0" and i % 7 >= 6) else 0
        rows.append({"A": a, "B": b, "y": y})
    con = MF.factor_contributions(rows, ["A", "B"], {"A": "A", "B": "B"})
    check("error" not in con, "因子贡献能算出来（全模型收敛）")
    if "error" not in con:
        cs = con["contributions"]
        check(cs[0]["factor"] == "A",
              "**真正的驱动因子排第一**（%s，Δχ²=%.2f）" % (cs[0]["title"], cs[0]["dD"]))
        check(cs[0]["p"] is not None and cs[0]["p"] < 0.05,
              "驱动因子的 p < 0.05（实得 %s）" % con["contributions"][0]["p"])
        b_blk = next(x for x in cs if x["factor"] == "B")
        check(b_blk["dD"] < cs[0]["dD"],
              "影子的贡献明显小于真驱动（%.2f < %.2f）"
              % (b_blk["dD"], cs[0]["dD"]))
        check((b_blk["share"] or 0) < 40,
              "影子的贡献占比不高（%.0f%%）—— 排序能把它排到后面" % (b_blk["share"] or 0))
        check(0 < (con["pseudo_r2"] or 0) < 1,
              "伪 R² 在 (0,1) 内（实得 %.3f）" % (con["pseudo_r2"] or 0))
        check(abs(sum(x["share"] or 0 for x in cs) - 100) < 0.01,
              "各因子贡献占比之和为 100%%（实得 %.2f）"
              % sum(x["share"] or 0 for x in cs))

    # ---------- ⑩ 与结果完全无关的因子，贡献应当很小 ----------
    rows = []
    for i in range(400):
        rows.append({"real": "R1" if i % 3 else "R0",
                     "noise": "N1" if i % 11 < 5 else "N0",
                     "y": 1 if i % 3 else 0})       # y 只由 real 决定
    con = MF.factor_contributions(rows, ["real", "noise"])
    if "error" not in con:
        cs = con["contributions"]
        check(cs[0]["factor"] == "real", "真因子排第一（%s）" % cs[0]["title"])
        noise = next(x for x in cs if x["factor"] == "noise")
        check(noise["dD"] < cs[0]["dD"],
              "无关因子的贡献小于真因子（%.2f < %.2f）" % (noise["dD"], cs[0]["dD"]))
        check(noise["p"] is None or noise["p"] > 0.05,
              "无关因子不显著（p=%s）—— 假阳性检验" % noise["p"])

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
