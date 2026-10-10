# -*- coding: utf-8 -*-
"""
ops/seed_demo_analysis.py —— 生成**带已知真相**的合成数据（就业去向 + 因子）

为什么要有"已知真相"
--------------------------------------------------------------------------
如果合成数据是随手编的，那分析出来的任何结果都无从判断对错。
本脚本按**写明的规则**生成数据，于是我们**知道真值**，就能检验：

    分析能不能把已知的规律找回来？会不会把无关的因子报成显著？

这就是把"分析功能"当成一个**可被检验的对象**，而不是"跑出来一堆数字看着挺专业"。

本脚本写死的真值（下面 RULES 就是它的实现）
--------------------------------------------------------------------------
1. **学历是"进三级医院"的真正驱动**（博士 72% / 硕士 50% / 本科 22%）。
2. **海外经历完全由学历决定**（博士 55% / 硕士 28% / 本科 10%），
   而**它对去向没有任何直接作用**。
   → 于是"海外经历"与"进三级医院"在**粗关联**上必然正相关，
     但**控制学历之后应当趋近无关**。这是教科书式的**混杂**，
     也是检验"粗 OR vs 调整 OR"这一整套机制的最好例子。
3. **户籍类型对"基层医疗"有真实影响**（农村 H2 更容易去基层）。
4. **性别、年龄段对去向没有作用** —— 用来做**假阳性检验**：
   如果分析把它们报成显著，那是方法有问题（而不是发现了规律）。

⚠ 必须说清楚的话
--------------------------------------------------------------------------
这是**合成数据**。用它跑出来的"规律"是我们自己写进去的，
唯一用途是验证分析口径能不能把已知规律找回来。
**不要**当成对真实人才的判断 —— 相关不等于因果，而且这批数据本来就是假的。

用法
    python ops/seed_demo_analysis.py show       # 只看生成规则与预期分布，不写
    python ops/seed_demo_analysis.py apply      # 生成（可重复执行，结果确定）
    python ops/seed_demo_analysis.py truth      # 跑一遍分析，和上面写的真值对照
    python ops/seed_demo_analysis.py cleanup    # 删掉本脚本写入的值
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")

FIELDS = ("F_PSN_CRED_AGE_BAND", "F_PSN_RES_OVERSEAS",
          "F_PSN_RES_DESTINATION", "F_PSN_RES_JOB_OUTCOME")

DEST = {"DE1": "三级医院", "DE2": "二级/专科医院", "DE3": "基层医疗",
        "DE4": "医药/器械企业", "DE5": "升学深造", "DE6": "出国出境",
        "DE7": "待业/其他"}

# ---- 真值：各学历的规则（这就是"我们知道的真相"）----
# p_tier1：进三级医院的概率（**只由学历决定**）
# p_overseas：有海外经历的概率（**只由学历决定，所以海外经历没有独立作用**）
# p_study：没进三级医院时，去深造的概率
# p_abroad：没进三级医院时，出国的概率（**这里才用到海外经历**）
#
# ⚠ 效应量必须**大到 120 人的样本能找回来**（实测教训）：
# 第一版设成 72%/50%/22%（真值 OR≈2.6），而每个学历组只有 21–72 人，
# 采样噪声直接把差距抹平 —— 实测 D4 59% vs D3 54%，粗 OR 只有 1.20，
# 于是"分析找不回真值"，看起来像分析方法有问题，其实是**数据不够**。
# 合成数据的用途就是"验证分析能否找回已知规律"，所以它的效应量必须足够大；
# 真实数据当然做不到这一点（那正是真实分析更难的地方）。
RULES = {
    "D4": dict(p_tier1=0.80, p_overseas=0.55, p_study=0.05, p_abroad=0.40),
    "D3": dict(p_tier1=0.45, p_overseas=0.28, p_study=0.12, p_abroad=0.30),
    "D2": dict(p_tier1=0.15, p_overseas=0.10, p_study=0.40, p_abroad=0.20),
    "D6": dict(p_tier1=0.80, p_overseas=0.55, p_study=0.05, p_abroad=0.40),
}
DEFAULT = dict(p_tier1=0.40, p_overseas=0.15, p_study=0.20, p_abroad=0.20)
# ⚠ 效应量还有**上限**：第一版拉成 0.85/0.45/0.10 之后 D2 实测 0/21 = 0%，
# 出现**完全分离** —— 模型于是拒绝给调整 OR（这个拒绝是**对的**：
# 分离时 OR 在数学上趋于无穷）。所以合成数据要的是"**强但有重叠**"：
# 现在 0.80/0.45/0.15，每个学历组里事件与非事件都存在。
# 这条经验同样适用于真实分析：结果变量在某层里全是同一个值时，
# 该给的是"算不出来"，而不是一个漂亮的无穷大。


def h(*parts) -> float:
    """确定性伪随机 [0,1)：同一个人每次算出来一样，便于复核与复现。"""
    s = "|".join(str(x) for x in parts).encode()
    return int(hashlib.sha1(s).hexdigest()[:12], 16) / float(16 ** 12)


def load_people(c):
    return c.execute("""
        SELECT p.person_id,
               coalesce(d.age_band, 'AG9') AS age_band,
               coalesce(d.hukou_type, 'H1') AS hukou_type,
               coalesce((SELECT e.degree_level FROM mt.education_record e
                          WHERE e.person_id = p.person_id
                          ORDER BY e.degree_level DESC NULLS LAST LIMIT 1), 'D3') AS degree,
               coalesce((SELECT e.is_clinical FROM mt.education_record e
                          WHERE e.person_id = p.person_id
                          ORDER BY e.start_date DESC NULLS LAST LIMIT 1), false) AS clinical
          FROM mt.person p
          LEFT JOIN mt.person_demographics d ON d.person_id = p.person_id
         WHERE p.person_id LIKE 'per_mock_%'
         ORDER BY p.person_id""").fetchall()


def generate(rows):
    """按 RULES 生成每个人的 (海外经历, 就业去向, 求职结果粗分类)。"""
    out = []
    for r in rows:
        pid, deg = r["person_id"], r["degree"]
        rule = RULES.get(deg, DEFAULT)
        # ① 海外经历：**只由学历决定** —— 所以它后面"看似有作用"全是混杂
        ov = "Y" if h(pid, "overseas") < rule["p_overseas"] else "N"
        # ② 去向
        u = h(pid, "dest")
        if u < rule["p_tier1"]:
            dest = "DE1"                                  # 三级医院
        else:
            v = h(pid, "dest2")
            if v < rule["p_study"]:
                dest = "DE5"                              # 升学深造
            elif v < rule["p_study"] + rule["p_abroad"] and ov == "Y":
                dest = "DE6"                              # 出国：**这里才用到海外经历**
            elif r["hukou_type"] == "H2" and h(pid, "grass") < 0.55:
                dest = "DE3"                              # 农村户籍 → 基层
            elif not r["clinical"] and h(pid, "farm") < 0.5:
                dest = "DE4"                              # 非临床 → 企业
            elif h(pid, "wait") < 0.10:
                dest = "DE7"                              # 待业/其他
            else:
                dest = "DE2"                              # 二级/专科
        # ③ 粗分类（与去向**保持一致**，避免两个结果变量互相矛盾）
        jo = {"DE1": "JO1", "DE2": "JO1", "DE3": "JO1", "DE4": "JO1",
              "DE5": "JO3", "DE6": "JO4", "DE7": "JO2"}[dest]
        out.append({"person_id": pid, "age_band": r["age_band"], "overseas": ov,
                    "dest": dest, "jo": jo, "degree": deg,
                    "hukou": r["hukou_type"], "clinical": r["clinical"]})
    return out


def cmd_show(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        rows = generate(load_people(c))
    print("生成规则（**确定性**：同一个人每次算出来一样）\n")
    print("  ① 学历 → 进三级医院的概率（**真效应**）")
    for d, r in RULES.items():
        print("       %s  三级医院 %-5.0f%%   海外经历 %-5.0f%%（**由学历决定**）"
              % (d, r["p_tier1"] * 100, r["p_overseas"] * 100))
    print("  ② 海外经历 **只由学历决定，对去向没有直接作用** —— 所以它会是教科书式的混杂")
    print("  ③ 户籍类型 → 农村(H2)更容易去基层医疗（**真效应**）")
    print("  ④ 性别、年龄段 → **对去向无作用**（用来做假阳性检验）")
    print("\n预期分布（%d 人）：" % len(rows))
    for k, v in Counter(x["dest"] for x in rows).most_common():
        print("    %-4s %-14s %3d 人（%.1f%%）" % (k, DEST[k], v, 100.0 * v / len(rows)))
    print("\n按学历看三级医院占比（这是要被分析找回来的真值）：")
    for d in ("D4", "D3", "D2"):
        sub = [x for x in rows if x["degree"] == d]
        if sub:
            n = sum(1 for x in sub if x["dest"] == "DE1")
            print("    %s  %2d/%2d = %.0f%%" % (d, n, len(sub), 100.0 * n / len(sub)))
    return 0


def cmd_apply(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        for f in FIELDS:
            if not c.execute("SELECT 1 FROM mt.field_catalog WHERE field_id=%s "
                             "AND status='active'", (f,)).fetchone():
                raise SystemExit("[X] 字段 %s 还没登记 —— 先跑 ops\\pg.py apply" % f)
        rows = generate(load_people(c))
    n = 0
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        for r in rows:
            for fid, code in (("F_PSN_CRED_AGE_BAND", r["age_band"]),
                              ("F_PSN_RES_OVERSEAS", r["overseas"]),
                              ("F_PSN_RES_DESTINATION", r["dest"]),
                              ("F_PSN_RES_JOB_OUTCOME", r["jo"])):
                c.execute("SELECT mt.set_value('person', %s, %s, %s)",
                          (r["person_id"], fid, code))
                n += 1
    print("[✓] 已写入 %d 条合成值（%d 人 × 4 变量）" % (n, len(rows)))
    print("    [!] **合成数据**：规律是我们写进去的，只能用于验证分析方法。")
    print("    核对真值：python ops\\seed_demo_analysis.py truth")
    print("    清理：    python ops\\seed_demo_analysis.py cleanup")
    return 0


def cmd_truth(a):
    """跑一遍分析，与脚本里写死的真值对照 —— 这是"分析对不对"的判据。"""
    sys.path.insert(0, os.path.join(BASE, "code", "analyze"))
    import multifactor as MF
    import psycopg as _p
    dims = {"年龄段": "D_AGE_BAND", "性别": "D_SEX", "学历": "D_DEGREE",
            "海外经历": "D_OVERSEAS_FIELD", "户籍类型": "D_HUKOU_TYPE"}
    with _p.connect(ADMIN, row_factory=dict_row) as c:
        reg = {r["dimension_id"]: r for r in c.execute(
            "SELECT dimension_id, expr_sql FROM mt.dimension_registry")}
        texp = reg["D_DEST_TIER1"]["expr_sql"]
        sel = ", ".join("%s AS f%d" % (reg[v]["expr_sql"], i)
                        for i, v in enumerate(dims.values()))
        raw = c.execute("SELECT %s, %s AS tv FROM mt.person p "
                        "LEFT JOIN mt.person_demographics d ON d.person_id=p.person_id "
                        "WHERE %s IS NOT NULL" % (sel, texp, texp)).fetchall()
    rows = []
    for r in raw:
        d = {"y": 1 if r["tv"] == "三级医院" else 0}
        for i, k in enumerate(dims):
            d[k] = r["f%d" % i]
        rows.append(d)
    res = MF.analyze(rows, list(dims), dims)
    print("结果变量：是否进三级医院（%d 人，事件 %d）" % (res["n"], res["n_events"]))
    print("\n分析找回来的东西（与真值对照）：")
    for b in res["factors"]:
        if b.get("skip"):
            print("  %-8s 跳过：%s" % (b["title"], b["skip"]))
            continue
        for it in b["levels"]:
            c_ = "%.2f" % it["crude_or"] if it["crude_or"] is not None else "—"
            a_ = "%.2f" % it["adj_or"] if it["adj_or"] is not None else "—"
            p_ = "%.3f" % it["p"] if it["p"] is not None else "—"
            print("  %-8s %-8s 粗OR=%-7s 调整OR=%-7s p=%s"
                  % (b["title"], str(it["level"])[:8], c_, a_, p_))
    print("\n真值应当体现为：")
    print("  · 学历      → 调整后仍强（真效应）")
    print("  · 海外经历  → 粗 OR 偏离 1，**调整后趋近 1**（混杂被剥离）")
    print("  · 户籍类型  → 对'是否进三级医院'弱；对'基层医疗'强（换结果变量再看）")
    print("  · 性别/年龄 → 都不显著（假阳性检验）")
    return 0


def cmd_cleanup(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        n = c.execute("""DELETE FROM mt.field_value
                          WHERE field_id = ANY(%s) AND subject_type='person'
                            AND subject_id LIKE 'per_mock_%%'""", (list(FIELDS),)).rowcount
        c.commit()
    print("[✓] 已删除 %d 条演示值（只删这 4 个字段 × per_mock_* 主体）" % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description="带已知真相的合成数据（就业去向 + 因子）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("show", cmd_show), ("apply", cmd_apply),
                     ("truth", cmd_truth), ("cleanup", cmd_cleanup)):
        sub.add_parser(name).set_defaults(fn=fn)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
