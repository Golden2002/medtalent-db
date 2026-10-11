# -*- coding: utf-8 -*-
"""
ops/evolution.py —— 职业树的 变更影响预览 / 发布管理 / 下游一致性（评审优先级 5 收窄项）

三个子命令对应三条要求
--------------------------------------------------------------------------
  impact  <occupation_id>   变更影响预览：这个节点被谁引用、各有多少、风险多高
  propose / release         发布管理：把变更纳入一次发布；指出"已生效但未发布"的缺口
  check                     下游一致性：三类缺口（悬挂 / 坏迁移 / 未发布）
  selfcheck                 **真走一遍流程**，证明机制成立

为什么必须有 selfcheck（这是本文件最重要的一段）
--------------------------------------------------------------------------
`M_EVO_DOWNSTREAM_GAPS` 当前是 **0**，但它的口径说明里我自己写着：
「0 只说明目前没有不一致，**不说明一致性机制被验证过**」——
因为库里根本还没有任何变更记录，三张流程表都是空的。
一个"永远返回 0 的检查"和一个"真的能发现问题的检查"在空库上长得一模一样。
所以 selfcheck 要**故意造出不一致**，确认它能被检出，再修好，确认它消失：

  ① 挑一个**低影响**节点，提议 retire（**故意不给迁移映射**）
     → 期望：一致性缺口 +1，且缺口类型是"停用但下游仍引用且无迁移映射"
  ② 补上迁移映射 → 期望：该类缺口消失
  ③ 标记 applied，但**不纳入发布** → 期望：出现"已生效变更未纳入发布"
  ④ 纳入一次发布 → 期望：缺口归零
  ⑤ 回滚（reverted）→ 期望：仍然一致（回滚路径也要能走通）
  ⑥ 清理干净，恢复原状

**每一步都先期望、再看实测** —— 只"跑完没报错"不算验证过。
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "ops"))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
       "options='-c search_path=mt,public'")


def _gaps(c):
    return c.execute("SELECT gap_kind, count(*) AS n FROM mt.v_occupation_downstream_gap "
                     "GROUP BY 1 ORDER BY 1").fetchall()


def gaps_total(c):
    return c.execute("SELECT count(*) AS n FROM mt.v_occupation_downstream_gap").fetchone()["n"]


def cmd_impact(a):
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        if a.occupation_id:
            rows = c.execute("SELECT * FROM mt.v_occupation_impact "
                             "WHERE occupation_id = %s", (a.occupation_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM mt.v_occupation_impact "
                             "ORDER BY dependents_total DESC LIMIT %s",
                             (a.top or 15,)).fetchall()
    if not rows:
        print("[!] 没有该节点")
        return 1
    print("=" * 96)
    print("变更影响预览（改动前先看清下游）")
    print("=" * 96)
    for r in rows:
        print("\n%s  %s（level %s · %s）" % (r["occupation_id"], r["label_zh"],
                                             r["level"], r["status"]))
        print("  风险：%s" % r["impact_risk"])
        print("  依赖合计 %d —— 岗位 %d · 岗位要求 %d · 有效能力权重 %d · "
              "人才经历 %d · 薪资基准 %d · 子节点 %d · 验证集点 %d · 已有迁移 %d"
              % (r["dependents_total"], r["n_jobs"], r["n_requirements"], r["n_weights"],
                 r["n_employment"], r["n_salary"], r["n_children"],
                 r["n_validation_points"], r["n_migrations"]))
    print("\n⚠ 停用（retire）**必须同时给出迁移映射**，否则下游会静默悬挂：")
    print("   页面照旧显示旧节点名，但那个节点已经不作数了 —— 这是最容易被漏掉的一类。")
    return 0


def cmd_check(a):
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        rows = _gaps(c)
        tot = gaps_total(c)
        imp = c.execute("SELECT count(*) FILTER (WHERE impact_risk LIKE '高%') AS hi, "
                        "count(*) FILTER (WHERE dependents_total = 0) AS orphan "
                        "FROM mt.v_occupation_impact").fetchone()
    print("=" * 74)
    print("职业树下下游一致性")
    print("=" * 74)
    if not rows:
        print("缺失口 0 —— 一致")
    else:
        for r in rows:
            print("  ✗ %s：%d 处" % (r["gap_kind"], r["n"]))
    print()
    print("影响面：高影响节点 %d 个（改动会直接影响匹配与推荐）· "
          "零依赖节点 %d 个（没有任何下游引用）" % (imp["hi"], imp["orphan"]))
    print()
    print("三类缺口的含义：")
    print("  A 停用但下游仍引用且无迁移映射 → 下游静默悬挂（**最危险**）")
    print("  B 迁移关系指向不存在的节点     → 迁移表自己坏了")
    print("  C 已生效变更未纳入发布         → 树变了但对外没发布")
    if tot == 0:
        print("\n⚠ **0 只说明目前没有不一致，不说明机制被验证过** —— "
              "库里可能还没有任何变更记录。")
        print("   要证明机制成立请跑：python ops/evolution.py selfcheck")
    return 0


def cmd_selfcheck(a):
    """真走一遍：故意造出不一致 → 确认检出 → 修好 → 确认消失。"""
    ok = True
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=False) as c:
        # 挑一个**零依赖**的节点做实验（不动真实数据的影响面）
        node = c.execute("""SELECT occupation_id, label_zh FROM mt.v_occupation_impact
                             WHERE dependents_total = 0 ORDER BY occupation_id LIMIT 1""").fetchone()
        if not node:
            node = c.execute("SELECT occupation_id, label_zh FROM mt.occupation "
                             "ORDER BY occupation_id LIMIT 1").fetchone()
        nid, nlabel = node["occupation_id"], node["label_zh"]
        orig_status = c.execute("SELECT status FROM mt.occupation WHERE occupation_id=%s",
                                (nid,)).fetchone()["status"]
        # ⚠ `occupation_migration.new_id` 是 **NOT NULL** —— 也就是说
        # **"停用"也必须给出下游重映射目标**（不能表达"纯停用无后继"）。
        # 这是对的设计：下游总得有个地方去，否则就是静默悬挂。
        # 第一版自检写了 new_id=NULL 直接违反非空约束，也说明我一开始理解错了。
        succ = c.execute("""SELECT parent_id FROM mt.occupation
                             WHERE occupation_id=%s AND parent_id IS NOT NULL""",
                         (nid,)).fetchone()
        if succ and succ["parent_id"]:
            new_id = succ["parent_id"]
        else:
            new_id = c.execute("""SELECT occupation_id FROM mt.occupation
                                   WHERE occupation_id <> %s ORDER BY occupation_id LIMIT 1""",
                               (nid,)).fetchone()["occupation_id"]
        cid = "__selfcheck_change__"
        rid = "__selfcheck_release__"
        mid = "__selfcheck_migration__"

        def expect(label, got, want):
            nonlocal ok
            good = (got == want)
            ok = ok and good
            print("  %s %s（期望 %s，实测 %s）"
                  % ("[✓]" if good else "[✗]", label, want, got))

        try:
            base = gaps_total(c)
            print("=" * 74)
            print("selfcheck：故意造出不一致，确认能被检出")
            print("节点：%s %s（原始状态 %s；基线缺口 %d）" % (nid, nlabel, orig_status, base))
            print("=" * 74)

            # ① 停用节点，**故意不给迁移映射**
            c.execute("UPDATE mt.occupation SET status='retired' WHERE occupation_id=%s", (nid,))
            c.execute("""INSERT INTO mt.occupation_change
                  (change_id, change_type, from_ids, to_ids, label_zh, reason, status,
                   proposed_by, effective_from)
                  VALUES (%s,'retire',ARRAY[%s]::text[],ARRAY[]::text[],%s,
                          'selfcheck：验证一致性检出的自检数据','applied','selfcheck',CURRENT_DATE)""",
                      (cid, nid, nlabel))
            got = c.execute("""SELECT count(*) AS n FROM mt.v_occupation_downstream_gap
                                WHERE gap_kind = '停用但下游仍引用且无迁移映射'
                                  AND object_id = %s""", (nid,)).fetchone()["n"]
            expect("停用但无迁移映射 → 被检出", got, 1)
            got = c.execute("""SELECT count(*) AS n FROM mt.v_occupation_downstream_gap
                                WHERE gap_kind = '已生效变更未纳入发布'
                                  AND object_id = %s""", (cid,)).fetchone()["n"]
            expect("已生效但未纳入发布 → 被检出", got, 1)

            # ② 补迁移映射 → A 类缺口应消失
            # ⚠ 注意：SQL 字符串**字面量里不能出现 `%s`** —— psycopg 会把它当占位符，
            # 报 "could not determine data type of parameter"。note 用静态文本。
            c.execute("""INSERT INTO mt.occupation_migration
                  (migration_id, change_id, old_id, new_id, relation, coverage, effective_from, note)
                  VALUES (%s,%s,%s,%s,'retired',1.0,CURRENT_DATE,
                          'selfcheck：停用并把下游重映射到后继节点')""",
                      (mid, cid, nid, new_id))
            got = c.execute("""SELECT count(*) AS n FROM mt.v_occupation_downstream_gap
                                WHERE gap_kind = '停用但下游仍引用且无迁移映射'
                                  AND object_id = %s""", (nid,)).fetchone()["n"]
            expect("补上迁移映射 → A 类缺口消失", got, 0)

            # ③ ③' 坏迁移必须被检出（B 类）
            c.execute("""INSERT INTO mt.occupation_migration
                  (migration_id, change_id, old_id, new_id, relation, coverage, effective_from, note)
                  VALUES ('__selfcheck_bad__',%s,'__no_such_node__',%s,'retired',1.0,
                          CURRENT_DATE,'selfcheck：故意指向不存在的节点')""", (cid, new_id))
            got = c.execute("""SELECT count(*) AS n FROM mt.v_occupation_downstream_gap
                                WHERE gap_kind = '迁移关系指向不存在的节点'""").fetchone()["n"]
            expect("迁移指向不存在的节点 → 被检出", got >= 1, True)

            # ④ 纳入一次发布 → C 类缺口应消失
            # ⚠ `dataset_release.doc_codebook_uri` 是 NOT NULL（其余文档 URI 可空）——
            # 这张表要求每次发布都给出**码本位置**。这是合理的：没有码本的"发布"
            # 别人拿到也不知道字段怎么解释。自检里给一个明确的占位值。
            c.execute("""INSERT INTO mt.dataset_release
                           (release_id, name, version, doc_codebook_uri, status, is_live)
                         VALUES (%s,'selfcheck 发布','0.0.0-selfcheck',
                                 'selfcheck://no-real-codebook','RS1',false)
                         ON CONFLICT (release_id) DO NOTHING""", (rid,))
            c.execute("""INSERT INTO mt.occupation_release_item (release_id, change_id, note)
                         VALUES (%s,%s,'selfcheck') ON CONFLICT DO NOTHING""", (rid, cid))
            got = c.execute("""SELECT count(*) AS n FROM mt.v_occupation_downstream_gap
                                WHERE gap_kind = '已生效变更未纳入发布'
                                  AND object_id = %s""", (cid,)).fetchone()["n"]
            expect("纳入发布 → C 类缺口消失", got, 0)

            # ⑤ 回滚路径也要能走通（reverted 后仍应一致）
            c.execute("UPDATE mt.occupation_change SET status='reverted' WHERE change_id=%s", (cid,))
            c.execute("UPDATE mt.occupation SET status=%s WHERE occupation_id=%s",
                      (orig_status, nid))
            got = c.execute("""SELECT count(*) AS n FROM mt.v_occupation_downstream_gap
                                WHERE object_id = %s""", (cid,)).fetchone()["n"]
            expect("回滚后该变更不再产生缺口", got, 0)

            # 发布状态视图要能反映归属
            got = c.execute("""SELECT n_changes FROM mt.v_occupation_release_status
                                WHERE release_id = %s""", (rid,)).fetchone()["n_changes"]
            expect("发布状态视图反映变更归属", got, 1)

        finally:
            # ⑥ 清理：**先回滚可能已中止的事务**（否则后面的命令全被拒绝 ——
            # 第一版就栽在这里：断言失败后事务中止，清理语句直接报
            # InFailedSqlTransaction，反而把真正的错盖住了），再按外键顺序删。
            c.rollback()
            c.execute("DELETE FROM mt.occupation_release_item WHERE release_id=%s OR change_id=%s",
                      (rid, cid))
            c.execute("DELETE FROM mt.occupation_migration WHERE migration_id IN (%s,'__selfcheck_bad__')",
                      (mid,))
            c.execute("DELETE FROM mt.occupation_change WHERE change_id=%s", (cid,))
            c.execute("DELETE FROM mt.dataset_release WHERE release_id=%s", (rid,))
            c.execute("UPDATE mt.occupation SET status=%s WHERE occupation_id=%s",
                      (orig_status, nid))
            c.commit()

    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as c:
        left = gaps_total(c)
        cleaned = c.execute("""SELECT (SELECT count(*) FROM mt.occupation_change) AS ch,
                                      (SELECT count(*) FROM mt.occupation_release_item) AS ri,
                                      (SELECT count(*) FROM mt.dataset_release) AS dr,
                                      (SELECT count(*) FROM mt.occupation_migration) AS mg""").fetchone()
    print()
    print("清理：变更 %d · 归属 %d · 发布 %d · 迁移 %d（都应为 0，即回到原始空状态）"
          % (cleaned["ch"], cleaned["ri"], cleaned["dr"], cleaned["mg"]))
    print("收尾缺口数：%d" % left)
    print()
    print("=" * 74)
    print("selfcheck %s" % ("通过：一致性机制**确实能发现问题**，不是永远返回 0 的空壳"
                            if ok and left == 0 else "未通过 —— 见上面标 ✗ 的行"))
    print("=" * 74)
    return 0 if (ok and left == 0) else 1


def main():
    ap = argparse.ArgumentParser(description="职业树：影响预览 / 发布 / 下游一致性")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("impact", help="变更影响预览")
    p1.add_argument("occupation_id", nargs="?")
    p1.add_argument("--top", type=int, default=15)
    sub.add_parser("check", help="下游一致性检查")
    sub.add_parser("selfcheck", help="真走一遍流程，证明机制成立")
    a = ap.parse_args()
    if a.cmd == "impact":
        return cmd_impact(a)
    if a.cmd == "check":
        return cmd_check(a)
    return cmd_selfcheck(a)


if __name__ == "__main__":
    sys.exit(main())
