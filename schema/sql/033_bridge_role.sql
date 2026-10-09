-- ============================================================================
-- 医学生人才信息库 · 033 小程序接入改用最小权限写角色（mt_bridge）v1.0.0
--
-- 问题（指标 I4.1 一直在报）
-- ---------------------------------------------------------------------------
-- `code/bridge/` 是**唯一往库里写人才数据**的路径（小程序交换包摄入），
-- 而它一直以 **postgres 超级用户** 连接。后果与只读门户此前的问题同类：
-- **写操作绕过全部权限设计** —— 没有列级限制、没有 RLS、没有"不能删"的约束。
-- 只要 bridge 的代码里出现一个 DELETE 或一条越界的 UPDATE，数据库不会拦。
--
-- 本迁移建一个**只有它真正需要的能力**的写角色：
--   mt_bridge：非超级、不绕过 RLS、无 DDL、不能删人、读不到加密身份表。
--
-- 权限清单怎么定的（实测代码里的 SQL 动作，不是拍脑袋）
-- ---------------------------------------------------------------------------
-- 把 code/bridge/*.py 里的 SQL 动词与表名统计出来，得到：
--   · INSERT：answer / assertion / consent_record / education_record /
--             experience_episode / experience_task / external_identity / person /
--             preference / response_session / skill_assertion / sync_event /
--             match_result / match_run / observation_window / talent_deletion_request
--   · UPDATE：external_identity / person / sync_event
--   · DELETE：**只有 match_result 一处**（重算某人某次匹配前先清旧结果）
--   · SELECT：上述若干 + job_posting / job_requirement / tombstone 等
-- 所以：
--   · 给 INSERT/UPDATE/SELECT；
--   · **DELETE 只给 match_result 一张表** —— 不给 person/answer/consent_record 等，
--     因为"删人"必须是显式的运维动作（走 tombstone / talent_deletion_request 流程），
--     不能让摄入路径顺手删掉；
--   · 读不到 person_pii、access_log、app_user、web_session、column_profile 等。
--
-- ⚠ 一个必须写下来的取舍：给的是**表级** INSERT/UPDATE，而不是列级。
--    更严的做法是逐列授权（本项目的只读侧就是这么做的），但摄入方的职责恰恰是
--    "生产这些表"，逐列授权会让每次调整交换契约都要同步改授权，
--    而收益只在"bridge 被完全控制"这个假设下才体现 —— 那时它本来就能伪造整包数据。
--    所以这里选择表级 + **明确不给 DELETE/不给敏感表**，
--    并在自检里断言"读不到 person_pii、不能删 person"。
--
-- 可重放：DO 块判存在 + 幂等 GRANT/REVOKE。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mt_bridge') THEN
    CREATE ROLE mt_bridge LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
  ALTER ROLE mt_bridge NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
END $$;

COMMENT ON ROLE mt_bridge IS
  '小程序接入（code/bridge）的写角色：能摄入数据，但无 DDL、不能删人、读不到加密身份表。'
  '它取代了此前"用 postgres 连接"的做法 —— 那条路上写操作绕过全部权限（指标 I4.1）。';

-- 它需要写的表（表级 INSERT/UPDATE/SELECT）
DO $$
DECLARE t text;
  write_tables constant text[] := ARRAY[
    'answer', 'assertion', 'consent_record', 'education_record',
    'experience_episode', 'experience_task', 'external_identity', 'person',
    'preference', 'response_session', 'skill_assertion', 'sync_event',
    'match_result', 'match_run', 'observation_window', 'talent_deletion_request',
    'outbox', 'proj_person_summary'];
BEGIN
  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_bridge';
  FOREACH t IN ARRAY write_tables LOOP
    -- 表可能不存在（例如 proj_person_summary 在别的迁移里），存在才授权
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema='mt' AND table_name=t AND table_type='BASE TABLE') THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON mt.%I TO mt_bridge', t);
    END IF;
  END LOOP;
  -- 只读的参考表（摄入时要回查，不该能改）
  FOREACH t IN ARRAY ARRAY['job_posting', 'job_requirement', 'occupation', 'concept',
                           'code_value', 'code_table', 'field_catalog', 'entity_catalog',
                           'tombstone', 'talent_deletion_request', 'consent_withdrawal_action'] LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema='mt' AND table_name=t AND table_type='BASE TABLE') THEN
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_bridge', t);
    END IF;
  END LOOP;
END $$;

-- DELETE 只给 match_result（代码里唯一的 DELETE，且是"重算前清旧结果"）
GRANT DELETE ON mt.match_result TO mt_bridge;

-- 明确收回敏感对象（即使将来有人手滑 GRANT 到 PUBLIC，这里也收一次）
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['person_pii', 'access_log', 'app_user', 'web_session',
                           'column_profile', 'access_policy', 'column_policy',
                           'export_denied', 'field_value'] LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema='mt' AND table_name=t) THEN
      EXECUTE format('REVOKE ALL ON mt.%I FROM mt_bridge', t);
    END IF;
  END LOOP;
  -- 不许删人：person 的 DELETE 不给（GRANT 里只有 SELECT/INSERT/UPDATE）
  EXECUTE 'REVOKE DELETE, TRUNCATE ON mt.person FROM mt_bridge';
END $$;

-- 自检：把"最小权限"变成可查的事实
DO $$
DECLARE bad text[] := ARRAY[]::text[];
BEGIN
  IF (SELECT rolsuper FROM pg_roles WHERE rolname='mt_bridge') THEN
    bad := bad || '它是超级用户';
  END IF;
  IF (SELECT rolbypassrls FROM pg_roles WHERE rolname='mt_bridge') THEN
    bad := bad || '它绕过 RLS';
  END IF;
  IF has_table_privilege('mt_bridge', 'mt.person_pii', 'SELECT') THEN
    bad := bad || '它能读 person_pii';
  END IF;
  IF has_table_privilege('mt_bridge', 'mt.person', 'DELETE') THEN
    bad := bad || '它能删 person';
  END IF;
  IF has_table_privilege('mt_bridge', 'mt.access_log', 'SELECT') THEN
    bad := bad || '它能读 access_log';
  END IF;
  IF has_schema_privilege('mt_bridge', 'mt', 'CREATE') THEN
    bad := bad || '它能建表（有 DDL 权限）';
  END IF;
  IF NOT has_table_privilege('mt_bridge', 'mt.person', 'INSERT') THEN
    bad := bad || '它反而不能写 person（收得太紧了）';
  END IF;
  IF NOT has_table_privilege('mt_bridge', 'mt.sync_event', 'INSERT') THEN
    bad := bad || '它反而不能写 sync_event（收得太紧了）';
  END IF;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION 'mt_bridge 的权限不符合最小权限：%', array_to_string(bad, '；');
  END IF;
  RAISE NOTICE 'mt_bridge 权限自检通过：能写该写的，读不到不该读的，不能删人';
END $$;

-- 可查视图：让"接入角色到底能做什么"一眼可见
CREATE OR REPLACE VIEW v_bridge_privileges AS
SELECT c.relname AS 表, 
       string_agg(p.priv, ', ' ORDER BY p.priv) AS 权限
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'mt'
  CROSS JOIN LATERAL (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) AS p(priv)
 WHERE c.relkind = 'r'
   AND has_table_privilege('mt_bridge', c.oid, p.priv)
 GROUP BY c.relname
 ORDER BY c.relname;
COMMENT ON VIEW v_bridge_privileges IS
  '小程序接入角色 mt_bridge 的权限一览（供安全复核）。它应当：能写摄入相关的表、'
  '只在 match_result 上有 DELETE、对 person 只有 SELECT/INSERT/UPDATE。';
GRANT SELECT ON v_bridge_privileges TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

COMMIT;
