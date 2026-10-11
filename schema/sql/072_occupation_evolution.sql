-- ============================================================================
-- 医学生人才信息库 · 072 职业树：变更影响预览 + 发布管理 + 下游一致性 v1.0.0
--
-- 【评审优先级 5 的收窄项】"职业树演化：改为补**变更影响预览、发布管理、下游一致性**"
--
-- 【先核对运行状态：结构都在，缺的是"计算"】
--   · `occupation_change`（16 列）：change_type ∈ add/split/merge/rename/retire/remap，
--     status ∈ proposed/approved/rejected/applied/**reverted**（**回滚已经建模**）
--   · `occupation_migration`（9 列）：relation ∈ renamed/split_into/merged_into/retired + coverage
--   · `dataset_release`（15 列）：version / is_live / status / checksum
--   · `occupation_candidate`（2 行）；`occupation` 151 个节点全 active
--   · **但这三张表都是 0 行** —— 也就是说：**流程建模了，但没有任何计算把它们打通**：
--       ① 没有任何"改这个节点会碰到谁"的计算 → 变更影响预览**完全缺失**
--       ② 变更与发布**没有关联**，也没有"哪些已生效变更还没发布"的检查
--       ③ `occupation_migration` 有 old_id/new_id/coverage，但**没有检查**下游是否悬挂
--
-- 【设计原则】不新建流程，**复用已有的表**，只补三样计算：
--   ① `v_occupation_impact`：每个节点的**下游依赖盘点**（血缘类预览）
--      —— 改之前先看清"这个节点被谁引用、有多少"。
--   ② `occupation_release_item` + `v_occupation_release_status`：把变更纳入**发布**，
--      并给出"已生效但未发布"的缺口（发布管理）。
--   ③ `v_occupation_downstream_gap`：下游一致性（悬挂引用 / 无效迁移 / 未发布变更）。
--
-- 【一个必须讲清的取舍：为什么 retire 必须同时有 migration】
-- 节点停用后，下游（岗位、权重、人的经历、薪资基准）都还指着旧 ID。
-- 只 retire 不 remap 会让下游**静默悬挂**：页面照旧显示旧节点名，但那个节点已经不作数了 ——
-- 这是本库最怕的「看不到 ≠ 没有」。所以一致性判据把"停用但无迁移映射"直接列为违规。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 变更影响预览：每个节点的下游依赖盘点
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_occupation_impact AS
SELECT o.occupation_id, o.label_zh, o.level, o.status, o.parent_id,
       (SELECT count(*) FROM job_posting j
         WHERE j.occupation_id = o.occupation_id) AS n_jobs,
       (SELECT count(*) FROM job_requirement r
         WHERE r.occupation_id = o.occupation_id) AS n_requirements,
       (SELECT count(*) FROM job_competency_weight w
         WHERE w.occupation_id = o.occupation_id AND w.valid_to IS NULL) AS n_weights,
       (SELECT count(*) FROM employment_record e
         WHERE e.occupation_id = o.occupation_id) AS n_employment,
       (SELECT count(*) FROM salary_benchmark s
         WHERE s.occupation_id = o.occupation_id) AS n_salary,
       (SELECT count(*) FROM occupation c
         WHERE c.parent_id = o.occupation_id) AS n_children,
       (SELECT count(*) FROM occupation_migration m
         WHERE m.old_id = o.occupation_id OR m.new_id = o.occupation_id) AS n_migrations,
       (SELECT count(*) FROM transition_case t
         WHERE t.from_occupation = o.occupation_id
            OR t.to_occupation = o.occupation_id) AS n_transition_cases,
       (SELECT count(*) FROM validation_point v
         WHERE v.target_occupation_id = o.occupation_id) AS n_validation_points,
       -- 合计依赖（不含迁移关系：迁移是"处置手段"，不是"被依赖"）
       (SELECT count(*) FROM job_posting j WHERE j.occupation_id = o.occupation_id)
     + (SELECT count(*) FROM job_requirement r WHERE r.occupation_id = o.occupation_id)
     + (SELECT count(*) FROM job_competency_weight w
         WHERE w.occupation_id = o.occupation_id AND w.valid_to IS NULL)
     + (SELECT count(*) FROM employment_record e WHERE e.occupation_id = o.occupation_id)
     + (SELECT count(*) FROM salary_benchmark s WHERE s.occupation_id = o.occupation_id)
     + (SELECT count(*) FROM occupation c WHERE c.parent_id = o.occupation_id)
     + (SELECT count(*) FROM validation_point v WHERE v.target_occupation_id = o.occupation_id)
       AS dependents_total,
       -- 风险分级：**依据是"改动会不会让下游失效"，不是"这个节点重不重要"**
       CASE
         WHEN (SELECT count(*) FROM job_posting j WHERE j.occupation_id = o.occupation_id) > 0
           OR (SELECT count(*) FROM occupation c WHERE c.parent_id = o.occupation_id) > 0
           OR (SELECT count(*) FROM job_competency_weight w
                WHERE w.occupation_id = o.occupation_id AND w.valid_to IS NULL) > 0
           THEN '高（有岗位样本/子节点/能力权重，改动会直接影响匹配与推荐）'
         WHEN (SELECT count(*) FROM employment_record e WHERE e.occupation_id = o.occupation_id) > 0
           OR (SELECT count(*) FROM salary_benchmark s WHERE s.occupation_id = o.occupation_id) > 0
           OR (SELECT count(*) FROM validation_point v
                WHERE v.target_occupation_id = o.occupation_id) > 0
           THEN '中（有人才经历/薪资/验证集引用）'
         WHEN (SELECT count(*) FROM job_requirement r WHERE r.occupation_id = o.occupation_id) > 0
           THEN '低（只有岗位要求引用）'
         ELSE '无（没有任何下游引用）'
       END AS impact_risk
  FROM occupation o;
COMMENT ON VIEW v_occupation_impact IS
  '职业节点**下游依赖盘点**（变更影响预览，血缘类）。'
  '改动一个节点前先看这里："它被谁引用、各有多少"，以及风险分级。'
  '⚠ 风险分级的依据是「改动会不会让下游失效」，**不是**「这个节点重不重要」—— '
  '一个"重要但没人引用"的节点改起来是安全的，而一个"不起眼但有 30 份 JD"的节点改起来会直接影响推荐。'
  '⚠ 停用（retire）还必须配 `occupation_migration` 重映射，否则下游会**静默悬挂**'
  '（见 `v_occupation_downstream_gap`）。';

-- ---------------------------------------------------------------------------
-- 2. 发布管理：变更纳入发布 + 未发布缺口
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS occupation_release_item (
  release_id  TEXT NOT NULL REFERENCES dataset_release(release_id) ON DELETE CASCADE,
  change_id   TEXT NOT NULL REFERENCES occupation_change(change_id) ON DELETE CASCADE,
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (release_id, change_id)
);
COMMENT ON TABLE occupation_release_item IS
  '职业树变更 → 数据集发布的归属关系（发布管理）。'
  '复用已有的 `dataset_release`，不另造一套发布机制 —— '
  '职业树变更本来就应当随一次"口径版本发布"一起对外生效。'
  '有了它才能回答"**哪些已生效的变更还没纳入任何发布**"。';

CREATE OR REPLACE VIEW v_occupation_release_status AS
SELECT r.release_id, r.version, r.name, r.status, r.is_live, r.published_at,
       count(i.change_id) AS n_changes,
       count(i.change_id) FILTER (WHERE ch.status = 'applied') AS n_applied,
       count(i.change_id) FILTER (WHERE ch.status = 'reverted') AS n_reverted,
       count(i.change_id) FILTER (WHERE ch.status IN ('proposed','approved')) AS n_pending,
       r.published_at IS NULL AS not_published,
       -- 已发布却有未决变更 → 发布内容与树状态不一致
       (r.published_at IS NOT NULL
        AND count(i.change_id) FILTER (WHERE ch.status IN ('proposed','approved')) > 0)
         AS published_with_pending
  FROM dataset_release r
  LEFT JOIN occupation_release_item i ON i.release_id = r.release_id
  LEFT JOIN occupation_change ch ON ch.change_id = i.change_id
 GROUP BY r.release_id, r.version, r.name, r.status, r.is_live, r.published_at;
COMMENT ON VIEW v_occupation_release_status IS
  '每次发布的变更归属与状态；`published_with_pending` 指出"发布内容与树状态不一致"'
  '（已发布，却还挂着未决变更）—— 这是发布管理里最容易漏的一类不一致。';

-- ---------------------------------------------------------------------------
-- 3. 下游一致性：悬挂引用 / 无效迁移 / 未发布变更
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_occupation_downstream_gap AS
-- A. 停用节点仍有下游引用，且没有迁移映射覆盖 → 下游会静默悬挂
SELECT '停用但下游仍引用且无迁移映射' AS gap_kind, o.occupation_id AS object_id,
       o.label_zh AS object_label, o.status AS detail,
       (SELECT count(*) FROM job_posting j WHERE j.occupation_id = o.occupation_id)
     + (SELECT count(*) FROM job_competency_weight w
         WHERE w.occupation_id = o.occupation_id AND w.valid_to IS NULL)
     + (SELECT count(*) FROM employment_record e WHERE e.occupation_id = o.occupation_id)
     + (SELECT count(*) FROM occupation c WHERE c.parent_id = o.occupation_id)
       AS badness
  FROM occupation o
 WHERE o.status <> 'active'
   AND NOT EXISTS (SELECT 1 FROM occupation_migration m WHERE m.old_id = o.occupation_id)

UNION ALL
-- B. 迁移关系指向不存在的节点 → 迁移表本身就是坏的
SELECT '迁移关系指向不存在的节点', m.migration_id,
       m.old_id || ' → ' || m.new_id, 'migration',
       1
  FROM occupation_migration m
 WHERE NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = m.old_id)
    OR (m.new_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = m.new_id))

UNION ALL
-- C. 已生效（applied）却没有纳入任何发布的变更 → 树变了但没"发布"
SELECT '已生效变更未纳入发布', ch.change_id, ch.change_type || ' ' || ch.coalesce_label,
       ch.status, 1
  FROM (SELECT c.*, coalesce(c.label_zh, array_to_string(c.from_ids, ',')) AS coalesce_label
          FROM occupation_change c) ch
 WHERE ch.status = 'applied'
   AND NOT EXISTS (SELECT 1 FROM occupation_release_item i WHERE i.change_id = ch.change_id);
COMMENT ON VIEW v_occupation_downstream_gap IS
  '职业树**下游一致性**缺口，三类：'
  'A 停用节点仍有下游引用且没有迁移映射（会让下游**静默悬挂** —— 页面照旧显示旧节点名，'
  '但那个节点已经不作数）；B 迁移关系指向不存在的节点（迁移表自己坏了）；'
  'C 已生效变更未纳入发布（树变了但对外没发布）。**三类都应为 0。**';

-- ---------------------------------------------------------------------------
-- 4. 口径
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_EVO_DOWNSTREAM_GAPS', '职业树下下游一致性缺口数', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(*) FROM v_occupation_downstream_gap)',
 '个', NULL, NULL,
 '三类缺口之和：停用但下游仍引用且无迁移映射 / 迁移关系指向不存在节点 / 已生效变更未纳入发布。'
 '**必须为 0** —— 它衡量"树改了之后，下游是不是还对得上"。',
 '本库当前为 0（还没有任何变更记录）。'
 '⚠ 0 只说明"目前没有不一致"，**不说明"一致性机制被验证过"** —— '
 '要验证它必须真的走一遍"提议 → 发布 → 检查"，见 ops/evolution.py 的 selfcheck。', 1),

('M_EVO_HIGH_IMPACT_NODES', '高影响职业节点数', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(*) FROM v_occupation_impact WHERE impact_risk LIKE ''高%'')',
 '个', NULL, '有岗位样本 / 子节点 / 有效能力权重',
 '改动会**直接影响匹配与推荐**的节点数。这是"改职业树前必须先看影响"的量化依据：'
 '这些节点上的改动若没有迁移映射，下游会立刻失效。',
 '数量多不代表要少改，而代表**改动要走发布流程**（一次发布带上全部迁移映射）。', 1),

('M_EVO_RELEASED_CHANGE_RATE', '变更纳入发布的比例', '供给', 'ratio',
 '(SELECT count(*) FROM occupation_change c WHERE EXISTS '
 '(SELECT 1 FROM occupation_release_item i WHERE i.change_id = c.change_id))',
 '(SELECT count(*) FROM occupation_change)', NULL,
 '%', NULL, NULL,
 '变更记录里**已纳入某次发布**的比例（发布管理的覆盖率）。',
 '分母为 0（还没有任何变更）时返回 **NULL**，不是 0 —— '
 '"还没有可算的对象"与"算出来是 0"是两件事。'
 '**已生效（applied）却未纳入发布的变更**单独由 M_EVO_DOWNSTREAM_GAPS 的 C 类盯住。', 1),

('M_EVO_DATASET_RELEASES', '数据集发布登记数', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(*) FROM dataset_release)',
 '个', NULL, '含职业树变更归属',
 '已登记的数据集发布次数。它是"口径版本"的对外锚点：'
 '职业树变更应当随一次发布一起生效，这样别人能说出"我在用哪一版口径"。',
 '本库当前为 0 —— **还没有任何一次正式发布**。'
 '这意味着任何"我们的口径是怎样的"都只是代码里的现状，而不是一个可引用的版本。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 5. 域登记 + 授权 + 断言
-- ---------------------------------------------------------------------------
INSERT INTO domain_registry (table_name, domain, declared_by, note) VALUES
('occupation_release_item', '演化层', 'migration_072',
 '职业树变更 → 数据集发布的归属关系（发布管理）。')
ON CONFLICT (table_name) DO UPDATE
  SET domain = EXCLUDED.domain, declared_by = EXCLUDED.declared_by, updated_at = now();

SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE r record; n int := 0; v numeric; n_high int;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry WHERE metric_id LIKE 'M_EVO\_%' LOOP
    PERFORM * FROM metric_value(r.metric_id); n := n + 1;
  END LOOP;
  IF n <> 4 THEN RAISE EXCEPTION '演化口径没登记全（%）', n; END IF;

  -- 视图必须可读（新视图忘了对账会连 T3 都读不到，061 踩过）
  FOR r IN SELECT unnest(ARRAY['v_occupation_impact','v_occupation_release_status',
                               'v_occupation_downstream_gap']) AS v LOOP
    IF NOT (has_table_privilege('mt_t3', 'mt.' || r.v, 'SELECT')
         OR has_any_column_privilege('mt_t3', 'mt.' || r.v, 'SELECT')) THEN
      RAISE EXCEPTION '对账后 T3 仍读不到 %', r.v;
    END IF;
  END LOOP;

  -- 影响预览必须**真的能给出依赖数**（不是全 0 的空壳）
  SELECT count(*) INTO n_high FROM v_occupation_impact WHERE impact_risk LIKE '高%';
  IF n_high = 0 THEN
    RAISE EXCEPTION '没有任何高影响节点 —— 151 个节点里必然有带 JD 样本的，说明视图没算对';
  END IF;
  RAISE NOTICE '影响预览：高影响节点 % 个（这些节点改动会直接影响匹配与推荐）', n_high;

  -- 一致性缺口必须能被算出（当前应为 0，且是**算出来的 0** 而不是空视图）
  SELECT count(*) INTO n FROM v_occupation_downstream_gap;
  SELECT count(*) INTO n FROM v_occupation_impact;
  IF n <> 151 THEN RAISE EXCEPTION '影响视图行数 % ≠ 职业节点数 151', n; END IF;

  -- 发布覆盖率：没有变更时必须是 NULL
  SELECT value INTO v FROM metric_value('M_EVO_RELEASED_CHANGE_RATE');
  IF EXISTS (SELECT 1 FROM occupation_change) AND v IS NULL THEN
    RAISE EXCEPTION '已有变更却算不出发布覆盖率';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM occupation_change) AND v IS NOT NULL THEN
    RAISE EXCEPTION '没有任何变更，发布覆盖率却给出 % —— 应为 NULL', v;
  END IF;
  RAISE NOTICE '演化层就绪：3 个视图 + 1 张归属表 + 4 个口径；'
    '发布覆盖率 = %（没有变更时为 NULL）', coalesce(v::text, 'NULL');
END $$;

COMMIT;
