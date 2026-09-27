-- ============================================================================
-- 013 完整算例：真实的人 × 真实的岗位，走一遍 50 维加权匹配
--
--   人  per_mock_0001  （120 名人才库中的第 1 位；本科护理学、省重点、期望沈阳、
--                       岗位族偏好 F15、薪资期望 20000-30000 元/月、
--                       执业护士证 + 规培证、手术室与儿科病区临床经历）
--   岗  job_1f9802ad8ade8e90af45（广州 临床研究护士，F15 交叉与新兴，
--                       学历 D2、经验 1-3年、薪资 8675-14902 元/月）
--
-- 说明：本文件是**算例与验证**，不是 schema 的一部分，因此向量组装写成临时视图，
--       事务结束即回滚，不往库里写任何东西。
--       真实工程实现里这层组装应落在 code/ 下（本次交付不动 code/）。
-- ============================================================================
BEGIN;
SET search_path TO mt, public;
\pset pager off

-- ---------------------------------------------------------------------------
-- ① 人侧向量：每个维度一个载荷；**没有值的维度不放进 JSON**（空就是空）
-- ---------------------------------------------------------------------------
CREATE TEMP VIEW pv AS
WITH e AS (
  SELECT er.*, cv.sort_order AS degree_rank
    FROM mt.education_record er
    LEFT JOIN mt.code_value cv ON cv.code_table_id = 'CT_DEGREE_LEVEL' AND cv.code = er.degree_level
   WHERE er.person_id = 'per_mock_0001')
SELECT 'DIM_DEGREE_LEVEL' AS dimension_id,
       jsonb_build_object('code', (SELECT degree_level FROM e ORDER BY degree_rank DESC NULLS LAST LIMIT 1),
                          'rank', (SELECT degree_rank FROM e ORDER BY degree_rank DESC NULLS LAST LIMIT 1)) AS payload
UNION ALL
SELECT 'DIM_MAJOR',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT major_code) FROM e
                                              WHERE major_code IS NOT NULL), '{}'::text[]))
UNION ALL
SELECT 'DIM_IS_CLINICAL_MAJOR',
       jsonb_build_object('code', CASE WHEN (SELECT bool_or(is_clinical) FROM e) THEN 'Y' ELSE 'N' END)
UNION ALL
SELECT 'DIM_ACADEMIC_RANK',
       jsonb_build_object('rank', (SELECT cv.sort_order FROM mt.code_value cv
                                    WHERE cv.code_table_id = 'CT_RANK_BAND'
                                      AND cv.code = (SELECT CASE
                                            WHEN min(rank_percentile) <= 5  THEN 'RB1'
                                            WHEN min(rank_percentile) <= 10 THEN 'RB2'
                                            WHEN min(rank_percentile) <= 25 THEN 'RB3'
                                            WHEN min(rank_percentile) <= 50 THEN 'RB4'
                                            ELSE 'RB5' END FROM e WHERE rank_percentile IS NOT NULL)))
UNION ALL
SELECT 'DIM_SCHOOL_TIER', jsonb_build_object('code', x.code, 'rank', x.rank)
  FROM (SELECT m.code, m.sort_order AS rank
          FROM (SELECT CASE tg WHEN '双一流' THEN 'ST1' WHEN '985' THEN 'ST3' WHEN '211' THEN 'ST4'
                               WHEN '省重点' THEN 'ST5' ELSE NULL END AS code
                  FROM e, unnest(school_tags) tg) z
          JOIN mt.code_value m ON m.code_table_id = 'CT_SCHOOL_TIER' AND m.code = z.code
         ORDER BY m.sort_order LIMIT 1) x
UNION ALL
SELECT 'DIM_CREDENTIAL',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT credential_type)
                                               FROM mt.credential WHERE person_id = 'per_mock_0001'), '{}'::text[]))
UNION ALL
SELECT 'DIM_CREDENTIAL_STATUS', jsonb_build_object('code', m.code, 'rank', m.sort_order)
  FROM (SELECT cv.code, cv.sort_order FROM mt.credential c
          JOIN mt.code_value cv ON cv.code_table_id = 'CT_CRED_STATUS' AND cv.code = c.status
         WHERE c.person_id = 'per_mock_0001' ORDER BY cv.sort_order LIMIT 1) m
UNION ALL
SELECT 'DIM_TRAINING',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT t.code) FROM (
           SELECT CASE c.credential_type WHEN 'C02' THEN 'T01' WHEN 'C03' THEN 'T02' END AS code
             FROM mt.credential c WHERE c.person_id = 'per_mock_0001') t
          WHERE t.code IS NOT NULL), '{}'::text[]))
UNION ALL
SELECT 'DIM_HEALTH_LIMIT',
       jsonb_build_object('codes', COALESCE((SELECT health_limits FROM mt.person_demographics
                                              WHERE person_id = 'per_mock_0001'), '{}'::text[]))
UNION ALL
SELECT 'DIM_POLITICAL', jsonb_build_object('code', political_status)
  FROM mt.person_demographics WHERE person_id = 'per_mock_0001'
UNION ALL
SELECT 'DIM_AGE_BAND', jsonb_build_object('code', b.code, 'rank', m.sort_order)
  FROM (SELECT CASE
                 WHEN EXTRACT(YEAR FROM CURRENT_DATE)::int - birth_year < 25 THEN 'AG1'
                 WHEN EXTRACT(YEAR FROM CURRENT_DATE)::int - birth_year < 30 THEN 'AG2'
                 WHEN EXTRACT(YEAR FROM CURRENT_DATE)::int - birth_year < 35 THEN 'AG3'
                 WHEN EXTRACT(YEAR FROM CURRENT_DATE)::int - birth_year < 40 THEN 'AG4'
                 WHEN EXTRACT(YEAR FROM CURRENT_DATE)::int - birth_year < 45 THEN 'AG5'
                 ELSE 'AG6' END AS code
          FROM mt.person_demographics WHERE person_id = 'per_mock_0001' AND birth_year IS NOT NULL) b
  JOIN mt.code_value m ON m.code_table_id = 'CT_AGE_BAND' AND m.code = b.code
UNION ALL
SELECT 'DIM_SEX', jsonb_build_object('code', sex)
  FROM mt.person_demographics WHERE person_id = 'per_mock_0001'
UNION ALL
SELECT 'DIM_WORK_YEARS', jsonb_build_object('code', b.code, 'rank', m.sort_order)
  FROM (SELECT CASE
                 WHEN COALESCE(months, 0) = 0  THEN 'EX0'
                 WHEN months <= 12  THEN 'EX1'
                 WHEN months <= 36  THEN 'EX2'
                 WHEN months <= 60  THEN 'EX3'
                 WHEN months <= 120 THEN 'EX4'
                 ELSE 'EX5' END AS code
          FROM (SELECT SUM(GREATEST(0, (EXTRACT(YEAR FROM COALESCE(end_date, CURRENT_DATE))
                                        - EXTRACT(YEAR FROM start_date)) * 12
                                       + (EXTRACT(MONTH FROM COALESCE(end_date, CURRENT_DATE))
                                          - EXTRACT(MONTH FROM start_date)))) AS months
                  FROM mt.employment_record WHERE person_id = 'per_mock_0001') s) b
  JOIN mt.code_value m ON m.code_table_id = 'CT_EXPERIENCE_BAND' AND m.code = b.code
UNION ALL
SELECT 'DIM_EMPLOYER_TYPE',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT employer_type)
                                               FROM mt.employment_record
                                              WHERE person_id = 'per_mock_0001'
                                                AND employer_type IS NOT NULL), '{}'::text[]))
UNION ALL
SELECT 'DIM_CLINICAL_BAND', jsonb_build_object('code', b.code, 'rank', m.sort_order)
  FROM (SELECT CASE
                 WHEN EXISTS (SELECT 1 FROM mt.credential
                               WHERE person_id = 'per_mock_0001' AND credential_type = 'C03') THEN 'CB4'
                 WHEN EXISTS (SELECT 1 FROM mt.credential
                               WHERE person_id = 'per_mock_0001' AND credential_type = 'C02') THEN 'CB3'
                 WHEN EXISTS (SELECT 1 FROM mt.clinical_exposure
                               WHERE person_id = 'per_mock_0001') THEN 'CB1'
                 ELSE 'CB0' END AS code) b
  JOIN mt.code_value m ON m.code_table_id = 'CT_CLINICAL_BAND' AND m.code = b.code
UNION ALL
SELECT 'DIM_CLINICAL_DEPARTMENT',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT m.code)
          FROM (SELECT department AS d FROM mt.clinical_exposure WHERE person_id = 'per_mock_0001') z
          JOIN mt.code_value m ON m.code_table_id = 'CT_CLINICAL_DEPARTMENT'
                              AND m.label_zh = CASE z.d
                                    WHEN '儿科病区' THEN '儿科'
                                    WHEN '内科病区' THEN '内科'
                                    WHEN '外科病区' THEN '外科'
                                    WHEN '临床药学室' THEN '药学与临床药学'
                                    ELSE z.d END), '{}'::text[]))
UNION ALL
SELECT 'DIM_CLINICAL_VOLUME', jsonb_build_object('code', b.code, 'rank', m.sort_order)
  FROM (SELECT CASE
                 WHEN v IS NULL OR v = 0 THEN 'VB0'
                 WHEN v < 50   THEN 'VB1'
                 WHEN v < 200  THEN 'VB2'
                 WHEN v < 500  THEN 'VB3'
                 WHEN v < 1000 THEN 'VB4'
                 ELSE 'VB5' END AS code
          FROM (SELECT max(procedure_count) AS v FROM mt.clinical_exposure
                 WHERE person_id = 'per_mock_0001') s) b
  JOIN mt.code_value m ON m.code_table_id = 'CT_VOLUME_BAND' AND m.code = b.code
UNION ALL
SELECT 'DIM_OVERSEAS', jsonb_build_object('code', CASE WHEN EXISTS (
         SELECT 1 FROM mt.education_record WHERE person_id = 'per_mock_0001' AND overseas) THEN 'Y' ELSE 'N' END)
UNION ALL
SELECT 'DIM_CONCEPT_SKILL',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT sa.concept_id)
          FROM mt.skill_assertion sa JOIN mt.concept c ON c.concept_id = sa.concept_id
         WHERE sa.person_id = 'per_mock_0001' AND c.concept_type = 'K1'), '{}'::text[]))
UNION ALL
SELECT 'DIM_CONCEPT_ABILITY',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT sa.concept_id)
          FROM mt.skill_assertion sa JOIN mt.concept c ON c.concept_id = sa.concept_id
         WHERE sa.person_id = 'per_mock_0001' AND c.concept_type = 'K3'), '{}'::text[]))
UNION ALL
SELECT 'DIM_SKILL_LEVEL', jsonb_build_object('rank', max(sa.level))
  FROM mt.skill_assertion sa WHERE sa.person_id = 'per_mock_0001'
UNION ALL
SELECT * FROM (
  SELECT 'DIM_SKILL_BASIS' AS dimension_id,
         jsonb_build_object('code', level_basis) AS payload
    FROM mt.skill_assertion
   WHERE person_id = 'per_mock_0001' AND level_basis IS NOT NULL
   ORDER BY 1 LIMIT 1) sb
UNION ALL
SELECT 'DIM_ASSESSMENT_DIMENSION',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT cv.code)
          FROM mt.assessment a JOIN mt.code_value cv
            ON cv.code_table_id = 'CT_ASSESSMENT_DIMENSION' AND cv.label_zh = a.dimension
         WHERE a.person_id = 'per_mock_0001'), '{}'::text[]))
UNION ALL
SELECT 'DIM_EXPECT_CITY',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(value_raw) FROM mt.preference
                                              WHERE person_id = 'per_mock_0001' AND pref_type = 'PF1'), '{}'::text[]))
UNION ALL
SELECT 'DIM_HUKOU_CITY',
       jsonb_build_object('code', COALESCE((SELECT cv.code FROM mt.person_demographics d
          JOIN mt.code_value cv ON cv.code_table_id = 'CT_CITY' AND cv.label_zh = d.hukou_province
         WHERE d.person_id = 'per_mock_0001'), NULL),
                          'text', (SELECT hukou_province FROM mt.person_demographics
                                    WHERE person_id = 'per_mock_0001'))
UNION ALL
SELECT 'DIM_HUKOU_TYPE', jsonb_build_object('code', hukou_type)
  FROM mt.person_demographics WHERE person_id = 'per_mock_0001'
UNION ALL
SELECT 'DIM_SALARY_EXPECT', jsonb_build_object(
         'lo', split_part(split_part(value_code, ':', 2), '-', 1)::numeric,
         'hi', split_part(split_part(value_code, ':', 2), '-', 2)::numeric)
  FROM mt.preference WHERE person_id = 'per_mock_0001' AND pref_type = 'PF4' AND value_code IS NOT NULL
UNION ALL
SELECT 'DIM_WORK_INTENSITY', jsonb_build_object('code', b.code, 'rank', m.sort_order)
  FROM (SELECT CASE value_raw WHEN '希望规律作息' THEN 'WI1' WHEN '可接受高强度' THEN 'WI3'
                              WHEN '可接受夜班' THEN 'WI4' ELSE NULL END AS code
          FROM mt.preference WHERE person_id = 'per_mock_0001' AND pref_type = 'PF5') b
  JOIN mt.code_value m ON m.code_table_id = 'CT_WORK_INTENSITY' AND m.code = b.code
UNION ALL
SELECT 'DIM_JOB_FAMILY_PREF',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(value_code) FROM mt.preference
                                              WHERE person_id = 'per_mock_0001' AND pref_type = 'PF3'
                                                AND value_code IS NOT NULL), '{}'::text[]))
UNION ALL
SELECT 'DIM_ACCEPT_CROSS_INDUSTRY', jsonb_build_object('code', value_code)
  FROM mt.preference WHERE person_id = 'per_mock_0001' AND pref_type = 'PF8' AND value_code IS NOT NULL
UNION ALL
SELECT 'DIM_EVIDENCE_STRENGTH', jsonb_build_object('rank', 30);

-- ---------------------------------------------------------------------------
-- ② 岗位侧向量
-- ---------------------------------------------------------------------------
CREATE TEMP VIEW jv AS
SELECT 'DIM_DEGREE_LEVEL' AS dimension_id,
       jsonb_build_object('code', jp.education_req, 'rank', cv.sort_order, 'min_rank', cv.sort_order) AS payload
  FROM mt.job_posting jp
  LEFT JOIN mt.code_value cv ON cv.code_table_id = 'CT_DEGREE_LEVEL' AND cv.code = jp.education_req
 WHERE jp.job_id = 'job_1f9802ad8ade8e90af45'
UNION ALL
-- 专业要求只存在于 job_requirement.raw_text（major_req 列全空），这里做一次"文本→码"桥接
SELECT 'DIM_MAJOR',
       jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT m.code) FROM (
           SELECT m2.code FROM mt.job_requirement jr
             JOIN mt.code_value m2 ON m2.code_table_id = 'CT_MAJOR' AND m2.code ~ '^M[0-9]{5}$'
            WHERE jr.job_id = 'job_1f9802ad8ade8e90af45' AND jr.requirement_type = 'RT1'
              AND jr.raw_text LIKE '%' || m2.label_zh || '%') m), '{}'::text[]))
UNION ALL
SELECT 'DIM_IS_CLINICAL_MAJOR', jsonb_build_object('code', CASE WHEN EXISTS (
         SELECT 1 FROM mt.job_requirement jr
           JOIN mt.code_value m ON m.code_table_id = 'CT_MAJOR' AND m.code ~ '^M100[0-9]{2}$'
          WHERE jr.job_id = 'job_1f9802ad8ade8e90af45' AND jr.requirement_type = 'RT1'
            AND jr.raw_text LIKE '%' || m.label_zh || '%') THEN 'Y' ELSE 'N' END)
UNION ALL
-- 证照要求：job_posting.license_req 全空，只能从 RT4 原文里按关键词桥接
SELECT 'DIM_CREDENTIAL', jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT k.code)
         FROM (VALUES ('执业医师', 'C01'), ('规培', 'C02'), ('GCP', 'C06'),
                      ('执业药师', 'C04'), ('护士', 'C05')) k(kw, code)
        WHERE EXISTS (SELECT 1 FROM mt.job_requirement jr
                       WHERE jr.job_id = 'job_1f9802ad8ade8e90af45'
                         AND jr.requirement_type = 'RT4'
                         AND jr.raw_text LIKE '%' || k.kw || '%')), '{}'::text[]))
UNION ALL
SELECT 'DIM_TRAINING', jsonb_build_object('codes', COALESCE((SELECT array_agg(DISTINCT 'T01'::text)
         FROM mt.job_requirement jr
        WHERE jr.job_id = 'job_1f9802ad8ade8e90af45' AND jr.raw_text LIKE '%规培%'), '{}'::text[]))
UNION ALL
SELECT 'DIM_WORK_YEARS', jsonb_build_object('code', m.code, 'rank', m.sort_order, 'min_rank', m.sort_order)
  FROM (SELECT CASE jp.experience_req WHEN '应届' THEN 'EX0' WHEN '1-3年' THEN 'EX2'
                                      WHEN '3-5年' THEN 'EX3' WHEN '5-10年' THEN 'EX4'
                                      WHEN '不限' THEN 'EX9' ELSE NULL END AS code
          FROM mt.job_posting jp WHERE jp.job_id = 'job_1f9802ad8ade8e90af45') b
  JOIN mt.code_value m ON m.code_table_id = 'CT_EXPERIENCE_BAND' AND m.code = b.code
UNION ALL
SELECT 'DIM_EXPECT_CITY', jsonb_build_object('codes', ARRAY[jp.city])
  FROM mt.job_posting jp WHERE jp.job_id = 'job_1f9802ad8ade8e90af45' AND jp.city IS NOT NULL
UNION ALL
SELECT 'DIM_JOB_FAMILY_PREF', jsonb_build_object('codes', ARRAY[jp.job_family])
  FROM mt.job_posting jp WHERE jp.job_id = 'job_1f9802ad8ade8e90af45' AND jp.job_family IS NOT NULL
UNION ALL
SELECT 'DIM_CAREER_GOAL', jsonb_build_object('codes', ARRAY[jp.job_family])
  FROM mt.job_posting jp WHERE jp.job_id = 'job_1f9802ad8ade8e90af45' AND jp.job_family IS NOT NULL
UNION ALL
-- 兴趣方向与岗位族的对应写在 CT_INTEREST_DOMAIN.external_mapping 里
SELECT 'DIM_INTEREST_DOMAIN', jsonb_build_object('codes', COALESCE((
         SELECT array_agg(DISTINCT cv.code) FROM mt.job_posting jp
           JOIN mt.code_value cv ON cv.code_table_id = 'CT_INTEREST_DOMAIN'
                                AND cv.external_mapping -> 'job_family' ? jp.job_family
          WHERE jp.job_id = 'job_1f9802ad8ade8e90af45'), '{}'::text[]))
UNION ALL
SELECT 'DIM_SALARY_EXPECT', jsonb_build_object('lo', jp.salary_min, 'hi', jp.salary_max)
  FROM mt.job_posting jp
 WHERE jp.job_id = 'job_1f9802ad8ade8e90af45' AND jp.salary_min IS NOT NULL AND jp.salary_max IS NOT NULL
UNION ALL
SELECT 'DIM_CONCEPT_SKILL', jsonb_build_object('codes', COALESCE((
         SELECT array_agg(DISTINCT jr.concept_id)
           FROM mt.job_requirement jr JOIN mt.concept c ON c.concept_id = jr.concept_id
          WHERE jr.job_id = 'job_1f9802ad8ade8e90af45' AND c.concept_type = 'K1'), '{}'::text[]))
UNION ALL
SELECT 'DIM_CONCEPT_ABILITY', jsonb_build_object('codes', COALESCE((
         SELECT array_agg(DISTINCT jr.concept_id)
           FROM mt.job_requirement jr JOIN mt.concept c ON c.concept_id = jr.concept_id
          WHERE jr.job_id = 'job_1f9802ad8ade8e90af45' AND c.concept_type = 'K3'), '{}'::text[]))
UNION ALL
-- 只有"完全跨行"岗位才提出跨行要求
SELECT 'DIM_ACCEPT_CROSS_INDUSTRY', jsonb_build_object('code', 'Y')
  FROM mt.job_posting jp
 WHERE jp.job_id = 'job_1f9802ad8ade8e90af45' AND jp.job_family = 'F16';

CREATE TEMP VIEW pvec AS
  SELECT COALESCE(jsonb_object_agg(dimension_id, payload), '{}'::jsonb) AS v FROM pv;
CREATE TEMP VIEW jvec AS
  SELECT COALESCE(jsonb_object_agg(dimension_id, payload), '{}'::jsonb) AS v FROM jv;

\echo === ③ 人侧向量（只含真实有值的维度） ===
SELECT jsonb_pretty(v) FROM pvec;
\echo === ④ 岗位侧向量 ===
SELECT jsonb_pretty(v) FROM jvec;

\echo === ⑤ 逐维度拆解（去掉 unknown 维度看可解释部分） ===
SELECT s.group_id, s.dimension_id, s.title_zh, s.role, s.weight::text, s.comparator,
       s.status, round(s.score, 3)::text AS score, s.contribution::text, s.reason
  FROM pvec p, jvec j, LATERAL mt.score_dimensions(p.v, j.v) s
 WHERE s.status <> 'unknown'
 ORDER BY s.group_id, s.dimension_id;

\echo === ⑥ 未知（空就是空）的维度：这些**不算 0 分**，只是没有信息 ===
SELECT s.group_id, s.dimension_id, s.title_zh, s.reason
  FROM pvec p, jvec j, LATERAL mt.score_dimensions(p.v, j.v) s
 WHERE s.status = 'unknown' ORDER BY s.group_id, s.dimension_id;

\echo === ⑦ 汇总：门禁 + 加权归一总分 + 覆盖率 ===
SELECT s.blocked_by, s.gate_failed, s.evaluable, s.unknown_dimensions,
       s.score_total, s.coverage
  FROM pvec p, jvec j, LATERAL mt.score_summary(p.v, j.v) s;

ROLLBACK;
