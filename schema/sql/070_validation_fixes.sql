-- ============================================================================
-- 医学生人才信息库 · 070 验证集两处设计修正 v1.0.0
--
-- 【修正一：漏掉了最有价值的那条信息】
-- 069 只记了 `target_occupation_id` 与 `in_library`，于是报告输出
-- "目标职业在库 3/3、不在库 0" —— 看着很好，**却把真正的问题盖掉了**。
-- 因为 `real_targets.json` 里的 `occupation_ids` 是**被映射选中的**那个节点，
-- 而映射是**刻意挑了一个"在语料里存在"的最近节点**：
--   冯唐的真实去向是医疗健康方向的 PE/投资；
--   语义更贴的 OCC-F07-02-02 医疗投资经理、OCC-F07-01-01 医药咨询顾问
--   **在 690 份 JD 里 0 岗位**；库里有样本的最近节点是券商医药行业研究员（30 份）。
-- 也就是说：**"目标在库"这件事本身是被映射规则"救"出来的**，
-- 而语义上正确的目标**根本不在库里**。
-- 只报 in_library 会让人以为覆盖没问题。
--
-- 修法：把 `nodes_without_jd`（语义更贴但库里 0 岗位的节点）**采集进来**，
-- 并在报告里把两件事分开说：
--   ① 被映射到的目标在不在库（可能"在"，但那是映射妥协的结果）；
--   ② **语义上正确的目标在不在库**（这才是"语料够不够"的真问题）。
--
-- 【修正二：我把"需要排除的材料"错报成了"泄漏"】
-- 069 的 `v_validation_leakage` 统计"该人 cutoff 之后有多少教育/经历记录"，
-- 报告直接把它写成"时间泄漏 7 个点 —— 必须为 0"。**这个语义是错的**：
-- cutoff 之后存在事实**是正常的**（真实世界当然会继续发生事情），
-- 要求的是**产出预测时把这些事实排除掉**。
-- "存在"不等于"用了"。在没有真跑过预测、没有记录特征快照之前，
-- 这个数字**不构成泄漏证据**，应当返回 NULL（"还没有可算的对象"）——
-- 这与本库"NULL ≠ 0"的纪律一致（把"还没做"报成"做错了"是同一类错误）。
--
-- 修法：`validation_point` 加 `features_cutoff_applied`（是否在产出预测时应用了截点过滤）
-- 与 `predicted_at`（何时产出的预测）。判据变成：
--   **已产出预测、却没有应用截点过滤** = 泄漏。
--   cutoff 之后存在的材料单独列示，说明它是"必须排除的量"，而不是"已经泄漏的量"。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 补字段：语义上正确的目标（可能不在库）+ 预测是否应用了截点过滤
-- ---------------------------------------------------------------------------
ALTER TABLE validation_point
  ADD COLUMN IF NOT EXISTS semantic_targets_without_jd TEXT,
  ADD COLUMN IF NOT EXISTS predicted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS features_cutoff_applied BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN validation_point.semantic_targets_without_jd IS
  '**语义上更贴、但在库里没有岗位样本**的目标职业（来自 real_targets.json 的 '
  'nodes_without_jd）。它与 target_occupation_id 分开记：'
  '后者常常是"映射规则刻意挑了一个在语料里存在的最近节点"的结果，'
  '只看它会得出"覆盖没问题"的错误结论。';
COMMENT ON COLUMN validation_point.features_cutoff_applied IS
  '产出该点预测时，是否**真的把 cutoff 之后的事实排除掉了**。'
  '只有它为 true 时才谈得上"没有泄漏"；为 false 且已产出预测 = 泄漏。'
  '（069 第一版没有这个字段，于是把"cutoff 之后存在材料"直接报成了泄漏 —— 语义错了。）';

-- ---------------------------------------------------------------------------
-- 2. 修正泄漏视图的语义
--
-- ⚠ `CREATE OR REPLACE VIEW` **不能改列名或列序** —— 不只是"不能改类型"。
-- 我这次插了新列（predicted_at / features_cutoff_applied），报错是
-- `cannot change name of view column "edu_after" to "predicted_at"`。
-- 所以必须先 DROP 再 CREATE；删除顺序要**先删依赖方**
-- （`v_validation_report` 依赖 `v_validation_leakage`）。
-- 重建后由本迁移末尾的 `apply_role_baseline()` 重新授权 ——
-- 否则 DROP 会把 GRANT 一起带走（052 已经踩过一次）。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_validation_report;
DROP VIEW IF EXISTS v_validation_leakage;

CREATE VIEW v_validation_leakage AS
SELECT p.point_id, p.case_id, v.person_id, p.cutoff_date, p.split,
       p.predicted_at, p.features_cutoff_applied,
       -- ⚠ 下面两个是"**必须排除的量**"，不是"已经泄漏的量"。
       -- 真实世界在决策时点之后当然会继续发生事情；要的是产出预测时把它们排除。
       (SELECT count(*) FROM education_record e
         WHERE e.person_id = v.person_id AND e.end_date > p.cutoff_date) AS edu_after,
       (SELECT count(*) FROM employment_record w
         WHERE w.person_id = v.person_id AND w.start_date > p.cutoff_date) AS emp_after,
       -- **真正的泄漏**：已经产出过预测，却没有应用截点过滤
       (p.predicted_at IS NOT NULL AND NOT p.features_cutoff_applied) AS leaked
  FROM validation_point p JOIN validation_case v ON v.case_id = p.case_id;
COMMENT ON VIEW v_validation_leakage IS
  '时间截点视图，两个概念**必须分开**：'
  '· `edu_after`/`emp_after` ＝ cutoff 之后**存在的材料**（正常现象，预测时必须排除）；'
  '· `leaked` ＝ **已产出预测却没有应用截点过滤**（这才是泄漏）。'
  '⚠ 069 第一版把前者直接报成"泄漏 7 个点"，语义错了 —— '
  '"存在"不等于"用了"；没跑过预测之前不构成泄漏证据（应为 NULL，不是报警）。'
  '⚠ 本库只能按**事件时间**判，因为 recorded_at 全是同一天（一次性批量入库）。'
  '真实增量采集开始后必须改成按 recorded_at 判。';

-- 重算报告视图（带上新字段）
CREATE VIEW v_validation_report AS
SELECT p.point_id, p.case_id, v.person_id, v.nature, p.split, p.cutoff_date,
       p.target_occupation_id, p.target_label, p.in_library,
       p.semantic_targets_without_jd, p.predicted_at, p.features_cutoff_applied,
       (SELECT count(*) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.direction = 'primary') AS n_primary,
       (SELECT count(*) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.direction = 'acceptable') AS n_acceptable,
       (SELECT count(*) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.direction = 'unacceptable') AS n_unacceptable,
       (SELECT count(DISTINCT a.rater) FROM validation_annotation a
         WHERE a.point_id = p.point_id) AS n_raters,
       p.in_library AS aspect1_target_in_library,
       (p.semantic_targets_without_jd IS NOT NULL)
         AS aspect1b_semantic_target_missing,
       (SELECT bool_and(a.qualification_ok) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.qualification_ok IS NOT NULL)
         AS aspect2_qualification_ok,
       (SELECT bool_and(a.ranking_reasonable) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.ranking_reasonable IS NOT NULL)
         AS aspect3_ranking_reasonable,
       (SELECT bool_and(a.evidence_present) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.evidence_present IS NOT NULL)
         AS aspect4_evidence_present,
       l.edu_after, l.emp_after, l.leaked
  FROM validation_point p
  JOIN validation_case v ON v.case_id = p.case_id
  LEFT JOIN v_validation_leakage l ON l.point_id = p.point_id;

-- ---------------------------------------------------------------------------
-- 3. 修正口径：泄漏只看"已产出预测却未过滤"；另加"语义目标缺失率"
-- ---------------------------------------------------------------------------
UPDATE metric_registry SET
  numerator_sql = '(SELECT count(*) FROM v_validation_leakage WHERE leaked)',
  denominator_sql = '(SELECT count(*) FROM validation_point WHERE predicted_at IS NOT NULL)',
  definition_note =
    '**已产出预测、却没有应用截点过滤**的预测点数 —— 这才是"时间泄漏"。'
    'cutoff 之后存在的材料单独列示（v_validation_leakage 的 edu_after/emp_after），'
    '那是"必须排除的量"，不是"已经泄漏的量"。',
  caveat =
    '⚠ **069 第一版把这两件事混了**：它统计"cutoff 之后存在多少记录"，'
    '于是报告写成"时间泄漏 7 个点 —— 必须为 0"，而真实世界在决策时点之后当然会继续发生事情。'
    '"存在"不等于"用了"；没跑过预测之前不构成泄漏证据 —— 此时分母为 0，口径应返回 **NULL**，'
    '而不是 0 或报警（把"还没做"报成"做错了"是本库一贯禁止的）。'
    '⚠ 本库只能按**事件时间**判（recorded_at 全是同一天）；'
    '真实增量采集开始后必须改成按 recorded_at 判。'
 WHERE metric_id = 'M_VALID_LEAKAGE_POINTS';

INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_VALID_SEMANTIC_TARGET_MISSING_RATE', '验证：语义目标不在库率', '结果', 'ratio',
 '(SELECT count(*) FROM validation_point WHERE semantic_targets_without_jd IS NOT NULL)',
 '(SELECT count(*) FROM validation_point WHERE target_occupation_id IS NOT NULL)', NULL,
 '%', NULL, '统计**有映射目标**的预测点',
 '语义上更贴的真实去向**在职业树里没有岗位样本**的预测点占比。'
 '它与「被映射到的目标在库率」**必须分开看**：'
 '后者常常是"映射规则刻意挑了语料里存在的最近节点"的结果，'
 '只看它会得出"覆盖没问题"的错误结论。'
 '实测冯唐案例：映射到券商医药行业研究员（30 份 JD，因此"在库"），'
 '而语义更贴的医疗投资经理 / 医药咨询顾问**在 690 份 JD 里 0 岗位**。',
 '本库真实案例只有 3 人，比例无统计意义，只能说明"存在这类失败"。'
 '要得到可用结论必须扩大真实案例数。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 4. 授权 + 断言
-- ---------------------------------------------------------------------------
SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE n int; v numeric; r record;
BEGIN
  -- 修正后：没有预测产物时，泄漏口径必须是 NULL（不是 0、不是报警）
  SELECT * INTO r FROM metric_value('M_VALID_LEAKAGE_POINTS');
  IF r.denominator = 0 AND r.value IS NOT NULL THEN
    RAISE EXCEPTION '还没有产出任何预测，泄漏口径却给出 %（应为 NULL）', r.value;
  END IF;
  RAISE NOTICE '泄漏口径：分母 %（还没有预测产物时为 0）· 值 %',
    r.denominator, coalesce(r.value::text, 'NULL');

  -- 新口径能算
  PERFORM * FROM metric_value('M_VALID_SEMANTIC_TARGET_MISSING_RATE');

  -- 语义目标缺失必须能被检出（冯唐案例就属于这一类）
  SELECT count(*) INTO n FROM validation_point
   WHERE semantic_targets_without_jd IS NOT NULL;
  RAISE NOTICE '语义更贴、但库里 0 岗位的目标：% 个预测点（这就是"该补语料"的证据）', n;

  n := 0;
  FOR r IN SELECT metric_id FROM metric_registry WHERE metric_id LIKE 'M_VALID\_%' LOOP
    PERFORM * FROM metric_value(r.metric_id); n := n + 1;
  END LOOP;
  RAISE NOTICE '验证口径共 % 条', n;
END $$;

COMMIT;
