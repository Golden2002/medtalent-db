-- ============================================================================
-- 医学生人才信息库 · 动态建模层 v0.5.0
--
-- 目标（对应用户提出的两点能力）：
--   ① 列可动态增删：真实使用中发现新画像维度 A，应能"方便地加进去"——**零 DDL**；
--   ② 行可动态创建：依据用户输入动态创建实例（记录）——**零 DDL**；
--   ③ 每个字段可预设 n 个选项供用户选择，选择结果入库并可校验。
--
-- 实现基础是 001/002 已有的三条扩展机制，本文件把它们**收敛成一组可调用的 API**，
-- 使"加维度/加选项/建实例/废弃"变成一次函数调用，而不是让人记四张表怎么插。
--
--   加一列（维度）→ mt.add_dimension(...)
--   建一行（实例）→ mt.create_instance(...)
--   写一个值      → mt.set_value(...)
--   删一列（软）  → mt.deprecate_dimension(...)
--   删一行        → mt.delete_instance(...)
--   取表单定义    → mt.form_schema(entity) / v_form_schema
--   取某个人的画像 → mt.profile_json(subject_type, subject_id)
--
-- 关键设计：**新实体也可以零 DDL**。entity_catalog.table_name 允许为 NULL，
-- 表示"纯值实体"——它的全部数据存在 field_value 里（对标 UK Biobank 的
-- 一切都是 field × instance × array）。只有需要强约束/外键/高并发查询时，
-- 才把实体"提升"为类型化表。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 0. 表结构调整（只增列 / 放宽约束，保持向后兼容）
-- ---------------------------------------------------------------------------
-- 纯值实体没有物理表，因此 table_name 必须可空
ALTER TABLE entity_catalog ALTER COLUMN table_name DROP NOT NULL;
ALTER TABLE entity_catalog ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'EK1';

-- 表单所需：是否必填、分组
ALTER TABLE field_catalog ADD COLUMN IF NOT EXISTS is_required BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE field_catalog ADD COLUMN IF NOT EXISTS form_section TEXT;

-- 实体类型受控：CT_ENTITY_KIND
INSERT INTO code_table (code_table_id, name, description, version, status, owner)
VALUES ('CT_ENTITY_KIND', '实体形态', '类型化表 / 纯值实体', '1.0.0', 'active', 'data')
ON CONFLICT (code_table_id) DO NOTHING;
INSERT INTO code_value (code_table_id, code, label_zh, label_en, level, sort_order, definition)
VALUES
 ('CT_ENTITY_KIND','EK1','类型化表','Table-backed',1,10,'有物理表，有列约束与外键'),
 ('CT_ENTITY_KIND','EK2','纯值实体','Value-only',1,20,'无物理表，全部值存 field_value，加字段零 DDL')
ON CONFLICT (code_table_id, code) DO UPDATE SET label_zh=EXCLUDED.label_zh;

-- 实体形态取值必须是受控码（EK1 类型化表 / EK2 纯值实体）
ALTER TABLE entity_catalog DROP CONSTRAINT IF EXISTS chk_entity_kind;
ALTER TABLE entity_catalog DROP CONSTRAINT IF EXISTS chk_entity_kind_code;
ALTER TABLE entity_catalog ADD CONSTRAINT chk_entity_kind_code
  CHECK (kind IN ('EK1','EK2'));
UPDATE entity_catalog SET kind = 'EK1' WHERE kind IN ('table','EK1');
UPDATE entity_catalog SET kind = 'EK2' WHERE kind IN ('value_only','EK2');

-- ---------------------------------------------------------------------------
-- 1. 注册实体（零 DDL 新增"实体"）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION register_entity(
  p_entity_id   TEXT,
  p_domain      TEXT DEFAULT 'talent',
  p_kind        TEXT DEFAULT 'EK2',       -- EK1 类型化表 / EK2 纯值实体
  p_id_prefix   TEXT DEFAULT NULL,
  p_description TEXT DEFAULT NULL
) RETURNS TEXT
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  tname TEXT;
BEGIN
  IF p_kind = 'EK1' THEN
    tname := p_entity_id;
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema='mt' AND table_name=tname) THEN
      RAISE EXCEPTION 'EK1 实体要求物理表 mt.% 已存在；若暂无表请用 EK2', tname;
    END IF;
  ELSE
    tname := NULL;   -- 纯值实体：无物理表
  END IF;

  INSERT INTO entity_catalog (entity_id, table_name, domain, description, id_prefix,
                              schema_version, access_tier, status, kind)
  VALUES (p_entity_id, tname, p_domain, p_description,
          COALESCE(p_id_prefix, left(regexp_replace(p_entity_id,'[^a-z]','','g'), 3)),
          '1.0.0', 'T1', 'active', p_kind)
  ON CONFLICT (entity_id) DO UPDATE
    SET domain = EXCLUDED.domain,
        description = COALESCE(EXCLUDED.description, entity_catalog.description),
        kind = EXCLUDED.kind,
        table_name = EXCLUDED.table_name;

  INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
  VALUES (session_user, 'entity', p_entity_id, 'add', '1.0.0',
          jsonb_build_object('kind', p_kind, 'domain', p_domain));
  RETURN p_entity_id;
END $$;

-- ---------------------------------------------------------------------------
-- 2. 加维度（列）—— 零 DDL，可选携带 n 个选项
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION add_dimension(
  p_entity      TEXT,
  p_field_id    TEXT,
  p_title       TEXT,
  p_options     JSONB DEFAULT NULL,       -- [{"code":"A1","label":"...","sort":10}, ...]
  p_data_type   TEXT DEFAULT 'code',      -- code | text | integer | number | date | boolean
  p_multi       BOOLEAN DEFAULT false,    -- true = 多选（可存多个 code）
  p_required    BOOLEAN DEFAULT false,
  p_description TEXT DEFAULT NULL,
  p_section     TEXT DEFAULT NULL,
  p_access_tier TEXT DEFAULT 'T1',
  p_owner       TEXT DEFAULT 'user'
) RETURNS TEXT
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  ct_id   TEXT;
  opt     JSONB;
  n_opt   INT := 0;
  ord     INT := 0;
  v_kind  TEXT;
BEGIN
  -- 注意：局部变量不能叫 kind —— 会与 entity_catalog.kind 列名歧义（真库实测报错）
  SELECT ec.kind INTO v_kind FROM entity_catalog ec WHERE ec.entity_id = p_entity;
  IF v_kind IS NULL THEN
    RAISE EXCEPTION '实体 % 未登记，请先调用 mt.register_entity()', p_entity;
  END IF;
  IF EXISTS (SELECT 1 FROM field_catalog WHERE field_id = p_field_id) THEN
    RAISE EXCEPTION '字段 % 已存在（如需重新启用请把 status 改回 active）', p_field_id;
  END IF;

  -- 有选项 → 自动建词表并写入 n 个选项
  IF p_options IS NOT NULL AND jsonb_typeof(p_options) = 'array'
     AND jsonb_array_length(p_options) > 0 THEN
    ct_id := 'CT_' || upper(p_field_id);
    INSERT INTO code_table (code_table_id, name, description, version, status, owner)
    VALUES (ct_id, p_title, '由 mt.add_dimension() 自动创建', '1.0.0', 'active', p_owner)
    ON CONFLICT (code_table_id) DO NOTHING;

    FOR opt IN SELECT * FROM jsonb_array_elements(p_options) LOOP
      ord := COALESCE((opt->>'sort')::int, ord + 10);
      INSERT INTO code_value (code_table_id, code, label_zh, label_en,
                              level, sort_order, definition)
      VALUES (ct_id, opt->>'code',
              COALESCE(opt->>'label', opt->>'code'),
              opt->>'label_en', 1, ord, opt->>'definition')
      ON CONFLICT (code_table_id, code) DO UPDATE
        SET label_zh = EXCLUDED.label_zh, sort_order = EXCLUDED.sort_order;
      n_opt := n_opt + 1;
    END LOOP;
    p_data_type := 'code';
  END IF;

  INSERT INTO field_catalog (
    field_id, entity_id, title, description, data_type, code_table_id,
    cardinality, is_arrayed, value_type, collection_method, strata, stability,
    is_required, form_section, access_tier, status, version, owner,
    value_range, is_private, showcase_order)
  VALUES (
    p_field_id, p_entity, p_title, p_description, p_data_type, ct_id,
    CASE WHEN p_multi THEN 'array' ELSE 'scalar' END,
    p_multi,
    CASE WHEN ct_id IS NULL THEN NULL WHEN p_multi THEN 'VT22' ELSE 'VT21' END,
    'manual', 'primary', 'updateable',
    p_required, p_section, p_access_tier, 'active', '1.0.0', p_owner,
    CASE WHEN p_data_type = 'integer' THEN '0-999999' ELSE NULL END,
    false,
    COALESCE((SELECT max(showcase_order) FROM field_catalog WHERE entity_id = p_entity), 0) + 10);

  INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
  VALUES (session_user, 'field', p_field_id, 'add', '1.0.0',
          jsonb_build_object('entity', p_entity, 'options', n_opt,
                             'multi', p_multi, 'data_type', p_data_type));
  RETURN p_field_id;
END $$;

-- ---------------------------------------------------------------------------
-- 3. 选项校验（"只能从给定选项里选"的强制点）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION assert_option(p_field_id TEXT, p_code TEXT) RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE ct TEXT;
BEGIN
  SELECT code_table_id INTO ct FROM field_catalog
  WHERE field_id = p_field_id AND status = 'active';
  IF ct IS NULL THEN
    RETURN;   -- 非枚举字段，无需校验
  END IF;
  IF NOT EXISTS (SELECT 1 FROM code_value
                 WHERE code_table_id = ct AND code = p_code AND NOT deprecated) THEN
    RAISE EXCEPTION '取值 % 不在字段 % 的候选选项内（词表 %）', p_code, p_field_id, ct
      USING ERRCODE = 'check_violation';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. 写一个值（列的值 / 多选 / 数值 / 文本）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_value(
  p_entity       TEXT,
  p_subject_id   TEXT,
  p_field_id     TEXT,
  p_code         TEXT DEFAULT NULL,
  p_codes        TEXT[] DEFAULT NULL,
  p_num          NUMERIC DEFAULT NULL,
  p_text         TEXT DEFAULT NULL,
  p_date         DATE DEFAULT NULL,
  p_instance_key TEXT DEFAULT '0',
  p_array_index  SMALLINT DEFAULT 0,
  p_confidence   NUMERIC DEFAULT 0.8
) RETURNS TEXT
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  fid   TEXT;
  vid   TEXT;
  c     TEXT;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM field_catalog
                 WHERE field_id = p_field_id AND entity_id = p_entity AND status = 'active') THEN
    RAISE EXCEPTION '字段 % 不存在或不属于实体 % / 已废弃', p_field_id, p_entity;
  END IF;

  IF p_code IS NOT NULL THEN
    PERFORM assert_option(p_field_id, p_code);
  END IF;
  IF p_codes IS NOT NULL THEN
    FOREACH c IN ARRAY p_codes LOOP
      PERFORM assert_option(p_field_id, c);
    END LOOP;
  END IF;

  vid := 'fvl_' || replace(gen_random_uuid()::text, '-', '');

  INSERT INTO field_value (value_id, field_id, subject_type, subject_id,
                           instance_key, array_index, value_text, value_num,
                           value_date, value_code, value_codes, confidence,
                           verify_status)
  VALUES (vid, p_field_id, p_entity, p_subject_id,
          p_instance_key, p_array_index, p_text, p_num,
          p_date, p_code, p_codes, p_confidence, 'V2')
  ON CONFLICT (field_id, subject_type, subject_id, instance_key, array_index)
  DO UPDATE SET value_text = EXCLUDED.value_text,
                value_num  = EXCLUDED.value_num,
                value_date = EXCLUDED.value_date,
                value_code = EXCLUDED.value_code,
                value_codes= EXCLUDED.value_codes,
                confidence = EXCLUDED.confidence,
                recorded_at= now()
  RETURNING value_id INTO vid;

  RETURN vid;
END $$;

-- ---------------------------------------------------------------------------
-- 5. 建实例（行）—— 零 DDL，自动分流"已知列"与"扩展字段"
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION create_instance(
  p_entity  TEXT,
  p_id      TEXT,
  p_payload JSONB
) RETURNS TEXT
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  tname    TEXT;
  v_kind   TEXT;
  key      TEXT;
  known    JSONB := '{}'::jsonb;
  extra    JSONB := '{}'::jsonb;
  attrs    JSONB := '{}'::jsonb;
  cols     TEXT[];
  collist  TEXT;
  pk       TEXT;
  fid      TEXT;
BEGIN
  SELECT ec.table_name, ec.kind INTO tname, v_kind
  FROM entity_catalog ec WHERE ec.entity_id = p_entity AND ec.status = 'active';
  IF v_kind IS NULL THEN
    RAISE EXCEPTION '实体 % 未登记', p_entity;
  END IF;

  -- 逐键分流：已知列 / 已登记扩展字段 / 未登记（拒绝）
  FOR key IN SELECT jsonb_object_keys(p_payload) LOOP
    IF tname IS NOT NULL AND EXISTS (
         SELECT 1 FROM information_schema.columns
         WHERE table_schema='mt' AND table_name=tname AND column_name=key) THEN
      known := known || jsonb_build_object(key, p_payload -> key);
    ELSIF EXISTS (SELECT 1 FROM field_catalog
                  WHERE field_id = key AND entity_id = p_entity AND status='active') THEN
      extra := extra || jsonb_build_object(key, p_payload -> key);
    ELSIF EXISTS (SELECT 1 FROM attribute_definition
                  WHERE attr_key = key AND entity_id = p_entity AND status <> 'deprecated') THEN
      attrs := attrs || jsonb_build_object(key, p_payload -> key);
    ELSE
      RAISE EXCEPTION
        '字段 % 既不是表 % 的列，也不是已登记的扩展字段。请先调用 mt.add_dimension(%, %, ...) 或登记 attribute_definition',
        key, COALESCE(tname,'(纯值实体)'), quote_literal(p_entity), quote_literal(key)
        USING ERRCODE = 'undefined_column';
    END IF;
  END LOOP;

  IF tname IS NULL THEN
    -- 纯值实体：全部写 field_value
    FOR key IN SELECT jsonb_object_keys(extra) LOOP
      PERFORM set_value(p_entity, p_id, key,
                        p_code  => extra ->> key,
                        p_text  => CASE WHEN jsonb_typeof(extra->key) = 'string'
                                        THEN extra ->> key END);
    END LOOP;
    RETURN p_id;
  END IF;

  -- 类型化表：补齐主键与 attrs，再动态插入
  IF NOT (known ? p_id_column(tname)) THEN
    known := known || jsonb_build_object(p_id_column(tname), p_id);
  END IF;
  IF attrs <> '{}'::jsonb THEN
    known := known || jsonb_build_object('attrs', attrs);
  END IF;

  -- 关键：只插入 payload 中显式给出的列。
  -- 不能用 `INSERT ... SELECT * FROM jsonb_populate_record(...)`——那会把未提供的列
  -- 全部填成 NULL，从而**绕过列默认值**并触发 NOT NULL 违约。
  SELECT string_agg(format('%I', k), ', ') INTO collist
  FROM jsonb_object_keys(known) AS t(k);

  EXECUTE format(
    'INSERT INTO mt.%1$I (%2$s) SELECT %2$s FROM jsonb_populate_record(NULL::mt.%1$I, $1)',
    tname, collist) USING known;

  -- 扩展字段写 field_value
  FOR key IN SELECT jsonb_object_keys(extra) LOOP
    PERFORM set_value(p_entity, p_id, key,
                      p_code => CASE WHEN jsonb_typeof(extra->key) = 'string'
                                     THEN extra ->> key END,
                      p_num  => CASE WHEN jsonb_typeof(extra->key) = 'number'
                                     THEN (extra ->> key)::numeric END,
                      p_text => CASE WHEN jsonb_typeof(extra->key) = 'string'
                                     THEN extra ->> key END);
  END LOOP;

  RETURN p_id;
END $$;

-- 取表的主键列名（单列主键）
CREATE OR REPLACE FUNCTION p_id_column(p_table TEXT) RETURNS TEXT
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT a.attname
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
  WHERE n.nspname='mt' AND c.relname = p_table
    AND i.indisprimary AND array_length(i.indkey,1) = 1
  LIMIT 1;
$$;

-- ---------------------------------------------------------------------------
-- 6. 删：维度软删除 / 实例删除
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION deprecate_dimension(p_field_id TEXT, p_reason TEXT DEFAULT NULL)
RETURNS TEXT
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE ct TEXT;
BEGIN
  SELECT code_table_id INTO ct FROM field_catalog WHERE field_id = p_field_id;
  IF ct IS NULL AND NOT EXISTS (SELECT 1 FROM field_catalog WHERE field_id = p_field_id) THEN
    RAISE EXCEPTION '字段 % 不存在', p_field_id;
  END IF;

  UPDATE field_catalog
  SET status = 'deprecated',
      version = (split_part(version,'.',1)::int + 1) || '.0.0',
      updated_at = now()
  WHERE field_id = p_field_id;

  IF ct IS NOT NULL THEN
    UPDATE code_table SET status = 'deprecated', updated_at = now()
    WHERE code_table_id = ct;
  END IF;

  INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
  VALUES (session_user, 'field', p_field_id, 'deprecate', NULL,
          jsonb_build_object('reason', p_reason, 'data_kept', true));
  RETURN p_field_id;
END $$;

CREATE OR REPLACE FUNCTION delete_instance(p_entity TEXT, p_id TEXT)
RETURNS TEXT
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  tname  TEXT;
  pk     TEXT;
  has_st BOOLEAN;
  n      INT;
BEGIN
  SELECT table_name INTO tname FROM entity_catalog WHERE entity_id = p_entity;
  IF tname IS NULL THEN
    DELETE FROM field_value WHERE subject_type = p_entity AND subject_id = p_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN format('value_only:%s values deleted', n);
  END IF;

  pk := p_id_column(tname);
  SELECT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema='mt' AND table_name=tname AND column_name='status')
    INTO has_st;

  IF has_st THEN
    EXECUTE format('UPDATE mt.%I SET status = %L WHERE %I = $1', tname, 'withdrawn', pk)
      USING p_id;
    DELETE FROM field_value WHERE subject_type = p_entity AND subject_id = p_id;
    RETURN 'soft:status=withdrawn';
  ELSE
    EXECUTE format('DELETE FROM mt.%I WHERE %I = $1', tname, pk) USING p_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    DELETE FROM field_value WHERE subject_type = p_entity AND subject_id = p_id;
    RETURN format('hard:%s rows deleted', n);
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 7. 读：表单定义（前端 n 选项的来源）与个人画像
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_form_schema AS
SELECT f.entity_id,
       f.field_id,
       f.title,
       f.description,
       f.data_type,
       f.cardinality,
       f.value_type,
       f.is_required,
       f.form_section,
       f.access_tier,
       f.status,
       f.code_table_id,
       COALESCE((
         SELECT jsonb_agg(jsonb_build_object(
                  'code', v.code, 'label', v.label_zh,
                  'en', v.label_en, 'sort', v.sort_order)
                ORDER BY v.sort_order, v.code)
         FROM code_value v
         WHERE v.code_table_id = f.code_table_id AND NOT v.deprecated
       ), '[]'::jsonb) AS options,
       jsonb_array_length(COALESCE((
         SELECT jsonb_agg(v.code) FROM code_value v
         WHERE v.code_table_id = f.code_table_id AND NOT v.deprecated), '[]'::jsonb)) AS option_count,
       COALESCE(f.showcase_order, 9999) AS form_order
FROM field_catalog f
WHERE f.status = 'active';

-- 前端一次性拿到整个表单（含 n 个选项）
CREATE OR REPLACE FUNCTION form_schema(p_entity TEXT) RETURNS JSONB
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT jsonb_build_object(
    'entity', p_entity,
    'generated_at', now(),
    'fields', COALESCE(jsonb_agg(jsonb_build_object(
        'field_id', field_id, 'title', title, 'description', description,
        'data_type', data_type, 'cardinality', cardinality,
        'required', is_required, 'section', form_section,
        'options', options, 'option_count', option_count)
      ORDER BY form_order, field_id), '[]'::jsonb))
  FROM v_form_schema WHERE entity_id = p_entity;
$$;

-- 某个人的完整画像（含扩展维度）
-- 键用 field_id#instance#array 组合，避免同字段多实例时 jsonb_object_agg 撞键报错
CREATE OR REPLACE FUNCTION profile_json(p_entity TEXT, p_subject_id TEXT) RETURNS JSONB
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT jsonb_object_agg(
           v.field_id || '#' || v.instance_key || '#' || v.array_index::text,
           jsonb_build_object(
             'field_id', v.field_id,
             'title', f.title,
             'code', v.value_code,
             'codes', v.value_codes,
             'text', v.value_text,
             'num', v.value_num,
             'instance_key', v.instance_key,
             'recorded_at', v.recorded_at))
  FROM field_value v
  JOIN field_catalog f ON f.field_id = v.field_id
  WHERE v.subject_type = p_entity AND v.subject_id = p_subject_id;
$$;

-- 选项清单（前端做下拉框直接用这个）
CREATE OR REPLACE VIEW v_option_set AS
SELECT ct.code_table_id,
       ct.name AS table_name,
       v.code,
       v.label_zh,
       v.label_en,
       v.sort_order,
       ct.status
FROM code_table ct
JOIN code_value v ON v.code_table_id = ct.code_table_id
WHERE NOT v.deprecated
ORDER BY ct.code_table_id, v.sort_order, v.code;

COMMIT;

-- ============================================================================
-- 使用示例（见 ops/tests/dynamic_model_test.sql）
--   SELECT mt.register_entity('profile_extra','talent','EK2');           -- 新的"纯值实体"
--   SELECT mt.add_dimension('person','F_PROFILE_A','基层服务意愿',
--            '[{"code":"A1","label":"非常愿意"},{"code":"A2","label":"愿意"}]'::jsonb);
--   SELECT mt.set_value('person','per_001','F_PROFILE_A', p_code => 'A1');
--   SELECT mt.create_instance('person','per_002','{"person_id":"per_002","subject_code":"MT-2","F_PROFILE_A":"A2"}'::jsonb);
--   SELECT mt.form_schema('person');
-- ============================================================================
