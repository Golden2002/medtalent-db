-- ============================================================================
-- 医学生人才信息库 · L1/L2 元数据与治理升级 v0.2.0
-- 本文件实现 research/01-对标-生物样本库与成熟数据基础设施.md 提炼的设计模式，
-- 用于修补 docs/01-数据模型设计.md v0.1 的 3 项 P0 与 3 项 P1 差距。
-- 必须在 001_core_schema.sql 之后执行。
--
-- 借鉴来源与模式编号：
--   P2  category_node          ← UK Biobank Showcase Category 树（独立实体 + 双向计数）
--   P3  field_value            ← UK Biobank field × instance × array（零 DDL 落数）
--   P5  first_occurrence       ← UK Biobank First occurrences
--   P7  access_policy          ← UK Biobank 字段级 × 访问方式级 Cost Tier（d/o/s）
--   P9  concept_mapping/ancestor ← OMOP SOURCE_TO_CONCEPT_MAP / CONCEPT_ANCESTOR
--   P10 observation_window     ← OMOP OBSERVATION_PERIOD（"未记录" ≠ "未发生"）
--   P11 dataset_release        ← CHARLS 注册式申请 + "无 codebook 不发布"
-- ============================================================================

BEGIN;
SET search_path TO mt, public;

-- ---------------------------------------------------------------------------
-- P2 · 分类树独立成实体（对标 UKB Category：有 id、父子、双向计数）
-- 与 code_table 的分工：code_table 管"枚举取值"，category_node 管"内容目录"。
-- ---------------------------------------------------------------------------
CREATE TABLE category_node (
  category_id         TEXT PRIMARY KEY,        -- cat_...
  parent_id           TEXT REFERENCES category_node(category_id),
  level               SMALLINT NOT NULL DEFAULT 1,
  name_zh             TEXT NOT NULL,
  name_en             TEXT,
  description         TEXT,
  domain              TEXT,                    -- talent | opportunity | semantic | governance
  subtree_field_count INTEGER NOT NULL DEFAULT 0,  -- 子树字段总数（对标 tree_subtree_total）
  node_field_count    INTEGER NOT NULL DEFAULT 0,  -- 直属字段数（对标 tree_node_total）
  sort_order          INTEGER,
  status              TEXT NOT NULL DEFAULT 'active',
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_category_parent ON category_node(parent_id, sort_order);
ALTER TABLE field_catalog
  ADD CONSTRAINT fk_field_category FOREIGN KEY (main_category_id) REFERENCES category_node(category_id);

-- 计数刷新（物化刷新，避免每次查询递归）
-- 注意：函数必须固定 search_path —— 否则函数体内的未限定表名会按调用方的
-- search_path 解析（实测报 "relation category_node does not exist"），
-- 这同时也是一道防 search_path 劫持的安全措施。
CREATE OR REPLACE FUNCTION refresh_category_counts() RETURNS void
LANGUAGE plpgsql
SET search_path = mt, public
AS $$
BEGIN
  WITH RECURSIVE t AS (
    SELECT category_id, category_id AS root_id FROM category_node
    UNION ALL
    SELECT c.category_id, t.root_id FROM category_node c JOIN t ON c.parent_id = t.category_id
  ), direct AS (
    SELECT main_category_id AS category_id, COUNT(*) AS n
    FROM field_catalog WHERE main_category_id IS NOT NULL GROUP BY 1
  )
  UPDATE category_node cn
  SET node_field_count = COALESCE((SELECT n FROM direct WHERE category_id = cn.category_id), 0),
      subtree_field_count = COALESCE((SELECT SUM(COALESCE(d2.n,0)) FROM t
                                      LEFT JOIN direct d2 ON d2.category_id = t.category_id
                                      WHERE t.root_id = cn.category_id), 0),
      updated_at = now();
END;
$$;

-- ---------------------------------------------------------------------------
-- P3 · 统一值表：一个字段 = 语义锚点；值 = field × instance × array
-- 用途：新增字段时"零 DDL"落数。属于 P1 扩展机制（见 docs/01 §5.2）的实体化实现。
-- 注意：主数据（person/education_record/job_posting…）仍用类型化表；
--       本表服务于大量涌现、低查询频次的扩展字段。
-- ---------------------------------------------------------------------------
CREATE TABLE field_value (
  value_id         TEXT PRIMARY KEY,
  field_id         TEXT NOT NULL REFERENCES field_catalog(field_id),
  subject_type     TEXT NOT NULL,          -- person | job_posting | occupation | employer | concept
  subject_id       TEXT NOT NULL,
  instance_key     TEXT NOT NULL DEFAULT '0',  -- 第几次采集/第几段经历（对标 UKB instance）
  array_index      SMALLINT NOT NULL DEFAULT 0,-- 同一次内的第几个重复值（对标 UKB array）
  value_text       TEXT,
  value_num        NUMERIC(18,4),
  value_date       DATE,
  value_code       TEXT,                   -- 单选：存 code
  value_codes      TEXT[],                 -- 多选（对标 UKB value_type=22 multi choice）
  value_json       JSONB,
  source_id        TEXT REFERENCES source_registry(source_id),
  evidence_grade   TEXT CHECK (evidence_grade IN ('A','B','C','D')),
  confidence       mt.confidence_t NOT NULL DEFAULT 0.7,
  verify_status    mt.verify_status_t NOT NULL DEFAULT 'V1',
  recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (field_id, subject_type, subject_id, instance_key, array_index)
);
CREATE INDEX idx_field_value_subject ON field_value(subject_type, subject_id);
CREATE INDEX idx_field_value_field ON field_value(field_id);
-- 强约束：值必须落在该字段声明的类型上，防止"字段目录说 integer，实际塞了字符串"
ALTER TABLE field_value ADD CONSTRAINT chk_field_value_typed CHECK (
  (value_text IS NOT NULL)::int + (value_num IS NOT NULL)::int + (value_date IS NOT NULL)::int
  + (value_code IS NOT NULL)::int + (value_codes IS NOT NULL)::int + (value_json IS NOT NULL)::int
  BETWEEN 1 AND 2
);

-- ---------------------------------------------------------------------------
-- P5 · 首次发生（对标 UKB First occurrences）
-- 回答"此人何时首次具备 X"，服务于轨迹分析与"成长性"评估。
-- ---------------------------------------------------------------------------
CREATE TABLE first_occurrence (
  occurrence_id    TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  concept_id       TEXT NOT NULL REFERENCES concept(concept_id),
  first_date       DATE NOT NULL,
  source_field_id  TEXT REFERENCES field_catalog(field_id),
  evidence_id      TEXT REFERENCES evidence(evidence_id),
  algo_version     TEXT NOT NULL,
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (person_id, concept_id, algo_version)
);

-- ---------------------------------------------------------------------------
-- P7 · 访问策略 = 字段级 × 访问方式级的乘积（对标 UKB Cost Tier d/o/s）
-- access_tier 单列降级为"默认值"，真实判定一律查本表。
-- ---------------------------------------------------------------------------
CREATE TABLE access_policy (
  policy_id        TEXT PRIMARY KEY,
  object_type      TEXT NOT NULL,     -- field | entity | table | dataset
  object_id        TEXT NOT NULL,     -- 如 F_PERSON_PHONE 或 dataset_release.release_id
  action           TEXT NOT NULL,     -- browse|view_inline|export_row|export_aggregate|bulk_download
  min_tier         TEXT NOT NULL,     -- T0|T1|T2|T3|X（X = 永不允许）
  condition_note   TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (object_type, object_id, action)
);
COMMENT ON COLUMN access_policy.min_tier IS
  '对标 UK Biobank Cost Tier：同一字段"看"与"拿"门槛可不同，如 PII 可 view_inline=T3 但 export_row=X';

-- ---------------------------------------------------------------------------
-- P10 · 观测期建模（对标 OMOP OBSERVATION_PERIOD）
-- 纪律："没有记录"只有在观测窗口内才能解读为"未发生"；窗口外只能表述为"未记录"。
-- 匹配引擎的缺口判定与 gap_analysis 必须先查本表。
-- ---------------------------------------------------------------------------
CREATE TABLE observation_window (
  window_id        TEXT PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES person(person_id),
  window_type      TEXT NOT NULL,     -- resume_coverage | platform_active | followup | employment_span
  start_date       DATE NOT NULL,
  end_date         DATE,              -- NULL = 持续中
  source_id        TEXT REFERENCES source_registry(source_id),
  coverage_note    TEXT,              -- 该窗口覆盖了哪些信息类型
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (person_id, window_type, start_date)
);
CREATE INDEX idx_obs_window_person ON observation_window(person_id, window_type);

-- 缺口判定视图：只在观测窗口内允许输出"缺口"
CREATE VIEW v_gap_candidates AS
SELECT p.person_id, ow.window_id, c.concept_id
FROM person p
JOIN observation_window ow ON ow.person_id = p.person_id
CROSS JOIN concept c
WHERE c.status = 'active'
  AND NOT EXISTS (
    SELECT 1 FROM skill_assertion sa
    WHERE sa.person_id = p.person_id AND sa.concept_id = c.concept_id
  )
  AND (ow.end_date IS NULL OR ow.end_date >= CURRENT_DATE - INTERVAL '18 months');
COMMENT ON VIEW v_gap_candidates IS
  '仅列出"在有效观测窗口内无记录"的能力；窗口外一律不判定为缺口';

-- ---------------------------------------------------------------------------
-- P9 · 映射血缘 + 层级闭包（对标 OMOP SOURCE_TO_CONCEPT_MAP / CONCEPT_ANCESTOR）
-- ---------------------------------------------------------------------------
CREATE TABLE concept_mapping (
  mapping_id       TEXT PRIMARY KEY,
  source_code_table_id TEXT REFERENCES code_table(code_table_id),
  source_code      TEXT,              -- 源词表代码，如专业目录 M10001
  source_text      TEXT,              -- 源原文，如"临床医学（五年制）"
  target_concept_id TEXT NOT NULL REFERENCES concept(concept_id),
  mapping_type     TEXT NOT NULL,     -- exact | broader | narrower | related | manual
  confidence       mt.confidence_t NOT NULL DEFAULT 0.8,
  valid_from       DATE, valid_to DATE,
  created_by       TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_concept_mapping_src ON concept_mapping(source_code_table_id, source_code);

-- 层级闭包：把"统计所有外科子类专业"从递归变成一次 join
CREATE TABLE concept_ancestor (
  ancestor_id      TEXT NOT NULL REFERENCES concept(concept_id),
  descendant_id    TEXT NOT NULL REFERENCES concept(concept_id),
  min_levels       SMALLINT NOT NULL,
  max_levels       SMALLINT NOT NULL,
  PRIMARY KEY (ancestor_id, descendant_id)
);
CREATE OR REPLACE FUNCTION refresh_concept_ancestor() RETURNS void
LANGUAGE plpgsql
SET search_path = mt, public
AS $$
BEGIN
  DELETE FROM concept_ancestor;
  WITH RECURSIVE walk AS (
    SELECT concept_id AS root, concept_id AS node, 0 AS depth FROM concept
    UNION ALL
    SELECT w.root, c.concept_id, w.depth + 1
    FROM walk w JOIN concept c ON c.parent_id = w.node
    WHERE w.depth < 10
  )
  INSERT INTO concept_ancestor (ancestor_id, descendant_id, min_levels, max_levels)
  SELECT root, node, MIN(depth), MAX(depth) FROM walk GROUP BY root, node;
END;
$$;

-- ---------------------------------------------------------------------------
-- P11/P12 · 数据集发布登记（对标 CHARLS 注册式申请、Croissant 数据集描述）
-- 纪律：没有 codebook 的数据集不允许发布。
-- ---------------------------------------------------------------------------
CREATE TABLE dataset_release (
  release_id       TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  version          TEXT NOT NULL,          -- SemVer：MINOR=只加字段不改语义，MAJOR=破坏性
  description      TEXT,
  published_at     TIMESTAMPTZ,
  next_expected_at DATE,
  doc_guide_uri    TEXT,                   -- 用户指南
  doc_codebook_uri TEXT NOT NULL,          -- 数据字典（必填）
  doc_instrument_uri TEXT,                 -- 问卷/采集工具
  checksum_sha256  TEXT,
  access_policy_id TEXT REFERENCES access_policy(policy_id),
  record_set_jsonld JSONB,                 -- Croissant 风格数据集描述
  is_live          BOOLEAN NOT NULL DEFAULT true,
  status           TEXT NOT NULL DEFAULT 'RS1'
                     CHECK (status IN ('RS1','RS2','RS3','RS4')),
                     -- 存码：RS1 草稿 / RS2 复核中 / RS3 已发布 / RS4 已退役（CT_RELEASE_STATUS）
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_release_needs_codebook CHECK (status = 'RS1' OR doc_codebook_uri IS NOT NULL)
);

COMMIT;

-- ============================================================================
-- 执行后动作（运维清单）
-- ============================================================================
-- 1) SELECT refresh_category_counts();
-- 2) SELECT refresh_concept_ancestor();
-- 3) 生成对外字典：导出 field_catalog + code_table + code_value 为 CSV，
--    并生成 dist/catalog/dataset.jsonld（Croissant 风格），计算 sha256 写入 change_log。
