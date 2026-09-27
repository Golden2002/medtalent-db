-- ============================================================================
-- 医学生人才信息库 · 扩展属性门禁 v0.4.0（T04）
-- 目的：让"机制 B（属性登记驱动）"的纪律由**数据库**强制，而不是靠人自觉。
--   规则 1：任何写入 *_table.attrs 的键，必须已在 attribute_definition 登记
--           且 status <> 'deprecated'，并且 entity_id 等于该表名。
--   规则 2：field_value.field_id 必须指向 field_catalog 中 status='active' 的字段。
-- 这样"绕过字典直接塞字段"会在数据库层被拒绝。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;   -- 同上：DROP ... IF EXISTS 的 NOTICE 无需刷屏

-- ---------------------------------------------------------------------------
-- 规则 1：attrs 键必须已登记
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION guard_attrs() RETURNS trigger
LANGUAGE plpgsql
SET search_path = mt, public
AS $$
DECLARE
  k        text;
  unknown  text[] := ARRAY[]::text[];
BEGIN
  IF NEW.attrs IS NULL THEN
    RETURN NEW;
  END IF;
  FOR k IN SELECT jsonb_object_keys(NEW.attrs) LOOP
    IF NOT EXISTS (
      SELECT 1 FROM attribute_definition ad
      WHERE ad.attr_key = k
        AND ad.entity_id = TG_TABLE_NAME
        AND ad.status <> 'deprecated'
    ) THEN
      unknown := unknown || k;
    END IF;
  END LOOP;
  IF array_length(unknown, 1) > 0 THEN
    RAISE EXCEPTION
      '未登记的扩展属性：表 % 的 attrs 键 % 不存在于 attribute_definition。'
      '请先 INSERT INTO attribute_definition(attr_key, entity_id, title, data_type) 完成登记。',
      TG_TABLE_NAME, array_to_string(unknown, ', ')
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

DO $$
DECLARE t record;
BEGIN
  FOR t IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname='mt' AND c.relkind='r'
      AND EXISTS (SELECT 1 FROM information_schema.columns col
                  WHERE col.table_schema='mt' AND col.table_name=c.relname
                    AND col.column_name='attrs')
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS trg_attrs_%1$s ON mt.%1$I; '
      'CREATE TRIGGER trg_attrs_%1$s BEFORE INSERT OR UPDATE ON mt.%1$I '
      'FOR EACH ROW EXECUTE FUNCTION mt.guard_attrs();', t.relname);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 规则 2：field_value 只能引用已登记的活跃字段
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION guard_field_value() RETURNS trigger
LANGUAGE plpgsql
SET search_path = mt, public
AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM field_catalog WHERE field_id = NEW.field_id;
  IF st IS NULL THEN
    RAISE EXCEPTION 'field_value 引用了未登记的 field_id：%', NEW.field_id
      USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF st <> 'active' THEN
    RAISE EXCEPTION '字段 % 的状态为 %，不允许写入新值', NEW.field_id, st
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_fv_guard ON field_value;
CREATE TRIGGER trg_fv_guard BEFORE INSERT OR UPDATE ON field_value
FOR EACH ROW EXECUTE FUNCTION mt.guard_field_value();

COMMIT;
