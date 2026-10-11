-- ============================================================================
-- 医学生人才信息库 · 071 泄漏口径改成分母型（NULL 语义才成立）v1.0.0
--
-- 【问题】070 把 `M_VALID_LEAKAGE_POINTS` 登记成 `kind='value'`（无分母），
-- 于是"还没有产出任何预测"时它算出 **0**，读起来像"没有泄漏"。
-- 但真实含义是"**还没有可算的对象**"—— 与"算出来是 0"是两件事，
-- 这正是本库反复强调的 NULL ≠ 0（删除确认率、资格判定正确率都按这条处理）。
--
-- 【为什么 070 的断言没抓到】
-- 070 里写的是 `IF r.denominator = 0 AND r.value IS NOT NULL THEN RAISE`，
-- 而 `value` 型口径的 numerator/denominator 是占位 `(SELECT 0)/(SELECT 1)`，
-- 所以 denominator=1，断言永远不触发 —— **断言写在了错的先决条件上**。
-- 教训：断言必须针对"这个口径在该情形下应返回什么"，而不是针对某个字段的值。
--
-- 【修法】改成 `kind='ratio'`：
--   分子 = 已产出预测却没应用截点过滤的点数
--   分母 = 已产出预测的点数
-- 分母为 0 时 `metric_value` 自然返回 NULL —— 语义与机制一致。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

UPDATE metric_registry SET
  title = '验证：有时间泄漏的预测点占比',
  kind = 'ratio',
  unit = '%',
  numerator_sql = '(SELECT count(*) FROM v_validation_leakage WHERE leaked)',
  denominator_sql = '(SELECT count(*) FROM validation_point WHERE predicted_at IS NOT NULL)',
  definition_note =
    '**已产出预测、却没有应用截点过滤**的预测点占已产出预测的比例 —— 这才是"时间泄漏"。'
    'cutoff 之后存在的材料单独列示（v_validation_leakage 的 edu_after/emp_after），'
    '那是"必须排除的量"，不是"已经泄漏的量"。',
  caveat =
    '⚠ 分母为 0（还没产出任何预测）时返回 **NULL**，不是 0 —— '
    '"还没有可算的对象"与"算出来是 0"是两件事。'
    '⚠ 071 之前它是 `value` 型（无分母），于是"没有预测"被算成 **0**，读起来像"没有泄漏"。'
    '⚠ **069 第一版**更早就错过了：它统计"cutoff 之后存在多少记录"，'
    '报告写成"时间泄漏 7 个点 —— 必须为 0"，而真实世界在决策时点之后当然会继续发生事情；'
    '"存在"不等于"用了"。'
    '⚠ 本库只能按**事件时间**判（recorded_at 全是同一天）；'
    '真实增量采集开始后必须改成按 recorded_at 判。'
 WHERE metric_id = 'M_VALID_LEAKAGE_POINTS';

SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE r record; n_pred int;
BEGIN
  SELECT * INTO r FROM metric_value('M_VALID_LEAKAGE_POINTS');
  SELECT count(*) INTO n_pred FROM validation_point WHERE predicted_at IS NOT NULL;

  -- 断言针对**该情形下应返回什么**（不是针对某个字段的值 —— 070 就栽在这里）
  IF n_pred = 0 AND r.value IS NOT NULL THEN
    RAISE EXCEPTION '还没有产出预测（分母 0），泄漏口径却给出 % —— 应为 NULL', r.value;
  END IF;
  IF n_pred = 0 AND r.denominator <> 0 THEN
    RAISE EXCEPTION '没有预测时分母应为 0，实得 %', r.denominator;
  END IF;
  IF n_pred > 0 AND r.value IS NULL THEN
    RAISE EXCEPTION '已有 % 个预测却算不出泄漏比例', n_pred;
  END IF;
  RAISE NOTICE '泄漏口径已改为分母型：分母 %（已产出预测）· 值 % —— '
    '没有预测时返回 NULL 而不是 0', r.denominator, coalesce(r.value::text, 'NULL');
END $$;

COMMIT;
