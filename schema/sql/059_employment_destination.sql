-- ============================================================================
-- 医学生人才信息库 · 059 就业去向（分类变量 + 分层维度）v1.0.0
--
-- 【用户反馈】"现在的分析和数据透视功能不明所以。我需要针对结果去分析因子的贡献，
--   模拟数据中应该有就业去向才对。对去向做分类，然后做多因子的分析。"
--
-- 这一版解决三件事里的第一件：**给出真正值得分析的结果变量**。
--
-- 【为什么原来的"求职结果"不够用】
-- 原来的 `F_PSN_RES_JOB_OUTCOME` 只有 已就业/待业中/升学深造/出国出境 四类，
-- 而且"已就业"是个**信息量很低的桶** —— 三甲医院和乡镇卫生院都算"已就业"，
-- 但它们背后的影响因素完全不同（学历、院校层次、专业、地域）。
-- 用它做多因子分析，结论只能是"学历高的人更容易已就业"这种正确但无用的话。
--
-- 【就业去向：把"结果"拆成有业务含义的分类】
-- 分类原则（不是随手分的）：
--   · **按用人单位的性质与层级**分，而不是按"有没有工作"分 ——
--     因为医学毕业生的真实决策是"去哪一类单位"，不是"要不要工作"；
--   · 层级要能对应到**职业树里的岗位族**（三甲/二级/基层/企业/深造/出国），
--     这样"去向 × 岗位族"的供需错配才能分析；
--   · 保留"待业/其他"作为一类（**不隐藏负面结果**），否则就业率会被高估。
--
-- 【同时登记成一个分层维度】
-- 光有字段还不够 —— 分析界面要能直接按它分层，所以同时写进
-- `dimension_registry`（这样 /pivot 与 /factors 的下拉里就自动出现）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 字段（零 DDL：add_dimension 会自动建词表 CT_F_PSN_RES_DESTINATION）
-- ---------------------------------------------------------------------------
SELECT mt.add_dimension(
  'person',
  'F_PSN_RES_DESTINATION',
  '就业去向',
  '[{"code":"DE1","label":"三级医院"},
    {"code":"DE2","label":"二级/专科医院"},
    {"code":"DE3","label":"基层医疗（社区/乡镇）"},
    {"code":"DE4","label":"医药/器械企业"},
    {"code":"DE5","label":"升学深造（读研/读博）"},
    {"code":"DE6","label":"出国出境"},
    {"code":"DE7","label":"待业/其他"}]'::jsonb,
  'code', false, false,
  '毕业时的就业去向（按用人单位性质与层级分类）。'
  '保留"待业/其他"是为了**不隐藏负面结果** —— 只统计"有着落的人"会把就业率算高。',
  '结果', 'T1', 'user');

-- ---------------------------------------------------------------------------
-- 2. 分层维度：让 /pivot 与 /factors 的下拉里直接可选
-- ---------------------------------------------------------------------------
INSERT INTO dimension_registry
  (dimension_id, title, entity, from_sql, expr_sql, code_table_id, group_name,
   note, min_cell, status)
VALUES
('D_DESTINATION', '就业去向', 'person',
 'person p',
 'coalesce((SELECT fv.value_code FROM field_value fv WHERE fv.subject_type=''person'' '
 'AND fv.subject_id=p.person_id AND fv.field_id=''F_PSN_RES_DESTINATION'' LIMIT 1), ''（未填）'')',
 'CT_F_PSN_RES_DESTINATION', '结果',
 '**本库的主结果变量**：按用人单位性质与层级分类（三级/二级/基层/企业/深造/出国/待业）。'
 '比"已就业/未就业"信息量大得多 —— 三甲与乡镇卫生院背后的影响因素完全不同。'
 '⚠ 当前值是**合成演示数据**，生成规则见 ops/seed_demo_analysis.py（带已知效应与混杂），'
 '所以它只能用来验证"分析能不能把已知规律找回来"，不能当业务结论。', 20, 'active')
ON CONFLICT (dimension_id) DO UPDATE
  SET expr_sql = EXCLUDED.expr_sql, code_table_id = EXCLUDED.code_table_id,
      note = EXCLUDED.note, status = 'active';

-- ---------------------------------------------------------------------------
-- 3. 两两合并的"研究问题"维度：把 7 类压成有决策含义的两三类
--    多因子分析里，**二分类结果的解释最清楚**（"哪些因素影响进入三级医院"）。
--    所以除了 7 类，再给两个常用二分口径，避免用户自己去想怎么合并。
-- ---------------------------------------------------------------------------
INSERT INTO dimension_registry
  (dimension_id, title, entity, from_sql, expr_sql, code_table_id, group_name,
   note, min_cell, status)
VALUES
('D_DEST_EMPLOYED_VS_STUDY', '去向：就业 vs 深造', 'person',
 'person p',
 'CASE (SELECT fv.value_code FROM field_value fv WHERE fv.subject_type=''person'' '
 'AND fv.subject_id=p.person_id AND fv.field_id=''F_PSN_RES_DESTINATION'' LIMIT 1) '
 'WHEN ''DE1'' THEN ''直接就业'' WHEN ''DE2'' THEN ''直接就业'' WHEN ''DE3'' THEN ''直接就业'' '
 'WHEN ''DE4'' THEN ''直接就业'' WHEN ''DE5'' THEN ''继续深造'' WHEN ''DE6'' THEN ''继续深造'' '
 'ELSE ''（未填/其他）'' END',
 NULL, '结果',
 '把 7 类压成"直接就业 vs 继续深造" —— 这是学生最真实的**二选一决策**，'
 '也是一个干净的二分类结果（多因子模型解释力最强）。', 20, 'active'),
('D_DEST_TIER1', '去向：是否进三级医院', 'person',
 'person p',
 'CASE (SELECT fv.value_code FROM field_value fv WHERE fv.subject_type=''person'' '
 'AND fv.subject_id=p.person_id AND fv.field_id=''F_PSN_RES_DESTINATION'' LIMIT 1) '
 'WHEN ''DE1'' THEN ''三级医院'' '
 'WHEN NULL THEN ''（未填）'' ELSE ''其他去向'' END',
 NULL, '结果',
 '把结果压成"是否进三级医院" —— 医学毕业生竞争最激烈的一类岗位，'
 '适合问"哪些因素影响进三甲"。', 20, 'active')
ON CONFLICT (dimension_id) DO UPDATE
  SET expr_sql = EXCLUDED.expr_sql, note = EXCLUDED.note, status = 'active';

DO $$
DECLARE n int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM field_catalog WHERE field_id='F_PSN_RES_DESTINATION') THEN
    RAISE EXCEPTION '就业去向字段没建成功';
  END IF;
  SELECT count(*) INTO n FROM code_value WHERE code_table_id='CT_F_PSN_RES_DESTINATION';
  IF n <> 7 THEN RAISE EXCEPTION '就业去向的码表应有 7 个码，实得 %', n; END IF;
  SELECT count(*) INTO n FROM dimension_registry
   WHERE dimension_id IN ('D_DESTINATION','D_DEST_EMPLOYED_VS_STUDY','D_DEST_TIER1');
  IF n <> 3 THEN RAISE EXCEPTION '三个去向维度没登记全（%）', n; END IF;
  RAISE NOTICE '就业去向就绪：1 个字段（7 类）+ 3 个分层维度';
END $$;

COMMIT;
