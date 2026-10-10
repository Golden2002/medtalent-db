-- ============================================================================
-- 医学生人才信息库 · 055 口径修正（量纲 / 合成数据分层）v1.0.0
--
-- 054 把口径填成能算的之后，**实跑立刻暴露了三个真问题**。这一版修它们。
-- 这正是"先确认口径"的价值：口径不实跑，问题会一直藏在好看的报表后面。
--
-- 【问题 1：量纲写错，而且错得很安静】
-- 实测 `match_result.score_total` 的范围是 **0.85 ~ 1.00（均值 0.965）**，
-- 即 **0–1 归一化得分**。而我在 F_MATCH_QUALITY 的阈值写的是 **≥ 60**（按百分制想当然），
-- 于是"得分达标"这一阶段**恒为 0**。
-- 症状是"漏斗第 2 段掉到 0"，看起来像"匹配质量极差"，
-- 真实原因是**口径量纲错了**。这类错误不会报错，只会给出一个安静的错误结论。
--
-- 【问题 2：两个口径打架，根因是合成数据混在真实数据里】
--   M_SUPPLY_CONSENT_RATE 有效授权覆盖率 = 113/124 = 91.13%
--   F_SUPPLY 第 5 阶段「有有效授权」     = 28
-- 查清了：
--   · 有技能断言的 120 人**全是合成人**（per_mock_*）
--   · 有有效授权的 113 人是**真实采集的人**（per_<hash>）
--   · 两拨人基本不重合 → 交集只有 28
-- **合成数据与真实数据同表混放，把每一个口径都同时稀释了。**
--
-- 修法不是"把合成数据藏掉"，而是：
--   ① 用**已有的注册表标记** `person.quality_flags` 含 `synthetic_fixture`
--      作为判据（**不用 person_id LIKE 'per_mock_%' 这种名字前缀** ——
--      按命名判断"这是什么数据"一定会误伤，P3-2 归档时已经吃过一次亏）；
--   ② 加一个**分类维度**「数据性质」，于是任何分析都能按它分层，
--      一眼看出结论是不是合成数据撑起来的；
--   ③ 加一个**数据卫生口径**「合成数据占比」，让污染程度本身可监控。
--
-- 【问题 3：另外两个 0 不是 bug，是真实状态】
--   校招岗位占比 0%（is_campus 全为 false）、雇主信息覆盖率 0%（employer 表为空）。
--   它们的 caveat 里已经预言了原因 —— 这正是"口径说明必须存档"的用处：
--   看到 0 的人能立刻知道是"业务没做"还是"数据没建"。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 合成数据的**注册表判据**（不用名字前缀）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION is_synthetic_person(p_person text)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT coalesce((SELECT 'synthetic_fixture' = ANY(p.quality_flags)
                     FROM person p WHERE p.person_id = p_person), false)
$$;
COMMENT ON FUNCTION is_synthetic_person(text) IS
  '这个主体是不是**合成夹具数据**。判据是 person.quality_flags 里有 synthetic_fixture —— '
  '**不是** person_id 的名字前缀。按命名判断数据性质会误伤（归档一次性探针时已吃过一次亏：'
  '看着像临时的其实是生产依赖），而 quality_flags 是**当初写入时就登记好的事实**。';

CREATE OR REPLACE VIEW v_person_real AS
  SELECT p.* FROM person p
   WHERE NOT ('synthetic_fixture' = ANY(p.quality_flags));
COMMENT ON VIEW v_person_real IS
  '只含**非合成**的人。做业务结论时应当用它；分析合成/真实差异时用 person 全量并按'
  '维度「数据性质」分层。';

CREATE OR REPLACE VIEW v_person_synthetic AS
  SELECT p.* FROM person p
   WHERE 'synthetic_fixture' = ANY(p.quality_flags);
COMMENT ON VIEW v_person_synthetic IS '只含合成夹具数据（用于自我检查与演示）。';

-- ---------------------------------------------------------------------------
-- 2. 修正量纲：匹配得分是 0–1 归一化，不是百分制
-- ---------------------------------------------------------------------------
UPDATE metric_registry SET
  definition_note = 'match_result.score_total 的平均值。'
    '⚠ **量纲是 0–1 的归一化得分，不是百分制**（实测范围 0.85–1.00）。'
    '把它当百分制会让阈值口径整体错位（F_MATCH_QUALITY 曾因此恒为 0）。',
  caveat = '**均值会掩盖分布**：0.965 可能是"都 0.96"，也可能是"一半 1.0 一半 0.9"。'
           '方法论明确要求看分布而不只看均值。另外不同 algo_version 的分不可混着比。',
  unit = '分'
 WHERE metric_id = 'M_MATCH_SCORE_AVG';

UPDATE metric_registry SET
  definition_note = 'match_result.score_total 的中位数（**0–1 归一化，不是百分制**）。'
 WHERE metric_id = 'M_MATCH_SCORE_MEDIAN';

-- 修 F_MATCH_QUALITY 的阈值：60 → 0.6（并留痕说明为什么）
UPDATE funnel_registry SET
  stages = jsonb_set(
    jsonb_set(stages, '{1,sql}',
      to_jsonb('SELECT count(*) FROM (SELECT person_id, max(score_total) AS best '
               'FROM match_result GROUP BY person_id HAVING max(score_total) >= 0.6) t'::text)),
    '{1,name}', to_jsonb('最高得分 ≥ 0.6'::text)),
  note = note || ' 【055 修正】阈值原写 60（按百分制想当然），而 score_total 是 '
                 '**0–1 归一化**（实测 0.85–1.00），导致"得分达标"恒为 0。'
                 '现改为 0.6。**这是口径量纲错误，不是匹配质量差** —— '
                 '一个写错的量纲能安静地产出完全错误的结论，所以每个口径都必须实跑验证。'
 WHERE funnel_id = 'F_MATCH_QUALITY';

UPDATE funnel_registry SET
  stages = jsonb_set(stages, '{2,note}',
    to_jsonb('60 是**本例的阈值约定**，不是行业标准；且得分是 0–1 量纲，故 0.6。'
             '改阈值要改这条 SQL 并留痕。'::text))
 WHERE funnel_id = 'F_MATCH_QUALITY';

-- ---------------------------------------------------------------------------
-- 3. 新增分类维度「数据性质」——**不藏，而是可分层**
-- ---------------------------------------------------------------------------
INSERT INTO dimension_registry
  (dimension_id, title, entity, from_sql, expr_sql, code_table_id, group_name,
   note, min_cell)
VALUES
('D_DATA_NATURE', '数据性质（合成/真实）', 'person',
 'person p',
 'CASE WHEN ''synthetic_fixture'' = ANY(p.quality_flags) THEN ''合成夹具'' '
 'ELSE ''真实采集'' END',
 NULL, '数据质量',
 '判据来自 person.quality_flags（写入时就登记的事实），不是名字前缀。'
 '**任何分析都应该先按这一维看一眼**：如果某个结论完全由"合成夹具"撑起来，'
 '那它只是在验证口径能不能跑，不是业务发现。', 1)
ON CONFLICT (dimension_id) DO UPDATE
  SET from_sql = EXCLUDED.from_sql, expr_sql = EXCLUDED.expr_sql,
      note = EXCLUDED.note, status = 'active';

-- ---------------------------------------------------------------------------
-- 4. 新增数据卫生口径：合成数据占比
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_DATA_SYNTHETIC_RATE', '合成数据占比', '供给', 'ratio',
 '(SELECT count(*) FROM person WHERE ''synthetic_fixture'' = ANY(quality_flags))',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '合成夹具人数 / 全部入库人才。判据是 quality_flags 含 synthetic_fixture。'
 '**这是一个数据卫生口径，不是业务口径**：它高说明库里的分析结论大多只能用来'
 '验证方法、不能当业务发现。',
 '合成数据本身有价值（口径验证、演示、回归测试），问题只在**不标注**；'
 '本库标注了，所以能做这个口径。做业务结论前先看它。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 5. 给被合成数据影响的业务口径补上"这条数字里混了合成数据"的说明
--    （**不删口径、不改分子分母** —— 数字本身没错，错的是"不说明"）
-- ---------------------------------------------------------------------------
UPDATE metric_registry SET caveat = coalesce(caveat || ' ', '') ||
  '【055】本口径的分母含合成夹具数据（当前 M_DATA_SYNTHETIC_RATE 很高），'
  '所以这个百分比**主要反映夹具**。做业务判断前请按维度「数据性质」分层，'
  '或改用 v_person_real。'
 WHERE metric_id IN ('M_SUPPLY_PROFILE_RATE','M_SUPPLY_EDU_RATE','M_SUPPLY_SKILL_RATE',
                     'M_SUPPLY_PREF_RATE','M_SUPPLY_CONSENT_RATE','M_SUPPLY_MATCHED_RATE',
                     'M_SUPPLY_EMPLOYED_RATE','M_SUPPLY_OVERSEAS_RATE','M_SUPPLY_PERSON',
                     'M_SUPPLY_SKILL_PER_PERSON','M_OUTCOME_EMPLOYED_PERSON');

-- ---------------------------------------------------------------------------
-- 6. 实跑验证：每条口径能算；漏斗修好后第 2 阶段不能再是"量纲导致的 0"
-- ---------------------------------------------------------------------------
DO $$
DECLARE r record; n int := 0; v numeric; stage2 bigint; stage1 bigint;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry WHERE status='active' ORDER BY 1 LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  RAISE NOTICE '口径实跑通过：% 条', n;

  FOR r IN SELECT funnel_id FROM funnel_registry WHERE status='active' ORDER BY 1 LOOP
    PERFORM * FROM funnel_run(r.funnel_id);
  END LOOP;
  RAISE NOTICE '漏斗实跑通过（含单调性断言）';

  -- 量纲修正的**正向证据**：阈值改对之后，阶段 1 与阶段 2 的关系应当合理
  SELECT subjects INTO stage1 FROM funnel_run('F_MATCH_QUALITY') WHERE stage_no = 1;
  SELECT subjects INTO stage2 FROM funnel_run('F_MATCH_QUALITY') WHERE stage_no = 2;
  RAISE NOTICE 'F_MATCH_QUALITY：被匹配 % 人 → 得分≥0.6 % 人（修正前第 2 段恒为 0）',
               stage1, stage2;
  IF stage1 > 0 AND stage2 = stage1 THEN
    RAISE NOTICE '  提示：阈值 0.6 把所有人都放进来了 —— 说明当前得分分布集中在 0.85 以上，'
                 '阈值不具区分度，需要按分布重新选（这正是"要看分布不只看均值"的例子）';
  END IF;

  -- 合成数据判据必须真的能区分出两拨人
  SELECT count(*) INTO v FROM person WHERE is_synthetic_person(person_id);
  IF v <= 0 THEN RAISE EXCEPTION '合成数据判据一个人都没识别出来 —— 判据失效'; END IF;
  RAISE NOTICE '合成数据判据有效：识别出 % 人', v;

  PERFORM * FROM metric_value('M_DATA_SYNTHETIC_RATE');
  SELECT value INTO v FROM metric_value('M_DATA_SYNTHETIC_RATE');
  -- ⚠ plpgsql 的 RAISE 里 `%` 是占位符、`%%` 才是字面百分号。
  -- 这一行我连续踩了两次（先是 %%%% 少参数、再是 %%% 又多一个）——
  -- 干脆把百分号从文案里去掉，只留占位符，消除这个坑。
  RAISE NOTICE '合成数据占比（百分数）= %', v;
END $$;

COMMIT;
