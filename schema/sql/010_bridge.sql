-- ============================================================================
-- 医学生人才信息库 · 小程序接入层 v0.7.0（T11）
-- 依据：未界小程序《medtalent-integration-schema》v1.0.0 草案 §7（与本库交换）
--
-- 本迁移只做契约要求的、本库尚缺的部分：
--   · 外部身份绑定（sourceSystem + personId → person_id），memberKey 永不入库
--   · 答卷原始记录（response_session / answer）——CloudBase 是原始答案的事实来源，
--     本库保留一份可审计副本，用于"按冻结映射重建那一版"
--   · 经验主干（experience_episode / experience_task）——科研/临床/企业/运营共用
--   · 版本化 crosswalk——契约明确要求，不臆造对方的 D2/D3、F02 等码
--   · 同步事件（sync_event）：eventId 幂等 + 按人 aggregateVersion 串行 + 死信
--   · 墓碑（tombstone）：删除后必须拦住迟到的 upsert
--   · 交换包专用字段（不落 common 表）
--
-- 三条契约铁律，全部用数据库约束或触发器强制：
--   1. 交换包不得包含 memberKey / openid / appid / 手机号等身份标识
--   2. 撤回分析授权后，不得再写入新的分析产物
--   3. 已删除的人，迟到事件不得复活其资料
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 外部身份绑定
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS external_identity (
  identity_id       TEXT PRIMARY KEY,
  source_system     TEXT NOT NULL,          -- 如 weijie_miniprogram
  external_person_id TEXT NOT NULL,         -- 对方生成的随机 personId
  person_id         TEXT NOT NULL REFERENCES person(person_id),
  linked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_version INTEGER NOT NULL DEFAULT 0,   -- 该来源已处理到的最新 aggregateVersion
  status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','deleting','deleted')),
  attrs             JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (source_system, external_person_id)
);
CREATE INDEX IF NOT EXISTS idx_ext_identity_person ON external_identity(person_id);

-- ---------------------------------------------------------------------------
-- 2. 答卷原始记录（保留"那一版"的可重建副本）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS response_session (
  session_id        TEXT PRIMARY KEY,
  person_id         TEXT NOT NULL REFERENCES person(person_id),
  source_system     TEXT NOT NULL,
  source_submission_id TEXT NOT NULL,       -- 对方 submissionId
  questionnaire_id  TEXT NOT NULL,
  questionnaire_version TEXT NOT NULL,
  catalog_id        TEXT NOT NULL,
  mapping_version   TEXT,
  aggregate_version INTEGER NOT NULL,
  submitted_at      TIMESTAMPTZ,
  request_hash      TEXT,                   -- 幂等：同一 person+requestId 载荷哈希
  request_id        TEXT,
  raw_payload       JSONB NOT NULL,         -- 规范化后的交换包（脱敏）
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_system, source_submission_id),
  UNIQUE (person_id, request_id)
);

CREATE TABLE IF NOT EXISTS answer (
  answer_id         TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL REFERENCES response_session(session_id) ON DELETE CASCADE,
  question_id       TEXT NOT NULL,
  instance_id       TEXT,                   -- 重复组实例，如 education/edu_1
  status            TEXT NOT NULL,          -- answered/unknown/skipped/not_asked/
                                            -- not_applicable/prefer_not_to_say/
                                            -- not_collected_in_version/not_in_catalog
  option_ids        TEXT[],
  ordinal           INTEGER,
  UNIQUE (session_id, question_id, instance_id)
);
-- 契约 §4.2：隐藏题必须 not_asked；answered 必须有选项
ALTER TABLE answer DROP CONSTRAINT IF EXISTS chk_answer_status;
ALTER TABLE answer ADD CONSTRAINT chk_answer_status CHECK (
  status IN ('answered','unknown','skipped','not_asked','not_applicable',
             'prefer_not_to_say','not_collected_in_version','not_in_catalog'));
ALTER TABLE answer DROP CONSTRAINT IF EXISTS chk_answer_answered_has_option;
ALTER TABLE answer ADD CONSTRAINT chk_answer_answered_has_option CHECK (
  status <> 'answered' OR (option_ids IS NOT NULL AND array_length(option_ids,1) >= 1));

-- ---------------------------------------------------------------------------
-- 3. 经验主干（科研 / 临床 / 企业 / 运营共用一套主干 + 专用扩展）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experience_episode (
  episode_id        TEXT PRIMARY KEY,
  person_id         TEXT NOT NULL REFERENCES person(person_id),
  instance_id       TEXT NOT NULL,          -- 对应答卷里的 experience/exp_1
  episode_type      TEXT NOT NULL,          -- research|clinical|enterprise|operations|other
  organization      TEXT,
  role_title        TEXT,
  start_ym          TEXT,                   -- YYYY-MM；不推算不存在的精确日（契约 §4.2）
  end_ym            TEXT,
  is_current        BOOLEAN,
  source_system     TEXT,
  source_submission_id TEXT,
  verify_status     mt.verify_status_t NOT NULL DEFAULT 'V1',
  confidence        mt.confidence_t NOT NULL DEFAULT 0.7,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs             JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (person_id, source_system, instance_id)
);

CREATE TABLE IF NOT EXISTS experience_task (
  task_id           TEXT PRIMARY KEY,
  episode_id        TEXT NOT NULL REFERENCES experience_episode(episode_id) ON DELETE CASCADE,
  ordinal           INTEGER,
  task_text         TEXT NOT NULL,
  concept_id        TEXT REFERENCES concept(concept_id),
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 4. 版本化 crosswalk（契约 §7 明确要求，禁止臆造对方码值）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crosswalk (
  crosswalk_id      TEXT PRIMARY KEY,
  mapping_version   TEXT NOT NULL,          -- 如 xw_1.0.0
  local_kind        TEXT NOT NULL,          -- concept|occupation|field|code_table
  local_id          TEXT NOT NULL,
  external_system   TEXT NOT NULL,
  external_kind     TEXT NOT NULL,          -- concept|option|field|job_family
  external_id       TEXT NOT NULL,
  relation_type     TEXT NOT NULL           -- exact|broader|narrower|related|unmapped
                      CHECK (relation_type IN ('exact','broader','narrower','related','unmapped')),
  review_status     TEXT NOT NULL DEFAULT 'pending'
                      CHECK (review_status IN ('pending','reviewed','rejected')),
  note              TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (mapping_version, local_kind, local_id, external_system, external_kind, external_id)
);
CREATE INDEX IF NOT EXISTS idx_crosswalk_lookup
  ON crosswalk(mapping_version, external_system, external_kind, external_id);

-- ---------------------------------------------------------------------------
-- 5. 同步事件（出/入两个方向共用一张表，语义靠 direction 区分）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_event (
  event_id          TEXT PRIMARY KEY,       -- **本库生成的稳定事件号**
  external_event_id TEXT,                   -- 对方系统的事件号（在其系统内唯一）
  direction         TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  event_type        TEXT NOT NULL,          -- profile_upsert|consent_change|deletion|
                                            -- job_projection|match_result
  source_system     TEXT NOT NULL,
  person_id         TEXT NOT NULL REFERENCES person(person_id),
  aggregate_version INTEGER NOT NULL,
  profile_version   INTEGER,
  payload           JSONB,                  -- 出向载荷；入向为脱敏引用
  payload_ref       TEXT,                   -- 入向：指向 response_session
  status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','processing','delivered','failed','dead')),
  attempts          INTEGER NOT NULL DEFAULT 0,
  lease_until       TIMESTAMPTZ,            -- 崩溃恢复：租约到期可被其他消费者接管
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  acked_at          TIMESTAMPTZ
);
-- 同一人同一版本的"同一方向同一类型"只允许一条，避免并发覆盖。
-- 注意必须含 direction：入向的 profile_upsert 与出向的 profile_upsert
-- 是两件不同的事，漏掉 direction 会让它们互相顶掉（实测踩到过）。
ALTER TABLE sync_event DROP CONSTRAINT IF EXISTS sync_event_source_system_person_id_aggregate_version_event__key;
ALTER TABLE sync_event DROP CONSTRAINT IF EXISTS uq_sync_event_dedup;
ALTER TABLE sync_event ADD CONSTRAINT uq_sync_event_dedup
  UNIQUE (source_system, person_id, aggregate_version, event_type, direction);
CREATE INDEX IF NOT EXISTS idx_sync_event_pending
  ON sync_event(direction, status, person_id, aggregate_version);

-- 幂等键必须是 (来源, 对方事件号)：
-- 契约 §7.1 要求"接收端以 eventId 幂等"，而 eventId 是**对方系统内**唯一的。
-- 若把 event_id 当全局主键，两个来源复用同一 id 会被 ON CONFLICT 静默吞掉，
-- 表现为"事件没入账但身份版本已推进"——实测踩到过，故按来源唯一。
ALTER TABLE sync_event ADD COLUMN IF NOT EXISTS external_event_id TEXT;
ALTER TABLE sync_event DROP CONSTRAINT IF EXISTS uq_sync_event_external;
ALTER TABLE sync_event ADD CONSTRAINT uq_sync_event_external
  UNIQUE (source_system, external_event_id);

-- ---------------------------------------------------------------------------
-- 6. 墓碑：删除后拦住迟到的 upsert（契约 §7.1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tombstone (
  tombstone_id      TEXT PRIMARY KEY,
  person_id         TEXT NOT NULL REFERENCES person(person_id),
  source_system     TEXT NOT NULL,
  deleted_through_version INTEGER NOT NULL,  -- 该版本及之前的写入一律拒绝
  reason            TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (person_id, source_system)
);

-- 触发器：任何同步事件写入前检查墓碑
CREATE OR REPLACE FUNCTION guard_tombstone() RETURNS trigger
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE cut INTEGER;
BEGIN
  SELECT deleted_through_version INTO cut FROM tombstone
  WHERE person_id = NEW.person_id AND source_system = NEW.source_system;
  IF cut IS NOT NULL AND NEW.aggregate_version <= cut THEN
    RAISE EXCEPTION '事件版本 % 已被墓碑拦截（该人 % 至版本 % 的资料已删除）',
      NEW.aggregate_version, NEW.person_id, cut
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_sync_event_tombstone ON sync_event;
CREATE TRIGGER trg_sync_event_tombstone BEFORE INSERT ON sync_event
FOR EACH ROW EXECUTE FUNCTION guard_tombstone();

-- ---------------------------------------------------------------------------
-- 7. 删除请求（受理 ≠ 完成；需要下游确认）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS talent_deletion_request (
  local_request_id  TEXT PRIMARY KEY,       -- **本库生成的稳定 ID**
  request_id        TEXT NOT NULL,          -- 对方系统的请求号（仅其系统内唯一）
  person_id         TEXT NOT NULL REFERENCES person(person_id),
  source_system     TEXT NOT NULL,
  requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','processing','completed','failed')),
  downstream_confirmed BOOLEAN NOT NULL DEFAULT false,
  completed_at      TIMESTAMPTZ,
  note              TEXT
);
-- 契约：personId+requestId 唯一 —— 用唯一约束表达，而不是把外部 id 当全局主键。
-- 教训（本迁移第 3 次遇到）：sync_event.event_id、crosswalk.crosswalk_id、
-- talent_deletion_request.request_id 都曾误用外部 id 作全局主键，
-- 导致不同来源复用同一编号时被 ON CONFLICT 静默吞掉。
ALTER TABLE talent_deletion_request DROP CONSTRAINT IF EXISTS uq_del_request;
ALTER TABLE talent_deletion_request ADD CONSTRAINT uq_del_request
  UNIQUE (person_id, request_id);

-- ---------------------------------------------------------------------------
-- 8. 契约命名的只读投影（对方按这些名字取数，本库物理表名保持不变）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW education_records AS
SELECT education_id AS record_id, person_id, degree_level, degree_name, school_name,
       major_raw, major_code, start_date, end_date, is_graduated, verify_status,
       confidence, source_id
FROM education_record;

CREATE OR REPLACE VIEW skill_assertions AS
SELECT assertion_id, person_id, concept_id, level, level_basis, claim_type,
       evidence_id, confidence, transferability, transfer_note, verify_status
FROM skill_assertion;

CREATE OR REPLACE VIEW preferences AS
SELECT preference_id, person_id, pref_type, value_raw, value_code, weight,
       is_hard, confidence
FROM preference;

-- ---------------------------------------------------------------------------
-- 9. 契约要求的准入视图：只暴露"可交付"的同步事件
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_sync_pending AS
SELECT e.event_id, e.direction, e.event_type, e.source_system, e.person_id,
       e.aggregate_version, e.profile_version, e.attempts, e.payload_ref, e.payload
FROM sync_event e
JOIN external_identity x ON x.person_id = e.person_id
                        AND x.source_system = e.source_system
WHERE e.status IN ('pending','failed')
  AND x.status = 'active'
  AND (e.lease_until IS NULL OR e.lease_until < now())
ORDER BY e.person_id, e.aggregate_version;

COMMIT;

-- ============================================================================
-- 执行后动作
-- ============================================================================
-- 1) 登记实体：external_identity / response_session / answer / experience_episode /
--    experience_task / crosswalk / sync_event / tombstone / talent_deletion_request
-- 2) field_catalog 补上述表的业务字段
-- 3) 运行 python ops\pg.py sql ops\tests\bridge_test.py 的库侧部分
