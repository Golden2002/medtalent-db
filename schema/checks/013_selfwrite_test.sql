-- ============================================================================
-- 013 自写值路径端到端测试（事务内执行，最后 ROLLBACK，不留任何数据）
-- 用法：python ops\pg.py sql schema\checks\013_selfwrite_test.sql
--
-- 验证链路：
--   ① assert_option 拒绝未登记的自写码
--   ② submit_option_candidate 把自写值放进 concept_candidate 候选池（不写词表）
--   ③ 再交一次 → evidence_count 累加，达到阈值自动置 ready
--   ④ promote_option_candidate 评审提升为正式 code_value
--   ⑤ assert_option 放行，且 v_option_set 能看到它
-- ============================================================================
BEGIN;
SET search_path TO mt, public;
\pset pager off

\echo === A. 自写前：字段开关与选项数 ===
SELECT field_id, entity_id, code_table_id, allow_custom, left(custom_hint, 30) AS hint_head
  FROM mt.field_catalog WHERE field_id = 'F_PREF_INTEREST_DOMAIN';
SELECT count(*) AS options_before FROM mt.code_value WHERE code_table_id = 'CT_INTEREST_DOMAIN';

\echo === B. 未登记的自写码必须被 assert_option 拒绝 ===
DO $$
DECLARE
  v_ok BOOLEAN := false;
BEGIN
  BEGIN
    PERFORM mt.assert_option('F_PREF_INTEREST_DOMAIN', '中医骨伤科传承');
  EXCEPTION WHEN check_violation THEN
    v_ok := true;
    RAISE NOTICE '[OK] assert_option 正确拒绝未登记码：%', SQLERRM;
  END;
  IF NOT v_ok THEN
    RAISE EXCEPTION 'assert_option 没有拒绝未登记码，测试失败';
  END IF;
END $$;

\echo === C. 提交自写值：只进候选池，不进词表 ===
SELECT mt.submit_option_candidate('F_PREF_INTEREST_DOMAIN', '中医骨伤科传承',
                                  'per_mock_0001', 'demo_submit_1') AS candidate_1;
SELECT mt.submit_option_candidate('F_PREF_INTEREST_DOMAIN', '中医骨伤科传承',
                                  'per_mock_0002', 'demo_submit_2') AS candidate_2;
SELECT mt.submit_option_candidate('F_PREF_INTEREST_DOMAIN', '中医骨伤科传承',
                                  'per_mock_0003', 'demo_submit_3') AS candidate_3;
-- 已存在选项的取值应返回 NULL（无需新建候选）
SELECT mt.submit_option_candidate('F_PREF_INTEREST_DOMAIN', ' 医疗人工智能与算法 ',
                                  'per_mock_0004') AS existing_option_returns_null;

SELECT candidate_id, phrase_key, phrase_sample, evidence_count, status,
       note::jsonb ->> 'kind' AS candidate_kind,
       note::jsonb ->> 'field_id' AS field_id
  FROM mt.concept_candidate WHERE phrase_key LIKE 'OPT|F_PREF_INTEREST_DOMAIN|%';

\echo === D. 提升为正式选项 ===
SELECT mt.promote_option_candidate(
         (SELECT candidate_id FROM mt.concept_candidate
           WHERE phrase_key LIKE 'OPT|F_PREF_INTEREST_DOMAIN|%'), 'IN30', '中医骨伤科',
         'TCM Orthopedics', NULL, 'reviewer_demo') AS promoted_to;

SELECT code_table_id, code, label_zh, label_en, sort_order, external_mapping
  FROM mt.code_value WHERE code = 'IN30' AND code_table_id = 'CT_INTEREST_DOMAIN';
SELECT status, promoted_to FROM mt.concept_candidate
 WHERE phrase_key LIKE 'OPT|F_PREF_INTEREST_DOMAIN|%';

\echo === E. 提升后 assert_option 放行 ===
SELECT mt.assert_option('F_PREF_INTEREST_DOMAIN', 'IN30') AS assert_ok_pass;

\echo === F. 被拒绝的字段（allow_custom=false）必须拒绝自写 ===
DO $$
BEGIN
  BEGIN
    PERFORM mt.submit_option_candidate('F_EDU_DEGREE_LEVEL', '旁听生');
    RAISE EXCEPTION '没有拦住 allow_custom=false 的字段，测试失败';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM LIKE '%没有拦住%' THEN RAISE; END IF;
    RAISE NOTICE '[OK] 正确拒绝不允许自写的字段：%', SQLERRM;
  END;
END $$;

\echo === G. 概念型维度（无词表）必须拒绝并提示走概念候选池 ===
DO $$
BEGIN
  BEGIN
    PERFORM mt.submit_option_candidate('F_SKL_CONCEPT_ID', '某种新能力');
    RAISE EXCEPTION '没有拦住无词表字段，测试失败';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM LIKE '%没有拦住%' THEN RAISE; END IF;
    RAISE NOTICE '[OK] 正确拒绝无词表字段：%', SQLERRM;
  END;
END $$;

\echo === H. 自写后选项数（临时可见，随后整体回滚） ===
SELECT count(*) AS options_after FROM mt.code_value WHERE code_table_id = 'CT_INTEREST_DOMAIN';

ROLLBACK;

\echo === I. 回滚后确认没有留下痕迹 ===
SELECT count(*) AS leftover_candidates FROM mt.concept_candidate
 WHERE phrase_key LIKE 'OPT|F_PREF_INTEREST_DOMAIN|%';
SELECT count(*) AS leftover_codes FROM mt.code_value
 WHERE code_table_id = 'CT_INTEREST_DOMAIN' AND code = 'IN30';
