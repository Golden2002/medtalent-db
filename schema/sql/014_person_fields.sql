-- ============================================================================
-- 医学生人才信息库 · 014 画像维度 → person 字段字典登记 v1.0.0
--
-- 补的是哪个缺口（docs/01 第 239 行 + docs/09 第 16-17 行的设计承诺）：
--   主数据（教育/工作/论文）仍用类型化表，field_value 只承接长尾扩展字段；
--   但「加一个画像维度 = 往 field_catalog 写一行」这条承诺没有兑现 ——
--   mt.dimension 的 50 个维度里，36 个填了 person_field_id，却没有一个以
--   entity_id='person' 登记，于是小程序拿不到它们（v_form_schema 里 person 只有 2 行），
--   也不受「字段必须登记才能写入」的门禁约束（mt.set_value / mt.create_instance）。
--
-- 本迁移做的事：为 50 个维度各登记 1 个 person 侧字典字段。
--
-- 【为什么不是把 dimension.person_field_id 直接改成 entity_id='person'】
--   那 35 个唯一 person_field_id 早已登记在**别的实体**名下，例如
--     F_EDU_DEGREE_LEVEL → education_record，F_CRED_TYPE → credential，
--     F_PREF_EXPECT_CITY → preference，F_JOB_ZONE → job_posting，
--     F_EVD_VERIFIABILITY → evidence，F_SKL_CONCEPT_ID → skill_assertion。
--   改 entity_id 会同时造成三处破坏：
--     ① schema/catalog/field_catalog_seed.csv 是字典的权威来源，同一个 field_id
--        出现两行 → code/validate_catalog.py 判 `field_id 重复`，直接违反 0 错门禁；
--     ② 那些实体的 v_form_schema 会失去自己的字段，mt.create_instance 对
--        「表列之外但已登记」的扩展键会报 undefined_column；
--     ③ load_catalog.py 的 ON CONFLICT DO UPDATE 刻意不写 entity_id（它只认
--        field_catalog_seed.csv），所以这个改动**无法从 CSV 重放**，库与 CSV 永久漂移。
--   因此采用 person 侧独立命名空间 F_PSN_<组>_<维度后缀>，并用 related_fields
--   指回物理落点字段，description 里写清落点，做到「登记不与落点抢归属」。
--
-- 【field_id 命名规则（确定性、可重放）】
--   field_id = 'F_PSN_' || <组段> || '_' || <dimension_id 去掉 'DIM_' 前缀>
--   组段按 group_id：G1=EDU G2=CRED G3=EXP G4=RES G5=SKL G6=INT G7=GEO G8=PAY G9=FAM
--   例：DIM_DEGREE_LEVEL(G1) → F_PSN_EDU_DEGREE_LEVEL；
--       DIM_SALARY_EXPECT(G8) → F_PSN_PAY_SALARY_EXPECT。
--   同一规则落成函数 mt.dim_field_id(group_id, dimension_id)（文件末），
--   供核对 SQL 与小程序按维度 id 反查字段，不靠人记。
--
-- 【列值来源（全部由 mt.dimension 行确定性派生）】
--   title          = dimension.title_zh
--   description    = 一句说明（含组名、kind、是否硬门槛、人侧落点 person_locator）
--   data_type      = enum/ordinal→code（无词表时 string）；set→array；range→string；text→text
--                    与 field_catalog 既有同类字段保持一致（F_PERSON_SEX=code、
--                    F_PREF_* =array、F_EDU_SCHOOL_TAGS=array），
--                    同时规避 validate_catalog 的 `cardinality=array 但 data_type≠array` 警告。
--   code_table_id  = dimension.code_table_id（软引用；FK 到 code_table，已核对 42 张词表全部存在）
--   cardinality    = set→array，其余→scalar
--   is_arrayed     = 与 cardinality 一致（口径同 008 mt.add_dimension）
--   is_required    = false（画像维度是采集项，不是录入必填项）
--   form_section   = dimension.group_title（9 组，供小程序按组折叠；该列是自由文本，无 CT_* 约束）
--   showcase_order = dimension.sort_order（10..500，保证小程序字段顺序与注册表一致）
--   allow_custom   = dimension.allow_custom
--   custom_hint    = dimension.custom_hint
--   access_tier    = 落点字段的 access_tier，但只向上保留 T2/T3（体检受限 T3、政治面貌/性别/
--                    出生年/户籍 T2 不因换实体登记而降低敏感度），其余归 T1
--   collection_method = 落点以 derived: 开头 → derived，否则 questionnaire
--   related_fields = 落点字段 id（dimension.person_field_id；14 个维度为 NULL）
--   status/version/owner = active / 1.0.0 / data
--
-- 可重放：50 条 INSERT ... ON CONFLICT (field_id) DO UPDATE，重复执行行数与内容不变。
-- 不修改 001-013 任何文件；不写 field_value；不建新表/新列。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 0. 兜底：person 实体必须存在（口径同 013 第 4 节：ON CONFLICT DO NOTHING，
--    绝不覆盖 code/load_catalog.py 写入的内容）。
--    field_catalog.entity_id 上有 FK → entity_catalog(entity_id)，
--    但「person」这个实体是 load_catalog.py 从 CSV 派生的，apply 单独跑时可能还没有。
-- ---------------------------------------------------------------------------
INSERT INTO entity_catalog (entity_id, table_name, domain, description, id_prefix,
                            schema_version, access_tier, status, kind)
VALUES ('person', 'person', 'talent', '人才主档：学历/证书/经历/偏好等类型化子表挂在它下面',
        'per', '1.0.0', 'T1', 'active', 'EK1')
ON CONFLICT (entity_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 1. 50 个 person 侧画像维度字段（UPSERT，可重放）
-- ---------------------------------------------------------------------------
-- DIM_DEGREE_LEVEL（G1）→ 落点 education_record.degree_level
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EDU_DEGREE_LEVEL', 'person', '学历层次', '画像维度 DIM_DEGREE_LEVEL（学历与专业 · 序数·硬门槛）在 person 侧的登记字段；人侧落点 education_record.degree_level。', 'code', 'CT_DEGREE_LEVEL', 'scalar',
  false, false, '学历与专业', 10, false, NULL,
  'T1', 'questionnaire', ARRAY['F_EDU_DEGREE_LEVEL'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_MAJOR（G1）→ 落点 education_record.major_code
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EDU_MAJOR', 'person', '专业匹配', '画像维度 DIM_MAJOR（学历与专业 · 多值集合·硬门槛）在 person 侧的登记字段；人侧落点 education_record.major_code。', 'array', 'CT_MAJOR', 'array',
  true, false, '学历与专业', 20, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_EDU_MAJOR_CODE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_IS_CLINICAL_MAJOR（G1）→ 落点 education_record.is_clinical
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EDU_IS_CLINICAL_MAJOR', 'person', '是否临床医学类', '画像维度 DIM_IS_CLINICAL_MAJOR（学历与专业 · 枚举）在 person 侧的登记字段；人侧落点 education_record.is_clinical。', 'code', 'CT_YES_NO', 'scalar',
  false, false, '学历与专业', 30, false, NULL,
  'T1', 'questionnaire', ARRAY['F_EDU_IS_CLINICAL'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_ACADEMIC_RANK（G1）→ 落点 education_record.rank_percentile
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EDU_ACADEMIC_RANK', 'person', '学业排名段位', '画像维度 DIM_ACADEMIC_RANK（学历与专业 · 序数）在 person 侧的登记字段；人侧落点 education_record.rank_percentile。', 'code', 'CT_RANK_BAND', 'scalar',
  false, false, '学历与专业', 40, false, NULL,
  'T1', 'questionnaire', ARRAY['F_EDU_RANK_PCT'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SCHOOL_TIER（G1）→ 落点 education_record.school_tags
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EDU_SCHOOL_TIER', 'person', '院校层次', '画像维度 DIM_SCHOOL_TIER（学历与专业 · 序数）在 person 侧的登记字段；人侧落点 education_record.school_tags。', 'code', 'CT_SCHOOL_TIER', 'scalar',
  false, false, '学历与专业', 50, false, NULL,
  'T1', 'questionnaire', ARRAY['F_EDU_SCHOOL_TAGS'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CREDENTIAL（G2）→ 落点 credential.credential_type
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_CREDENTIAL', 'person', '资质证书持有', '画像维度 DIM_CREDENTIAL（资质与准入 · 多值集合·硬门槛）在 person 侧的登记字段；人侧落点 credential.credential_type。', 'array', 'CT_CREDENTIAL_TYPE', 'array',
  true, false, '资质与准入', 60, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_CRED_TYPE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CREDENTIAL_STATUS（G2）→ 落点 credential.status
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_CREDENTIAL_STATUS', 'person', '证书状态', '画像维度 DIM_CREDENTIAL_STATUS（资质与准入 · 序数·硬门槛）在 person 侧的登记字段；人侧落点 credential.status。', 'code', 'CT_CRED_STATUS', 'scalar',
  false, false, '资质与准入', 70, false, NULL,
  'T1', 'questionnaire', ARRAY['F_CRED_STATUS'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_TRAINING（G2）→ 落点 credential.credential_type
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_TRAINING', 'person', '规培与培养经历', '画像维度 DIM_TRAINING（资质与准入 · 多值集合·硬门槛）在 person 侧的登记字段；人侧落点 credential.credential_type。', 'array', 'CT_TRAINING_TYPE', 'array',
  true, false, '资质与准入', 80, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_TRAIN_TYPE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_HEALTH_LIMIT（G2）→ 落点 person_demographics.health_limits
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_HEALTH_LIMIT', 'person', '体检受限项', '画像维度 DIM_HEALTH_LIMIT（资质与准入 · 多值集合·硬门槛）在 person 侧的登记字段；人侧落点 person_demographics.health_limits。', 'array', 'CT_HEALTH_LIMIT', 'array',
  true, false, '资质与准入', 90, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T3', 'questionnaire', ARRAY['F_PERSON_HEALTH_LIMITS'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_POLITICAL（G2）→ 落点 person_demographics.political_status
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_POLITICAL', 'person', '政治面貌', '画像维度 DIM_POLITICAL（资质与准入 · 枚举·硬门槛）在 person 侧的登记字段；人侧落点 person_demographics.political_status。', 'code', 'CT_POLITICAL', 'scalar',
  false, false, '资质与准入', 100, false, NULL,
  'T2', 'questionnaire', ARRAY['F_PERSON_POLITICAL'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_AGE_BAND（G2）→ 落点 person_demographics.birth_year
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_AGE_BAND', 'person', '年龄段', '画像维度 DIM_AGE_BAND（资质与准入 · 序数）在 person 侧的登记字段；人侧落点 person_demographics.birth_year。', 'code', 'CT_AGE_BAND', 'scalar',
  false, false, '资质与准入', 110, false, NULL,
  'T2', 'questionnaire', ARRAY['F_PERSON_BIRTH_YEAR'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SEX（G2）→ 落点 person_demographics.sex
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_CRED_SEX', 'person', '性别', '画像维度 DIM_SEX（资质与准入 · 枚举）在 person 侧的登记字段；人侧落点 person_demographics.sex。', 'code', 'CT_SEX', 'scalar',
  false, false, '资质与准入', 120, false, NULL,
  'T2', 'questionnaire', ARRAY['F_PERSON_SEX'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_WORK_YEARS（G3）→ 落点 derived:employment_record 起止日期
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_WORK_YEARS', 'person', '工作年限', '画像维度 DIM_WORK_YEARS（经历与年限 · 序数·硬门槛）在 person 侧的登记字段；人侧落点 derived:employment_record 起止日期。', 'code', 'CT_EXPERIENCE_BAND', 'scalar',
  false, false, '经历与年限', 130, false, NULL,
  'T1', 'derived', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_EMPLOYER_TYPE（G3）→ 落点 employment_record.employer_type
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_EMPLOYER_TYPE', 'person', '单位类型', '画像维度 DIM_EMPLOYER_TYPE（经历与年限 · 多值集合）在 person 侧的登记字段；人侧落点 employment_record.employer_type。', 'array', 'CT_EMPLOYER_TYPE', 'array',
  true, false, '经历与年限', 140, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_EMP_EMPLOYER_TYPE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CLINICAL_BAND（G3）→ 落点 derived:clinical_exposure + credential
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_CLINICAL_BAND', 'person', '临床暴露层级', '画像维度 DIM_CLINICAL_BAND（经历与年限 · 序数）在 person 侧的登记字段；人侧落点 derived:clinical_exposure + credential。', 'code', 'CT_CLINICAL_BAND', 'scalar',
  false, false, '经历与年限', 150, false, NULL,
  'T1', 'derived', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CLINICAL_DEPARTMENT（G3）→ 落点 clinical_exposure.department
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_CLINICAL_DEPARTMENT', 'person', '临床科室经历', '画像维度 DIM_CLINICAL_DEPARTMENT（经历与年限 · 多值集合）在 person 侧的登记字段；人侧落点 clinical_exposure.department。', 'array', 'CT_CLINICAL_DEPARTMENT', 'array',
  true, false, '经历与年限', 160, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_CLIN_DEPARTMENT'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CLINICAL_VOLUME（G3）→ 落点 clinical_exposure.procedure_count
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_CLINICAL_VOLUME', 'person', '操作例数区间', '画像维度 DIM_CLINICAL_VOLUME（经历与年限 · 序数）在 person 侧的登记字段；人侧落点 clinical_exposure.procedure_count。', 'code', 'CT_VOLUME_BAND', 'scalar',
  false, false, '经历与年限', 170, false, NULL,
  'T1', 'questionnaire', ARRAY['F_CLIN_PROCEDURE_COUNT'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_PROJECT_TYPE（G3）→ 落点 project_record.project_type
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_PROJECT_TYPE', 'person', '项目经历类型', '画像维度 DIM_PROJECT_TYPE（经历与年限 · 多值集合）在 person 侧的登记字段；人侧落点 project_record.project_type。', 'array', 'CT_PROJECT_TYPE', 'array',
  true, false, '经历与年限', 180, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PRJ_PROJECT_TYPE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_PROJECT_ROLE（G3）→ 落点 project_record.role
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_PROJECT_ROLE', 'person', '项目角色', '画像维度 DIM_PROJECT_ROLE（经历与年限 · 序数）在 person 侧的登记字段；人侧落点 project_record.role。', 'code', 'CT_PROJECT_ROLE', 'scalar',
  false, false, '经历与年限', 190, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_JOB_ZONE（G3）→ 落点 derived:学位与年限
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_EXP_JOB_ZONE', 'person', '岗位准备度', '画像维度 DIM_JOB_ZONE（经历与年限 · 序数）在 person 侧的登记字段；人侧落点 derived:学位与年限。', 'code', 'CT_JOB_ZONE', 'scalar',
  false, false, '经历与年限', 200, false, NULL,
  'T1', 'derived', ARRAY['F_JOB_ZONE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_RESEARCH_LEVEL（G4）→ 落点 derived:research_output
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_RES_RESEARCH_LEVEL', 'person', '科研层级', '画像维度 DIM_RESEARCH_LEVEL（科研与产出 · 序数）在 person 侧的登记字段；人侧落点 derived:research_output。', 'code', 'CT_RESEARCH_LEVEL', 'scalar',
  false, false, '科研与产出', 210, false, NULL,
  'T1', 'derived', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_RESEARCH_OUTPUT_TYPE（G4）→ 落点 research_output.output_type
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_RES_RESEARCH_OUTPUT_TYPE', 'person', '科研产出类型', '画像维度 DIM_RESEARCH_OUTPUT_TYPE（科研与产出 · 多值集合）在 person 侧的登记字段；人侧落点 research_output.output_type。', 'array', 'CT_RESEARCH_OUTPUT_TYPE', 'array',
  true, false, '科研与产出', 220, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_RES_OUTPUT_TYPE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_AWARD_LEVEL（G4）→ 落点 award_honor.level
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_RES_AWARD_LEVEL', 'person', '获奖层级', '画像维度 DIM_AWARD_LEVEL（科研与产出 · 序数）在 person 侧的登记字段；人侧落点 award_honor.level。', 'code', 'CT_AWARD_LEVEL', 'scalar',
  false, false, '科研与产出', 230, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_AWD_AWARD_LEVEL'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_OVERSEAS（G4）→ 落点 education_record.overseas
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_RES_OVERSEAS', 'person', '海外经历', '画像维度 DIM_OVERSEAS（科研与产出 · 枚举）在 person 侧的登记字段；人侧落点 education_record.overseas。', 'code', 'CT_YES_NO', 'scalar',
  false, false, '科研与产出', 240, false, NULL,
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CONCEPT_SKILL（G5）→ 落点 concept:K1
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_SKL_CONCEPT_SKILL', 'person', '技能概念（K1）', '画像维度 DIM_CONCEPT_SKILL（能力与知识 · 多值集合）在 person 侧的登记字段；人侧落点 concept:K1。', 'array', NULL, 'array',
  true, false, '能力与知识', 250, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_SKL_CONCEPT_ID'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CONCEPT_ABILITY（G5）→ 落点 concept:K3
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_SKL_CONCEPT_ABILITY', 'person', '通用能力概念（K3）', '画像维度 DIM_CONCEPT_ABILITY（能力与知识 · 多值集合）在 person 侧的登记字段；人侧落点 concept:K3。', 'array', NULL, 'array',
  true, false, '能力与知识', 260, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_SKL_CONCEPT_ID'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SKILL_LEVEL（G5）→ 落点 skill_assertion.level
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_SKL_SKILL_LEVEL', 'person', '能力等级', '画像维度 DIM_SKILL_LEVEL（能力与知识 · 序数）在 person 侧的登记字段；人侧落点 skill_assertion.level。', 'code', 'CT_ABILITY_LEVEL', 'scalar',
  false, false, '能力与知识', 270, false, NULL,
  'T1', 'questionnaire', ARRAY['F_SKL_LEVEL'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SKILL_BASIS（G5）→ 落点 skill_assertion.level_basis
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_SKL_SKILL_BASIS', 'person', '等级判定依据', '画像维度 DIM_SKILL_BASIS（能力与知识 · 枚举）在 person 侧的登记字段；人侧落点 skill_assertion.level_basis。', 'code', 'CT_LEVEL_BASIS', 'scalar',
  false, false, '能力与知识', 280, false, NULL,
  'T1', 'questionnaire', ARRAY['F_SKL_LEVEL_BASIS'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_ASSESSMENT_DIMENSION（G5）→ 落点 assessment.dimension
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_SKL_ASSESSMENT_DIMENSION', 'person', '测评维度得分', '画像维度 DIM_ASSESSMENT_DIMENSION（能力与知识 · 多值集合）在 person 侧的登记字段；人侧落点 assessment.dimension。', 'array', 'CT_ASSESSMENT_DIMENSION', 'array',
  true, false, '能力与知识', 290, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_ASM_DIMENSION'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_LANGUAGE_LEVEL（G5）→ 落点 credential:C09
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_SKL_LANGUAGE_LEVEL', 'person', '语言能力', '画像维度 DIM_LANGUAGE_LEVEL（能力与知识 · 序数）在 person 侧的登记字段；人侧落点 credential:C09。', 'code', 'CT_LANGUAGE_LEVEL', 'scalar',
  false, false, '能力与知识', 300, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_INTEREST_DOMAIN（G6）→ 落点 preference:PF9
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_INT_INTEREST_DOMAIN', 'person', '兴趣方向', '画像维度 DIM_INTEREST_DOMAIN（兴趣与价值取向 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF9。', 'array', 'CT_INTEREST_DOMAIN', 'array',
  true, false, '兴趣与价值取向', 310, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_INTEREST_DOMAIN'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CAREER_GOAL（G6）→ 落点 preference:PF12
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_INT_CAREER_GOAL', 'person', '职业目标', '画像维度 DIM_CAREER_GOAL（兴趣与价值取向 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF12。', 'array', 'CT_CAREER_GOAL', 'array',
  true, false, '兴趣与价值取向', 320, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_CAREER_GOAL'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_WORK_STYLE（G6）→ 落点 preference:PF10
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_INT_WORK_STYLE', 'person', '工作风格', '画像维度 DIM_WORK_STYLE（兴趣与价值取向 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF10。', 'array', 'CT_WORK_STYLE', 'array',
  true, false, '兴趣与价值取向', 330, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_WORK_STYLE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_VALUE_ORIENT（G6）→ 落点 preference:PF11
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_INT_VALUE_ORIENT', 'person', '价值观取向', '画像维度 DIM_VALUE_ORIENT（兴趣与价值取向 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF11。', 'array', 'CT_VALUE_ORIENT', 'array',
  true, false, '兴趣与价值取向', 340, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_VALUE_ORIENT'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_ORG_CULTURE_FIT（G6）→ 落点 preference:PF16
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_INT_ORG_CULTURE_FIT', 'person', '组织文化契合', '画像维度 DIM_ORG_CULTURE_FIT（兴趣与价值取向 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF16。', 'array', 'CT_ORG_CULTURE', 'array',
  true, false, '兴趣与价值取向', 350, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_ORG_CULTURE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_EXPECT_CITY（G7）→ 落点 preference:PF1
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_GEO_EXPECT_CITY', 'person', '期望工作城市', '画像维度 DIM_EXPECT_CITY（地域与流动 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF1。', 'array', 'CT_CITY', 'array',
  true, false, '地域与流动', 360, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_EXPECT_CITY'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_CITY_TIER（G7）→ 落点 derived:DIM_EXPECT_CITY
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_GEO_CITY_TIER', 'person', '城市层级', '画像维度 DIM_CITY_TIER（地域与流动 · 序数）在 person 侧的登记字段；人侧落点 derived:DIM_EXPECT_CITY。', 'code', 'CT_CITY_TIER', 'scalar',
  false, false, '地域与流动', 370, false, NULL,
  'T1', 'derived', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_MOBILITY（G7）→ 落点 preference:PF15
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_GEO_MOBILITY', 'person', '地域流动意愿', '画像维度 DIM_MOBILITY（地域与流动 · 序数）在 person 侧的登记字段；人侧落点 preference:PF15。', 'code', 'CT_MOBILITY', 'scalar',
  false, false, '地域与流动', 380, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_MOBILITY'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_HUKOU_CITY（G7）→ 落点 person_demographics.hukou_province
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_GEO_HUKOU_CITY', 'person', '户籍所在地', '画像维度 DIM_HUKOU_CITY（地域与流动 · 枚举）在 person 侧的登记字段；人侧落点 person_demographics.hukou_province。', 'code', 'CT_CITY', 'scalar',
  false, false, '地域与流动', 390, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T2', 'questionnaire', ARRAY['F_PERSON_HUKOU_CITY'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_HUKOU_TYPE（G7）→ 落点 person_demographics.hukou_type
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_GEO_HUKOU_TYPE', 'person', '户籍类型', '画像维度 DIM_HUKOU_TYPE（地域与流动 · 枚举）在 person 侧的登记字段；人侧落点 person_demographics.hukou_type。', 'code', 'CT_HUKOU_TYPE', 'scalar',
  false, false, '地域与流动', 400, false, NULL,
  'T2', 'questionnaire', ARRAY['F_PERSON_HUKOU_TYPE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_WORK_MODE（G7）→ 落点 preference:PF13
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_GEO_WORK_MODE', 'person', '工作方式偏好', '画像维度 DIM_WORK_MODE（地域与流动 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF13。', 'array', 'CT_WORK_MODE', 'array',
  true, false, '地域与流动', 410, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_WORK_MODE'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SALARY_EXPECT（G8）→ 落点 preference:PF4
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_PAY_SALARY_EXPECT', 'person', '薪资期望区间', '画像维度 DIM_SALARY_EXPECT（薪酬与强度 · 区间）在 person 侧的登记字段；人侧落点 preference:PF4。', 'string', NULL, 'scalar',
  false, false, '薪酬与强度', 420, false, NULL,
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SALARY_BAND（G8）→ 落点 derived:DIM_SALARY_EXPECT
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_PAY_SALARY_BAND', 'person', '薪资档位', '画像维度 DIM_SALARY_BAND（薪酬与强度 · 序数）在 person 侧的登记字段；人侧落点 derived:DIM_SALARY_EXPECT。', 'code', 'CT_SALARY_BAND', 'scalar',
  false, false, '薪酬与强度', 430, false, NULL,
  'T1', 'derived', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_WORK_INTENSITY（G8）→ 落点 preference:PF5
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_PAY_WORK_INTENSITY', 'person', '工作强度容忍', '画像维度 DIM_WORK_INTENSITY（薪酬与强度 · 序数）在 person 侧的登记字段；人侧落点 preference:PF5。', 'code', 'CT_WORK_INTENSITY', 'scalar',
  false, false, '薪酬与强度', 440, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_SHIFT_WILLING（G8）→ 落点 preference:PF14
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_PAY_SHIFT_WILLING', 'person', '值班意愿', '画像维度 DIM_SHIFT_WILLING（薪酬与强度 · 序数）在 person 侧的登记字段；人侧落点 preference:PF14。', 'code', 'CT_SHIFT_WILLING', 'scalar',
  false, false, '薪酬与强度', 450, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', ARRAY['F_PREF_SHIFT_WILLING'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_GROWTH_PREF（G8）→ 落点 preference:PF7
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_PAY_GROWTH_PREF', 'person', '成长性偏好', '画像维度 DIM_GROWTH_PREF（薪酬与强度 · 序数）在 person 侧的登记字段；人侧落点 preference:PF7。', 'code', 'CT_GROWTH_PREF', 'scalar',
  false, false, '薪酬与强度', 460, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_STABILITY_PREF（G8）→ 落点 preference:PF6
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_PAY_STABILITY_PREF', 'person', '稳定性偏好', '画像维度 DIM_STABILITY_PREF（薪酬与强度 · 序数）在 person 侧的登记字段；人侧落点 preference:PF6。', 'code', 'CT_STABILITY_PREF', 'scalar',
  false, false, '薪酬与强度', 470, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_JOB_FAMILY_PREF（G9）→ 落点 preference:PF3
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_FAM_JOB_FAMILY_PREF', 'person', '岗位族偏好', '画像维度 DIM_JOB_FAMILY_PREF（岗位族与综合 · 多值集合）在 person 侧的登记字段；人侧落点 preference:PF3。', 'array', 'CT_JOB_FAMILY', 'array',
  true, false, '岗位族与综合', 480, true, '可自写补充：不在选项内的取值先进入候选池（concept_candidate），经评审后由 mt.promote_option_candidate 提升为正式选项',
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_ACCEPT_CROSS_INDUSTRY（G9）→ 落点 preference:PF8
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_FAM_ACCEPT_CROSS_INDUSTRY', 'person', '是否接受转行', '画像维度 DIM_ACCEPT_CROSS_INDUSTRY（岗位族与综合 · 枚举）在 person 侧的登记字段；人侧落点 preference:PF8。', 'code', 'CT_YES_NO', 'scalar',
  false, false, '岗位族与综合', 490, false, NULL,
  'T1', 'questionnaire', NULL, 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- DIM_EVIDENCE_STRENGTH（G9）→ 落点 derived:evidence 与 verify_status
INSERT INTO field_catalog (
  field_id, entity_id, title, description, data_type, code_table_id, cardinality,
  is_arrayed, is_required, form_section, showcase_order, allow_custom, custom_hint,
  access_tier, collection_method, related_fields, status, version, owner
) VALUES (
  'F_PSN_FAM_EVIDENCE_STRENGTH', 'person', '画像证据强度', '画像维度 DIM_EVIDENCE_STRENGTH（岗位族与综合 · 序数）在 person 侧的登记字段；人侧落点 derived:evidence 与 verify_status。', 'code', 'CT_EVIDENCE_STRENGTH', 'scalar',
  false, false, '岗位族与综合', 500, false, NULL,
  'T1', 'derived', ARRAY['F_EVD_VERIFIABILITY'], 'active', '1.0.0', 'data'
)
ON CONFLICT (field_id) DO UPDATE SET
  entity_id = EXCLUDED.entity_id, title = EXCLUDED.title,
  description = EXCLUDED.description, data_type = EXCLUDED.data_type,
  code_table_id = EXCLUDED.code_table_id, cardinality = EXCLUDED.cardinality,
  is_arrayed = EXCLUDED.is_arrayed, is_required = EXCLUDED.is_required,
  form_section = EXCLUDED.form_section, showcase_order = EXCLUDED.showcase_order,
  allow_custom = EXCLUDED.allow_custom, custom_hint = EXCLUDED.custom_hint,
  access_tier = EXCLUDED.access_tier,
  collection_method = EXCLUDED.collection_method,
  related_fields = EXCLUDED.related_fields, status = EXCLUDED.status,
  version = EXCLUDED.version, owner = EXCLUDED.owner, updated_at = now();

-- ---------------------------------------------------------------------------
-- 2. 维度 → person 字段 的确定性映射函数
--    只做 id 派生，不查库、不改数据；让核对 SQL 与小程序都能按维度 id 直接取字段。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dim_field_id(p_group_id TEXT, p_dimension_id TEXT)
RETURNS TEXT
LANGUAGE sql IMMUTABLE SET search_path = mt, public AS $$
  SELECT 'F_PSN_' || CASE p_group_id
           WHEN 'G1' THEN 'EDU'  WHEN 'G2' THEN 'CRED' WHEN 'G3' THEN 'EXP'
           WHEN 'G4' THEN 'RES'  WHEN 'G5' THEN 'SKL'  WHEN 'G6' THEN 'INT'
           WHEN 'G7' THEN 'GEO'  WHEN 'G8' THEN 'PAY'  WHEN 'G9' THEN 'FAM'
         END || '_' || substring(p_dimension_id from 5)
  WHERE p_group_id IN ('G1','G2','G3','G4','G5','G6','G7','G8','G9')
    AND p_dimension_id LIKE 'DIM\_%';
$$;

-- ---------------------------------------------------------------------------
-- 3. 审计流水（变更日志是 append-only，按项目既有约定每次留痕一条）
-- ---------------------------------------------------------------------------
INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
VALUES (session_user, 'field', 'person:dimension-fields', 'backfill', '1.0.0',
        jsonb_build_object('inserted', 50, 'entity', 'person',
                           'namespace', 'F_PSN_', 'source', 'mt.dimension',
                           'form_sections', 9));

COMMIT;

-- ============================================================================
-- 核对（真实输出见报告）：
--   SELECT count(*) FROM field_catalog WHERE entity_id='person' AND status='active';
--   SELECT count(*) FROM v_form_schema WHERE entity_id='person';
--   SELECT form_section, count(*) FROM field_catalog
--    WHERE entity_id='person' AND field_id LIKE 'F_PSN_%' GROUP BY 1 ORDER BY 1;
-- ============================================================================
