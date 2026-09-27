-- ============================================================================
-- 013 可重放性指纹：把 013 建立/修改的全部对象压成一个 md5
-- 用法：python ops\pg.py sql schema\checks\013_fingerprint.sql
-- 连续两次 apply 之后该指纹必须完全相同。
-- ============================================================================
\pset pager off
SET search_path TO mt, public;

SELECT md5(string_agg(x, '|' ORDER BY x)) AS state_fingerprint
FROM (
  -- 维度注册表全部列
  SELECT 'DIM|' || dimension_id || '|' || group_id || '|' || title_zh || '|' || kind || '|' ||
         comparator || '|' || COALESCE(code_table_id, '-') || '|' || allow_custom::text || '|' ||
         is_hard::text || '|' || role || '|' || weight::text || '|' ||
         COALESCE(person_locator, '-') || '|' || COALESCE(person_field_id, '-') || '|' ||
         COALESCE(job_locator, '-') || '|' || sort_order::text || '|' || status AS x
    FROM mt.dimension
  UNION ALL
  -- 013 新增/追加的全部码值
  SELECT 'CV|' || code_table_id || '|' || code || '|' || label_zh || '|' ||
         COALESCE(label_en, '-') || '|' || COALESCE(parent_code, '-') || '|' ||
         level::text || '|' || sort_order::text || '|' || COALESCE(definition, '-') || '|' ||
         external_mapping::text
    FROM mt.code_value
   WHERE code_table_id IN ('CT_INTEREST_DOMAIN', 'CT_WORK_STYLE', 'CT_VALUE_ORIENT', 'CT_YES_NO',
                           'CT_AGE_BAND', 'CT_RANK_BAND', 'CT_PROJECT_TYPE', 'CT_PROJECT_ROLE',
                           'CT_AWARD_LEVEL', 'CT_CLINICAL_DEPARTMENT', 'CT_ASSESSMENT_DIMENSION',
                           'CT_CITY', 'CT_VOLUME_BAND', 'CT_WORK_INTENSITY', 'CT_GROWTH_PREF',
                           'CT_STABILITY_PREF', 'CT_EVIDENCE_STRENGTH')
      OR (code_table_id = 'CT_PREFERENCE_TYPE' AND code >= 'PF9')
  UNION ALL
  -- 新词表的元数据
  SELECT 'CT|' || code_table_id || '|' || name || '|' || description || '|' ||
         hierarchical::text || '|' || status
    FROM mt.code_table
   WHERE code_table_id IN ('CT_INTEREST_DOMAIN', 'CT_WORK_STYLE', 'CT_VALUE_ORIENT', 'CT_YES_NO',
                           'CT_AGE_BAND', 'CT_RANK_BAND', 'CT_PROJECT_TYPE', 'CT_PROJECT_ROLE',
                           'CT_AWARD_LEVEL', 'CT_CLINICAL_DEPARTMENT', 'CT_ASSESSMENT_DIMENSION',
                           'CT_CITY', 'CT_VOLUME_BAND', 'CT_WORK_INTENSITY', 'CT_GROWTH_PREF',
                           'CT_STABILITY_PREF', 'CT_EVIDENCE_STRENGTH',
                           'CT_PREFERENCE_TYPE')
  UNION ALL
  -- 013 新建/追加的字段与自写开关
  SELECT 'FC|' || field_id || '|' || entity_id || '|' || title || '|' || data_type || '|' ||
         COALESCE(code_table_id, '-') || '|' || cardinality || '|' || allow_custom::text || '|' ||
         COALESCE(custom_hint, '-') || '|' || status
    FROM mt.field_catalog
   WHERE field_id LIKE 'F_PREF_%' OR field_id LIKE 'F_CLIN_%' OR field_id LIKE 'F_PRJ_%'
      OR field_id LIKE 'F_AWD_%' OR field_id LIKE 'F_ASM_%' OR field_id = 'F_PERSON_HUKOU_CITY'
      OR field_id IN ('F_EDU_MAJOR_CODE', 'F_CRED_TYPE', 'F_TRAIN_TYPE',
                      'F_PERSON_HEALTH_LIMITS', 'F_EMP_EMPLOYER_TYPE', 'F_RES_OUTPUT_TYPE',
                      'F_SKL_CONCEPT_ID')
  UNION ALL
  -- 维度表上的约束定义
  SELECT 'CON|' || conname || '|' || pg_get_constraintdef(oid)
    FROM pg_constraint WHERE conrelid = 'mt.dimension'::regclass
  UNION ALL
  -- 新增的可选策略行与实体登记
  SELECT 'EP|' || policy_id || '|' || candidate_kind || '|' || min_evidence::text || '|' ||
         auto_promote::text
    FROM mt.evolution_policy WHERE candidate_kind = 'field_option'
  UNION ALL
  SELECT 'EC|' || entity_id || '|' || COALESCE(table_name, '-') || '|' || domain || '|' ||
         COALESCE(id_prefix, '-') || '|' || kind
    FROM mt.entity_catalog
   WHERE entity_id IN ('preference', 'project_record', 'award_honor', 'assessment')
) t;

\echo === 行数明细 ===
SELECT (SELECT count(*) FROM mt.dimension) AS dimensions,
       (SELECT count(*) FROM mt.code_table) AS code_tables,
       (SELECT count(*) FROM mt.code_value) AS code_values,
       (SELECT count(*) FROM mt.field_catalog) AS fields,
       (SELECT count(*) FROM mt.field_catalog WHERE allow_custom) AS fields_allow_custom,
       (SELECT count(*) FROM mt.v_dimension_dependency_gap) AS dependency_gaps;
