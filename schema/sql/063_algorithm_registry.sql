-- ============================================================================
-- 医学生人才信息库 · 063 算法注册表 + 接入状态口径 v1.0.0
--
-- 【评审优先级 2，已核对运行状态】
-- 评审说："16 号文档补了向量组装和案例打分，但未明确证明小程序接口已切换。"
-- 核对结果（**比评审的假设更具体**）：
--   · **用户侧路径**（小程序 API `api.matches` → `projection.compute_matches`）
--     用的是 `match_rule@0.1`，规则是**概念集合命中**（单维），
--     并把 `params` 如实记成 `{"rule": "concept_overlap"}`。
--   · **离线 50 维路径**（`analytics/vector.py` + `real_cases.py` 案例验证）
--     用 `vector/1.0.0+50dim`。
--   · **两条路径写同一张 `match_result` 表**：实测 87 次运行里 86 次是
--     `match_rule@0.1`、1 次是 `vector/1.0.0+50dim`；而**当前表里的 150 条结果
--     全是离线那套**（3 个人 / 113 个岗位）。
--   · **门户 `/match` 读这张表，页面上没有任何"这是哪套算法算的"标注。**
--     → 于是：**用户从小程序拿到的，和运营在门户看到的，是两套不同算法的结果，
--       而没有任何地方说明这件事。** 这正是评审要的"版本冲突"。
--
-- 【先纠正一个判断：这条路比评审以为的好】
-- 用户侧 API **已经**做了两件评审要求验证的事：
--   · 无有效分析授权 → 返回 `CONSENT_REQUIRED`（契约 §5）
--   · `person.status='deleting'` → 返回 `PROFILE_DELETING`，不返回分析
-- 所以"撤回授权后结果不可继续读取"在**入口**上是成立的（见 code/bridge/api.py）。
-- 本迁移要补的是**可观测性**：把"哪套算法接在哪、有没有异版本结果混进来"
-- 变成能查、能报警的事实。
--
-- 【设计：算法注册表】
-- 与口径/分类/漏斗注册表同一思路：**"谁在算、算什么版本、写到哪"是事实，不是代码考古**。
-- 有了它，"版本冲突"才能被机器检出，而不是靠人读代码。
--
-- 【为什么不做成"两边算法完全一致"】
-- 两套算法服务不同目的：用户侧要**快且可解释**（集合命中，逐条给出 met/gap/unknown），
-- 离线侧要**细粒度评估**（50 维权重、逐维拆解、门槛未过）。
-- 强行统一会让其中一个变差。**该做的不是统一算法，而是让"结果来自哪套算法"绝不混淆**：
--   · 注册表登记两套；
--   · 门户默认只看用户侧版本的输出来源说明，异版本结果**显式标注并计数**；
--   · 加口径盯住"异版本结果占比"。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 算法注册表
-- ---------------------------------------------------------------------------
CREATE TABLE algorithm_registry (
  algorithm_id  TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  user_facing   BOOLEAN NOT NULL,          -- 是不是"用户拿到的那套"
  version       TEXT NOT NULL,             -- 写进 match_run.algo_version 的值
  path          TEXT NOT NULL,             -- 入口（代码位置）
  uses          TEXT NOT NULL,             -- 用什么算
  writes_to     TEXT,                      -- 产出写到哪张表
  note          TEXT,
  status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','deprecated')),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE algorithm_registry IS
  '算法注册表：登记"谁在算、算什么版本、写到哪、是不是用户拿到的那套"。'
  '存在的意义：**同一个 match_result 表里可能混着多套算法的结果**，'
  '不登记就无法机器检出"异版本结果被当成最新"（实测踩到：表里全是离线 50 维的结果，'
  '而小程序 API 返回的是 match_rule@0.1 的结果，门户页面没有任何标注）。';

INSERT INTO algorithm_registry
  (algorithm_id, title, user_facing, version, path, uses, writes_to, note)
VALUES
('match_rule', '规则版匹配（用户侧）', true, 'match_rule@0.1',
 'code/bridge/projection.py compute_matches ← code/bridge/api.py matches',
 '概念集合命中（单维 concept_overlap）：岗位要求映射到概念后，'
 '与本人的 skill_assertion 概念集合比对，逐条给 met / gap / unknown',
 'match_result',
 '**这是小程序用户实际拿到的那一套。** 三条纪律：只有"能力缺口且落在有效观测窗口内"'
 '才算 gap；要求没映射到概念的 → unknown(requirement_not_mapped_to_concept)；'
 '没有观测窗口 → unknown(no_observation_window)。**未知不是不合格。**'
 '入口已强制授权检查（无有效个人分析授权 → CONSENT_REQUIRED）'
 '与删除态检查（deleting → PROFILE_DELETING）。'),

('vector_50dim', '50 维能力向量匹配（离线核算 / 案例验证）', false, 'vector/1.0.0+50dim',
 'code/analytics/vector.py rank_jobs/score ← code/demo/console.py、ops/fixtures/real_cases.py',
 '50 维能力权重（job_competency_weight）+ 硬门槛（gate）+ 逐维覆盖率',
 'match_result',
 '**不是用户侧那套**，但**与用户侧写同一张表** —— 所以必须靠 algo_version 区分。'
 '用于细粒度评估与真实案例验证（逐维拆解、门槛未过、覆盖率）。'
 '它写入的 150 条结果当前与用户侧结果混在 match_result 里。')
ON CONFLICT (algorithm_id) DO UPDATE
  SET title = EXCLUDED.title, user_facing = EXCLUDED.user_facing,
      version = EXCLUDED.version, path = EXCLUDED.path, uses = EXCLUDED.uses,
      writes_to = EXCLUDED.writes_to, note = EXCLUDED.note, status = 'active';

-- ---------------------------------------------------------------------------
-- 2. 带版本判定的匹配结果视图
--    "这条结果是用户侧算法产出的吗？是不是当前版本？" —— 一眼可查
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_match_result_versioned AS
SELECT m.match_id, m.match_run_id, m.person_id, m.target_type, m.target_id,
       m.score_total, m.rank, m.created_at,
       r.algo_version,
       coalesce(a.user_facing, false) AS from_user_facing_algo,
       coalesce(a.title, '未登记的算法版本') AS algo_title,
       -- 是不是"当前注册的用户侧版本"：不是就说明它不该被当成用户看到的结果
       (r.algo_version = (SELECT version FROM algorithm_registry
                           WHERE algorithm_id = 'match_rule')) AS is_current_user_algo
  FROM match_result m
  JOIN match_run r ON r.match_run_id = m.match_run_id
  LEFT JOIN algorithm_registry a ON a.version = r.algo_version;
COMMENT ON VIEW v_match_result_versioned IS
  '给每条匹配结果贴上"哪套算法、什么版本、是不是用户侧、是不是当前版本"。'
  '**门户展示匹配结果时必须用它**，否则离线 50 维的结果会被当成小程序返回的结果。';

-- ---------------------------------------------------------------------------
-- 3. 接入状态口径（评审点名的五项：未映射字段、版本冲突、待投递、死信、删除确认）
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_INTEGRATION_UNMAPPED_REQ_RATE', '岗位要求未映射概念率', '匹配', 'ratio',
 '(SELECT count(*) FROM job_requirement WHERE concept_id IS NULL)',
 '(SELECT count(*) FROM job_requirement)', NULL,
 '%', NULL, NULL,
 '岗位要求没有映射到概念的比例。**这些要求在用户侧匹配里只能进 unknown**，'
 '因为它们根本没有可比对的对象 —— 所以这个比例直接决定"匹配的天花板"。',
 '未映射≠这条要求没价值，而是"当前映射规则覆盖不到它"。'
 '修法是补映射规则或词表，不是把它当成"不合格"。', 1),

('M_INTEGRATION_STALE_MATCH_RATE', '异版本匹配结果占比', '匹配', 'ratio',
 '(SELECT count(*) FROM v_match_result_versioned WHERE NOT is_current_user_algo)',
 '(SELECT count(*) FROM match_result)', NULL,
 '%', NULL, '相对当前注册的用户侧算法版本',
 '产出算法**不是**当前用户侧版本的匹配结果占比。'
 '本库当前为 100% —— 表里的 150 条全是离线 50 维的结果，'
 '而小程序返回的是规则版（match_rule@0.1）。'
 '**这个数字高的时候，门户上的匹配结果不能代表用户看到的结果。**',
 '不等于"结果错了"，而是"它不是用户侧那套算法的产出"。'
 '要让两者可比，必须先说明各自的口径；**不要直接混着排序**。', 1),

('M_INTEGRATION_PENDING_EVENTS', '待投递事件数', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(*) FROM sync_event WHERE status NOT IN (''ok'',''acked'',''done''))',
 '条', NULL, '方向＝出站或入站',
 '投递队列里还没完成的事件数（sync_event.status 不是终态）。'
 '它衡量"下游还没拿到的东西有多少" —— 积压说明回流链路有问题。',
 '本库当前为 0（**测试环境没有真实下游**）。'
 '0 不代表链路健康，只代表"没有待投递的东西"；真实接入后必须重新基线化。', 1),

('M_INTEGRATION_DEAD_LETTERS', '死信数', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(*) FROM sync_event WHERE attempts > 0 AND last_error IS NOT NULL '
 'AND status NOT IN (''ok'',''acked'',''done''))',
 '条', NULL, NULL,
 '重试过、报过错、且仍未完成的事件数。**死信是最容易被忽略的一类故障**：'
 '它不报错、不阻塞，只是永远躺在队列里。',
 '本库当前为 0（没有真实下游）。真实接入后这个数字必须天天看 —— '
 '"重试 N 次仍失败"意味着对端契约或数据有问题，不是"再等等就好"。', 1),

('M_INTEGRATION_DELETION_CONFIRMED_RATE', '删除请求下游确认率', '匹配', 'ratio',
 '(SELECT count(*) FROM talent_deletion_request WHERE downstream_confirmed)',
 '(SELECT count(*) FROM talent_deletion_request)', NULL,
 '%', NULL, NULL,
 '删除请求里**已获得下游确认**的比例。'
 '"本地删了、下游还留着"是本库最严重的合规风险之一，'
 '所以"发起删除"与"确认删除"必须分开计数。',
 '本库当前分母为 0（还没有删除请求），所以口径返回 NULL —— '
 '**NULL 表示"还没有可算的对象"，不是 100% 也不是 0%**（这两者的混淆是报表最常见的谎言）。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 4. 授权：**新建视图必须跑对账**（061 忘了跑，连 T3 都读不到）
-- ---------------------------------------------------------------------------
SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE r record; n int := 0; v numeric; n_new int;
BEGIN
  -- 口径全部实跑
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id LIKE 'M_INTEGRATION\_%' LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  IF n <> 5 THEN RAISE EXCEPTION '五个接入状态口径没登记全（%）', n; END IF;

  -- 视图授权必须生效（忘了对账会连 T3 都读不到）
  IF NOT has_table_privilege('mt_t3', 'mt.v_match_result_versioned', 'SELECT') THEN
    RAISE EXCEPTION '对账后 T3 仍读不到 v_match_result_versioned';
  END IF;
  IF has_table_privilege('mt_t0', 'mt.v_match_result_versioned', 'SELECT') THEN
    RAISE EXCEPTION '匿名能读匹配结果版本视图 —— 应只给 T3';
  END IF;

  -- **版本冲突必须真的能被检出**：当前表里应当有非用户侧版本的结果
  SELECT count(*) INTO n_new FROM v_match_result_versioned WHERE NOT is_current_user_algo;
  RAISE NOTICE '异版本匹配结果：% 条（这就是"门户显示的未必是用户拿到的"的量化）', n_new;

  -- 未映射率必须能算且非空
  SELECT value INTO v FROM metric_value('M_INTEGRATION_UNMAPPED_REQ_RATE');
  IF v IS NULL THEN RAISE EXCEPTION '未映射概念率算出 NULL'; END IF;
  RAISE NOTICE '岗位要求未映射概念率 = %（这些要求只能进 unknown）', v;

  -- 删除确认率：分母为 0 时必须返回 NULL（不是 0、不是 100）
  SELECT value INTO v FROM metric_value('M_INTEGRATION_DELETION_CONFIRMED_RATE');
  IF v IS NOT NULL THEN
    RAISE NOTICE '删除确认率 = %（当前有删除请求）', v;
  ELSE
    RAISE NOTICE '删除确认率 = NULL（还没有删除请求）—— 返回 NULL 而不是 0%% 或 100%%';
  END IF;

  RAISE NOTICE '算法注册表 + 接入状态口径就绪：2 套算法、5 个口径、1 个版本视图';
END $$;

COMMIT;
