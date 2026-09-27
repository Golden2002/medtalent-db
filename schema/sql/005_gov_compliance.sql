-- ============================================================================
-- 医学生人才信息库 · 合规与治理对象 v0.3.0
-- 依据：docs/06-合规与数据治理.md §11（DDL 补丁）
-- 设计原则：
--   1. 撤回级联规则、保留期规则一律"数据化"，不允许写在代码的 if-else 里；
--   2. 审计表只追加（REVOKE UPDATE/DELETE），保证溯源不可篡改；
--   3. 主体权利（查阅/更正/删除/撤回同意/解释/拒绝自动化）走同一张台账。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;

-- ---------------------------------------------------------------------------
-- 1. 撤回同意的级联规则（数据化，替代硬编码）
-- ---------------------------------------------------------------------------
CREATE TABLE consent_withdrawal_action (
  purpose          TEXT PRIMARY KEY,     -- 取值 CT_CONSENT_PURPOSE
  must_delete      TEXT[] NOT NULL,      -- 必须删除的对象/表清单
  keep_aggregate   BOOLEAN NOT NULL DEFAULT true,  -- 是否允许保留不可反推的聚合结果
  sla_days         SMALLINT NOT NULL DEFAULT 15,
  basis_note       TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO consent_withdrawal_action (purpose, must_delete, keep_aggregate, sla_days, basis_note) VALUES
 ('CP1', ARRAY['match_result','gap_analysis'], true,  5, '撤回后停止匹配，历史分数删除'),
 ('CP2', ARRAY['{}'],                          true,  5, '仅保留已发布的聚合统计，不回滚'),
 ('CP3', ARRAY['derived_feature'],             true, 15, '删除个体派生特征'),
 ('CP4', ARRAY['{}'],                          true, 15, '已出版内容按出版协议处理'),
 ('CP5', ARRAY['field_value','assertion'],     false, 5, '第三方共享撤回需同步通知接收方')
ON CONFLICT (purpose) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. 主体权利台账（PIPL 第 45/46/47 条、GDPR 第 15-22 条）
-- ---------------------------------------------------------------------------
CREATE TABLE subject_request (
  request_id       TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  request_type     TEXT NOT NULL,      -- access | rectify | delete | withdraw_consent | explain | object_automated
  received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  due_at           TIMESTAMPTZ,        -- 由 SLA 计算（默认 15 个工作日）
  handled_at       TIMESTAMPTZ,
  outcome          TEXT,               -- fulfilled | partially_fulfilled | rejected
  reject_basis     TEXT,               -- 拒绝的法律依据（必须可引用）
  handler          TEXT,
  detail           JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (person_id, request_type, received_at)
);
CREATE INDEX idx_subject_request_open ON subject_request(due_at) WHERE handled_at IS NULL;

-- ---------------------------------------------------------------------------
-- 3. 保留期即数据（PIPL 第 19 条最小必要与最短期限）
-- ---------------------------------------------------------------------------
CREATE TABLE retention_policy (
  policy_id        TEXT PRIMARY KEY,
  entity_name      TEXT NOT NULL,      -- 表名或逻辑对象名
  field_pattern    TEXT,               -- 为空表示整行；非空表示列级保留
  retain_days      INTEGER NOT NULL CHECK (retain_days > 0),
  action           TEXT NOT NULL CHECK (action IN ('RA1','RA2','RA3')),
                     -- 存码：RA1 删除 / RA2 匿名化 / RA3 归档（CT_RETENTION_ACTION）
  legal_hold       BOOLEAN NOT NULL DEFAULT false,  -- 涉诉/审计时冻结
  basis_note       TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO retention_policy (policy_id, entity_name, field_pattern, retain_days, action, basis_note) VALUES
 ('rp_pii_contact',  'person_pii',        'phone_enc,email_enc,wechat_enc', 30,  'RA1', '联系方式仅在服务期内保留'),
 ('rp_resume_raw',   'narrative',         NULL, 90,  'RA2', '原始简历文本 90 天后去标识化'),
 ('rp_match',        'match_result',      NULL, 730, 'RA1', '匹配结果保留 24 个月'),
 ('rp_access_log',   'access_log',        NULL, 1095,'RA3', '审计日志保留 36 个月'),
 ('rp_change_log',   'change_log',        NULL, 1825,'RA3', '变更流水保留 60 个月'),
 ('rp_subject_req',  'subject_request',   NULL, 1095,'RA3', '权利请求台账保留 36 个月')
ON CONFLICT (policy_id) DO NOTHING;

CREATE TABLE data_lifecycle_run (
  run_id           TEXT PRIMARY KEY,
  policy_id        TEXT NOT NULL REFERENCES retention_policy(policy_id),
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ,
  affected_rows    INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'running',  -- running | ok | skipped_legal_hold | failed
  error_note       TEXT
);

-- ---------------------------------------------------------------------------
-- 4. 数据泄露事件留痕（五阶段：发现-遏制-评估-通知-复盘）
-- ---------------------------------------------------------------------------
CREATE TABLE incident_report (
  incident_id      TEXT PRIMARY KEY,
  discovered_at    TIMESTAMPTZ NOT NULL,
  severity         TEXT NOT NULL CHECK (severity IN ('SV1','SV2','SV3','SV4')),
                     -- 存码：SV1 低 / SV2 中 / SV3 高 / SV4 严重（CT_SEVERITY）
  tiers_hit        TEXT[],             -- 涉及的访问层级 T0..T3
  fields_hit       TEXT[],             -- 涉及的 field_id
  subject_count    INTEGER,
  cause_note       TEXT,
  contained_at     TIMESTAMPTZ,
  assessed_at      TIMESTAMPTZ,
  notified_subjects_at   TIMESTAMPTZ,
  notified_regulator_at  TIMESTAMPTZ,
  postmortem_uri   TEXT,
  status           TEXT NOT NULL DEFAULT 'open'
);

-- ---------------------------------------------------------------------------
-- 5. 来源下架时间（合规响应：收到要求后可按来源一键下架）
-- ---------------------------------------------------------------------------
ALTER TABLE source_registry ADD COLUMN IF NOT EXISTS downloaded_at TIMESTAMPTZ;
ALTER TABLE source_registry ADD COLUMN IF NOT EXISTS takedown_reason TEXT;

-- ---------------------------------------------------------------------------
-- 6. 字段级访问策略种子（先补 person_pii 字段登记，见 field_catalog_seed.csv）
-- ---------------------------------------------------------------------------
INSERT INTO access_policy (policy_id, object_type, object_id, action, min_tier) VALUES
 ('ap_hl_agg',     'field','F_PERSON_HEALTH_LIMITS','export_aggregate','T2'),
 ('ap_hl_view',    'field','F_PERSON_HEALTH_LIMITS','view_inline',     'T3'),
 ('ap_hl_row',     'field','F_PERSON_HEALTH_LIMITS','export_row',      'X'),
 ('ap_pii_name_v', 'field','F_PERSON_PII_NAME',     'view_inline',     'T3'),
 ('ap_pii_name_r', 'field','F_PERSON_PII_NAME',     'export_row',      'X'),
 ('ap_pii_cont_v', 'field','F_PERSON_PII_CONTACT',  'view_inline',     'T3'),
 ('ap_pii_cont_r', 'field','F_PERSON_PII_CONTACT',  'export_row',      'X'),
 ('ap_pii_id_v',   'field','F_PERSON_PII_ID_HASH',  'view_inline',     'T3'),
 ('ap_pii_id_r',   'field','F_PERSON_PII_ID_HASH',  'export_row',      'X'),
 ('ap_jobraw_row', 'field','F_JOB_RAW_REF',         'export_row',      'X')
ON CONFLICT (object_type, object_id, action) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 7. 审计加固：审计与变更流水只追加
-- ---------------------------------------------------------------------------
REVOKE UPDATE, DELETE ON access_log FROM PUBLIC;
REVOKE UPDATE, DELETE ON change_log FROM PUBLIC;

COMMIT;

-- ============================================================================
-- 执行后动作
-- ============================================================================
-- 1) entity_catalog 登记：consent_withdrawal_action、subject_request、
--    retention_policy、data_lifecycle_run、incident_report
-- 2) field_catalog 登记上述各表的业务字段（含 person_pii 的 4 个字段）
-- 3) SELECT * FROM access_policy; 抽查 10 条策略
