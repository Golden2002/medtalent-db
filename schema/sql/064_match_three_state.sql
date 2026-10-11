-- ============================================================================
-- 医学生人才信息库 · 064 匹配结果三态展示：资格判定 / 信息完整度 / 并列 v1.0.0
--
-- 【评审优先级 3，已用实测数据证实三条判断】
-- 评审说："已知维度上的高分不能覆盖未知的硬门槛""并列应显示并列组或名次范围"
-- "覆盖率分母应是该岗位适用且应评估的要求"。实测：
--   · **150 条结果里有 99 条 score_total = 1.00（满分）**，
--     而 explanation 写着"可评维度 4 个，未知 41 个，可评权重覆盖 9%"。
--     → **满分是在 91% 未知的情况下给出的**。
--   · 99 条同分的结果，rank 却排到 1–50 —— **靠 job_id 顺序伪造了精确名次**。
--   · 6750 条维度评估的分布（这才是 coverage 分母该怎么分的依据）：
--       两侧都没有值 3957 · 岗位侧没有值 1108 · 人侧没有值 1093
--       满足 541 · 部分满足 31 · 确认不满足 20
--   · `mt.dimension` 里 **8 个硬门槛**（证书/证书状态/学历/体检/专业/政治面貌/规培/工作年限）
--     —— 评审说"8 个硬门槛里可评的只有 4 个"，与实测一致。
--
-- 【三条修法】
--   ① **coverage 分母排除"岗位侧没有值"**：那是"该岗位没提这一项"＝**不适用**，
--      不是"该采集而没采到"。混在一起会**低估**覆盖率，也会让人以为"系统缺数据"。
--      真正"应评估但缺失"的是：人侧没有值 / 两侧都没有值。
--   ② **资格判定独立成三态**：已确认满足 / 已确认不满足 / **待核实**。
--      **只要有硬门槛是 unknown，结论就只能是"待核实"** ——
--      排序分再高也不能把它说成"匹配"。这是评审那句"高分不能覆盖未知硬门槛"的落地。
--   ③ **并列给名次范围**：同分的结果共享 [rank_min, rank_max]，不再靠 ID 顺序排先后。
--
-- 【阈值不写死】
--   "覆盖率低于多少就不给结论"是**业务判断**，评审明确说"正式阈值应通过真实验证确定"。
--   所以它进注册表 `match_display_policy`（带"临时值"标注），而不是硬编码在页面里。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 展示策略注册表（阈值不写死在代码里）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS match_display_policy (
  policy_id   TEXT PRIMARY KEY,
  value       NUMERIC NOT NULL,
  unit        TEXT NOT NULL,
  provisional BOOLEAN NOT NULL DEFAULT true,
  note        TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE match_display_policy IS
  '匹配结果的**展示策略**（阈值类）。放注册表而不是代码里，'
  '因为阈值是业务判断、要随真实验证调整；provisional=true 表示"当前是临时值"。';
INSERT INTO match_display_policy (policy_id, value, unit, provisional, note) VALUES
('min_coverage_for_conclusion', 0.30, '比例', true,
 '信息完整度低于这个值时，页面**不给"匹配"结论**，只显示"信息不足，需补数据"。'
 '0.30 是**临时值**（评审指出文档里的 0.3 只是示例）—— '
 '正式阈值要用带时间截点的真实验证集确定（评审优先级 4）。'
 '在它被验证之前，页面会标注"阈值未经验证"。')
ON CONFLICT (policy_id) DO UPDATE SET value = EXCLUDED.value, note = EXCLUDED.note;

-- ---------------------------------------------------------------------------
-- 2. 信息完整度与资格判定（把 45 个维度拆成"已评估 / 应评估但缺失 / 不适用"）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_match_coverage AS
WITH dim AS (
  SELECT m.match_id, m.person_id, m.target_id, m.match_run_id, m.score_total,
         kv.key AS dimension_id,
         kv.value->>'status' AS status,
         kv.value->>'reason' AS reason,
         coalesce((kv.value->>'weight')::numeric, 0) AS weight,
         coalesce(d.is_hard, false) AS is_hard,
         coalesce(d.title_zh, kv.key) AS dim_title
    FROM match_result m
    CROSS JOIN LATERAL jsonb_each(m.score_breakdown) kv
    LEFT JOIN dimension d ON d.dimension_id = kv.key AND d.status = 'active'
)
SELECT
  match_id, person_id, target_id, match_run_id, score_total,
  count(*) AS n_dims,
  count(*) FILTER (WHERE status IN ('met','partial','gap')) AS n_scored,
  count(*) FILTER (WHERE status = 'met') AS n_met,
  count(*) FILTER (WHERE status = 'partial') AS n_partial,
  count(*) FILTER (WHERE status = 'gap') AS n_gap,
  -- **不适用**：该岗位没提这一项 → 不计入覆盖率分母
  count(*) FILTER (WHERE status = 'unknown' AND reason = '岗位侧没有值')
    AS n_not_applicable,
  -- **应评估但缺失**：人侧没值 或 两侧都没值 → 计入分母，且是"未知"
  count(*) FILTER (WHERE status = 'unknown' AND reason <> '岗位侧没有值')
    AS n_unknown,
  count(*) FILTER (WHERE status IN ('met','partial','gap')
                      OR (status = 'unknown' AND reason <> '岗位侧没有值'))
    AS n_applicable,
  -- 已评估占"应评估"的比例（修正后的覆盖率）
  CASE WHEN count(*) FILTER (WHERE status IN ('met','partial','gap')
                                OR (status = 'unknown' AND reason <> '岗位侧没有值')) = 0
       THEN NULL
       ELSE round(count(*) FILTER (WHERE status IN ('met','partial','gap'))::numeric
                  / count(*) FILTER (WHERE status IN ('met','partial','gap')
                                       OR (status = 'unknown'
                                           AND reason <> '岗位侧没有值')), 4)
  END AS coverage_corrected,
  -- 原始覆盖率（分母＝全部维度）—— 保留用于对照，说明两者差别
  round(count(*) FILTER (WHERE status IN ('met','partial','gap'))::numeric
        / greatest(count(*), 1), 4) AS coverage_raw,
  -- 硬门槛
  count(*) FILTER (WHERE is_hard) AS n_hard,
  count(*) FILTER (WHERE is_hard AND status IN ('met','partial')) AS n_hard_met,
  count(*) FILTER (WHERE is_hard AND status = 'gap') AS n_hard_gap,
  count(*) FILTER (WHERE is_hard AND status = 'unknown') AS n_hard_unknown,
  -- **资格判定三态**：只要有硬门槛待核实，就不能说"匹配"
  CASE
    WHEN count(*) FILTER (WHERE is_hard AND status = 'gap') > 0 THEN '已确认不满足'
    WHEN count(*) FILTER (WHERE is_hard AND status = 'unknown') > 0 THEN '待核实'
    WHEN count(*) FILTER (WHERE is_hard AND status IN ('met','partial')) > 0
      THEN '已确认满足'
    ELSE '待核实'
  END AS verdict,
  -- 关键未知项（按权重降序的前几项，供页面直接展示"还缺什么"）
  (SELECT string_agg(x.dim_title || '（权重 ' || x.weight || '）', '、'
                     ORDER BY x.weight DESC)
     FROM (SELECT dim_title, weight FROM dim d2
            WHERE d2.match_id = dim.match_id
              AND d2.status = 'unknown' AND d2.reason <> '岗位侧没有值'
            ORDER BY d2.weight DESC LIMIT 5) x) AS top_unknown
FROM dim
GROUP BY match_id, person_id, target_id, match_run_id, score_total;
COMMENT ON VIEW v_match_coverage IS
  '匹配结果的信息完整度与**资格判定三态**。'
  '覆盖率分母＝**该岗位适用且应评估的维度**（排除"岗位侧没有值"＝不适用），'
  '应评估但缺失的仍计入未知。**只要有硬门槛是 unknown，verdict 就是"待核实"** —— '
  '排序分再高也不能把信息不足说成匹配。';

-- ---------------------------------------------------------------------------
-- 3. 并列名次范围（不靠 ID 顺序伪造精确排名）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_match_ranked AS
SELECT match_id, match_run_id, person_id, target_id, score_total,
       rank_min,
       rank_min + tie_n - 1 AS rank_max,
       tie_n,
       CASE WHEN tie_n > 1
            THEN '并列第 ' || rank_min || '–' || (rank_min + tie_n - 1) || ' 名'
            ELSE '第 ' || rank_min || ' 名' END AS rank_label
FROM (
  SELECT m.match_id, m.match_run_id, m.person_id, m.target_id, m.score_total,
         rank() OVER (PARTITION BY m.match_run_id, m.person_id
                          ORDER BY m.score_total DESC NULLS LAST) AS rank_min,
         count(*) OVER (PARTITION BY m.match_run_id, m.person_id, m.score_total) AS tie_n
    FROM match_result m
) t;
COMMENT ON VIEW v_match_ranked IS
  '匹配结果的**名次范围**。同分结果共享 [rank_min, rank_max]，'
  '不靠 job_id 顺序制造精确名次（实测 99/150 条同分 1.00，'
  '原来的 rank 却排到 1–50，是**伪精确**）。';

-- ---------------------------------------------------------------------------
-- 4. 三个口径（让"信息不足被误读成高匹配"这件事可监控）
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_MATCH_COVERAGE_CORRECTED_MEDIAN', '匹配信息完整度中位数（修正分母）', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(percentile_cont(0.5) WITHIN GROUP '
 '(ORDER BY coverage_corrected)::numeric * 100, 1) '
 'FROM v_match_coverage WHERE coverage_corrected IS NOT NULL)',
 '%', NULL, '分母＝该岗位适用且应评估的维度',
 '每条匹配结果的"已评估维度 / 应评估维度"的中位数。'
 '**分母排除了"岗位侧没有值"（＝该岗位没提这一项，不适用）** —— '
 '而"人侧没有值""两侧都没有值"仍计入，因为那是**该采集而没采到**。',
 '本库实测约 0.10 —— 也就是说**九成的应评估内容是未知的**。'
 '在这个完整度下，任何"满分"都只是"少数能比的项目恰好全中"，不是匹配得好。', 1),

('M_MATCH_TIE_RATE', '并列结果占比', '匹配', 'ratio',
 '(SELECT count(*) FROM v_match_ranked WHERE tie_n > 1)',
 '(SELECT count(*) FROM match_result)', NULL,
 '%', NULL, NULL,
 '与其他结果**同分**的匹配结果占比。同分本身不是错（离散维度必然并列），'
 '错的是**给它一个精确名次**。所以这个比例高的时候，页面必须显示名次范围而不是名次。',
 '本库实测极高（99/150 条同为满分）—— 这既说明并列普遍，'
 '也说明**当前排序分几乎没有区分度**，因为大部分维度都是未知。', 1),

('M_MATCH_HARDGATE_UNKNOWN_RATE', '硬门槛待核实率', '匹配', 'ratio',
 '(SELECT count(*) FROM v_match_coverage WHERE n_hard_unknown > 0)',
 '(SELECT count(*) FROM v_match_coverage)', NULL,
 '%', NULL, '硬门槛＝mt.dimension.is_hard（共 8 个：证书/证书状态/学历/体检/专业/政治面貌/规培/工作年限）',
 '至少有一个**硬门槛**处于"待核实"的匹配结果占比。'
 '硬门槛不过就不能录用，所以"硬门槛未知"时，**结论只能是待核实**，'
 '不能被已知维度上的高分覆盖 —— 这正是评审指出的那条。',
 '比例高说明"资格判定普遍做不了"，而不是"候选人普遍不合格"。'
 '两者的处置完全不同：前者要补采集，后者要筛人。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 5. 授权 + 实跑验证（含"高分不能覆盖未知硬门槛"的硬断言）
-- ---------------------------------------------------------------------------
SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE r record; n int := 0; v numeric; n_hard int; n_bad int; n_tie int;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id IN ('M_MATCH_COVERAGE_CORRECTED_MEDIAN','M_MATCH_TIE_RATE',
                                'M_MATCH_HARDGATE_UNKNOWN_RATE') LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  IF n <> 3 THEN RAISE EXCEPTION '三个展示口径没登记全（%）', n; END IF;

  -- 硬门槛数量必须与评审说的 8 个一致（不一致说明维度定义变了，要复核）
  SELECT count(*) INTO n_hard FROM dimension WHERE status='active' AND is_hard;
  IF n_hard <> 8 THEN
    RAISE NOTICE '注意：硬门槛数量是 %（评审材料里是 8 个）—— 维度定义可能已变更，需复核', n_hard;
  END IF;

  -- **核心断言：只要硬门槛有 unknown，verdict 就必须是"待核实"**。
  -- 违反它就是"高分覆盖了未知硬门槛"。
  SELECT count(*) INTO n_bad FROM v_match_coverage
   WHERE n_hard_unknown > 0 AND verdict <> '待核实';
  IF n_bad > 0 THEN
    RAISE EXCEPTION '有 % 条结果在硬门槛未知的情况下给出了非「待核实」的结论 —— '
      '这正是"高分覆盖未知硬门槛"，必须修', n_bad;
  END IF;

  -- 断言：覆盖率修正后 ≥ 原始（因为分母变小）
  SELECT count(*) INTO n_bad FROM v_match_coverage
   WHERE coverage_corrected IS NOT NULL AND coverage_raw IS NOT NULL
     AND coverage_corrected < coverage_raw;
  IF n_bad > 0 THEN
    RAISE EXCEPTION '有 % 条修正后覆盖率反而小于原始值 —— 分母口径写反了', n_bad;
  END IF;

  -- 并列必须被识别出来
  SELECT count(*) INTO n_tie FROM v_match_ranked WHERE tie_n > 1;
  RAISE NOTICE '并列结果 % 条（这些必须显示名次范围而不是精确名次）', n_tie;

  PERFORM * FROM metric_value('M_MATCH_HARDGATE_UNKNOWN_RATE');
  RAISE NOTICE '三态展示就绪：覆盖率修正分母 + 资格判定三态 + 名次范围 + 3 个口径';
END $$;

COMMIT;
