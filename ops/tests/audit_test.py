# -*- coding: utf-8 -*-
"""
ops/tests/audit_test.py —— 审计"只记真的变了什么"的回归断言（迁移 066/067）

为什么要单独立一套
--------------------------------------------------------------------------
起因是一次 **10 倍性能劣化**：完整回归里的备份步骤从 664 秒变成 7224 秒（2 小时）。
根因：库 255 MB 里 `change_log` 占 **210 MB（82%）**，其中机械重算类表
（job_requirement + job_posting）占 **771,715 行 / 148 MB（70%）**，
而备份做完整 dump/restore，成本几乎全在这里。

更糟的是那些行**没有信息量**：内容只有
`{"op":"UPDATE","row":"job_id=...","mode":"row"}` —— 看不出改了哪一列。

所以修法不是"少记"，而是"**只记真的变了什么**"：
  ① 无实质变化的 UPDATE 不写审计（零信息行不该占体量）；
  ② 有变化时记**变化的列名**（从"某行被更新过"变成"哪几列变了"）；
  ③ 只改簿记列（updated_at/recorded_at 之类）也算无实质变化。

这一套把三条钉住。⚠ **注意每一条都要验"拦得住"与"拦过头"两侧** ——
只验一侧会出现"审计整个没了但测试全绿"。本文件就是被这个教训逼出来的：
I1.8 第一版写 `r[0]`，只有在**真的检出违规**时才崩，通过时一切正常。
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check
ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        def q1(sql, p=None):
            return c.execute(sql, p).fetchone()

        # 挑一个岗位做实验，记下原值以便恢复
        row = q1("SELECT job_id, title_raw, title_normalized FROM mt.job_posting "
                 "WHERE title_raw IS NOT NULL ORDER BY job_id LIMIT 1")
        if not row:
            return H.report(width=74, list_fails=True)
        jid, t_raw, t_norm = row["job_id"], row["title_raw"], row["title_normalized"]
        obj = "job_id=" + jid

        def n_audit():
            return q1("SELECT count(*) AS n FROM mt.change_log "
                      "WHERE object_type='job_posting' AND object_name=%s", (obj,))["n"]

        try:
            # ---------- ① 无实质变化的 UPDATE：不写审计 ----------
            n0 = n_audit()
            c.execute("UPDATE mt.job_posting SET title_raw=%s WHERE job_id=%s",
                      (t_raw, jid))
            check(n_audit() == n0,
                  "**无实质变化的 UPDATE 不写审计**（%d → %d）—— 零信息行被拦住"
                  % (n0, n_audit()))

            # ---------- ② 只改簿记列：也不写审计 ----------
            col = q1("SELECT column_name FROM mt.audit_ignore_columns "
                     "WHERE table_name='job_posting' ORDER BY column_name LIMIT 1")
            if col:
                n0 = n_audit()
                c.execute('UPDATE mt.job_posting SET "%s" = now() WHERE job_id = %%s'
                          % col["column_name"], (jid,))
                check(n_audit() == n0,
                      "只改簿记列（%s）不写审计（%d → %d）—— 簿记列名单生效"
                      % (col["column_name"], n0, n_audit()))
            else:
                check(False, "job_posting 没有登记簿记列 —— audit_ignore_columns 不完整")

            # ---------- ③ 真实变更：**必须**写审计，且带变化的列名 ----------
            n0 = n_audit()
            c.execute("UPDATE mt.job_posting SET title_normalized = "
                      "coalesce(title_normalized,'') || 'x' WHERE job_id=%s", (jid,))
            n1 = n_audit()
            check(n1 > n0,
                  "**真实变更必须写审计**（%d → %d）—— 不能拦过头" % (n0, n1))
            d = q1("""SELECT detail FROM mt.change_log
                       WHERE object_type='job_posting' AND object_name=%s
                       ORDER BY change_id DESC LIMIT 1""", (obj,))["detail"]
            ch = d.get("changed_columns") or []
            check("title_normalized" in ch,
                  "审计里**记录了变化的列名**（%s）—— 从「某行被更新过」变成「改了哪一列」"
                  % ch)
            check("changed_count" in d and d["changed_count"] >= 1,
                  "审计里带变化列数（changed_count=%s）" % d.get("changed_count"))
            # 值不进审计（避免体积与个人信息问题）
            check(not any(isinstance(x, dict) for x in ch),
                  "审计只记列名、不记值（避免体积膨胀与个人信息外泄）")

            # ---------- ④ summary 模式的表仍然不逐行记（050 不能被 066 破坏）----------
            modes = {r["table_name"]: r["mode"] for r in c.execute(
                "SELECT table_name, mode FROM mt.audit_mode")}
            check(modes.get("job_requirement") == "summary",
                  "job_requirement 仍是 summary 粒度（050 的成果没被 066 破坏）")
            n0 = q1("SELECT count(*) AS n FROM mt.change_log "
                    "WHERE object_type='job_requirement'")["n"]
            c.execute("UPDATE mt.job_requirement SET raw_text = raw_text "
                      "WHERE requirement_id = (SELECT requirement_id FROM mt.job_requirement "
                      "ORDER BY requirement_id LIMIT 1)")
            n1 = q1("SELECT count(*) AS n FROM mt.change_log "
                    "WHERE object_type='job_requirement'")["n"]
            check(n1 == n0,
                  "summary 模式的表逐行 UPDATE 仍不写审计（%d → %d）" % (n0, n1))

            # ---------- ⑤ 噪声与体量可监控 ----------
            nr = q1("SELECT * FROM mt.metric_value('M_AUDIT_NOISE_RATE')")
            check(nr["value"] is not None and 0 <= nr["value"] <= 100,
                  "机械噪声审计占比能算（实得 %s%%）" % nr["value"])
            check(nr["value"] > 50,
                  "当前噪声占比确实很高（%s%%）—— 它是体量与备份耗时的主因，"
                  "也是「要不要归档历史噪声」这个决策的依据" % nr["value"])
            mb = q1("SELECT * FROM mt.metric_value('M_AUDIT_CHANGELOG_MB')")
            check(mb["value"] is not None and mb["value"] > 0,
                  "审计流水体量能算（实得 %s MB）" % mb["value"])

            # ---------- ⑥ 簿记列名单本身要可读（它是定义层）----------
            # ⚠ 判据必须**同时看表级与列级**：本库的授权由对账按**列**生成，
            # 表级 GRANT 会被收回，此时 has_table_privilege 返回 false 而表其实可读。
            # 我在 062 已经踩过一次，这里第一版又踩了 —— 用 has_any_column_privilege。
            readable = q1("""SELECT
                  has_table_privilege('mt_t0','mt.audit_ignore_columns','SELECT')
               OR has_any_column_privilege('mt_t0','mt.audit_ignore_columns','SELECT')
                 AS x""")["x"]
            check(readable,
                  "audit_ignore_columns 对匿名可读（公开的定义层，读者能自己核对）"
                  "—— 表级被对账收回、列级仍可读，所以判据要两个都看")
            dom = q1("SELECT domain FROM mt.domain_registry "
                     "WHERE table_name='audit_ignore_columns'")
            check(dom is not None,
                  "audit_ignore_columns 已在 domain_registry 登记域（建表时就该一起做）")
        finally:
            # 恢复现场：把 title_normalized 改回去
            c.execute("UPDATE mt.job_posting SET title_normalized=%s WHERE job_id=%s",
                      (t_norm, jid))

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
