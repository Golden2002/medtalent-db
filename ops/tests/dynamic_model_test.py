# -*- coding: utf-8 -*-
"""
ops/tests/dynamic_model_test.py —— 动态建模能力验证（mock 数据驱动）

要验证的两件事（来自用户需求）：
  ① 真实使用中发现新的用户画像维度 A，应能"方便地加进去"  → 列可动态增删
  ② 依据用户输入动态创建实例（记录）                      → 行可动态创建
  外加：每个字段预设 n 个选项供用户选择，选择结果入库并可校验。

验证方式：全程 mock 数据，**整个测试在一个事务内执行并最终 ROLLBACK**，
因此可反复运行、不污染库。所有断言失败即抛异常并中止。

硬证据：测试前后对比「mt 下基础表数量」与「person 表列数」——
若两者都不变，则证明新增维度/新增实例**没有执行任何 DDL**。

用法：python ops/tests/dynamic_model_test.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")

PASS, FAIL = H.PASS, H.FAIL
TIMINGS = {}
check, ok, fail = H.check, H.ok, H.fail


def one(cur, sql, params=None):
    cur.execute(sql, params)
    r = cur.fetchone()
    return list(r.values())[0] if r else None


def rows(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchall()


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    conn.autocommit = False
    cur = conn.cursor()

    print("=" * 78)
    print("动态建模能力验证（mock 数据）  —— 全程单事务，结束即回滚")
    print("=" * 78)

    # ------------------------------------------------------------------
    # P0 基线快照
    # ------------------------------------------------------------------
    print("\n【P0】基线快照")
    base = {
        "tables": one(cur, "SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema='mt' AND table_type='BASE TABLE'"),
        "person_cols": one(cur, "SELECT count(*) FROM information_schema.columns "
                                "WHERE table_schema='mt' AND table_name='person'"),
        "fields": one(cur, "SELECT count(*) FROM field_catalog"),
        "entities": one(cur, "SELECT count(*) FROM entity_catalog"),
    }
    for k, v in base.items():
        print("  %-14s = %s" % (k, v))

    # ------------------------------------------------------------------
    # P1 新增画像维度 A（单选，4 个选项）
    # ------------------------------------------------------------------
    print("\n【P1】新增画像维度 A：'基层服务意愿'（单选，4 个选项）")
    t0 = time.perf_counter()
    cur.execute("""
        SELECT mt.add_dimension(
            p_entity      => 'person',
            p_field_id    => 'F_PROFILE_A',
            p_title       => '基层服务意愿',
            p_options     => %s::jsonb,
            p_required    => true,
            p_section     => '就业偏好',
            p_description => '面对基层医疗岗位的接受程度')""",
        (json.dumps([
            {"code": "A1", "label": "非常愿意", "sort": 10},
            {"code": "A2", "label": "愿意",     "sort": 20},
            {"code": "A3", "label": "犹豫",     "sort": 30},
            {"code": "A4", "label": "不愿意",   "sort": 40},
        ], ensure_ascii=False),))
    TIMINGS["add_dimension"] = (time.perf_counter() - t0) * 1000

    check(one(cur, "SELECT count(*) FROM field_catalog WHERE field_id='F_PROFILE_A'") == 1,
          "维度 A 已登记进 field_catalog")
    check(one(cur, "SELECT count(*) FROM code_value WHERE code_table_id='CT_F_PROFILE_A'") == 4,
          "自动创建词表 CT_F_PROFILE_A 并写入 4 个选项")
    check(one(cur, "SELECT count(*) FROM information_schema.columns "
                   "WHERE table_schema='mt' AND table_name='person'") == base["person_cols"],
          "person 表列数未变（%d）——新维度是「数据」而不是「结构」" % base["person_cols"])
    check(one(cur, "SELECT count(*) FROM information_schema.tables WHERE table_schema='mt' "
                   "AND table_type='BASE TABLE'") == base["tables"],
          "基础表数量未变（%d）——新增维度零 DDL" % base["tables"])
    print("  → 耗时 %.1f ms" % TIMINGS["add_dimension"])

    # ------------------------------------------------------------------
    # P2 mock 用户：动态创建 20 个实例并写入维度 A
    # ------------------------------------------------------------------
    print("\n【P2】用 mock 数据动态创建 20 个用户实例，并记录维度 A")
    t0 = time.perf_counter()
    codes = ["A1", "A2", "A3", "A4"]
    for i in range(1, 21):
        cur.execute("""
            SELECT mt.create_instance(
                p_entity  => 'person',
                p_id      => %s,
                p_payload => %s::jsonb)""",
            ("per_dmtest_%02d" % i,
             json.dumps({
                 "person_id": "per_dmtest_%02d" % i,
                 "subject_code": "MT-DMTEST-%03d" % i,
                 "enroll_channel": "EC1",
                 "F_PROFILE_A": codes[i % 4],
             }, ensure_ascii=False)))
    TIMINGS["insert_20"] = (time.perf_counter() - t0) * 1000

    check(one(cur, "SELECT count(*) FROM person WHERE person_id LIKE 'per_dmtest_%'") == 20,
          "20 个 person 实例已创建（走类型化表）")
    check(one(cur, "SELECT count(*) FROM field_value "
                   "WHERE field_id='F_PROFILE_A' AND subject_id LIKE 'per_dmtest_%'") == 20,
          "20 条维度 A 的值已写入 field_value")
    check(one(cur, "SELECT count(DISTINCT subject_id) FROM field_value "
                   "WHERE field_id='F_PROFILE_A'") == 20, "20 个主体各不相同")
    print("  → 20 行耗时 %.1f ms（平均 %.2f ms/行）"
          % (TIMINGS["insert_20"], TIMINGS["insert_20"] / 20))

    dist = rows(cur, """
        SELECT v.value_code, cv.label_zh, count(*) AS n
        FROM field_value v
        JOIN field_catalog f ON f.field_id = v.field_id
        JOIN code_value cv ON cv.code_table_id = f.code_table_id AND cv.code = v.value_code
        WHERE v.field_id='F_PROFILE_A'
        GROUP BY 1,2 ORDER BY 1""")
    print("  → 维度 A 分布：" + "，".join("%s(%s)=%d" % (r["value_code"], r["label_zh"], r["n"])
                                          for r in dist))

    # ------------------------------------------------------------------
    # P3 再加三个维度：多选 / 数值 / 文本
    # ------------------------------------------------------------------
    print("\n【P3】追加 3 个维度（多选 / 数值 / 文本），验证类型自由")
    cur.execute("""SELECT mt.add_dimension(p_entity=>'person',
        p_field_id=>'F_PROFILE_B', p_title=>'可接受的岗位族',
        p_options=>%s::jsonb, p_multi=>true, p_section=>'就业偏好')""",
        (json.dumps([{"code": "F01", "label": "临床医疗", "sort": 10},
                     {"code": "F02", "label": "药企医学事务", "sort": 20},
                     {"code": "F04", "label": "CRO/临床研究", "sort": 30},
                     {"code": "F08", "label": "医疗AI/数字健康", "sort": 40}],
                    ensure_ascii=False),))
    cur.execute("""SELECT mt.add_dimension(p_entity=>'person',
        p_field_id=>'F_PROFILE_C', p_title=>'可接受最低年薪(万)',
        p_data_type=>'number', p_section=>'薪酬期望')""")
    cur.execute("""SELECT mt.add_dimension(p_entity=>'person',
        p_field_id=>'F_PROFILE_D', p_title=>'自我介绍补充',
        p_data_type=>'text', p_section=>'其他')""")

    check(one(cur, "SELECT count(*) FROM field_catalog WHERE field_id IN "
                   "('F_PROFILE_A','F_PROFILE_B','F_PROFILE_C','F_PROFILE_D')") == 4,
          "4 个新维度均已登记进 field_catalog")

    for i, (b, c) in enumerate([(["F01", "F02"], 25), (["F04"], 18),
                                (["F02", "F08"], 35), (["F01"], 15)], start=1):
        cur.execute("SELECT mt.set_value(p_entity=>'person', p_subject_id=>%s, "
                    "p_field_id=>'F_PROFILE_B', p_codes=>%s)",
                    ("per_dmtest_%02d" % i, b))
        cur.execute("SELECT mt.set_value(p_entity=>'person', p_subject_id=>%s, "
                    "p_field_id=>'F_PROFILE_C', p_num=>%s)",
                    ("per_dmtest_%02d" % i, c))
        cur.execute("SELECT mt.set_value(p_entity=>'person', p_subject_id=>%s, "
                    "p_field_id=>'F_PROFILE_D', p_text=>%s)",
                    ("per_dmtest_%02d" % i, "mock 自述 %d" % i))

    check(one(cur, "SELECT count(*) FROM field_value WHERE field_id='F_PROFILE_B'") == 4,
          "多选维度 B 写入了 4 条（含多次选择）")
    check(one(cur, "SELECT value_codes FROM field_value WHERE field_id='F_PROFILE_B' "
                   "AND subject_id='per_dmtest_01'") == ["F01", "F02"],
          "多选项按数组存储（['F01','F02']）")
    check(one(cur, "SELECT value_num FROM field_value WHERE field_id='F_PROFILE_C' "
                   "AND subject_id='per_dmtest_01'") == 25,
          "数值维度 C 写入正确（25）")

    cross = rows(cur, """
        SELECT a.value_code AS a_code, b.value_codes AS b_codes, count(*) AS n
        FROM field_value a
        JOIN field_value b ON a.subject_id=b.subject_id AND b.field_id='F_PROFILE_B'
        WHERE a.field_id='F_PROFILE_A'
        GROUP BY 1,2 ORDER BY 1""")
    print("  → A × B 交叉：" + "；".join(
        "%s×%s=%d" % (r["a_code"], ",".join(r["b_codes"] or []), r["n"]) for r in cross))

    # ------------------------------------------------------------------
    # P4 选项约束：只能从给定选项里选
    # ------------------------------------------------------------------
    print("\n【P4】选项约束：写入未定义的选项必须被拒绝")
    try:
        with conn.transaction():
            cur.execute("SELECT mt.set_value(p_entity=>'person', p_subject_id=>'per_dmtest_01', "
                        "p_field_id=>'F_PROFILE_A', p_code=>'A9')")
        fail("非法选项 A9 竟被接受")
    except psycopg.errors.CheckViolation:
        ok("非法选项 A9 被拒绝（取值必须落在候选选项内）")
    except Exception as e:  # noqa: BLE001
        fail("拒绝原因不符预期：%s" % type(e).__name__)

    # ------------------------------------------------------------------
    # P5 行动态增删：给用户新增一段教育经历，再删除
    # ------------------------------------------------------------------
    print("\n【P5】行动态增删：新增一段教育经历，再删除")
    cur.execute("""SELECT mt.create_instance(p_entity=>'education_record',
        p_id=>'edu_mock_01',
        p_payload=>%s::jsonb)""",
        (json.dumps({"education_id": "edu_mock_01", "person_id": "per_dmtest_01",
                     "degree_level": "D3", "school_name": "某医科大学",
                     "major_raw": "外科学", "is_clinical": True,
                     "start_date": "2022-09-01", "end_date": "2025-06-30"},
                    ensure_ascii=False),))
    check(one(cur, "SELECT count(*) FROM education_record WHERE education_id='edu_mock_01'") == 1,
          "教育经历实例已创建（未提供的列使用了默认值）")
    check(one(cur, "SELECT verify_status FROM education_record "
                   "WHERE education_id='edu_mock_01'") == "V1",
          "未提供的列走了默认值（verify_status=V1）而非 NULL")
    r = one(cur, "SELECT mt.delete_instance('education_record','edu_mock_01')")
    check(r.startswith("hard"), "删除返回：%s" % r)
    check(one(cur, "SELECT count(*) FROM education_record "
                   "WHERE education_id='edu_mock_01'") == 0, "教育经历实例已删除")

    # ------------------------------------------------------------------
    # P6 零 DDL 新增"实体"：纯值实体
    # ------------------------------------------------------------------
    print("\n【P6】零 DDL 新增实体：'profile_survey'（纯值实体，无物理表）")
    cur.execute("SELECT mt.register_entity(p_entity_id=>'profile_survey', "
                "p_domain=>'talent', p_kind=>'EK2', p_description=>'用户画像补充问卷')")
    cur.execute("""SELECT mt.add_dimension(p_entity=>'profile_survey',
        p_field_id=>'F_SURVEY_Q1', p_title=>'每周可投入学习时长',
        p_options=>%s::jsonb)""",
        (json.dumps([{"code": "Q1", "label": "<5小时"}, {"code": "Q2", "label": "5-10小时"},
                     {"code": "Q3", "label": ">10小时"}], ensure_ascii=False),))
    cur.execute("""SELECT mt.create_instance(p_entity=>'profile_survey', p_id=>'per_dmtest_01',
        p_payload=>%s::jsonb)""",
        (json.dumps({"F_SURVEY_Q1": "Q2"}, ensure_ascii=False),))

    check(one(cur, "SELECT kind FROM entity_catalog WHERE entity_id='profile_survey'") == "EK2",
          "纯值实体已注册（kind=EK2）")
    check(one(cur, "SELECT count(*) FROM field_value WHERE subject_type='profile_survey' "
                   "AND subject_id='per_dmtest_01'") == 1, "纯值实体的值已写入 field_value")
    check(one(cur, "SELECT count(*) FROM information_schema.tables WHERE table_schema='mt' "
                   "AND table_name='profile_survey'") == 0,
          "确认没有为它建物理表（零 DDL）")

    form = one(cur, "SELECT mt.form_schema('profile_survey')")
    check(len(form["fields"]) == 1 and form["fields"][0]["option_count"] == 3,
          "form_schema 能给出该实体的字段与 3 个选项（前端可直接渲染）")

    # ------------------------------------------------------------------
    # P7 列软删除：废弃维度 A，数据必须保留
    # ------------------------------------------------------------------
    print("\n【P7】维度软删除：废弃维度 A，数据必须保留")
    n_before = one(cur, "SELECT count(*) FROM field_value WHERE field_id='F_PROFILE_A'")
    cur.execute("SELECT mt.deprecate_dimension('F_PROFILE_A', '字段设计不合理，改用新维度')")
    check(one(cur, "SELECT status FROM field_catalog WHERE field_id='F_PROFILE_A'") == "deprecated",
          "维度 A 已标记 deprecated")
    check(one(cur, "SELECT count(*) FROM field_value WHERE field_id='F_PROFILE_A'") == n_before,
          "废弃后数据仍在（%d 条），未丢数据" % n_before)
    check(one(cur, "SELECT count(*) FROM v_form_schema WHERE field_id='F_PROFILE_A'") == 0,
          "废弃维度自动从表单 schema 中消失")
    check(one(cur, "SELECT count(*) FROM v_form_schema WHERE field_id='F_PROFILE_B'") == 1,
          "其他维度不受影响")

    # ------------------------------------------------------------------
    # P8 终局证据
    # ------------------------------------------------------------------
    print("\n【P8】终局证据对比")
    after = {
        "tables": one(cur, "SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema='mt' AND table_type='BASE TABLE'"),
        "person_cols": one(cur, "SELECT count(*) FROM information_schema.columns "
                                "WHERE table_schema='mt' AND table_name='person'"),
        "fields": one(cur, "SELECT count(*) FROM field_catalog"),
        "entities": one(cur, "SELECT count(*) FROM entity_catalog"),
    }
    print("  %-14s %8s -> %-8s %s" % ("基础表", base["tables"], after["tables"],
                                      "未变" if base["tables"] == after["tables"] else "变了"))
    print("  %-14s %8s -> %-8s %s" % ("person 列数", base["person_cols"], after["person_cols"],
                                      "未变" if base["person_cols"] == after["person_cols"] else "变了"))
    print("  %-14s %8s -> %-8s (+%d)" % ("已登记字段", base["fields"], after["fields"],
                                         after["fields"] - base["fields"]))
    print("  %-14s %8s -> %-8s (+%d)" % ("已登记实体", base["entities"], after["entities"],
                                         after["entities"] - base["entities"]))
    check(base["tables"] == after["tables"], "基础表数量未变 → **本次新增维度/实体/实例，DDL 次数 = 0**")
    check(base["person_cols"] == after["person_cols"], "person 表列数未变 → 列是动态的、不是结构变更")
    check(after["fields"] - base["fields"] == 5, "新增 5 个已登记维度")
    check(after["entities"] - base["entities"] == 1, "新增 1 个已登记实体")

    conn.rollback()

    def _timing():
        print("耗时：新增维度 %.1f ms；批量创建 20 行 %.1f ms（%.2f ms/行）"
              % (TIMINGS["add_dimension"], TIMINGS["insert_20"],
                 TIMINGS["insert_20"] / 20))

    return H.report(note="（事务已回滚，库中无残留）", extra=_timing)


if __name__ == "__main__":
    sys.exit(main())
