-- ===========================================================================
-- ops/fixtures/check_dimension_coverage.sql —— 人侧维度覆盖率核对（只读）
--
-- 本文件由 `python ops\fixtures\gen_talent.py --emit-coverage-sql` 生成，
-- 内容来自 gen_talent.py 的 COVERAGE_SQL（单一事实来源），**不要手改**。
-- 口径：分母 = mock 人才数（per_mock_%）；分子 = 该维度取到非空取值的人数。
--
-- 跑法：python ops\pg.py sql ops\fixtures\check_dimension_coverage.sql
-- ===========================================================================
\pset border 2
\pset pager off

\echo '=== 人侧维度覆盖率（mock 样本 / 分母见末行）==='
SELECT * FROM (
    SELECT 0 AS ord, 'DIM_DEGREE_LEVEL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND degree_level IS NOT NULL) AS persons
    UNION ALL
    SELECT 1 AS ord, 'DIM_MAJOR' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND major_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 2 AS ord, 'DIM_IS_CLINICAL_MAJOR' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND is_clinical IS NOT NULL) AS persons
    UNION ALL
    SELECT 3 AS ord, 'DIM_ACADEMIC_RANK' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND rank_percentile IS NOT NULL) AS persons
    UNION ALL
    SELECT 4 AS ord, 'DIM_SCHOOL_TIER' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND school_tags IS NOT NULL AND cardinality(school_tags) > 0) AS persons
    UNION ALL
    SELECT 5 AS ord, 'DIM_CREDENTIAL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.credential WHERE person_id LIKE 'per_mock_%' AND credential_type IS NOT NULL) AS persons
    UNION ALL
    SELECT 6 AS ord, 'DIM_CREDENTIAL_STATUS' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.credential WHERE person_id LIKE 'per_mock_%' AND status IS NOT NULL) AS persons
    UNION ALL
    SELECT 7 AS ord, 'DIM_TRAINING' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.training_record WHERE person_id LIKE 'per_mock_%' AND training_type IS NOT NULL) AS persons
    UNION ALL
    SELECT 8 AS ord, 'DIM_HEALTH_LIMIT' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.person_demographics WHERE person_id LIKE 'per_mock_%' AND health_limits IS NOT NULL) AS persons
    UNION ALL
    SELECT 9 AS ord, 'DIM_POLITICAL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.person_demographics WHERE person_id LIKE 'per_mock_%' AND political_status IS NOT NULL) AS persons
    UNION ALL
    SELECT 10 AS ord, 'DIM_AGE_BAND' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.person_demographics WHERE person_id LIKE 'per_mock_%' AND birth_year IS NOT NULL) AS persons
    UNION ALL
    SELECT 11 AS ord, 'DIM_SEX' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.person_demographics WHERE person_id LIKE 'per_mock_%' AND sex IS NOT NULL) AS persons
    UNION ALL
    SELECT 12 AS ord, 'DIM_WORK_YEARS' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.employment_record WHERE person_id LIKE 'per_mock_%' AND start_date IS NOT NULL) AS persons
    UNION ALL
    SELECT 13 AS ord, 'DIM_EMPLOYER_TYPE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.employment_record WHERE person_id LIKE 'per_mock_%' AND employer_type IS NOT NULL) AS persons
    UNION ALL
    SELECT 14 AS ord, 'DIM_CLINICAL_BAND' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.clinical_exposure WHERE person_id LIKE 'per_mock_%') AS persons
    UNION ALL
    SELECT 15 AS ord, 'DIM_CLINICAL_DEPARTMENT' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.clinical_exposure WHERE person_id LIKE 'per_mock_%' AND department_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 16 AS ord, 'DIM_CLINICAL_VOLUME' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.clinical_exposure WHERE person_id LIKE 'per_mock_%' AND procedure_count IS NOT NULL) AS persons
    UNION ALL
    SELECT 17 AS ord, 'DIM_PROJECT_TYPE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.project_record WHERE person_id LIKE 'per_mock_%' AND project_type IS NOT NULL) AS persons
    UNION ALL
    SELECT 18 AS ord, 'DIM_PROJECT_ROLE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.project_record WHERE person_id LIKE 'per_mock_%' AND role IS NOT NULL) AS persons
    UNION ALL
    SELECT 19 AS ord, 'DIM_JOB_ZONE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND degree_level IS NOT NULL) AS persons
    UNION ALL
    SELECT 20 AS ord, 'DIM_RESEARCH_LEVEL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.research_output WHERE person_id LIKE 'per_mock_%') AS persons
    UNION ALL
    SELECT 21 AS ord, 'DIM_RESEARCH_OUTPUT_TYPE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.research_output WHERE person_id LIKE 'per_mock_%' AND output_type IS NOT NULL) AS persons
    UNION ALL
    SELECT 22 AS ord, 'DIM_AWARD_LEVEL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.award_honor WHERE person_id LIKE 'per_mock_%' AND level IS NOT NULL) AS persons
    UNION ALL
    SELECT 23 AS ord, 'DIM_OVERSEAS' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.education_record WHERE person_id LIKE 'per_mock_%' AND overseas IS NOT NULL) AS persons
    UNION ALL
    SELECT 24 AS ord, 'DIM_CONCEPT_SKILL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%' AND concept_id LIKE 'CON-K1-%') AS persons
    UNION ALL
    SELECT 25 AS ord, 'DIM_CONCEPT_ABILITY' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%' AND concept_id LIKE 'CON-K3-%') AS persons
    UNION ALL
    SELECT 26 AS ord, 'DIM_SKILL_LEVEL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%' AND level IS NOT NULL) AS persons
    UNION ALL
    SELECT 27 AS ord, 'DIM_SKILL_BASIS' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%' AND level_basis IS NOT NULL) AS persons
    UNION ALL
    SELECT 28 AS ord, 'DIM_ASSESSMENT_DIMENSION' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.assessment WHERE person_id LIKE 'per_mock_%' AND dimension IS NOT NULL) AS persons
    UNION ALL
    SELECT 29 AS ord, 'DIM_LANGUAGE_LEVEL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.credential WHERE person_id LIKE 'per_mock_%' AND credential_type = 'C09') AS persons
    UNION ALL
    SELECT 30 AS ord, 'DIM_INTEREST_DOMAIN' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF9' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 31 AS ord, 'DIM_CAREER_GOAL' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF12' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 32 AS ord, 'DIM_WORK_STYLE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF10' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 33 AS ord, 'DIM_VALUE_ORIENT' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF11' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 34 AS ord, 'DIM_ORG_CULTURE_FIT' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF16' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 35 AS ord, 'DIM_EXPECT_CITY' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF1' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 36 AS ord, 'DIM_CITY_TIER' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF1' AND value_code IN (SELECT code FROM mt.code_value WHERE code_table_id = 'CT_CITY')) AS persons
    UNION ALL
    SELECT 37 AS ord, 'DIM_MOBILITY' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF15' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 38 AS ord, 'DIM_HUKOU_CITY' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.person_demographics WHERE person_id LIKE 'per_mock_%' AND hukou_province IS NOT NULL) AS persons
    UNION ALL
    SELECT 39 AS ord, 'DIM_HUKOU_TYPE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.person_demographics WHERE person_id LIKE 'per_mock_%' AND hukou_type IS NOT NULL) AS persons
    UNION ALL
    SELECT 40 AS ord, 'DIM_WORK_MODE' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF13' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 41 AS ord, 'DIM_SALARY_EXPECT' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF4' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 42 AS ord, 'DIM_SALARY_BAND' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF4' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 43 AS ord, 'DIM_WORK_INTENSITY' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF5' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 44 AS ord, 'DIM_SHIFT_WILLING' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF14' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 45 AS ord, 'DIM_GROWTH_PREF' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF7' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 46 AS ord, 'DIM_STABILITY_PREF' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF6' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 47 AS ord, 'DIM_JOB_FAMILY_PREF' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF3' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 48 AS ord, 'DIM_ACCEPT_CROSS_INDUSTRY' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.preference WHERE person_id LIKE 'per_mock_%' AND pref_type = 'PF8' AND value_code IS NOT NULL) AS persons
    UNION ALL
    SELECT 49 AS ord, 'DIM_EVIDENCE_STRENGTH' AS dimension_id, (SELECT count(DISTINCT person_id) AS n FROM mt.evidence WHERE person_id LIKE 'per_mock_%') AS persons
) t ORDER BY ord;

\echo '=== 分母：mock 人才数 ==='
SELECT count(*) AS mock_person FROM mt.person WHERE person_id LIKE 'per_mock_%';
