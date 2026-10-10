-- ============================================================================
-- 医学生人才信息库 · 060 就业去向的口径（结果侧）v1.0.0
--
-- 059 给了"就业去向"这个结果变量，这一版给它两个**业务口径**，
-- 让"结果侧"也能进指标门禁与口径注册表（否则分析界面有、质量门禁看不到）。
--
-- 口径的价值在于**说清分母**：
--   · "三级医院去向占比"的分母是**填了去向的人**，不是全体人才。
--     没填的人不进分母（缺失≠空值，见 ops/import_persons.py 的说明）。
--   · 保留"待业/其他"在分母里 —— 只算"有着落的人"会把比例算高。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_OUTCOME_TIER1_RATE', '三级医院去向占比', '结果', 'ratio',
 '(SELECT count(*) FROM field_value WHERE field_id=''F_PSN_RES_DESTINATION'' '
 'AND subject_type=''person'' AND value_code=''DE1'')',
 '(SELECT count(*) FROM field_value WHERE field_id=''F_PSN_RES_DESTINATION'' '
 'AND subject_type=''person'')', NULL,
 '%', NULL, '限定在「填了就业去向的人」',
 '去向为三级医院的人 / 填了就业去向的人。'
 '**分母不是全体人才** —— 没填去向的人无从判断，把他算进分母等于把"不知道"当成"没进三甲"。',
 '当前值是**合成演示数据**（生成规则见 ops/seed_demo_analysis.py，'
 '其中学历是进三甲的真驱动、海外经历只是影子），所以只能验证口径算得对不对，'
 '不能当业务结论。', 20),

('M_OUTCOME_STUDY_RATE', '继续深造占比', '结果', 'ratio',
 '(SELECT count(*) FROM field_value WHERE field_id=''F_PSN_RES_DESTINATION'' '
 'AND subject_type=''person'' AND value_code IN (''DE5'',''DE6''))',
 '(SELECT count(*) FROM field_value WHERE field_id=''F_PSN_RES_DESTINATION'' '
 'AND subject_type=''person'')', NULL,
 '%', NULL, '限定在「填了就业去向的人」',
 '升学深造 + 出国出境 的人 / 填了就业去向的人。'
 '把两类合并是因为它们都是"不在当期就业市场"的选择 —— '
 '分开看时出国的人太少（本例 3 人），合并后比例才稳定。',
 '合并口径会掩盖两类之间的差异；要分开看就单独取 DE5 或 DE6。'
 '同样受合成数据限制。', 20)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

DO $$
DECLARE r record; n int := 0; v numeric;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id IN ('M_OUTCOME_TIER1_RATE','M_OUTCOME_STUDY_RATE') LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  IF n <> 2 THEN RAISE EXCEPTION '两个去向口径没登记全'; END IF;
  -- **非 0 断言**：分母有值就说明口径真能算（合成数据已经填了去向）
  SELECT value INTO v FROM metric_value('M_OUTCOME_TIER1_RATE');
  IF v IS NULL THEN RAISE EXCEPTION '三级医院去向占比算出 NULL —— 分母可能为空'; END IF;
  RAISE NOTICE '去向口径就绪：三级医院去向占比 = %', v;
END $$;

COMMIT;
