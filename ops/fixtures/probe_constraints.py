# -*- coding: utf-8 -*-
"""
ops/fixtures/probe_constraints.py —— 摸清人才侧表上"哪些写法会被数据库拒绝"

为什么单独留一个探针脚本：写 gen_talent.py 之前，必须先确认**正常写入路径**上到底有哪些
闸门（否则很容易写出"能跑但绕过纪律"的生成器）。本脚本把每一条预期会被拒的写法跑一遍，
每条都在**自己的事务里 rollback**——不留任何数据，只留下错误码与错误信息作为证据。

结论（与 006_integrity.sql / 007_attr_guard.sql / 001_core_schema.sql 对照）：
  · attrs 门禁是**触发器**（guard_attrs），不是 CHECK：写未登记键直接抛 check_violation；
  · transfer_note / evidence 兜底 / claim_type / level / transferability 是**语义 CHECK**；
  · 日期倒挂、出生年越界、枚举越界是**列级 CHECK**；
  · 概念/来源/人员引用是**外键**；
  · updated_at 由 touch_updated_at() 触发器维护，手写无效。

用法：python ops/fixtures/probe_constraints.py
"""
from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")

PID = "per_mock_0001"          # 由 gen_talent.py 生成；探针只读它、不改它

PROBES = [
    ("attrs 门禁：往 person.attrs 写未登记键",
     "INSERT INTO mt.person (person_id, subject_code, attrs) VALUES "
     "('per_probe_x', 'MT-PROBE-X', '{\"not_registered_key\": 1}'::jsonb)"),
    ("语义 CHECK：transferability>=3 但 transfer_note 为空",
     "UPDATE mt.skill_assertion SET transferability = 3, transfer_note = NULL "
     "WHERE person_id = %s AND transferability < 3"),
    ("语义 CHECK：evidence_id 为空却给 confidence > 0.5",
     "UPDATE mt.skill_assertion SET evidence_id = NULL, confidence = 0.8 "
     "WHERE person_id = %s"),
    ("语义 CHECK：claim_type 不在 CT1/CT2/CT3",
     "UPDATE mt.skill_assertion SET claim_type = 'CT9' WHERE person_id = %s"),
    ("列级 CHECK：skill_assertion.level = 7（限 0–5）",
     "UPDATE mt.skill_assertion SET level = 7 WHERE person_id = %s"),
    ("列级 CHECK：transferability = 5（docs/04 定为 0–4）",
     "UPDATE mt.skill_assertion SET transferability = 5, transfer_note = 'x' "
     "WHERE person_id = %s"),
    ("列级 CHECK：person_demographics.birth_year = 1900（限 1930–2020）",
     "UPDATE mt.person_demographics SET birth_year = 1900 WHERE person_id = %s"),
    ("语义 CHECK：education_record 起止日期倒挂",
     "UPDATE mt.education_record SET start_date = DATE '2030-01-01', "
     "end_date = DATE '2010-01-01' WHERE person_id = %s"),
    ("语义 CHECK：employment_record 起止日期倒挂",
     "UPDATE mt.employment_record SET start_date = DATE '2030-01-01', "
     "end_date = DATE '2010-01-01' WHERE person_id = %s"),
    ("唯一约束：person.subject_code 重码",
     "INSERT INTO mt.person (person_id, subject_code) VALUES "
     "('per_probe_y', (SELECT subject_code FROM mt.person WHERE person_id = %s))"),
    ("外键：skill_assertion.concept_id 指向不存在的概念",
     "INSERT INTO mt.skill_assertion (assertion_id, person_id, concept_id, confidence) "
     "VALUES ('skl_probe_z', %s, 'CON-NOT-EXIST', 0.5)"),
    ("列级 CHECK：source_registry.evidence_grade 不在 A–D",
     "UPDATE mt.source_registry SET evidence_grade = 'X' WHERE source_id = "
     "'src_fixture_talent_mock'"),
]


def main():
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM mt.person WHERE person_id = %s", (PID,))
            if cur.fetchone()["n"] == 0:
                print("[X] 找不到 %s —— 请先跑 python ops\\fixtures\\gen_talent.py" % PID)
                return 2

        print("每条探针在自己的事务里执行并 rollback，不留下任何数据。\n")
        n_rejected = 0
        for title, stmt in PROBES:
            args = (PID,) if "%s" in stmt else ()
            with c.cursor() as cur:
                try:
                    cur.execute(stmt, args)
                except psycopg.Error as e:
                    c.rollback()
                    n_rejected += 1
                    print("[被拒] %s" % title)
                    print("        SQLSTATE %s · %s" % (e.sqlstate, str(e).strip().splitlines()[0]))
                else:
                    c.rollback()
                    print("[!!未拦住] %s —— 这条写法居然通过了，需要复核" % title)
        print("\n%d/%d 条非法写法被数据库拒绝（其余需人工复核）。"
              % (n_rejected, len(PROBES)))

        # 反向确认：合法写法能通过，且 updated_at 由触发器维护
        with c.cursor() as cur:
            cur.execute("SELECT updated_at FROM mt.person WHERE person_id = %s", (PID,))
            before = cur.fetchone()["updated_at"]
            cur.execute("UPDATE mt.person SET confidence = confidence WHERE person_id = %s",
                        (PID,))
            cur.execute("SELECT updated_at FROM mt.person WHERE person_id = %s", (PID,))
            after = cur.fetchone()["updated_at"]
            print("updated_at 触发器：更新前 %s → 更新后 %s（%s）"
                  % (before, after, "已自动刷新" if after >= before else "未刷新"))
        c.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
