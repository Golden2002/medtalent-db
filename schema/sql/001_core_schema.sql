-- ============================================================================
-- 医学生人才信息库 · L1 规范层核心 Schema v0.1.0
-- 目标数据库：PostgreSQL 15+（亦兼容 DuckDB 的只读分析视图；SQLite 需降级 JSONB/TEXT[]）
-- 设计依据：docs/01-数据模型设计.md
-- 变更本文件必须同步：schema/catalog/field_catalog.csv + docs/CHANGELOG-schema.md
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS mt;          -- medical talent
SET search_path TO mt, public;

-- ---------------------------------------------------------------------------
-- 0. 全局枚举（用域而非原生 ENUM，便于扩展；取值仍须在 code_table 登记）
--    枚举口径（全库统一）：**列里存代码表里的"码"，不存英文标签**；
--    中英文标签只存在于 code_value.label_zh / label_en，JOIN 词表即得。
--    反面教训：早期域写 ('raw','parsed',...)，而 CT_VERIFY_STATUS 的码是 V0–V4，
--    导致按词表插入 V2 时被域约束拒绝——冒烟测试暴露，已统一为码。
-- ---------------------------------------------------------------------------
CREATE DOMAIN mt.verify_status_t AS TEXT
  CHECK (VALUE IN ('V0','V1','V2','V3','V4'));
CREATE DOMAIN mt.access_tier_t AS TEXT
  CHECK (VALUE IN ('T0','T1','T2','T3'));
CREATE DOMAIN mt.confidence_t AS NUMERIC(3,2)
  CHECK (VALUE >= 0 AND VALUE <= 1);

-- ---------------------------------------------------------------------------
-- 1. 治理域（先建，被其他表引用）
-- ---------------------------------------------------------------------------

-- 数据源登记：任何数据进入本库前必须在此登记
CREATE TABLE source_registry (
  source_id        TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  source_type      TEXT NOT NULL,     -- platform_api | web_scrape | official_doc | partner_feed | survey | interview | manual | derived
  base_url         TEXT,
  license_note     TEXT,              -- 许可/条款/robots 结论
  robots_checked_at DATE,
  credibility      NUMERIC(3,2) CHECK (credibility BETWEEN 0 AND 1),
  evidence_grade   TEXT CHECK (evidence_grade IN ('A','B','C','D')),
                     -- A 官方统计/部委原文 | B 平台聚合统计(含样本量) | C 单条在招JD | D 未证实
  update_freq      TEXT,              -- daily | weekly | monthly | oneoff
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T0',
  status           TEXT NOT NULL DEFAULT 'active',  -- active | paused | retired
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 采集批次
CREATE TABLE ingest_run (
  ingest_run_id    TEXT PRIMARY KEY,
  source_id        TEXT NOT NULL REFERENCES source_registry(source_id),
  started_at       TIMESTAMPTZ NOT NULL,
  finished_at      TIMESTAMPTZ,
  record_count     INTEGER DEFAULT 0,
  error_count      INTEGER DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
  tool_version     TEXT,
  params           JSONB NOT NULL DEFAULT '{}'::jsonb,
  log_path         TEXT
);

-- 逐条血缘（record_uid = '<entity>:<pk>'）
CREATE TABLE provenance (
  provenance_id    TEXT PRIMARY KEY,
  record_uid       TEXT NOT NULL,
  source_id        TEXT REFERENCES source_registry(source_id),
  ingest_run_id    TEXT REFERENCES ingest_run(ingest_run_id),
  source_url       TEXT,
  published_at     TIMESTAMPTZ,
  fetched_at       TIMESTAMPTZ,
  extract_method   TEXT,              -- html_parse | llm_extract | manual_entry | api_json
  extractor_version TEXT,
  evidence_grade   TEXT CHECK (evidence_grade IN ('A','B','C','D')),
  human_verified   BOOLEAN NOT NULL DEFAULT false,
  note             TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_provenance_record ON provenance(record_uid);
CREATE INDEX idx_provenance_run ON provenance(ingest_run_id);

-- 同意管理
CREATE TABLE consent_record (
  consent_id       TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL,     -- 逻辑外键（person 稍后建，避免循环依赖）
  purpose          TEXT NOT NULL,     -- 取值 CT_CONSENT_PURPOSE：CP1匹配/CP2研究报告/CP3产品改进/CP4公开出版/CP5第三方共享
  scope            TEXT,              -- 数据范围描述
  granted_at       TIMESTAMPTZ NOT NULL,
  revoked_at       TIMESTAMPTZ,
  channel          TEXT,              -- web_form | offline | contract
  evidence_uri     TEXT,
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 访问审计（仅记录，不删）
CREATE TABLE access_log (
  access_id        BIGSERIAL PRIMARY KEY,
  actor            TEXT NOT NULL,
  actor_role       TEXT,
  action           TEXT NOT NULL,     -- read | export | query | match_run
  target           TEXT NOT NULL,
  access_tier      mt.access_tier_t,
  row_count        INTEGER,
  purpose          TEXT,
  at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail           JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 变更日志（append-only）
CREATE TABLE change_log (
  change_id        BIGSERIAL PRIMARY KEY,
  at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor            TEXT,
  object_type      TEXT NOT NULL,     -- table | field | code_table | entity | metric
  object_name      TEXT NOT NULL,
  change_type      TEXT NOT NULL,     -- add | promote | deprecate | rename | backfill | fix
  from_version     TEXT,
  to_version       TEXT,
  detail           JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- 2. 语义域：代码表 / 字段目录 / 实体目录 / 概念本体 / 属性登记
-- ---------------------------------------------------------------------------

CREATE TABLE code_table (
  code_table_id    TEXT PRIMARY KEY,  -- 如 CT_OCCUPATION_FAMILY
  name             TEXT NOT NULL,
  description      TEXT,
  hierarchical     BOOLEAN NOT NULL DEFAULT false,
  external_standard TEXT,             -- 职业分类大典2022 | ISCO-08 | SOC | ESCO | 自定义
  version          TEXT NOT NULL DEFAULT '1.0.0',
  status           TEXT NOT NULL DEFAULT 'active',
  owner            TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE code_value (
  code_table_id    TEXT NOT NULL REFERENCES code_table(code_table_id),
  code             TEXT NOT NULL,
  label_zh         TEXT NOT NULL,
  label_en         TEXT,
  definition       TEXT,
  parent_code      TEXT,              -- 层级
  level            INTEGER,
  sort_order       INTEGER,
  aliases          TEXT[],            -- 别名，用于模糊匹配与解析
  external_mapping JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"ISCO-08":"2211","O*NET":"29-1215.00"}
  effective_from   DATE,
  effective_to     DATE,              -- NULL = 现行
  deprecated       BOOLEAN NOT NULL DEFAULT false,
  replaced_by      TEXT,
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (code_table_id, code)
);
CREATE INDEX idx_code_value_label ON code_value(code_table_id, label_zh);
CREATE INDEX idx_code_value_parent ON code_value(code_table_id, parent_code);

-- 实体注册表：新增实体只需登记，不必改核心代码
CREATE TABLE entity_catalog (
  entity_id        TEXT PRIMARY KEY,  -- 如 job_posting
  table_name       TEXT NOT NULL,
  domain           TEXT NOT NULL,     -- talent | opportunity | semantic | governance
  description      TEXT,
  id_prefix        TEXT NOT NULL,
  schema_version   TEXT NOT NULL DEFAULT '1.0.0',
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T1',
  status           TEXT NOT NULL DEFAULT 'active',   -- active | deprecated | proposed
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 字段目录（对标 UK Biobank field / Schema 1 的 27 属性）：变量级数据字典，
-- 是"数据字典即代码"的落点。本表本身可导出为 CSV/JSON-LD 并进版本控制。
CREATE TABLE field_catalog (
  field_id         TEXT PRIMARY KEY,  -- 如 F_PERSON_EDU_LEVEL
  entity_id        TEXT NOT NULL REFERENCES entity_catalog(entity_id),
  title            TEXT NOT NULL,
  description      TEXT,
  -- 取值组（对标 UKB value_type / base_type / item_type）
  data_type        TEXT NOT NULL,     -- string|text|integer|number|date|datetime|boolean|code|array|json
  value_type       TEXT,              -- 11 int | 21 single_choice | 22 multi_choice | 31 real | 41 string | 51 date | 61 time | 101 compound | 201 blob
  item_type        TEXT DEFAULT 'data', -- data | sample | bulk | record
  unit             TEXT,
  value_range      TEXT,
  code_table_id    TEXT REFERENCES code_table(code_table_id),
  -- 结构组（对标 UKB instanced / arrayed）
  cardinality      TEXT NOT NULL DEFAULT 'scalar',   -- scalar | array
  is_arrayed       BOOLEAN NOT NULL DEFAULT false,
  array_min        SMALLINT,
  array_max        SMALLINT,
  instanced        SMALLINT NOT NULL DEFAULT 0,      -- 0 无 | 1 固定 | 2 可变
  instance_role    TEXT,              -- 该字段在多次采集中代表什么：应届|规培期|在职|历次测评
  -- 分层与稳定性
  strata           TEXT NOT NULL DEFAULT 'primary',  -- primary | auxiliary | derived
  stability        TEXT NOT NULL DEFAULT 'updateable', -- complete | updateable | accruing | variable
  -- 采集与来源
  collection_method TEXT,             -- questionnaire|interview|resume_parse|jd_parse|api|derived|manual|system
  applies_to       TEXT,
  related_fields   TEXT[],
  derivation       TEXT,              -- 派生字段：算法与输入字段
  -- 分类与可见性（对标 UKB main_category / private / debut / showcase_order）
  main_category_id TEXT,              -- 指向 category_node
  is_private       BOOLEAN NOT NULL DEFAULT false,
  debut_version    TEXT,
  showcase_order   INTEGER,
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T1',  -- 默认层级；细粒度策略见 access_policy
  status           TEXT NOT NULL DEFAULT 'active',
  version          TEXT NOT NULL DEFAULT '1.0.0',
  owner            TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_field_catalog_entity ON field_catalog(entity_id, status);

-- 属性登记（机制 B 的唯一入口）
CREATE TABLE attribute_definition (
  attr_key         TEXT PRIMARY KEY,  -- 如 JOB_LODGING_PROVIDED
  entity_id        TEXT NOT NULL REFERENCES entity_catalog(entity_id),
  title            TEXT NOT NULL,
  data_type        TEXT NOT NULL,
  unit             TEXT,
  code_table_id    TEXT REFERENCES code_table(code_table_id),
  value_range      TEXT,
  indexed          BOOLEAN NOT NULL DEFAULT false,   -- true → 应提升为正式列
  status           TEXT NOT NULL DEFAULT 'active',   -- active|proposed|promoted|deprecated
  promoted_to_field TEXT REFERENCES field_catalog(field_id),
  version          TEXT NOT NULL DEFAULT '1.0.0',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 统一概念本体：技能/知识/能力/任务/技术/兴趣/风格/情境/价值观
CREATE TABLE concept (
  concept_id       TEXT PRIMARY KEY,  -- con_...
  concept_type     TEXT NOT NULL,     -- skill|knowledge|ability|task|technology|interest|work_style|work_context|value
  preferred_label  TEXT NOT NULL,
  label_en         TEXT,
  definition       TEXT,
  alt_labels       TEXT[],
  parent_id        TEXT REFERENCES concept(concept_id),
  level            INTEGER,
  reusability      TEXT,              -- transversal | cross_sector | occupation_specific（对标 ESCO）
  external_mapping JSONB NOT NULL DEFAULT '{}'::jsonb, -- {"ESCO":"...","O*NET":"2.A.1.a"}
  esco_uri         TEXT,              -- ESCO 技能 URI 作全球唯一主键（data.europa.eu/esco/skill/{uuid}），本地仅加中文标签
  onet_element_id  TEXT,              -- O*NET 元素 ID（如 2.B.1.a 可迁移技能）
  market_demand    NUMERIC(6,3),      -- 派生：需求热度，由 job_requirement 聚合回填
  status           TEXT NOT NULL DEFAULT 'active',
  version          TEXT NOT NULL DEFAULT '1.0.0',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_concept_type ON concept(concept_type, status);
CREATE INDEX idx_concept_label ON concept(preferred_label);

CREATE TABLE concept_relation (
  relation_id      TEXT PRIMARY KEY,
  source_id        TEXT NOT NULL REFERENCES concept(concept_id),
  relation_type    TEXT NOT NULL,     -- broader|narrower|related|essential_for|substitute_of|prerequisite_of
  target_id        TEXT NOT NULL REFERENCES concept(concept_id),
  weight           NUMERIC(4,3),
  evidence         TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_concept_rel_src ON concept_relation(source_id, relation_type);

-- ---------------------------------------------------------------------------
-- 3. 人才域
-- ---------------------------------------------------------------------------

CREATE TABLE person (
  person_id        TEXT PRIMARY KEY,  -- per_<ULID>
  subject_code     TEXT UNIQUE,       -- 对外展示的假名编号，如 MT-2026-000123
  schema_version   TEXT NOT NULL DEFAULT '1.0.0',
  status           TEXT NOT NULL DEFAULT 'active',  -- active|archived|withdrawn
  enroll_channel   TEXT,              -- self_signup|partner|research|import
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T1',
  source_id        TEXT REFERENCES source_registry(source_id),
  ingest_run_id    TEXT REFERENCES ingest_run(ingest_run_id),
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  quality_flags    TEXT[] NOT NULL DEFAULT '{}',
  valid_from       DATE,
  valid_to         DATE,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- PII 独立隔离：加密存储、T3、默认不可 join 出库
CREATE TABLE person_pii (
  person_id        TEXT PRIMARY KEY REFERENCES person(person_id),
  full_name_enc    BYTEA,
  phone_enc        BYTEA,
  email_enc        BYTEA,
  wechat_enc       BYTEA,
  id_hash          TEXT,              -- 证件号 HMAC，用于去重不做还原
  emergency_contact_enc BYTEA,
  encryption_key_id TEXT NOT NULL,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE person_demographics (
  person_id        TEXT PRIMARY KEY REFERENCES person(person_id),
  birth_year       INTEGER CHECK (birth_year BETWEEN 1930 AND 2020),
  sex              TEXT,              -- code: CT_SEX
  hukou_province   TEXT,              -- code: CT_REGION
  hukou_type       TEXT,              -- 城镇/农村
  nationality      TEXT,
  ethnicity        TEXT,
  political_status TEXT,
  marital_status   TEXT,
  health_limits    TEXT[],            -- 色觉异常/听力/其他（T3，需单独同意）
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE education_record (
  education_id     TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  degree_level     TEXT NOT NULL,     -- 专科|本科|硕士|博士|博士后（CT_DEGREE_LEVEL）
  degree_name      TEXT,              -- 医学学士/临床医学硕士/学术型博士…
  school_name      TEXT,
  school_id        TEXT,              -- 指向 school 维表/代码表
  school_tags      TEXT[],            -- 985|211|双一流|海外QS100|军医院校
  major_raw        TEXT,              -- 原文专业名
  major_code       TEXT,              -- 映射《学科专业目录》代码
  is_clinical      BOOLEAN,           -- 是否临床医学类
  start_date       DATE, end_date DATE,
  is_graduated     BOOLEAN,
  gpa              NUMERIC(4,2), gpa_scale NUMERIC(4,2),
  rank_percentile  NUMERIC(5,2),
  supervisor_note  TEXT,
  joint_program    TEXT,              -- 联合培养/交换
  overseas         BOOLEAN DEFAULT false,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  quality_flags    TEXT[] NOT NULL DEFAULT '{}',
  source_id        TEXT REFERENCES source_registry(source_id),
  valid_from DATE, valid_to DATE,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_edu_person ON education_record(person_id);

CREATE TABLE training_record (
  training_id      TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  training_type    TEXT NOT NULL,     -- 规培|专培|进修|住院总|轮转|实习|进修班（CT_TRAINING_TYPE）
  institution      TEXT,
  department       TEXT,
  start_date DATE, end_date DATE,
  months           NUMERIC(4,1),
  is_completed     BOOLEAN,
  certificate_ref  TEXT,              -- 指向 credential
  note             TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_training_person ON training_record(person_id);

CREATE TABLE clinical_exposure (
  exposure_id      TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  department       TEXT,
  department_code  TEXT,
  procedure_count  INTEGER,           -- 主刀/一助例数
  case_volume      INTEGER,
  skills           TEXT[],            -- 具体操作能力（概念 ID 或原文）
  duration_months  NUMERIC(4,1),
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE credential (
  credential_id    TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  credential_type  TEXT NOT NULL,     -- 执业资格|规培证|专培证|GCP|执业药师|语言|计算机|其他（CT_CREDENTIAL_TYPE）
  name             TEXT NOT NULL,
  issuing_body     TEXT,
  credential_no_hash TEXT,
  obtained_date    DATE,
  expires_date     DATE,
  status           TEXT,              -- 有效|过期|备考中|未通过
  is_required_for  TEXT[],            -- 该证对哪些岗位族是硬性（概念/代码）
  confidence       mt.confidence_t NOT NULL DEFAULT 0.9,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  evidence_id      TEXT,              -- 循环引用：应用层校验
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_cred_person ON credential(person_id);

CREATE TABLE employment_record (
  employment_id    TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  employer_name    TEXT,
  employer_id      TEXT,
  employer_type    TEXT,              -- CT_EMPLOYER_TYPE
  occupation_id    TEXT,              -- 指向 occupation
  title_raw        TEXT,
  title_normalized TEXT,
  level            TEXT,
  start_date DATE, end_date DATE,
  is_current       BOOLEAN,
  duties           TEXT[],
  achievements     TEXT,
  leave_reason     TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  valid_from DATE, valid_to DATE,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_emp_person ON employment_record(person_id);

CREATE TABLE research_output (
  output_id        TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  output_type      TEXT NOT NULL,     -- 论著|综述|病例报告|meta|专利|基金|会议|审稿|软著（CT_RESEARCH_OUTPUT_TYPE）
  title            TEXT,
  venue            TEXT,
  doi              TEXT,
  pmid             TEXT,
  year             INTEGER,
  author_position  TEXT,              -- 第一|共一|通讯|第二|其他
  is_first_author  BOOLEAN,
  if_value         NUMERIC(6,3),
  jcr_quartile     TEXT,
  cas_quartile     TEXT,              -- 中科院分区
  citation_count   INTEGER,
  funding_source   TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.85,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_research_person ON research_output(person_id);

CREATE TABLE project_record (
  project_id       TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  project_type     TEXT,              -- 国自然|省级|横向|临床研究|企业项目|学生创新
  name             TEXT,
  role             TEXT,
  scale_note       TEXT,              -- 经费/样本量/团队规模
  outcome          TEXT,
  start_date DATE, end_date DATE,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE award_honor (
  award_id         TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  name             TEXT NOT NULL,
  level            TEXT,              -- 国家级|省级|校级|院级|国际
  year             INTEGER,
  rank             TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE preference (
  preference_id    TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  pref_type        TEXT NOT NULL,     -- city|industry|occupation_family|salary|intensity|stability|growth|remote
  value_raw        TEXT,
  value_code       TEXT,
  weight           NUMERIC(4,3),      -- 偏好权重（匹配用）
  is_hard          BOOLEAN DEFAULT false,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.7,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 注意：表名不能叫 constraint —— CONSTRAINT 是 PostgreSQL 保留字。
CREATE TABLE person_constraint (
  constraint_id    TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  constraint_type  TEXT NOT NULL,     -- 户籍|地域|脱产|体检|签证|家庭|时间
  description      TEXT,
  is_blocking      BOOLEAN DEFAULT true,
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T3',
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 证据：一切能力主张的依据
CREATE TABLE evidence (
  evidence_id      TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  evidence_type    TEXT NOT NULL,     -- 证书|论文|作品|项目|他人评价|测评|自述|临床记录
  source_party     TEXT,              -- 证据来源方，取值 CT_EVIDENCE_SOURCE_TYPE：ES1本人|ES2第三方机构|ES3雇主|ES4同行/上级
  title            TEXT,
  uri              TEXT,              -- 文件路径/URL/DOI
  verifiability    INTEGER CHECK (verifiability BETWEEN 0 AND 5), -- 0 不可核验 5 第三方可查
  cel_level        TEXT,              -- CEL 证据等级：E0 自述 | E1 证书 | E2 第三方评估 | E3 作品 | E4 履职记录
  verifier         TEXT,
  verified_at      TIMESTAMPTZ,
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T2',
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_evidence_person ON evidence(person_id);

-- ★ 能力主张：本项目最核心的表
CREATE TABLE skill_assertion (
  assertion_id     TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  concept_id       TEXT NOT NULL REFERENCES concept(concept_id),
  level            SMALLINT CHECK (level BETWEEN 0 AND 5),
  level_basis      TEXT,              -- 等级判定依据，取值 CT_LEVEL_BASIS：LB1自述|LB2测评|LB3证书|LB4产出|LB5上级或同行|LB6临床例数
  claim_type       TEXT,              -- 主张类型，取值 CT_CLAIM_TYPE：CT1能力|CT2成就|CT3潜质
  essentiality     TEXT,              -- essential|bonus|neutral
  evidence_id      TEXT REFERENCES evidence(evidence_id),
  confidence       mt.confidence_t NOT NULL DEFAULT 0.5,
  transferability  SMALLINT CHECK (transferability BETWEEN 0 AND 4),
                     -- 可迁移性，取值 CT_TRANSFERABILITY：X0不可迁移 … X4通用能力（本库差异化字段）
  transfer_note    TEXT,              -- 迁移说明：>2 级必须填写，如"外科住院总的应急决策→项目危机处理"
  first_observed_at DATE,
  last_verified_at TIMESTAMPTZ,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT chk_assertion_evidence CHECK (evidence_id IS NOT NULL OR confidence <= 0.5)
);
CREATE INDEX idx_assertion_person ON skill_assertion(person_id);
CREATE INDEX idx_assertion_concept ON skill_assertion(concept_id);

CREATE TABLE assessment (
  assessment_id    TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  instrument       TEXT NOT NULL,     -- 工具名
  dimension        TEXT,
  score            NUMERIC(8,3),
  norm_group       TEXT,              -- 常模
  percentile       NUMERIC(5,2),
  taken_at         DATE,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  source_id        TEXT REFERENCES source_registry(source_id),
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE narrative (
  narrative_id     TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  narrative_type   TEXT NOT NULL,     -- resume_raw|personal_statement|interview_transcript|coach_note
  raw_ref          TEXT NOT NULL,     -- 指向 L0 原始文件
  raw_sha256       TEXT NOT NULL,
  language         TEXT,
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T2',
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE trajectory_event (
  event_id         TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  event_type       TEXT,              -- 升学|规培|转行|跳槽|停滞|创业|考公|出国
  event_date       DATE,
  from_state       TEXT, to_state TEXT,
  decision_note    TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.6,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- 4. 机会域
-- ---------------------------------------------------------------------------

CREATE TABLE occupation (
  occupation_id    TEXT PRIMARY KEY,
  -- 职业三码主键（对标研究结论：单一职业码粒度不足，尤其医学专科方向）
  cn_occode        TEXT,              -- 《中华人民共和国职业分类大典（2022）》细类码
  isco08_code      TEXT,              -- ISCO-08（10/43/130/436 四级）
  onet_soc_code    TEXT,              -- O*NET-SOC（含 .xx 细分，医学专科必需）
  code             TEXT,              -- 兼容旧字段：主用码
  code_system      TEXT,
  label_zh         TEXT NOT NULL,
  label_en         TEXT,
  level            INTEGER,           -- 大类1/中类2/小类3/细类4
  parent_id        TEXT REFERENCES occupation(occupation_id),
  family           TEXT,              -- 17 岗位族之一
  medical_reliance SMALLINT CHECK (medical_reliance BETWEEN 0 AND 5),  -- 对医学背景依赖度
  transition_ease  SMALLINT CHECK (transition_ease BETWEEN 0 AND 5),
                     -- 转出容易度 0-5（注意：与 skill_assertion.transferability 的 X0-X4 是两套刻度）
  license_required TEXT[],
  degree_typical   TEXT,
  entry_paths      TEXT[],
  description      TEXT,
  status           TEXT NOT NULL DEFAULT 'active',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_occupation_family ON occupation(family);

CREATE TABLE employer (
  employer_id      TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  name_en          TEXT,
  employer_type    TEXT,              -- 三级医院|基层医疗|药企|CRO|器械|保险|政府|高校|咨询|投资|互联网|创业…
  industry         TEXT,
  ownership        TEXT,              -- 国企|外企|民营|外资|合资|事业单位
  scale            TEXT,
  province         TEXT, city TEXT,
  website          TEXT,
  hiring_active    BOOLEAN,
  note             TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE job_posting (
  job_id           TEXT PRIMARY KEY,
  source_id        TEXT REFERENCES source_registry(source_id),
  ingest_run_id    TEXT REFERENCES ingest_run(ingest_run_id),
  source_url       TEXT,
  source_posting_no TEXT,             -- 原始平台岗位号，用于去重
  employer_id      TEXT REFERENCES employer(employer_id),
  employer_name_raw TEXT,
  title_raw        TEXT NOT NULL,
  title_normalized TEXT,
  occupation_id    TEXT REFERENCES occupation(occupation_id),
  job_family       TEXT,
  level            TEXT,
  job_zone         TEXT,              -- O*NET Job Zone 1-5：入门所需准备度
  parse_confidence mt.confidence_t NOT NULL DEFAULT 0.7,
                     -- 解析置信度：JD 语义模棱两可（如"有经验者优先"）时应降此值，而非硬判 mandatory
  employment_type  TEXT,              -- 全职|实习|兼职|劳务|博士后
  is_campus        BOOLEAN,           -- 是否校招/应届可投
  province         TEXT, city TEXT, work_location TEXT,
  salary_min       NUMERIC(12,2), salary_max NUMERIC(12,2),
  salary_currency  TEXT DEFAULT 'CNY',
  salary_period    TEXT,              -- 月|年|日|面议
  salary_months    INTEGER,           -- 几薪
  education_req    TEXT,
  major_req        TEXT,
  experience_req   TEXT,
  license_req      TEXT[],
  headcount        INTEGER,
  published_at     DATE, deadline DATE,
  raw_text_ref     TEXT NOT NULL,     -- 指向 L0
  raw_sha256       TEXT NOT NULL,
  parse_version    TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.7,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  quality_flags    TEXT[] NOT NULL DEFAULT '{}',
  valid_from DATE, valid_to DATE,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_job_family ON job_posting(job_family, verify_status);
CREATE INDEX idx_job_city ON job_posting(city);
CREATE UNIQUE INDEX uq_job_dedup ON job_posting(source_id, source_posting_no) WHERE source_posting_no IS NOT NULL;

CREATE TABLE job_task (
  task_id          TEXT PRIMARY KEY,
  job_id           TEXT NOT NULL REFERENCES job_posting(job_id) ON DELETE CASCADE,
  task_order       INTEGER,
  task_text        TEXT NOT NULL,
  concept_id       TEXT REFERENCES concept(concept_id),
  frequency        TEXT,              -- 频次描述
  importance       NUMERIC(4,3),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE job_requirement (
  requirement_id   TEXT PRIMARY KEY,
  job_id           TEXT REFERENCES job_posting(job_id) ON DELETE CASCADE,
  occupation_id    TEXT REFERENCES occupation(occupation_id),
  requirement_kind TEXT NOT NULL,     -- hard|soft|bonus
  requirement_type TEXT NOT NULL,     -- education|major|experience|license|skill|knowledge|language|other
  concept_id       TEXT REFERENCES concept(concept_id),
  raw_text         TEXT NOT NULL,
  min_level        SMALLINT,
  essentiality     NUMERIC(4,3),
  substitutable_by JSONB NOT NULL DEFAULT '[]'::jsonb,
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_jobreq_job ON job_requirement(job_id);
CREATE INDEX idx_jobreq_concept ON job_requirement(concept_id);

-- 岗位族 × 概念 → 能力权重矩阵（对标 O*NET importance/level）
CREATE TABLE job_competency_weight (
  weight_id        TEXT PRIMARY KEY,
  occupation_id    TEXT NOT NULL REFERENCES occupation(occupation_id),
  concept_id       TEXT NOT NULL REFERENCES concept(concept_id),
  importance       NUMERIC(4,3) CHECK (importance BETWEEN 0 AND 1),
  level_required   SMALLINT CHECK (level_required BETWEEN 0 AND 5),
  essentiality     TEXT,              -- essential|bonus
  evidence_basis   TEXT,              -- 来源：JD 统计 / 专家评定 / 从业者访谈
  sample_size      INTEGER,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (occupation_id, concept_id)
);

CREATE TABLE transition_case (
  case_id          TEXT PRIMARY KEY,
  person_id        TEXT REFERENCES person(person_id),   -- 可空（公开案例）
  from_occupation  TEXT, to_occupation TEXT NOT NULL,
  from_industry    TEXT, to_industry TEXT,
  bridge_concepts  TEXT[],            -- 桥梁能力
  prerequisites    TEXT,
  years_taken      NUMERIC(3,1),
  outcome_note     TEXT,
  source_url       TEXT,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.6,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE salary_benchmark (
  benchmark_id     TEXT PRIMARY KEY,
  occupation_id    TEXT, job_family TEXT,
  city             TEXT, region_tier TEXT,
  degree_level     TEXT,
  years_experience TEXT,
  p25 NUMERIC(12,2), p50 NUMERIC(12,2), p75 NUMERIC(12,2), p90 NUMERIC(12,2),
  currency         TEXT DEFAULT 'CNY', period TEXT DEFAULT 'month',
  sample_size      INTEGER,
  source_id        TEXT REFERENCES source_registry(source_id),
  as_of            DATE,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.6,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  attrs            JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- 5. 通用断言表（机制 C：探索期缓冲区，必须定期固化或清理）
-- ---------------------------------------------------------------------------
CREATE TABLE assertion (
  assertion_id     TEXT PRIMARY KEY,
  subject_type     TEXT NOT NULL,     -- person|job_posting|occupation|employer|concept
  subject_id       TEXT NOT NULL,
  predicate        TEXT NOT NULL,     -- 自由谓词，如 has_training_in
  object_type      TEXT,              -- concept|code|value|text|person|job_posting
  object_id        TEXT,
  object_value     JSONB,
  polarity         SMALLINT NOT NULL DEFAULT 1,
  confidence       mt.confidence_t NOT NULL DEFAULT 0.5,
  evidence_id      TEXT REFERENCES evidence(evidence_id),
  source_id        TEXT REFERENCES source_registry(source_id),
  valid_from DATE, valid_to DATE,
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  status           TEXT NOT NULL DEFAULT 'provisional'  -- provisional|promoted|discarded
);
CREATE INDEX idx_assertion_subject ON assertion(subject_type, subject_id);
CREATE INDEX idx_assertion_predicate ON assertion(predicate);

-- ---------------------------------------------------------------------------
-- 6. 派生特征与指标
-- ---------------------------------------------------------------------------
CREATE TABLE derived_feature (
  feature_id       TEXT PRIMARY KEY,
  subject_type     TEXT NOT NULL,
  subject_id       TEXT NOT NULL,
  feature_name     TEXT NOT NULL,
  value_numeric    NUMERIC(12,4),
  value_text       TEXT,
  value_json       JSONB,
  algo_version     TEXT NOT NULL,
  inputs_hash      TEXT NOT NULL,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subject_type, subject_id, feature_name, algo_version)
);
CREATE INDEX idx_feature_name ON derived_feature(feature_name);

CREATE TABLE metric_definition (
  metric_id        TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  description      TEXT,
  definition_sql   TEXT NOT NULL,
  grain            TEXT,
  version          TEXT NOT NULL DEFAULT '1.0.0',
  owner            TEXT,
  status           TEXT NOT NULL DEFAULT 'active',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 7. 匹配域
-- ---------------------------------------------------------------------------
CREATE TABLE match_run (
  match_run_id     TEXT PRIMARY KEY,
  algo_version     TEXT NOT NULL,
  params           JSONB NOT NULL DEFAULT '{}'::jsonb,
  person_count     INTEGER,
  job_count        INTEGER,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ,
  status           TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE match_result (
  match_id         TEXT PRIMARY KEY,
  match_run_id     TEXT NOT NULL REFERENCES match_run(match_run_id),
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  target_type      TEXT NOT NULL,     -- occupation | job_posting
  target_id        TEXT NOT NULL,
  score_total      NUMERIC(5,2),
  score_breakdown  JSONB NOT NULL DEFAULT '{}'::jsonb,
  matched_concepts TEXT[],
  gap_concepts     TEXT[],
  explanation      TEXT,              -- 可解释理由，必填
  rank             INTEGER,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_match_person ON match_result(person_id, score_total DESC);

CREATE TABLE gap_analysis (
  gap_id           TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  target_id        TEXT NOT NULL,
  concept_id       TEXT NOT NULL REFERENCES concept(concept_id),
  current_level    SMALLINT, required_level SMALLINT,
  gap_size         SMALLINT,
  remedy_type      TEXT,              -- 课程|证书|项目|实习|论文|转岗
  remedy_detail    TEXT,
  effort_estimate  TEXT,
  priority         SMALLINT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 8. 视图：分析层（星型/长表），供 DuckDB/BI 只读消费
-- ---------------------------------------------------------------------------
CREATE VIEW v_skill_supply AS
SELECT c.concept_id, c.preferred_label, c.concept_type,
       COUNT(DISTINCT sa.person_id) AS person_count,
       AVG(sa.level) AS avg_level,
       AVG(sa.transferability) AS avg_transferability
FROM concept c LEFT JOIN skill_assertion sa ON sa.concept_id = c.concept_id
WHERE c.status = 'active'
GROUP BY 1,2,3;

CREATE VIEW v_skill_demand AS
SELECT c.concept_id, c.preferred_label,
       COUNT(DISTINCT jr.job_id) AS posting_count,
       COUNT(DISTINCT jp.job_family) AS family_count,
       AVG(jr.essentiality) AS avg_essentiality
FROM job_requirement jr
JOIN concept c ON c.concept_id = jr.concept_id
LEFT JOIN job_posting jp ON jp.job_id = jr.job_id
WHERE jr.requirement_type IN ('RT5','RT6')   -- RT5 技能 / RT6 知识（存码，不存标签）
GROUP BY 1,2;

COMMIT;

-- ============================================================================
-- 迁移与演进规则（不在本文件内执行，作为纪律约定）
-- ============================================================================
-- 1) 加字段：优先 attribute_definition + attrs；确需正式列时走 expand→backfill→contract。
-- 2) 加实体：entity_catalog 登记 + 新表 + field_catalog 补全字段 + change_log 记录。
-- 3) 加枚举值：code_value 插行，禁止改 CHECK 约束（除非新建域）。
-- 4) 任何 DROP/重命名：禁止直接执行；必须新增列 + 双写 + 弃用标记（append-only 原则）。
-- 5) attrs 中的键必须存在于 attribute_definition（由入库校验器强制）。
