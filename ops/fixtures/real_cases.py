# -*- coding: utf-8 -*-
"""
ops/fixtures/real_cases.py —— 把三位真实公开案例写成库里的**有效数据**

和 gen_talent.py 的关系：那个造合成样本，这个写真实案例。两者的共同纪律是
**幂等 + 自证 + 不越界**，区别在证据来源：合成样本的证据 URI 一律用 `.invalid`
保留域（永不解析到真实来源），真实案例的证据 URI 必须是真的、可点开的公开页面。

数据来源与红线
--------------------------------------------------------------------------
· 只写**职业公共记录**：院校、学位、专业、任职单位、职务、公开著作。
  不写家庭住址、私人联系方式、身份证、健康状况、家庭成员等 ——
  这些既不公开也不该进库；`person_pii` 一行都不产生。
· 每一条事实都挂 `evidence` 行（真实 URL + 出处方 + 采集时间）。
  **证据级别封顶 E2/E3**：公开报道与百科是"第三方记录"，
  不是履职原件，所以不能当 A 级证据用。申报时这一点必须说清楚。
· 查不到的一律留空（NULL），并把"哪些查不到"写进
  `person.attrs.public_case_gaps` —— 空就是空，不用默认值填。

为什么用 attrs 存"公开案例标签"而不是加列
--------------------------------------------------------------------------
`guard_attrs()` 要求 attrs 的每个键都先在 `attribute_definition` 登记。
本脚本先登记三个属性（`public_case_label` / `public_case_gaps` /
`public_case_sources`）再写值 —— 走的正是"机制 B：登记后零 DDL 扩展"。
如果绕过登记直接写，数据库会直接拒绝，这是设计好的门禁，不是障碍。

用法：
    python ops/fixtures/real_cases.py            # 写入（幂等）
    python ops/fixtures/real_cases.py --report    # 只读：看这 3 个人现在能匹配成什么样
    python ops/fixtures/real_cases.py --reset     # 只删这 3 个人及其从属行
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "analytics"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
import vector as V  # noqa: E402  ← 中文→码的归一化只有这一个实现（code/analytics/vector.py）

JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_cases.json")
DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "connect_timeout=5 options='-c search_path=mt,public'")

PREFIX = "per_real_"                     # 独占命名空间，绝不与 per_mock_/per_live_ 混
SRC_ID = "src_public_profile"
# 这些人是从公开网页上读到的真实人物 → source_type 用 SRC2（网页采集），
# 证据等级 C（一般二手）。不给 B：B 在本项目里留给"权威机构发布的二手材料"。
TAGS = ["real_public_case", "public_figure", "public_secondary_source"]

# 公开来源三元事实 → CEL 等级的映射。写成表是为了让"为什么给这一级"可复核，
# 而不是在每个 case 里各判一次。
CEL_BY_KIND = {"官网": "E2", "学术": "E2", "百科": "E0", "媒体": "E2"}
EV_TYPE_BY_KIND = {"官网": "EV3", "学术": "EV2", "百科": "EV7", "媒体": "EV7"}

ATTRS = [
    ("public_case_label", "person", "公开案例标识",
     "该行的公开身份标签（仅用于真实公开案例，便于人工核对；不是采集字段）"),
    ("public_case_gaps", "person", "公开信息缺口",
     "公开资料未覆盖的字段清单。写在这里是为了让「空」可解释：不是没采到，是公开记录里没有"),
    ("public_case_sources", "person", "公开来源清单",
     "分号分隔的公开来源 URL，与 evidence 表互为冗余（evidence 逐条可查，这里给出全貌）"),
    ("public_case_date_precision", "person", "公开日期精度",
     "哪些字段的日期只到年或月。公开报道很少给到日，补 -01 是为了可排序，"
     "但精度必须显式写出来，否则 -01-01 会被当成事实"),
    ("public_case_unmapped", "person", "未能归一化的原始值",
     "词表里装不下的原始取值（如产出类型）。不静默丢弃：要么补码，要么报出来"),
    # 以下三个是**网页采集**这一机制通用的溯源字段，任何来源都该带
    ("source_kind", "evidence", "来源类型",
     "官网 / 媒体 / 百科 / 学术。决定证据等级：官网与学术可到 E2，百科与媒体是第三方记录"),
    ("fetch_http_status", "evidence", "抓取 HTTP 状态",
     "采集当时的 HTTP 状态码。留空表示没实际抓取（例如人工录入）"),
    ("source_date", "evidence", "来源发布日期",
     "该来源的发布日期；未知留空，不用采集日期冒充"),
    # ⚠ 命名必须带实体前缀：attribute_definition 的主键是 **attr_key 单列**，
    # 所以同一个属性名只能属于一个实体 —— `date_precision` 不能同时登记给
    # education_record 和 employment_record，第二次登记会撞主键。
    # 这是 schema 的真实约束（不是我的偏好），所以按实体命名。
    ("edu_date_precision", "education_record", "教育日期精度",
     "该行 start/end 的日期精度：year / month / day。公开资料只到年时补 -01-01，精度写在这里"),
    ("edu_major_code_source", "education_record", "专业码来源",
     "registry=来源直接给了码；label_match=由专业中文按码表标签保守匹配得到（需人工复核）"),
    ("emp_date_precision", "employment_record", "任职日期精度",
     "同 edu_date_precision，用于任职起止"),
    ("output_type_raw", "research_output", "产出类型原文",
     "归一化前的原始中文（如「著作」「译作」）。保留原文是因为码表可能再次变化，"
     "而「当初写的是什么」是不可再生的信息"),
    # 下面只再加这一个。第二轮检索时本来还想往 attrs 里塞 round / emp_city /
    # case_volume_note / level_raw / mapped，**一律不登记**，理由分两类：
    #   · round（检索轮次）：ID 前缀 evd2_ / edu2_ 已经表达了轮次，再加属性是重复；
    #   · 其余：它们指向的是**我们没有的字段**（employment_record 没有城市列、
    #     clinical_exposure 没有例数说明列、award_honor 没有层级原文列…）。
    #     把"缺字段"塞进 attrs 会让缺口消失 —— 应该如实写进 person 的 gap 清单。
    #     **属性表不是缺失字段的回收站。**
    ("pref_evidence_quote", "preference", "偏好原话",
     "该偏好的公开表述原文。偏好是从公开言论里摘的，原话是它唯一的证据形态，"
     "不留原话就无法复核这条偏好有没有被摘错"),
]


def load_cases():
    if not os.path.isfile(JSON_PATH):
        print("[!] 缺少 %s" % JSON_PATH)
        print("    这是真实案例的**唯一数据来源**：先由联网检索产出该文件，再运行本脚本。")
        print("    不内置任何硬编码的事实——查不到就写 null，而不是猜一个。")
        return None
    with io.open(JSON_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
def register_source(c):
    # 注：这里刻意**不写 attrs**。第一版往 source_registry.attrs 里塞了
    # scope/pii/cel_cap 三个键，被 guard_attrs() 直接拒绝（未在 attribute_definition
    # 登记）—— 门禁对自己人一样生效，这是好事。这三个信息本来就该放
    # `license_note` 这个真实列里，而不是为一个临时用途去字典里加三个属性。
    c.execute("""
        INSERT INTO source_registry (source_id, name, source_type, base_url,
                                     license_note, credibility, evidence_grade,
                                     update_freq, access_tier, status)
        VALUES (%s, %s, 'SRC2', NULL, %s, 0.6, 'C', 'irregular', 'T1', 'active')
        ON CONFLICT (source_id) DO UPDATE
           SET name = EXCLUDED.name, license_note = EXCLUDED.license_note,
               credibility = EXCLUDED.credibility, evidence_grade = EXCLUDED.evidence_grade
    """, (SRC_ID, "公开报道与机构官网（个案核对）",
          "仅用于方法验证的公开人物职业记录，每条事实附原始链接。"
          "范围：仅职业公共记录；无任何非公开个人信息（不写 person_pii）。"
          "证据级别封顶 E3（第三方记录，非履职原件）。"
          "不得用于商业画像或对外发布。"))
    c.execute("""
        INSERT INTO ingest_run (ingest_run_id, source_id, started_at, finished_at,
                               record_count, error_count, status, tool_version, params)
        VALUES ('run_public_profile', %s, now(), now(), 0, 0, 'ok', 'real_cases/1.0.0',
                %s::jsonb)
        ON CONFLICT (ingest_run_id) DO UPDATE SET finished_at = now()
    """, (SRC_ID, json.dumps({"method": "public web sources, manually verified"},
                             ensure_ascii=False)))


def register_attrs(c):
    for key, entity, title, desc in ATTRS:
        c.execute("""
            INSERT INTO attribute_definition (attr_key, entity_id, title, data_type,
                                              status, version, created_at)
            VALUES (%s, %s, %s, 'text', 'active', '1.0.0', now())
            ON CONFLICT (attr_key) DO NOTHING
        """, (key, entity, title))
    # 说明写在 attribute_definition 的 title 里；desc 拼进 title 太丑，改为打印核对
    return [a[0] for a in ATTRS]


def reset_case(c, pid):
    """只删这个人的从属行与本人。用**动态发现的外键子表**而不是写死清单 ——
    写死清单在加表之后会静默漏删，留下孤儿行（这个坑在 console.py --reset 上踩过）。"""
    kids = c.execute("""
        SELECT cl.relname AS t, a.attname AS col
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        JOIN pg_class cl2 ON cl2.oid = con.confrelid
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
        WHERE con.contype='f' AND n.nspname='mt' AND cl2.relname='person'
          AND cl.relname <> 'person'
    """).fetchall()
    for k in kids:
        c.execute('DELETE FROM mt.%s WHERE %s = %%s' % (k["t"], k["col"]), (pid,))
    c.execute("DELETE FROM mt.person WHERE person_id=%s", (pid,))
    return len(kids)


def merge_extra(c, case, extra, pid):
    """把第二轮检索到的**增量事实**合并进库。

    为什么要分两轮：第一轮先把三个人写进去、跑出画像向量，看到"只填到 6–10/50 维"，
    才知道要专门去补哪些维度。第二轮就是按那个缺口去搜的 ——
    所以 extra 里的每一条，都对应第一轮报告里的一行"公开资料未覆盖"。

    这一步的主要工作是**取值归一化**：偏好类的公开表述是中文原话
    （"写作与文学、投资…"），而匹配用的是**码**。不归一化就直接写，
    集合比较会拿中文去和岗位的码求交集 → 交集为空 → 被判成"确认不满足"。
    **把'没归一化'当成'不合格'是在冤枉候选人**，所以映射不上就只留原文并记进 gaps。
    """
    if not extra:
        return {}, []
    key = case["case_key"]
    n = {"education": 0, "employment": 0, "clinical": 0, "output": 0,
         "preference": 0, "award": 0, "evidence": 0}
    unmapped = []

    # 增量来源 → evidence。轮次不做成属性：ID 前缀 evd2_ 已经表达了。
    for i, s in enumerate(extra.get("sources") or [], 1):
        n["evidence"] += 1
        c.execute("""
            INSERT INTO evidence (evidence_id, person_id, evidence_type, source_party,
                                  title, uri, verifiability, cel_level, access_tier, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'T1', %s::jsonb)
        """, ("evd2_%s_%02d" % (key, i), pid, EV_TYPE_BY_KIND.get(s.get("kind"), "EV7"),
              s.get("publisher"), s.get("title"), s.get("url"),
              2 if s.get("kind") in ("官网", "学术") else 1,
              CEL_BY_KIND.get(s.get("kind"), "E0"),
              json.dumps({"source_kind": s.get("kind"), "source_date": s.get("date"),
                          "fetch_http_status": (str(s["http_status"])
                                                if s.get("http_status") else None)},
                         ensure_ascii=False)))

    for i, e in enumerate(extra.get("education_extra") or [], 1):
        tags = e.get("school_tags") or []
        c.execute("""
            INSERT INTO education_record (education_id, person_id, degree_level, school_name,
                major_raw, major_code, overseas, is_clinical, confidence, verify_status,
                source_id, school_tags, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'V1', %s, %s, '{}'::jsonb)
        """, ("edu2_%s_%02d" % (key, i), pid, e.get("degree_level"),
              e.get("school_name"), e.get("major_raw"),
              map_major_code(c, e.get("major_raw")), e.get("overseas"),
              # 是否临床类专业：只有专业码能说明，映射不上就留空而不是猜
              None, e.get("confidence", 0.8), SRC_ID, tags or None))
        n["education"] += 1

    for i, w in enumerate(extra.get("employment_extra") or [], 1):
        c.execute("""
            INSERT INTO employment_record (employment_id, person_id, employer_name,
                employer_type, title_raw, confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, 'V1', %s, '{}'::jsonb)
        """, ("emp2_%s_%02d" % (key, i), pid, w.get("employer_name"),
              w.get("employer_type"), w.get("title_raw"), w.get("confidence", 0.8),
              SRC_ID))
        n["employment"] += 1
        # 任职城市：公开信息里有，但 employment_record 没有城市列。
        # **不塞进 attrs 假装存下了** —— 如实记成"缺字段"，下次做字段设计时看得见。
        if w.get("city"):
            unmapped.append("任职城市无处落库（employment_record 无城市列）：%s %s"
                            % (w.get("employer_name"), w.get("city")))

    for i, ce in enumerate(extra.get("clinical_extra") or [], 1):
        c.execute("""
            INSERT INTO clinical_exposure (exposure_id, person_id, department,
                department_code, duration_months, confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, 'V1', %s, '{}'::jsonb)
        """, ("clx2_%s_%02d" % (key, i), pid, ce.get("department_raw"),
              ce.get("department_code"), ce.get("duration_months"),
              ce.get("confidence", 0.6), SRC_ID))
        n["clinical"] += 1
        if ce.get("case_volume_note"):
            unmapped.append("临床例数说明无处落库（clinical_exposure 无该列）：%s"
                            % str(ce["case_volume_note"])[:30])

    for i, o in enumerate(extra.get("outputs_extra") or [], 1):
        otype = map_output_type(c, o.get("output_type"))
        if not otype:
            unmapped.append("产出类型未归一化：%s（%s）" % (o.get("output_type"), o.get("title")))
            continue
        c.execute("""
            INSERT INTO research_output (output_id, person_id, output_type, title, venue,
                year, confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'V1', %s, %s::jsonb)
        """, ("out2_%s_%02d" % (key, i), pid, otype, o.get("title"), o.get("venue"),
              o.get("year"), o.get("confidence", 0.8), SRC_ID,
              json.dumps({"output_type_raw": o.get("output_type")}, ensure_ascii=False)))
        n["output"] += 1

    # 偏好：按维度注册表找到该 pref_type 的码表，把中文语句映射成码。
    # 一句话常含多个方向（"写作与文学、投资…"）→ 映射出几个码就写几行；
    # 组装层是按行收集 codes 的，多行正好对应多值维度。
    ct_of = {r["pf"]: r["ct"] for r in c.execute("""
        SELECT split_part(person_locator, ':', 2) AS pf, code_table_id AS ct
          FROM mt.dimension WHERE person_locator LIKE 'preference:%'""")}
    for i, p in enumerate(extra.get("preferences_extra") or [], 1):
        pf = p.get("pref_type")
        raw = p.get("value_raw") or ""
        ct = ct_of.get(pf)
        codes, _un = V.text_to_codes(c, ct, [raw]) if ct else ([], [])
        quote = json.dumps({"pref_evidence_quote": p.get("evidence_quote")},
                           ensure_ascii=False)
        if not codes:
            c.execute("""
                INSERT INTO preference (preference_id, person_id, pref_type, value_raw,
                    confidence, attrs)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """, ("prf2_%s_%02d" % (key, i), pid, pf, raw, p.get("confidence", 0.7), quote))
            unmapped.append("%s 未归一化到码表：%s" % (pf, raw[:28]))
            n["preference"] += 1
            continue
        for j, code in enumerate(codes, 1):
            c.execute("""
                INSERT INTO preference (preference_id, person_id, pref_type, value_raw,
                    value_code, confidence, attrs)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """, ("prf2_%s_%02d_%02d" % (key, i, j), pid, pf, raw, code,
                  p.get("confidence", 0.7), quote))
            n["preference"] += 1

    # 奖项：level 也要落码（CT_AWARD_LEVEL），映射不上就留空并记 gaps
    for i, aw in enumerate(extra.get("awards_extra") or [], 1):
        lv_codes, _u = V.text_to_codes(c, "CT_AWARD_LEVEL", [str(aw.get("level") or "")])
        if not lv_codes:
            unmapped.append("奖项层级未归一化：%s（%s）" % (aw.get("level"), aw.get("name")))
        c.execute("""
            INSERT INTO award_honor (award_id, person_id, name, level, year, confidence,
                verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, 'V1', %s, '{}'::jsonb)
        """, ("awd2_%s_%02d" % (key, i), pid, aw.get("name"),
              (lv_codes or [None])[0], aw.get("year"), aw.get("confidence", 0.8), SRC_ID))
        n["award"] += 1

    # 语言能力：**有公开描述，但我们没有字段放**（人侧落点是 credential:C09，
    # credential 表却没有等级列）。"数据有、字段没有"必须显式记下来，
    # 否则下次还会有人以为"没查到"。
    for lg in extra.get("language_extra") or []:
        unmapped.append("语言能力无处落库（缺字段）：%s" % str(lg.get("value_raw"))[:36])

    gaps = list(extra.get("still_unknown") or [])
    if unmapped or gaps:
        # 复用第一轮已经登记过的两个键（public_case_unmapped / public_case_gaps），
        # **追加而不是新增**：语义完全相同，再加两个键只会让属性表膨胀一倍
        # （而且 attr_key 是主键，还得为它们各写一条登记）。
        cur = c.execute("""SELECT attrs->>'public_case_unmapped' AS u,
                                  attrs->>'public_case_gaps' AS g
                             FROM mt.person WHERE person_id = %s""", (pid,)).fetchone()
        merged_u = "；".join(x for x in [(cur or {}).get("u"), "；".join(unmapped)] if x)
        merged_g = "；".join(x for x in [(cur or {}).get("g"), "；".join(gaps)] if x)
        c.execute("""UPDATE mt.person
                        SET attrs = attrs || jsonb_build_object(
                              'public_case_unmapped', %s::text, 'public_case_gaps', %s::text)
                      WHERE person_id = %s""", (merged_u or None, merged_g or None, pid))
    return n, unmapped


def load_extra():
    """第二轮检索的增量事实。没有这个文件也能跑（第一轮的数据仍然完整）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "real_cases_extra.json")
    if not os.path.isfile(path):
        return {}
    with io.open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {x["case_key"]: x for x in (data.get("cases") or [])}


def evidence_id(pk, i):
    return "evd_%s_%02d" % (pk, i)


# ---------------------------------------------------------------------------
# 日期精度：真实公开资料的日期只到年或月
# ---------------------------------------------------------------------------
# 公开报道几乎不会给出"某年某月某日"。如果直接塞进 DATE 列，就必须自己补一个
# `-01-01` —— 那是在**造事实**：把"1993 年入学"变成"1993 年 1 月 1 日入学"。
# 但整条丢掉又损失了真实信息（年份是真的）。
# 所以：补到当月/当年 1 号让它**可排序可算区间**，同时把精度写进 attrs，
# 让任何读它的人一眼看到"日不是事实"。三条精度档与来源的粒度一一对应。
def norm_date(v):
    if not v:
        return None, None
    s = str(v).strip()
    if len(s) == 4 and s.isdigit():
        return s + "-01-01", "year"
    if len(s) == 7 and s[4] == "-":
        return s + "-01", "month"
    if len(s) == 10:
        return s, "day"
    return None, None


def map_output_type(c, raw, cache={}):
    """产出类型中文 → CT_RESEARCH_OUTPUT_TYPE 码。

    真实案例里的"著作/译作"在原词表里**没有对应码**（词表只有学术产出）。
    没有就补码（RO10/RO11 已按扩展机制补进 code_table_seed.csv），
    而不是把它硬塞进"原创论著"—— 那是把数据改得能放下，不是把库改得能装下。
    """
    if not raw:
        return None
    if not cache:
        for r in c.execute("SELECT code, label_zh FROM mt.code_value "
                           "WHERE code_table_id='CT_RESEARCH_OUTPUT_TYPE'"):
            cache[r["label_zh"]] = r["code"]
    s = str(raw)
    alias = {"著作": "著作（含译作）", "译作": "著作（含译作）", "图书": "著作（含译作）",
             "科普": "科普与公众传播作品", "专栏": "科普与公众传播作品",
             "论著": "原创论著", "论文": "原创论著", "综述": "综述"}
    want = alias.get(s, s)
    for lab, code in cache.items():
        if lab == want or lab in s or s in lab:
            return code
    return None


def map_major_code(c, raw, cache={}):
    """专业中文 → CT_MAJOR 码，用与岗位侧**同一套**保守包含匹配。

    为什么必须在人侧也做一次：岗位侧的专业要求写在自由文本里（RT1 raw_text），
    要靠码表标签匹配才能变成码；如果人侧只有自由文本、不去匹配，
    那么 `DIM_MAJOR`（硬门槛，权重 0.9）在真实案例上永远是人侧无值 →
    整个人的专业维度都进不了匹配。同一套归一化用在两侧，才有可比性。
    """
    if not raw:
        return None
    if not cache:
        for r in c.execute("SELECT code, label_zh FROM mt.code_value "
                           "WHERE code_table_id='CT_MAJOR' ORDER BY length(label_zh) DESC"):
            cache[r["label_zh"]] = r["code"]
    rows = c.execute("""SELECT code FROM mt.code_value WHERE code_table_id='CT_MAJOR'
                        AND %s LIKE '%%' || label_zh || '%%'
                        ORDER BY length(label_zh) DESC LIMIT 1""", (str(raw),)).fetchone()
    return rows["code"] if rows else None


def insert_case(c, case, dims=None):
    key = case["case_key"]
    pid = PREFIX + key
    reset_case(c, pid)
    srcs = case.get("sources") or []

    gaps = case.get("unavailable") or []
    unc = case.get("uncertainties") or []
    attrs = {
        "public_case_label": case.get("public_name"),
        "public_case_gaps": "；".join(gaps + unc) or None,
        "public_case_sources": "；".join(s.get("url", "") for s in srcs) or None,
    }
    attrs = {k: v for k, v in attrs.items() if v}

    # 顺序有讲究：**先写 person 再写 evidence**。
    # evidence.person_id 有外键指向 person，先写来源会被数据库直接拒绝
    # （实测：`Key (person_id)=(per_real_fengtang) is not present in table "person"`）。
    # 这不是可以靠"反正最后一致"绕过的：外键检查是逐语句的。
    c.execute("""
        INSERT INTO person (person_id, subject_code, schema_version, status,
                            enroll_channel, access_tier, source_id, ingest_run_id,
                            confidence, verify_status, quality_flags, attrs)
        VALUES (%s, %s, '1.0.0', 'active', 'EC4', 'T2', %s, 'run_public_profile',
                0.7, 'V1', %s, %s::jsonb)
        ON CONFLICT (person_id) DO UPDATE
           SET quality_flags = EXCLUDED.quality_flags, attrs = EXCLUDED.attrs,
               access_tier = EXCLUDED.access_tier, source_id = EXCLUDED.source_id,
               updated_at = now()
    """, (pid, "MT-REAL-" + key.upper()[:8], SRC_ID, TAGS, json.dumps(attrs, ensure_ascii=False)))

    # 每条公开来源 → 一行 evidence（可逐条追溯）
    ev_ids = []
    for i, s in enumerate(srcs, 1):
        eid = evidence_id(key, i)
        ev_ids.append(eid)
        c.execute("""
            INSERT INTO evidence (evidence_id, person_id, evidence_type, source_party,
                                  title, uri, verifiability, cel_level, access_tier, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'T1', %s::jsonb)
        """, (eid, pid, EV_TYPE_BY_KIND.get(s.get("kind"), "EV7"),
              s.get("publisher"), s.get("title"), s.get("url"),
              2 if s.get("kind") in ("官网", "学术") else 1,
              CEL_BY_KIND.get(s.get("kind"), "E0"),
              json.dumps({"source_kind": s.get("kind"),
                          "source_date": s.get("date"),
                          "fetch_http_status": (str(s["http_status"])
                                                if s.get("http_status") else None)},
                         ensure_ascii=False)))

    if case.get("birth_year") or case.get("sex"):
        c.execute("""
            INSERT INTO person_demographics (person_id, birth_year, sex, attrs)
            VALUES (%s, %s, %s, '{}'::jsonb)
            ON CONFLICT (person_id) DO UPDATE
               SET birth_year = EXCLUDED.birth_year, sex = EXCLUDED.sex
        """, (pid, case.get("birth_year"), case.get("sex")))
    # 注：base_city 是"现居/工作城市"，不是户籍。库里 hukou_province 存的是城市名，
    # 但没有"现居地"列 —— 这一点记进 gaps，不硬塞到户籍列里假装是户籍。

    n = {"education": 0, "credential": 0, "employment": 0, "clinical": 0, "output": 0}
    prec = {}          # 哪些字段的日期只到年/月 —— 写进 attrs，让精度可见
    unmapped = []      # 词表里装不下的原始值。**不静默丢弃**，要么补码要么报出来
    for i, e in enumerate(case.get("education") or [], 1):
        sd, sp = norm_date(e.get("start_date"))
        ed, ep = norm_date(e.get("end_date"))
        if sp:
            prec["education[%d].start_date" % i] = sp
        if ep:
            prec["education[%d].end_date" % i] = ep
        major_code = e.get("major_code") or map_major_code(c, e.get("major_raw"))
        if not major_code and e.get("major_raw"):
            unmapped.append("专业未归一化：%s" % e["major_raw"])
        c.execute("""
            INSERT INTO education_record (education_id, person_id, degree_level, degree_name,
                school_name, major_raw, major_code, is_clinical, start_date, end_date,
                is_graduated, overseas, confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'V1', %s, %s::jsonb)
        """, ("edu_%s_%02d" % (key, i), pid, e.get("degree_level"), e.get("degree_name"),
              e.get("school_name"), e.get("major_raw"), major_code,
              e.get("is_clinical"), sd, ed,
              e.get("is_graduated"), e.get("overseas"),
              e.get("confidence", 0.8), SRC_ID,
              json.dumps({"edu_date_precision": {"start": sp, "end": ep},
                          "edu_major_code_source": "registry" if e.get("major_code")
                          else ("label_match" if major_code else None)},
                         ensure_ascii=False)))
        n["education"] += 1
    for i, x in enumerate(case.get("credentials") or [], 1):
        c.execute("""
            INSERT INTO credential (credential_id, person_id, credential_type, name,
                issuing_body, obtained_date, status, confidence, verify_status,
                evidence_id, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, 'V1', %s, %s, '{}'::jsonb)
        """, ("crd_%s_%02d" % (key, i), pid, x.get("credential_type"), x.get("name"),
              x.get("issuing_body"), x.get("obtained_date"), x.get("confidence", 0.7),
              ev_ids[0] if ev_ids else None, SRC_ID))
        n["credential"] += 1
    for i, w in enumerate(case.get("employment") or [], 1):
        sd, sp = norm_date(w.get("start_date"))
        ed, ep = norm_date(w.get("end_date"))
        if sp:
            prec["employment[%d].start_date" % i] = sp
        if ep:
            prec["employment[%d].end_date" % i] = ep
        c.execute("""
            INSERT INTO employment_record (employment_id, person_id, employer_name,
                employer_type, title_raw, start_date, end_date, is_current, duties,
                confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'V1', %s, %s::jsonb)
        """, ("emp_%s_%02d" % (key, i), pid, w.get("employer_name"), w.get("employer_type"),
              w.get("title_raw"), sd, ed, w.get("is_current"), w.get("duties") or None,
              w.get("confidence", 0.8), SRC_ID,
              json.dumps({"emp_date_precision": {"start": sp, "end": ep}},
                         ensure_ascii=False)))
        n["employment"] += 1
    for i, ce in enumerate(case.get("clinical_exposure") or [], 1):
        c.execute("""
            INSERT INTO clinical_exposure (exposure_id, person_id, department,
                department_code, duration_months, confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, 'V1', %s, '{}'::jsonb)
        """, ("clx_%s_%02d" % (key, i), pid, ce.get("department_raw"),
              ce.get("department_code"), ce.get("duration_months"),
              ce.get("confidence", 0.6), SRC_ID))
        n["clinical"] += 1
    for i, o in enumerate(case.get("research_outputs") or [], 1):
        otype = map_output_type(c, o.get("output_type"))
        if not otype:
            unmapped.append("产出类型未归一化：%s（%s）" % (o.get("output_type"), o.get("title")))
            continue
        c.execute("""
            INSERT INTO research_output (output_id, person_id, output_type, title, venue,
                year, author_position, confidence, verify_status, source_id, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'V1', %s, %s::jsonb)
        """, ("out_%s_%02d" % (key, i), pid, otype, o.get("title"),
              o.get("venue"), o.get("year"), o.get("author_position"),
              o.get("confidence", 0.8), SRC_ID,
              json.dumps({"output_type_raw": o.get("output_type")}, ensure_ascii=False)))
        n["output"] += 1
    for i, p in enumerate(case.get("public_preferences") or [], 1):
        c.execute("""
            INSERT INTO preference (preference_id, person_id, pref_type, value_raw,
                value_code, confidence, attrs)
            VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb)
        """, ("prf_%s_%02d" % (key, i), pid, p.get("pref_type"), p.get("value_raw"),
              p.get("value_code"), p.get("confidence", 0.5)))

    # 日期精度与未归一化的原始值落到 person.attrs，让人不必回查 JSON
    extra = {}
    if prec:
        extra["public_case_date_precision"] = prec
    if unmapped:
        extra["public_case_unmapped"] = "；".join(unmapped)
    if extra:
        c.execute("UPDATE mt.person SET attrs = attrs || %s::jsonb WHERE person_id=%s",
                  (json.dumps(extra, ensure_ascii=False), pid))
    return pid, n, prec, unmapped


# ---------------------------------------------------------------------------
def build_report(c, pids):
    """把三个人的画像向量与匹配结果打印出来 —— 报告页面的数据来源与它一致。"""
    import vector as V
    dims = V.load_dims(c)
    out = []
    for pid in pids:
        pv, prov = V.person_payloads(c, pid, dims)
        filled = sorted(pv.keys())
        misses = {k: v["reason"] for k, v in prov.items() if v["status"] != "ok"}
        rows = V.rank_jobs(c, pid, limit=5, dims=dims)
        out.append({"person_id": pid, "dims_filled": len(filled), "dims": filled,
                    "missing_reasons": misses, "top": rows})
    return out


def persist_matches(c, pid, run_id, pids_jobs=None):
    """把匹配结果落库。匹配是**结论**，结论必须能追溯到一次运行（match_run）。"""
    import vector as V
    dims = V.load_dims(c)
    rows = V.rank_jobs(c, pid, limit=50, dims=dims)
    for r in rows:
        summ, det, _, _ = V.score(c, pid, r["job_id"], dims)
        breakdown = {d["dimension_id"]: {"status": d["status"],
                                         "score": None if d["score"] is None else float(d["score"]),
                                         "weight": float(d["weight"]),
                                         "reason": d["reason"]}
                     for d in det if d["status"] != "skipped"}
        c.execute("""
            INSERT INTO match_result (match_id, match_run_id, person_id, target_type,
                target_id, score_total, score_breakdown, explanation, rank)
            VALUES (%s, %s, %s, 'job', %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (match_id) DO UPDATE
               SET score_total = EXCLUDED.score_total,
                   score_breakdown = EXCLUDED.score_breakdown,
                   explanation = EXCLUDED.explanation, rank = EXCLUDED.rank
        """, ("mr_%s_%s" % (pid, r["job_id"]), run_id, pid, r["job_id"],
              r["score"], json.dumps(breakdown, ensure_ascii=False),
              "可评维度 %d 个，未知 %d 个，门槛未过 %d 个，可评权重覆盖 %.0f%%"
              % (r["evaluable"], r["unknown"], r["gate_failed"],
                 100 * (r["coverage"] or 0)), r["rank"]))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--json", metavar="PATH", help="把体检+案例结果写成 JSON（供报告页/文档用）")
    a = ap.parse_args()

    data = load_cases()
    if data is None:
        return 2
    cases = data.get("cases") or []
    pids = [PREFIX + x["case_key"] for x in cases]

    with psycopg.connect(DSN, row_factory=dict_row) as c:
        if a.reset:
            for pid in pids:
                n = reset_case(c, pid)
                print("[reset] %s（清 %d 张子表）" % (pid, n))
            c.commit()
            return 0

        if a.report:
            for r in build_report(c, pids):
                print("\n=== %s：画像向量有值 %d / 50 维 ===" % (r["person_id"], r["dims_filled"]))
                print("  有值：" + "、".join(r["dims"]) if r["dims"] else "  （无）")
                print("  top5：")
                for t in r["top"]:
                    print("    #%-2d %-7s cov=%-5s 门槛未过=%-2s %s"
                          % (t["rank"], ("%.3f" % t["score"]) if t["score"] is not None else "NULL",
                             ("%.2f" % t["coverage"]) if t["coverage"] is not None else "NULL",
                             t["gate_failed"], t["title"]))
            return 0

        register_source(c)
        register_attrs(c)
        c.execute("""
            INSERT INTO match_run (match_run_id, algo_version, params, started_at, status)
            VALUES ('run_real_cases_015', 'vector/1.0.0+50dim', %s::jsonb, now(), 'running')
            ON CONFLICT (match_run_id) DO UPDATE SET started_at = now(), status = 'running'
        """, (json.dumps({"dims": 50, "note": "真实公开案例的匹配运行"}, ensure_ascii=False),))

        total = {}
        extras = load_extra()
        if extras:
            print("[i] 第二轮增量事实：%d 个 case（real_cases_extra.json）" % len(extras))
        for case in cases:
            pid, n, prec, unmapped = insert_case(c, case)
            n2, un2 = merge_extra(c, case, extras.get(case["case_key"]), pid)
            for k, v in n.items():
                total[k] = total.get(k, 0) + v
            for k, v in n2.items():
                total["r2_" + k] = total.get("r2_" + k, 0) + v
            print("[+] %-12s %s  基础(教育%d 工作%d 产出%d) "
                  "+ 增量(教育%d 工作%d 临床%d 产出%d 偏好%d 奖项%d 来源%d)"
                  % (case["case_key"], pid, n["education"], n["employment"], n["output"],
                     n2.get("education", 0), n2.get("employment", 0),
                     n2.get("clinical", 0), n2.get("output", 0),
                     n2.get("preference", 0), n2.get("award", 0), n2.get("evidence", 0)))
            if unmapped:
                print("    [!] 未归一化：%s" % "；".join(unmapped[:3]))
            if un2:
                print("    [!] 增量未归一化/无落点：%s" % "；".join(un2[:3]))
        c.commit()

        n_match = 0
        for case in cases:
            n_match += persist_matches(c, PREFIX + case["case_key"], "run_real_cases_015")
        c.execute("""UPDATE match_run SET status='ok', finished_at=now(),
                            person_count=%s, job_count=%s
                      WHERE match_run_id='run_real_cases_015'""",
                  (len(cases), n_match // max(len(cases), 1)))
        c.commit()
        print("[✓] 写入 %d 个真实案例；匹配结果 %d 行（run_real_cases_015）" % (len(cases), n_match))
        print("    子表合计：%s" % total)

        rep = build_report(c, pids)
        for r in rep:
            print("    %-24s 画像向量有值 %d/50 维，首位匹配 %s"
                  % (r["person_id"], r["dims_filled"],
                     r["top"][0]["title"] if r["top"] else "-"))
        if a.json:
            with io.open(a.json, "w", encoding="utf-8") as fh:
                json.dump({"cases": cases, "vectors": rep}, fh, ensure_ascii=False, indent=2)
            print("    [JSON] %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
