-- ============================================================================
-- 医学生人才信息库 · 075 真实岗位覆盖：地区与来源 v1.0.0
--
-- 【为什么会有这个迁移：核对目标原文时发现的缺口】
-- 评审优先级 ① 的原文是"真实岗位覆盖的**职业/地区/来源**"。
-- 我先前只做了**职业**覆盖（`M_DEMAND_OCC_COVERAGE_L2/L3`），
-- **地区和来源一个口径都没有**（实测 `metric_id ~ 'REGION|CITY'` = 0 条、
-- `~ 'SOURCE'` = 0 条）。这是"以为做完了、其实漏了三分之一"的典型 ——
-- 只有回头逐字核对需求才会发现。
--
-- 【判据】仍用 `source_registry.is_synthetic`（**写入时登记的事实**），
-- 不用来源名称匹配 —— 本库的口径纪律是「判据是事实，不是名字」，
-- 而且我在默认列那次已经因为用名字猜而返工过一次（迁移 074）。
--
-- 【分母纪律】每一层的分母必须是**同一层级的全体**：
--   · 地区覆盖：分母 = **有岗位的城市总数**（不是"全国城市数"，
--     后者含大量本来就不该有岗位的城市，会把分子稀释成没意义的小数）；
--   · 来源覆盖：分母 = **提供岗位的来源总数**。
--   把不同层级的对象混进一个分母，就是我在职业覆盖上修过的那个错
--   （23/98 而不是 23/151）。
--
-- 【当前预期结果】真实岗位 = 0，所以两条覆盖率都应当是 **0.00%** ——
-- 这不是"算错了"，而是**事实**：需求侧没有任何真实数据，
-- 因此"真实岗位覆盖了哪些城市/哪些来源"这个问题目前无解，
-- 而卡片必须把这件事说出来，而不是显示一个空的表格。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 来源性质清单视图（卡片要能列出"哪些来源是合成的"）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_job_source_nature AS
SELECT s.source_id, s.name,
       s.is_synthetic,
       count(j.job_id) AS n_jobs,
       count(DISTINCT j.city) AS n_cities,
       count(DISTINCT j.occupation_id) AS n_occupations
  FROM source_registry s
  LEFT JOIN job_posting j ON j.source_id = s.source_id
 GROUP BY s.source_id, s.name, s.is_synthetic;
COMMENT ON VIEW v_job_source_nature IS
  '每个岗位来源的性质与覆盖（岗位数 / 城市数 / 职业数）。'
  '判据是 `source_registry.is_synthetic`（写入时登记的事实），不是来源名称。'
  '用途：让"真实岗位覆盖了哪些来源"能一眼看到 —— '
  '而不是只看一个总数（总数会掩盖"某个来源全是合成"这种情况）。';

-- ---------------------------------------------------------------------------
-- 2. 两条覆盖口径（地区 / 来源），分母同为"有岗位的"该层级对象
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_DEMAND_REAL_REGION_COVERAGE', '真实岗位地区覆盖率', '需求', 'ratio',
 '(SELECT count(DISTINCT j.city) FROM job_posting j '
 '   JOIN source_registry s ON s.source_id = j.source_id '
 '  WHERE NOT s.is_synthetic AND j.city IS NOT NULL)',
 '(SELECT count(DISTINCT j.city) FROM job_posting j WHERE j.city IS NOT NULL)', NULL,
 '%', NULL, '分母＝**有岗位的城市总数**（不是全国城市数）',
 '有**真实**岗位样本的城市占"有岗位的城市"的比例。'
 '它回答"真实招聘信息覆盖了哪些地区"，与职业覆盖率互补 —— '
 '只看职业覆盖会漏掉"某个地区完全没有真实数据"这种情况。',
 '⚠ 分母是**有岗位的城市**，不是全国城市数：后者含大量本来就不该有岗位的城市，'
 '会把分子稀释成没有意义的小数（与职业覆盖那次"23/98 而非 23/151"同一个道理）。'
 '⚠ 本库实测 **0.00%** —— 需求侧没有任何真实数据，所以这个问题目前无解；'
 '**0% 是事实，不是算错**。', 1),

('M_DEMAND_REAL_SOURCE_COVERAGE', '真实岗位来源覆盖率', '需求', 'ratio',
 '(SELECT count(DISTINCT j.source_id) FROM job_posting j '
 '   JOIN source_registry s ON s.source_id = j.source_id '
 '  WHERE NOT s.is_synthetic)',
 '(SELECT count(DISTINCT j.source_id) FROM job_posting j)', NULL,
 '%', NULL, '分母＝提供岗位的来源总数',
 '提供**真实**岗位的来源占"有岗位的来源"的比例。'
 '它衡量"数据来源的多样性"：只有一个合成来源，说明整个需求侧是自造的；'
 '即便有多个来源，若全是合成，覆盖仍然等于 0。',
 '⚠ 来源数多**不等于**数据好：十个合成来源仍然是 0% 真实覆盖。'
 '所以这条口径必须与 `source_registry.is_synthetic` 一起看，不能只看来源个数。'
 '⚠ 本库实测 **0.00%**。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 3. 授权 + 实跑验证
-- ---------------------------------------------------------------------------
SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE r record; v numeric; n_src int; n_syn int; n_city int;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id IN ('M_DEMAND_REAL_REGION_COVERAGE',
                                'M_DEMAND_REAL_SOURCE_COVERAGE') LOOP
    PERFORM * FROM metric_value(r.metric_id);
  END LOOP;

  -- 视图可读（新视图忘了对账会连 T3 都读不到，061 踩过）
  IF NOT (has_table_privilege('mt_t3','mt.v_job_source_nature','SELECT')
       OR has_any_column_privilege('mt_t3','mt.v_job_source_nature','SELECT')) THEN
    RAISE EXCEPTION '对账后 T3 仍读不到 v_job_source_nature';
  END IF;

  -- 地区覆盖的分母必须是"有岗位的城市"，且必须有值
  SELECT count(DISTINCT city) INTO n_city FROM job_posting WHERE city IS NOT NULL;
  SELECT * INTO r FROM metric_value('M_DEMAND_REAL_REGION_COVERAGE');
  IF r.denominator <> n_city THEN
    RAISE EXCEPTION '地区覆盖分母 % ≠ 有岗位的城市数 %', r.denominator, n_city;
  END IF;
  RAISE NOTICE '地区覆盖：分母 % 个城市 · 真实覆盖 %', n_city, coalesce(r.value::text, 'NULL');

  -- 口径值必须在 0–100 之间（写成 >100 说明分子用错了 —— 覆盖率那次踩过 704%）
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id IN ('M_DEMAND_REAL_REGION_COVERAGE',
                                'M_DEMAND_REAL_SOURCE_COVERAGE') LOOP
    SELECT value INTO v FROM metric_value(r.metric_id);
    IF v IS NOT NULL AND (v < 0 OR v > 100) THEN
      RAISE EXCEPTION '% 的值 % 不在 0–100 —— 分子分母层级不一致', r.metric_id, v;
    END IF;
  END LOOP;

  -- 来源性质清单必须覆盖全部来源（含合成标记），且合成/真实两侧都要能看见
  SELECT count(*) INTO n_src FROM v_job_source_nature;
  SELECT count(*) INTO n_syn FROM v_job_source_nature WHERE is_synthetic;
  IF n_src = 0 THEN RAISE EXCEPTION '来源性质清单为空'; END IF;
  RAISE NOTICE '来源清单：% 个来源，其中合成 % 个、真实 % 个',
    n_src, n_syn, n_src - n_syn;
END $$;

COMMIT;
