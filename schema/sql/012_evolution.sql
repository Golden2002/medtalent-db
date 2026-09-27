-- ============================================================================
-- 医学生人才信息库 · 演化层 v0.9.0（T13）
--
-- 需求：社会需求与岗位在动态变化，所以
--   ① 职业树的枝叶要能**生长**（新岗位出现、旧的退役、节点拆分与合并）
--   ② 个人画像与建模能力也要**生长**（词表持续扩充）
--   ③ 能力与职业之间的映射要具备**动态性、持续性、扩展性**
--
-- 三条设计原则（贯穿全文件）：
--
--   【P1 时间是第一等维度】任何演化对象都带 valid_from / valid_to，
--       任何结论都能回答"这是什么时候的事实"。禁止原地覆盖。
--
--   【P2 ID 不复用、只退役】一个 occupation_id 一旦用过就永远指同一件事。
--       节点拆分/合并/改名一律走迁移表 occupation_migration，
--       否则历史报告与历史匹配结果会指向错误的含义。
--
--   【P3 提升必须有证据】新岗位/新能力不会因为"有人觉得该有"就进树，
--       必须满足：支撑 JD 条数 ≥ 阈值 + 有评审记录。
--       候选池 candidate_* 是缓冲区，concept/occupation 是正式层。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 给职业树本身加上时间维
-- ---------------------------------------------------------------------------
ALTER TABLE occupation ADD COLUMN IF NOT EXISTS valid_from DATE;
ALTER TABLE occupation ADD COLUMN IF NOT EXISTS valid_to DATE;
ALTER TABLE occupation ADD COLUMN IF NOT EXISTS retired_reason TEXT;
UPDATE occupation SET valid_from = COALESCE(valid_from, DATE '2026-01-01')
WHERE valid_from IS NULL;

CREATE INDEX IF NOT EXISTS idx_occupation_valid ON occupation(valid_from, valid_to);

-- concept 是可增长的词表对象，需要记录"最近一次扩充/修改"时间
ALTER TABLE concept ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- 时点查询：某个日期**当时**存在的职业树
CREATE OR REPLACE FUNCTION occupation_asof(p_as_of DATE) RETURNS SETOF occupation
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT * FROM occupation o
  WHERE o.valid_from <= p_as_of
    AND (o.valid_to IS NULL OR o.valid_to > p_as_of)
  ORDER BY o.level, o.occupation_id;
$$;

-- ---------------------------------------------------------------------------
-- 2. 职业树变更记录（提案 → 决策 → 生效）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS occupation_change (
  change_id        TEXT PRIMARY KEY,
  change_type      TEXT NOT NULL
                     CHECK (change_type IN ('add','split','merge','rename','retire','remap')),
  from_ids         TEXT[],          -- split/merge/retire/rename 的源节点
  to_ids           TEXT[],          -- 产物节点（新 ID，绝不复用旧 ID）
  family           TEXT,
  label_zh         TEXT,
  reason           TEXT,
  evidence_count   INTEGER NOT NULL DEFAULT 0,   -- 支撑 JD 条数
  evidence_sample  JSONB,                        -- 支撑样例（岗位名/来源）
  status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed','approved','rejected','applied','reverted')),
  proposed_by      TEXT,
  decided_by       TEXT,
  decided_at       TIMESTAMPTZ,
  effective_from   DATE,
  note             TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_occ_change_status ON occupation_change(status, created_at DESC);

-- 迁移表：老 ID → 新 ID 的语义映射（拆分/合并/改名后仍可追溯）
CREATE TABLE IF NOT EXISTS occupation_migration (
  migration_id     TEXT PRIMARY KEY,
  change_id        TEXT REFERENCES occupation_change(change_id),
  old_id           TEXT NOT NULL,
  new_id           TEXT NOT NULL,
  relation         TEXT NOT NULL
                     CHECK (relation IN ('renamed','split_into','merged_into','retired')),
  coverage         NUMERIC(4,3),   -- 拆分时：老节点有多大比例落到该新节点（可空）
  effective_from   DATE NOT NULL,
  note             TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (old_id, new_id, relation, effective_from)
);
CREATE INDEX IF NOT EXISTS idx_occ_migration_old ON occupation_migration(old_id);

-- 追溯：给定任一历史 ID，找到它今天对应哪些节点
CREATE OR REPLACE FUNCTION occupation_resolve(p_old_id TEXT) RETURNS TABLE(new_id TEXT, relation TEXT, depth INT)
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  WITH RECURSIVE walk AS (
    SELECT m.new_id, m.relation, 1 AS depth FROM occupation_migration m WHERE m.old_id = p_old_id
    UNION ALL
    SELECT m2.new_id, m2.relation, w.depth + 1
    FROM walk w JOIN occupation_migration m2 ON m2.old_id = w.new_id
    WHERE w.depth < 8
  )
  SELECT DISTINCT walk.new_id, walk.relation, walk.depth FROM walk ORDER BY depth, new_id;
$$;

-- ---------------------------------------------------------------------------
-- 3. 候选池：从真实 JD 里长出来的候选岗位 / 候选能力
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS occupation_candidate (
  candidate_id     TEXT PRIMARY KEY,
  title_key        TEXT NOT NULL,        -- 归一化后的岗位名（聚类键）
  title_sample     TEXT,                 -- 原始样例
  family_guess     TEXT,
  evidence_count   INTEGER NOT NULL DEFAULT 0,   -- 支撑 JD 条数
  sample_job_ids   TEXT[],
  first_seen       DATE NOT NULL DEFAULT CURRENT_DATE,
  last_seen        DATE NOT NULL DEFAULT CURRENT_DATE,
  status           TEXT NOT NULL DEFAULT 'watching'
                     CHECK (status IN ('watching','ready','promoted','rejected','dropped')),
  promoted_to      TEXT,                 -- 提升后的 occupation_id
  note             TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (title_key)
);

CREATE TABLE IF NOT EXISTS concept_candidate (
  candidate_id     TEXT PRIMARY KEY,
  phrase_key       TEXT NOT NULL,        -- 归一化后的能力短语
  phrase_sample    TEXT,
  evidence_count   INTEGER NOT NULL DEFAULT 0,
  sample_requirement_ids TEXT[],
  suggested_concept_id TEXT,             -- 建议挂到哪个既有概念（可空）
  first_seen       DATE NOT NULL DEFAULT CURRENT_DATE,
  last_seen        DATE NOT NULL DEFAULT CURRENT_DATE,
  status           TEXT NOT NULL DEFAULT 'watching'
                     CHECK (status IN ('watching','ready','promoted','rejected','merged','dropped')),
  promoted_to      TEXT,
  note             TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (phrase_key)
);

-- 提升阈值即数据
CREATE TABLE IF NOT EXISTS evolution_policy (
  policy_id        TEXT PRIMARY KEY,
  candidate_kind   TEXT NOT NULL CHECK (candidate_kind IN ('occupation','concept')),
  min_evidence     INTEGER NOT NULL DEFAULT 5,   -- 至少多少条 JD 支撑才算 ready
  min_days_seen    INTEGER NOT NULL DEFAULT 0,   -- 至少观察多少天（0=不要求）
  auto_promote     BOOLEAN NOT NULL DEFAULT false,
  note             TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO evolution_policy (policy_id, candidate_kind, min_evidence, min_days_seen, note) VALUES
 ('ep_occ',     'occupation', 5, 0, '岗位候选：≥5 条 JD 支撑才进入 ready，人工评审后提升'),
 ('ep_concept', 'concept',    3, 0, '能力候选：≥3 条要求支撑才进入 ready')
ON CONFLICT (policy_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. 能力↔职业映射的版本化（现在是"快照式"，改造为"版本式"）
--    现状问题：job_competency_weight 每次重算都 DELETE + 重建，
--    导致①无法回答"半年前这个能力的需求是多少"②无法做漂移分析。
-- ---------------------------------------------------------------------------
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS period_label TEXT;
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS rule_version TEXT;
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS mapping_version TEXT;
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS valid_from DATE;
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS valid_to DATE;
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE job_competency_weight ADD COLUMN IF NOT EXISTS demand_weight NUMERIC(10,3);

-- 关键：原来的 UNIQUE(occupation_id, concept_id) 会**阻止保留历史版本**，必须换掉
ALTER TABLE job_competency_weight DROP CONSTRAINT IF EXISTS job_competency_weight_occupation_id_concept_id_key;
ALTER TABLE job_competency_weight DROP CONSTRAINT IF EXISTS uq_jcw_versioned;
ALTER TABLE job_competency_weight ADD CONSTRAINT uq_jcw_versioned
  UNIQUE (occupation_id, concept_id, run_id);
CREATE INDEX IF NOT EXISTS idx_jcw_current ON job_competency_weight(occupation_id, valid_to);

-- 每次重算 = 一个版本
CREATE TABLE IF NOT EXISTS competency_run (
  run_id           TEXT PRIMARY KEY,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  as_of            DATE NOT NULL,
  period_label     TEXT,                 -- 如 2026H1
  rule_version     TEXT NOT NULL,
  mapping_version  TEXT,
  min_sample       INTEGER,
  occupation_count INTEGER,
  concept_count    INTEGER,
  row_count        INTEGER,
  note             TEXT
);

-- 漂移：相邻两个版本之间，同一 (职业, 能力) 的重要性变化
CREATE TABLE IF NOT EXISTS competency_drift (
  drift_id         TEXT PRIMARY KEY,
  from_run_id      TEXT REFERENCES competency_run(run_id),
  to_run_id        TEXT REFERENCES competency_run(run_id),
  occupation_id    TEXT NOT NULL,
  concept_id       TEXT NOT NULL,
  from_importance  NUMERIC(4,3),
  to_importance    NUMERIC(4,3),
  delta            NUMERIC(5,3),
  drift_type       TEXT NOT NULL
                     CHECK (drift_type IN ('rising','falling','new','vanished','stable')),
  from_essentiality TEXT,
  to_essentiality   TEXT,
  sample_size      INTEGER,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_drift_run ON competency_drift(to_run_id, drift_type);

-- 只取"当前有效"的权重
CREATE OR REPLACE VIEW v_competency_current AS
SELECT * FROM job_competency_weight WHERE valid_to IS NULL;

-- 演化健康度
CREATE OR REPLACE VIEW v_evolution_health AS
SELECT
  (SELECT count(*) FROM occupation WHERE status='active' AND valid_to IS NULL) AS active_nodes,
  (SELECT count(*) FROM occupation WHERE valid_to IS NOT NULL)                 AS retired_nodes,
  (SELECT count(*) FROM occupation_migration)                                  AS migrations,
  (SELECT count(*) FROM occupation_candidate WHERE status='watching')          AS occ_watching,
  (SELECT count(*) FROM occupation_candidate WHERE status='ready')             AS occ_ready,
  (SELECT count(*) FROM concept_candidate    WHERE status='ready')             AS con_ready,
  (SELECT count(*) FROM competency_run)                                        AS runs,
  (SELECT count(*) FROM competency_drift WHERE drift_type IN ('rising','new')) AS rising_signals;

COMMIT;
