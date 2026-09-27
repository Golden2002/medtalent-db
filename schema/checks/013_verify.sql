-- ============================================================================
-- 013 验收核对（只读，可重复执行）
-- 用法：python ops\pg.py sql schema\checks\013_verify.sql
-- ============================================================================
\pset pager off
SET search_path TO mt, public;

\echo === 1. dimension 行数（期望 50） ===
SELECT count(*) AS dimension_rows FROM mt.dimension;

\echo === 2. field_catalog.allow_custom / custom_hint 列是否存在 ===
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = 'mt' AND table_name = 'field_catalog'
   AND column_name IN ('allow_custom', 'custom_hint')
 ORDER BY column_name;

\echo === 3. 维度按角色与类型分布 ===
SELECT role, is_hard, count(*) AS n, round(sum(weight), 2) AS weight_sum
  FROM mt.dimension GROUP BY 1, 2 ORDER BY 1, 2;
SELECT kind, comparator, count(*) AS n FROM mt.dimension GROUP BY 1, 2 ORDER BY 1;

\echo === 4. 词表总数与选项总数（精确 count(*)） ===
SELECT (SELECT count(*) FROM mt.code_table)  AS code_tables,
       (SELECT count(*) FROM mt.code_value)  AS code_values,
       (SELECT count(*) FROM mt.field_catalog) AS fields;

\echo === 5. 本次新增的 17 张词表的选项数 ===
SELECT code_table_id, count(*) AS options
  FROM mt.code_value
 WHERE code_table_id IN ('CT_INTEREST_DOMAIN','CT_WORK_STYLE','CT_VALUE_ORIENT','CT_YES_NO',
                         'CT_AGE_BAND','CT_RANK_BAND','CT_PROJECT_TYPE','CT_PROJECT_ROLE',
                         'CT_AWARD_LEVEL','CT_CLINICAL_DEPARTMENT','CT_ASSESSMENT_DIMENSION',
                         'CT_CITY','CT_VOLUME_BAND','CT_WORK_INTENSITY','CT_GROWTH_PREF',
                         'CT_STABILITY_PREF','CT_EVIDENCE_STRENGTH')
 GROUP BY 1 ORDER BY 1;

\echo === 6. CT_PREFERENCE_TYPE 追加情况（期望 16 个，PF1-PF8 原样 + PF9-PF16） ===
SELECT count(*) AS preference_types,
       count(*) FILTER (WHERE code IN ('PF9','PF10','PF11','PF12','PF13','PF14','PF15','PF16')) AS added
  FROM mt.code_value WHERE code_table_id = 'CT_PREFERENCE_TYPE';

\echo === 7. 依赖兜底（期望 0 行） ===
SELECT * FROM mt.v_dimension_dependency_gap;

\echo === 8. allow_custom 的字段数（期望：维度注册表里 allow_custom=true 且登记了字段的那些） ===
SELECT count(*) FILTER (WHERE allow_custom) AS custom_ok,
       count(*) FILTER (WHERE NOT allow_custom) AS custom_off
  FROM mt.field_catalog;

\echo === 9. 自写路径的候选池策略 ===
SELECT policy_id, candidate_kind, min_evidence, auto_promote FROM mt.evolution_policy ORDER BY 1;

\echo === 10. 比较函数是否存在 ===
SELECT p.proname, pg_get_function_arguments(p.oid) AS args
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mt'
   AND p.proname IN ('cmp_enum','cmp_set','cmp_ordinal','cmp_range','cmp_dimension',
                     'score_dimensions','score_summary','submit_option_candidate',
                     'promote_option_candidate','normalize_option_text','dimension_coverage')
 ORDER BY p.proname;

\echo === 11. 四种比较函数的真实返回值自测 ===
SELECT mt.cmp_enum('D3', ARRAY['D2','D3'])       AS enum_hit,
       mt.cmp_enum('D1', ARRAY['D2','D3'])       AS enum_miss,
       mt.cmp_enum(NULL, ARRAY['D2'])            AS enum_unknown,
       mt.cmp_ordinal(30, 20)                    AS ord_ok,
       mt.cmp_ordinal(20, 30)                    AS ord_one_short,
       mt.cmp_ordinal(10, 30)                    AS ord_far,
       mt.cmp_set(ARRAY['F01','F02'], ARRAY['F01','F02','F03']) AS set_cover,
       mt.cmp_range(10000, 15000, 9000, 13000)   AS range_partial,
       mt.cmp_range(10000, 15000, 20000, 30000)  AS range_above,
       mt.cmp_range(NULL, 15000, 9000, 13000)    AS range_unknown;

\echo === 12. 两侧真实覆盖率：locator 翻译成精确 count(*)（表.列 / preference:PFx / concept:Kx） ===
SELECT dimension_id, side, relation, column_name, non_null, total,
       round(100.0 * non_null / NULLIF(total, 0), 1) AS pct, note
  FROM mt.dimension_coverage() ORDER BY dimension_id, side;

\echo === 12b. 两侧都有真实取值的维度（可参与匹配的维度） ===
WITH c AS (
  SELECT dimension_id,
         bool_or(side = 'person' AND non_null > 0) AS person_ok,
         bool_or(side = 'job'    AND non_null > 0) AS job_ok
    FROM mt.dimension_coverage() GROUP BY 1)
SELECT count(*) FILTER (WHERE person_ok AND job_ok) AS two_sided,
       count(*) FILTER (WHERE person_ok AND NOT job_ok) AS person_only,
       count(*) FILTER (WHERE NOT person_ok AND job_ok) AS job_only,
       (SELECT count(*) FROM mt.dimension WHERE status = 'active') AS registered
  FROM c;

\echo === 12c. 两侧都有值的维度清单 ===
WITH c AS (
  SELECT dimension_id,
         bool_or(side = 'person' AND non_null > 0) AS person_ok,
         bool_or(side = 'job'    AND non_null > 0) AS job_ok
    FROM mt.dimension_coverage() GROUP BY 1)
SELECT d.dimension_id, d.title_zh, d.kind, d.role, d.weight
  FROM mt.dimension d JOIN c ON c.dimension_id = d.dimension_id
 WHERE c.person_ok AND c.job_ok ORDER BY d.sort_order;

\echo === 12d. 全部 50 个维度的可测性分桶（可复现 §6.1 的口径） ===
WITH c AS (
  SELECT dimension_id,
         bool_or(side = 'person' AND non_null > 0) AS p,
         bool_or(side = 'job'    AND non_null > 0) AS j,
         count(*) FILTER (WHERE non_null IS NOT NULL) AS measured
    FROM mt.dimension_coverage() GROUP BY 1)
SELECT CASE WHEN COALESCE(c.p,false) AND COALESCE(c.j,false) THEN 'A 双侧都有可测值'
            WHEN COALESCE(c.p,false) THEN 'C 仅人侧'
            WHEN COALESCE(c.j,false) THEN 'D 仅岗侧'
            WHEN COALESCE(c.measured,0) = 0 THEN 'E 两侧都无法测'
            ELSE 'E2 定位符测不到值' END AS bucket,
       count(*) AS n,
       string_agg(d.dimension_id, ', ' ORDER BY d.dimension_id) AS dimensions
  FROM mt.dimension d LEFT JOIN c ON c.dimension_id = d.dimension_id
 GROUP BY 1 ORDER BY 1;

\echo === 12e. 声明允许自写的维度数与对应字段数 ===
SELECT (SELECT count(*) FROM mt.dimension WHERE allow_custom) AS self_writable_dims,
       (SELECT count(DISTINCT person_field_id) FROM mt.dimension
         WHERE allow_custom AND person_field_id IS NOT NULL) AS self_writable_fields,
       (SELECT count(*) FROM mt.field_catalog WHERE allow_custom) AS fields_flagged;

\echo === 13. 已确认的岗位侧硬缺口（列存在但全空 / 列不存在） ===
SELECT 'job_posting.province' AS col, count(province) AS non_null, count(*) AS total FROM mt.job_posting
UNION ALL SELECT 'job_posting.major_req', count(major_req), count(*) FROM mt.job_posting
UNION ALL SELECT 'job_posting.license_req', count(license_req), count(*) FROM mt.job_posting
UNION ALL SELECT 'job_posting.employment_type', count(employment_type), count(*) FROM mt.job_posting
UNION ALL SELECT 'job_posting.job_zone', count(job_zone), count(*) FROM mt.job_posting
UNION ALL SELECT 'job_posting.is_campus', count(is_campus), count(*) FROM mt.job_posting
UNION ALL SELECT 'job_posting.experience_req', count(experience_req), count(*) FROM mt.job_posting
UNION ALL SELECT 'job_requirement.min_level', count(min_level), count(*) FROM mt.job_requirement
UNION ALL SELECT 'clinical_exposure.department_code', count(department_code), count(*) FROM mt.clinical_exposure
UNION ALL SELECT 'preference.PF1.value_code', count(value_code), count(*) FROM mt.preference WHERE pref_type='PF1'
UNION ALL SELECT 'preference.PF2.rows', count(*), 120 FROM mt.preference WHERE pref_type='PF2'
UNION ALL SELECT 'preference.PF6.rows', count(*), 120 FROM mt.preference WHERE pref_type='PF6'
UNION ALL SELECT 'preference.PF9-PF16.rows', count(*), 0 FROM mt.preference
   WHERE pref_type IN ('PF9','PF10','PF11','PF12','PF13','PF14','PF15','PF16');

\echo === 13b. school_tags 现状：存的是中文标签而不是码值 ===
SELECT school_tags, count(*) FROM mt.education_record GROUP BY 1 ORDER BY 2 DESC;

\echo === 14. job_posting.experience_min_years / employer_type 列是否存在（期望 0 行） ===
SELECT column_name FROM information_schema.columns
 WHERE table_schema='mt' AND table_name='job_posting'
   AND column_name IN ('experience_min_years','employer_type');
