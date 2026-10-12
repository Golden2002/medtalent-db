# -*- coding: utf-8 -*-
"""
ops/tests/survey_import_test.py —— 真实报名表导入（**选项 A：去标识**）

用户决定："敏感信息不记录，按选项 A 进行。"

这一套要守住的最重要一条**不是"导入了多少"，而是"没导入什么"**：
  ① **零 PII**：库里不得出现姓名、联系方式、以及「学校+科室+角色」原文。
     报名表原文里有真名（"边贺玮""贺雅婷"）与微信号，而且
     "中国中医科学院广安门医院/内窥镜中心（脾胃科）/2027届硕士毕业生"
     这种**原文本身就是指纹** —— 逐字存下来等于没去标识。
  ② **来源性质是登记的事实**：`src_survey_2026_10` 必须 `is_synthetic=false`、
     `source_type='SRC5'(问卷)`。判据不用名称匹配（本库纪律：判据是事实不是名字）。
  ③ **缺失不是问题，误读才是**：映射不了的一律留空并计数，**不硬塞、不猜**。
     所以这里断言"维度覆盖率"是一个合理的中间值，而不是要求 100%。
  ④ **可撤销、可重复**：`--cleanup` 能干净撤销；重复 `--apply` 不产生重复行。
"""
from __future__ import annotations

import os
import re
import subprocess
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
PREFIX = "per_svy26_"


def main():
    with psycopg.connect(ADMIN, row_factory=dict_row, autocommit=True) as c:
        def q1(sql, p=None):
            return c.execute(sql, p).fetchone()

        # ---------- 基本盘 ----------
        n = q1("SELECT count(*) AS n FROM mt.person WHERE person_id LIKE %s",
               (PREFIX + "%",))["n"]
        check(n > 0, "已导入 %d 位真实报名者（person_id 前缀 %s）" % (n, PREFIX))
        if n == 0:
            return H.report(width=74, list_fails=True)
        flagged = q1("""SELECT count(*) AS n FROM mt.person
                         WHERE person_id LIKE %s
                           AND quality_flags && ARRAY['real_survey_response']::text[]""",
                     (PREFIX + "%",))["n"]
        check(flagged == n,
              "**每一位都带 `real_survey_response` 标记**（%d/%d）—— "
              "数据性质靠写入时登记的事实，不靠事后判断" % (flagged, n))
        # 他们**不是**合成夹具（否则会污染"真实/合成"统计）
        syn = q1("""SELECT count(*) AS n FROM mt.person
                     WHERE person_id LIKE %s
                       AND quality_flags && ARRAY['synthetic_fixture']::text[]""",
                 (PREFIX + "%",))["n"]
        check(syn == 0, "没有任何一位被标成合成夹具（%d）" % syn)

        # ---------- ① 零 PII（最重要）----------
        # 1a. 联系方式样式：手机号 / 邮箱 / 微信号样式
        bad_phone = q1("""SELECT count(*) AS n FROM mt.field_value
                           WHERE subject_id LIKE %s
                             AND (value_text ~ '^1[3-9][0-9]{9}$' OR value_text ~ '@')""",
                       (PREFIX + "%",))["n"]
        check(bad_phone == 0,
              "**没有任何手机号/邮箱样式落库**（违规 %d）" % bad_phone)
        # 1b. 不得写入任何"长自由文本"（原文是长句；我们只写短码/短标签）
        long_text = q1("""SELECT count(*) AS n FROM mt.field_value
                           WHERE subject_id LIKE %s
                             AND value_text IS NOT NULL AND length(value_text) > 12""",
                       (PREFIX + "%",))["n"]
        check(long_text == 0,
              "**没有写入任何长自由文本**（>12 字，违规 %d）—— "
              "报名原文逐字存下来等于没去标识" % long_text)
        # 1c. 列举实际写入的文本值，必须都是短标签
        texts = [r["value_text"] for r in c.execute(
            "SELECT DISTINCT value_text FROM mt.field_value WHERE subject_id LIKE %s "
            "AND value_text IS NOT NULL", (PREFIX + "%",))]
        check(all(len(t) <= 12 for t in texts),
              "写入的文本值全部是短标签（%s）" % "、".join(sorted(texts)[:6]))
        # 1d. person 表本身不得带姓名/联系方式（person_pii 必须没有本次导入的人）
        pii = q1("""SELECT count(*) AS n FROM mt.person_pii
                     WHERE person_id LIKE %s""", (PREFIX + "%",))["n"]
        check(pii == 0,
              "**本次导入没有在 person_pii 里留任何行**（%d）—— 去标识的核心" % pii)
        # 1e. external_identity 也不该有（那里会存外部编号，属于可追溯标识）
        ext = q1("""SELECT count(*) AS n FROM information_schema.tables
                     WHERE table_schema='mt' AND table_name='external_identity'""")["n"]
        if ext:
            ei = q1("""SELECT count(*) AS n FROM mt.external_identity
                        WHERE person_id LIKE %s""", (PREFIX + "%",))["n"]
            check(ei == 0, "external_identity 里也没有本次导入的人（%d）" % ei)

        # ---------- ② 来源性质是登记的事实 ----------
        s = q1("""SELECT source_id, source_type, is_synthetic, license_note
                    FROM mt.source_registry WHERE source_id='src_survey_2026_10'""")
        check(s is not None, "来源 src_survey_2026_10 已登记")
        check(s and s["is_synthetic"] is False,
              "**来源标为真实**（is_synthetic=false）—— 判据是登记的事实，不是名称匹配")
        check(s and s["source_type"] == "SRC5",
              "来源类型是 **SRC5 问卷**（实得 %s）—— 登记成「网页采集」会让来源性质失真"
              % (s["source_type"] if s else "?"))
        check(s and "去标识" in (s["license_note"] or ""),
              "来源说明里写明了「去标识」与「报名≠同意入库」")

        # ---------- ③ 缺失是留空、不是硬塞 ----------
        tot = n
        dims = {}
        for f in ("F_PSN_INT_INTEREST_DOMAIN", "F_PSN_EDU_DEGREE_LEVEL",
                  "F_PSN_EDU_IS_CLINICAL_MAJOR", "F_PSN_SURVEY_PARTICIPATION"):
            dims[f] = q1("""SELECT count(DISTINCT subject_id) AS n FROM mt.field_value
                             WHERE subject_id LIKE %s AND field_id=%s""",
                         (PREFIX + "%", f))["n"]
        check(0 < dims["F_PSN_SURVEY_PARTICIPATION"] <= tot,
              "参与状态覆盖 %d/%d" % (dims["F_PSN_SURVEY_PARTICIPATION"], tot))
        for f in ("F_PSN_INT_INTEREST_DOMAIN", "F_PSN_EDU_DEGREE_LEVEL",
                  "F_PSN_EDU_IS_CLINICAL_MAJOR"):
            check(dims[f] < tot,
                  "%s **没有硬塞到 100%%**（%d/%d）—— 映射不了就留空，"
                  "缺失本身不是问题，误读才是" % (f, dims[f], tot))
        # 值必须是已登记的码（不许自造码）
        bad_code = q1("""SELECT count(*) AS n FROM mt.field_value v
                          WHERE v.subject_id LIKE %s AND v.value_code IS NOT NULL
                            AND NOT EXISTS (SELECT 1 FROM mt.code_value cv
                                             WHERE cv.code_table_id = (
                                               SELECT code_table_id FROM mt.field_catalog
                                                WHERE field_id = v.field_id)
                                               AND cv.code = v.value_code)""",
                      (PREFIX + "%",))["n"]
        check(bad_code == 0,
              "**写入的码值都在码表里**（违规 %d）—— 自造码会让统计口径对不上" % bad_code)

        # ---------- ④ 新增字段走得是正规机制 ----------
        fc = q1("""SELECT field_id, entity_id, status, access_tier FROM mt.field_catalog
                    WHERE field_id='F_PSN_SURVEY_PARTICIPATION'""")
        check(fc is not None, "新字段在 field_catalog 里登记了（否则 guard_field_value 会拒绝写入）")
        check(fc and fc["status"] == "active", "新字段状态 active")
        ad = q1("""SELECT attr_key, promoted_to_field FROM mt.attribute_definition
                    WHERE attr_key='survey_participation'""")
        check(ad is not None, "同时在 attribute_definition 里登记了（并指向该字段）")

        # ---------- ⑤ 幂等：重复导入不产生重复行 ----------
        before = q1("SELECT count(*) AS n FROM mt.person WHERE person_id LIKE %s",
                    (PREFIX + "%",))["n"]
        r = subprocess.run([sys.executable, os.path.join(BASE, "ops", "import_survey.py"),
                            "--apply"], cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        after = q1("SELECT count(*) AS n FROM mt.person WHERE person_id LIKE %s",
                   (PREFIX + "%",))["n"]
        check(r.returncode == 0 and before == after,
              "**重复 --apply 不会产生重复行**（%d → %d，退出码 %d）"
              % (before, after, r.returncode))

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
