-- ============================================================================
-- 医学生人才信息库 · 061 数据性质隔离 + 岗位覆盖与完整性口径 v1.0.0
--
-- 【评审第 1 条，已核对运行状态并证实】
--   ① 岗位：**690 个全部来自 `src_fixture_careers`（本地招聘站点 fixture）**
--      —— 真实岗位 **0** 个。它们能验证管道，**不能验证真实招聘市场的推荐效果**。
--   ② 覆盖率分母错得离谱：实际被覆盖的是 **level 3** 的 **23/98**，
--      而"151"把岗位族(17)与子族(36)也算进了分母。
--      按 23/151 讲"具体职业覆盖率 15%"是**分母口径错误**。
--   ③ 能力权重 10866 行全部基于 fixture 岗位 → 只能算"演示权重"。
--   ④ 岗位质量"太干净"（过期 0、学历要求缺失 0、城市缺失 0）——
--      合成语料不会有缺失，**这本身就是它不像真实数据的证据**。
--
-- 【修法：把"数据性质"变成登记时写下的事实，而不是事后靠名字猜】
--   人侧早有 `person.quality_flags` 含 `synthetic_fixture`（120/123）；
--   岗位侧没有 —— 判据藏在 `source_registry.name` 的文字里（"fixture"）。
--   靠名字猜数据性质**一定会误伤**（归档一次性探针时已经吃过一次亏：
--   看着像临时的其实是生产依赖）。所以给 `source_registry` 加一个**显式字段**
--   `is_synthetic`，并在写入侧登记 —— 判据是**事实**，不是文字匹配。
--
-- 【为什么"数据性质"必须能分层】
--   评审第 1 条的验收标准是："**可以明确指出哪些结论由真实数据支撑**"。
--   所以不只是"显示三个数"，而是要让**每一个分析**都能按数据性质分层。
--   因此同时登记一个 `D_JOB_NATURE` 维度 → 透视与多因子页自动可选。
--
-- 【覆盖率分母的算法】
--   覆盖率必须**按层级分别算**，不能把不同层级的节点混在一个分母里：
--     level 1 = 大类、level 2 = 方向、level 3 = 具体职业
--   "具体职业覆盖率" = 有岗位样本的 level 3 节点 / 全部活跃 level 3 节点。
--   同时给出 level 2 覆盖率（方向级），因为岗位归类不一定细到 level 3。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 来源登记表加"是否合成"—— 登记事实，不靠名字猜
-- ---------------------------------------------------------------------------
ALTER TABLE source_registry
  ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN NOT NULL DEFAULT false;
COMMENT ON COLUMN source_registry.is_synthetic IS
  '这个来源的数据是不是**合成/夹具**。必须是**写入时登记的事实** —— '
  '不要靠 name 里有没有 "fixture" 去猜（按命名判断数据性质一定会误伤，'
  '本项目在归档一次性探针时已经吃过一次亏）。'
  '新增来源时若为真实采集，显式置 false；合成夹具显式置 true。';

-- 一次性回填：**只做这一次**，之后由登记侧负责
UPDATE source_registry SET is_synthetic = true
 WHERE name LIKE '%fixture%' OR name LIKE '%合成%' OR name LIKE '%测试%';
UPDATE source_registry SET is_synthetic = false WHERE source_id = 'src_public_profile';

-- 断言：回填之后，岗位侧的合成判据必须与文字判据一致（防止回填漏掉）
DO $$
DECLARE n_bad int;
BEGIN
  SELECT count(*) INTO n_bad
    FROM job_posting j JOIN source_registry s ON s.source_id = j.source_id
   WHERE s.is_synthetic <> (s.name LIKE '%fixture%' OR s.name LIKE '%合成%');
  IF n_bad > 0 THEN
    RAISE EXCEPTION '有 % 个岗位的来源 is_synthetic 与名称不一致 —— 回填有问题', n_bad;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. 统一的数据性质视图（人 / 岗位各自用各自已登记的判据）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_job_nature AS
SELECT j.job_id, j.occupation_id, j.city, j.source_id,
       coalesce(s.name, j.source_id) AS source_name,
       s.source_type, s.evidence_grade, s.is_synthetic,
       CASE WHEN s.is_synthetic THEN '合成夹具' ELSE '真实采集' END AS nature,
       (j.deadline IS NOT NULL AND j.deadline < current_date) AS is_expired
  FROM job_posting j LEFT JOIN source_registry s ON s.source_id = j.source_id;
COMMENT ON VIEW v_job_nature IS
  '岗位 + 来源性质（真实采集 / 合成夹具）。**判据是 source_registry.is_synthetic**'
  '（写入时登记的事实），不是名称匹配。做任何需求侧结论前先按它分层。';

-- ---------------------------------------------------------------------------
-- 3. 口径：真实/合成、覆盖（按层级）、完整性、过期、权重的真实支撑
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_DATA_REAL_JOB_RATE', '真实岗位占比', '需求', 'ratio',
 '(SELECT count(*) FROM v_job_nature WHERE NOT is_synthetic)',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, NULL,
 '来自**真实采集来源**的岗位 / 全部岗位。判据是 source_registry.is_synthetic = false。'
 '**这是判断"需求侧结论能不能当业务结论"的第一道门** —— 占比低时，'
 '所有基于岗位的分析都只是在验证管道。',
 '真实采集 ≠ 有效：还要看是否过期、要求是否完整。'
 '本库当前为 0%（690 个岗位全部来自本地 fixture）。', 1),

('M_DATA_SYNTHETIC_JOB_RATE', '合成岗位占比', '需求', 'ratio',
 '(SELECT count(*) FROM v_job_nature WHERE is_synthetic)',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, NULL,
 '来自合成夹具的岗位 / 全部岗位。与"真实岗位占比"互为补数，'
 '分开列是为了让**两个方向都看得见**（只看真实占比容易忽略"剩下的是什么"）。',
 '合成数据有价值（验证管道、回归测试、演示），问题只在**不标注**；本库标注了。', 1),

('M_DEMAND_OCC_COVERAGE_L3', '具体职业覆盖率（level 3）', '需求', 'ratio',
 '(SELECT count(DISTINCT occupation_id) FROM job_posting '
 ' WHERE occupation_id IS NOT NULL)',
 '(SELECT count(*) FROM occupation WHERE level = 3 AND status = ''active'')', NULL,
 '%', NULL, '分母＝活跃的 level 3 节点',
 '有岗位样本的 **level 3（具体职业）** 节点 / 全部活跃 level 3 节点。'
 '⚠ **分母必须是同一层级**：职业树里 level 1 是大类、level 2 是方向、level 3 是具体职业，'
 '把 151 个节点（17+36+98）当分母算出来的"15%"是**分母口径错误**。'
 '当前实测 23/98 = 23.5%。',
 '覆盖率低说明"岗位库里没有这个职业的样本"，**不等于**"市场上没有这个职业"。'
 '补样本要优先补目标用户需要、而当前没有样本的职业。', 1),

('M_DEMAND_OCC_COVERAGE_L2', '方向覆盖率（level 2）', '需求', 'ratio',
 '(SELECT count(DISTINCT o.parent_id) FROM job_posting j JOIN occupation o '
 '  ON o.occupation_id = j.occupation_id WHERE o.parent_id IS NOT NULL)',
 '(SELECT count(*) FROM occupation WHERE level = 2 AND status = ''active'')', NULL,
 '%', NULL, '分母＝活跃的 level 2 节点',
 '至少有一个子职业拿到岗位样本的 **level 2（方向）** 节点 / 全部活跃 level 2 节点。'
 '岗位归类不一定细到 level 3，所以方向级覆盖率通常更高、也更稳。',
 '方向级覆盖高不代表具体职业覆盖高 —— 一个方向下只要有 1 个具体职业有样本就算覆盖。', 1),

('M_DEMAND_EXPIRED_RATE', '过期岗位比例', '需求', 'ratio',
 '(SELECT count(*) FROM job_posting '
 ' WHERE (deadline IS NOT NULL AND deadline < current_date) '
 '    OR (valid_to IS NOT NULL AND valid_to < current_date))',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, NULL,
 '已过截止日期或已失效的岗位 / 全部岗位。'
 '**推荐系统里过期岗位是最容易造成"看起来有匹配、其实没有机会"的原因**，'
 '所以它必须是一个被盯住的口径，而不是靠人偶尔想起来清理。',
 '本库当前为 0% —— 但这**恰恰是合成语料的特征**：'
 '真实招聘数据必然有过期岗位。0% 说明"还没接过真实数据"，不是"质量好"。', 1),

('M_DEMAND_REQ_COMPLETENESS', '关键要求完整率', '需求', 'ratio',
 '(SELECT count(*) FROM job_posting WHERE education_req IS NOT NULL '
 ' AND city IS NOT NULL AND occupation_id IS NOT NULL '
 ' AND EXISTS (SELECT 1 FROM job_requirement r WHERE r.job_id = job_posting.job_id))',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, '关键要求＝学历/城市/职业归类/至少一条要求',
 '四项关键要求都齐的岗位 / 全部岗位。缺任何一项都会让匹配退化成'
 '"在已知维度上比高低"，所以这个口径衡量的是**匹配的前提**是否具备。',
 '完整率高 **不等于**要求准确；合成语料天然完整，真实数据不会。本库当前 100%。', 1),

('M_MATCH_WEIGHT_REAL_SUPPORT', '能力权重的真实样本支撑率', '匹配', 'ratio',
 '(SELECT count(*) FROM job_competency_weight w '
 ' WHERE EXISTS (SELECT 1 FROM job_requirement r JOIN job_posting j ON j.job_id = r.job_id '
 '               JOIN source_registry s ON s.source_id = j.source_id '
 '              WHERE j.occupation_id = w.occupation_id AND NOT s.is_synthetic))',
 '(SELECT count(*) FROM job_competency_weight)', NULL,
 '%', NULL, '按职业关联到真实岗位',
 '能力权重里**有真实岗位样本支撑**的行 / 全部权重行。'
 '权重是从岗位要求算出来的；如果那些岗位全是合成语料，权重就只能算"演示权重"，'
 '不能用来解释真实市场的要求强度。',
 '本库当前为 0% —— 所以现有权重**仅供演示**。'
 '任何"某能力在市场上很重要"的说法，在这个口径转正之前都不成立。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 4. 岗位侧的数据性质维度（让每个分析都能按它分层）
-- ---------------------------------------------------------------------------
INSERT INTO dimension_registry
  (dimension_id, title, entity, from_sql, expr_sql, code_table_id, group_name,
   note, min_cell, status)
VALUES
('D_JOB_NATURE', '数据性质（真实/合成）', 'job_posting',
 'v_job_nature j',
 'j.nature', NULL, '数据质量',
 '判据来自 source_registry.is_synthetic（写入时登记的事实），不是名称匹配。'
 '**需求侧的每一个结论都应该先按这一维看一眼**：如果结论完全由"合成夹具"撑起来，'
 '那它只是在验证管道，不是市场发现。', 1, 'active')
ON CONFLICT (dimension_id) DO UPDATE
  SET from_sql = EXCLUDED.from_sql, expr_sql = EXCLUDED.expr_sql,
      note = EXCLUDED.note, status = 'active';

-- ---------------------------------------------------------------------------
-- 5. 实跑验证（含"分母层级"的硬断言 —— 这正是评审指出的错误）
-- ---------------------------------------------------------------------------
DO $$
DECLARE r record; n int := 0; v numeric; n_l3 int; n_l3_all int; n_all int;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id IN ('M_DATA_REAL_JOB_RATE','M_DATA_SYNTHETIC_JOB_RATE',
                                'M_DEMAND_OCC_COVERAGE_L3','M_DEMAND_OCC_COVERAGE_L2',
                                'M_DEMAND_EXPIRED_RATE','M_DEMAND_REQ_COMPLETENESS',
                                'M_MATCH_WEIGHT_REAL_SUPPORT') LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  IF n <> 7 THEN RAISE EXCEPTION '七个数据性质/覆盖口径没登记全（%）', n; END IF;

  -- 硬断言：覆盖率的分母只能是同一层级
  SELECT count(DISTINCT occupation_id) INTO n_l3 FROM job_posting;
  SELECT count(*) INTO n_l3_all FROM occupation WHERE level = 3 AND status = 'active';
  IF n_l3 >= n_l3_all THEN
    RAISE EXCEPTION 'level 3 覆盖数(%) 不应 >= 该层级总数(%)', n_l3, n_l3_all;
  END IF;
  SELECT count(*) INTO n_all FROM occupation WHERE status = 'active';
  IF n_l3_all = n_all THEN
    RAISE EXCEPTION 'level 3 节点数等于全部节点数 —— 层级判据失效';
  END IF;
  RAISE NOTICE '覆盖率分母校验：level3 % / %（活跃节点共 % —— 混在一起算就是分母错误）',
               n_l3, n_l3_all, n_all;

  -- 真实岗位占比必须真的能算出数（哪怕是 0%，也不能是 NULL）
  SELECT value INTO v FROM metric_value('M_DATA_REAL_JOB_RATE');
  IF v IS NULL THEN RAISE EXCEPTION '真实岗位占比算出 NULL'; END IF;
  RAISE NOTICE '真实岗位占比 = %（0 表示需求侧结论目前只能验证管道）', v;

  PERFORM * FROM metric_value('M_MATCH_WEIGHT_REAL_SUPPORT');
  RAISE NOTICE '数据性质隔离就绪：7 个口径 + 1 个岗位侧维度';
END $$;

COMMIT;
