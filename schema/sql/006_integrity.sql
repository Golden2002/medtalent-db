-- ============================================================================
-- 医学生人才信息库 · 完整性层 v0.4.1（T03）
-- 内容：
--   1. 业务级 CHECK 约束（域已覆盖 confidence/verify_status，本文件补语义约束）
--   2. touch_updated_at() —— 统一维护 updated_at
--   3. log_change()       —— append-only 变更流水（谁、何时、动了哪张表哪一行）
--
-- 两条设计选择：
--   · 约束用 DROP ... IF EXISTS + ADD，使本文件可重复执行（迁移可重放）。
--   · 审计触发器默认**挂到 mt 下所有表**，只排除日志/批量/派生表。
--     理由：v0.4.0 用手写白名单时漏掉了 observation_window，被回归测试抓到；
--     白名单式审计在表持续增加时必然漏项，改用排除法更稳。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
-- 本文件大量使用 DROP ... IF EXISTS 以实现可重放；屏蔽其 NOTICE 噪音，只保留警告以上
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 语义 CHECK 约束（幂等写法）
-- ---------------------------------------------------------------------------
ALTER TABLE skill_assertion DROP CONSTRAINT IF EXISTS chk_assert_transfer_note;
ALTER TABLE skill_assertion
  ADD CONSTRAINT chk_assert_transfer_note
  CHECK (transferability IS NULL OR transferability < 3 OR transfer_note IS NOT NULL);

ALTER TABLE job_posting DROP CONSTRAINT IF EXISTS chk_job_salary_range;
ALTER TABLE job_posting
  ADD CONSTRAINT chk_job_salary_range
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max);

ALTER TABLE education_record DROP CONSTRAINT IF EXISTS chk_edu_dates;
ALTER TABLE education_record
  ADD CONSTRAINT chk_edu_dates CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date);

ALTER TABLE employment_record DROP CONSTRAINT IF EXISTS chk_emp_dates;
ALTER TABLE employment_record
  ADD CONSTRAINT chk_emp_dates CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date);

ALTER TABLE training_record DROP CONSTRAINT IF EXISTS chk_trn_dates;
ALTER TABLE training_record
  ADD CONSTRAINT chk_trn_dates CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date);

ALTER TABLE observation_window DROP CONSTRAINT IF EXISTS chk_ow_dates;
ALTER TABLE observation_window
  ADD CONSTRAINT chk_ow_dates CHECK (end_date IS NULL OR start_date <= end_date);

ALTER TABLE subject_request DROP CONSTRAINT IF EXISTS chk_sr_due;
ALTER TABLE subject_request
  ADD CONSTRAINT chk_sr_due CHECK (due_at IS NULL OR due_at >= received_at);

ALTER TABLE job_requirement DROP CONSTRAINT IF EXISTS chk_req_min_level;
ALTER TABLE job_requirement
  ADD CONSTRAINT chk_req_min_level CHECK (min_level IS NULL OR min_level BETWEEN 0 AND 5);

ALTER TABLE job_requirement DROP CONSTRAINT IF EXISTS chk_req_essentiality;
ALTER TABLE job_requirement
  ADD CONSTRAINT chk_req_essentiality CHECK (essentiality IS NULL OR essentiality BETWEEN 0 AND 1);

ALTER TABLE observation_window DROP CONSTRAINT IF EXISTS chk_ow_type;
ALTER TABLE observation_window
  ADD CONSTRAINT chk_ow_type CHECK (window_type IN ('W1','W2','W3','W4'));

ALTER TABLE skill_assertion DROP CONSTRAINT IF EXISTS chk_assert_claim_type;
ALTER TABLE skill_assertion
  ADD CONSTRAINT chk_assert_claim_type CHECK (claim_type IS NULL OR claim_type IN ('CT1','CT2','CT3'));

-- ---------------------------------------------------------------------------
-- 2. updated_at 自动维护（挂到所有带 updated_at 的表）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql
SET search_path = mt, public
AS $$
BEGIN
  NEW.updated_at := now();
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
    WHERE n.nspname = 'mt' AND c.relkind = 'r'
      AND EXISTS (SELECT 1 FROM information_schema.columns col
                  WHERE col.table_schema='mt' AND col.table_name=c.relname
                    AND col.column_name='updated_at')
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS trg_touch_%1$s ON mt.%1$I; '
      'CREATE TRIGGER trg_touch_%1$s BEFORE UPDATE ON mt.%1$I '
      'FOR EACH ROW EXECUTE FUNCTION mt.touch_updated_at();', t.relname);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 3. 变更流水（append-only）—— 排除法挂载
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION log_change() RETURNS trigger
LANGUAGE plpgsql
SET search_path = mt, public
AS $$
DECLARE
  rec   jsonb;
  k     text;
  ident text := 'unknown';
BEGIN
  rec := to_jsonb(COALESCE(NEW, OLD));
  FOR k IN SELECT jsonb_object_keys(rec) LOOP
    IF k = 'id' OR k LIKE '%\_id' THEN
      ident := k || '=' || COALESCE(rec ->> k, '');
      EXIT;
    END IF;
  END LOOP;

  INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
  VALUES (current_user, TG_TABLE_NAME, ident, lower(TG_OP),
          jsonb_build_object('op', TG_OP, 'table', TG_TABLE_NAME, 'row', ident));
  RETURN NULL;   -- AFTER 触发器返回值被忽略
END;
$$;

DO $$
DECLARE
  t record;
  -- 排除：自身日志、批量采集、高频派生、闭包缓存
  skip text[] := ARRAY[
    'change_log','access_log','provenance','ingest_run',
    'field_value','search_document','embedding',
    'derived_feature','match_result','match_run','gap_analysis',
    'data_lifecycle_run','concept_ancestor','category_node','job_task'
  ];
  n int := 0;
BEGIN
  FOR t IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n2 ON n2.oid = c.relnamespace
    WHERE n2.nspname='mt' AND c.relkind='r'
      AND c.relname <> ALL(skip)
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS trg_audit_%1$s ON mt.%1$I; '
      'CREATE TRIGGER trg_audit_%1$s AFTER INSERT OR UPDATE OR DELETE ON mt.%1$I '
      'FOR EACH ROW EXECUTE FUNCTION mt.log_change();', t.relname);
    n := n + 1;
  END LOOP;
  RAISE NOTICE '审计触发器已挂载到 % 张表', n;
END $$;

COMMIT;
