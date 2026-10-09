-- ============================================================================
-- 医学生人才信息库 · 035 补齐 mt_bridge 的字典读取权 v1.0.0
--
-- 背景（实测扫出来的，不是猜的）
-- ---------------------------------------------------------------------------
-- 把 bridge 换成最小权限角色后逐次报错定位缺什么，太慢。
-- 于是写了一次静态分析（ops/fixtures/_bridge_privs.py）：把 bridge 四个模块里
-- SQL 语句提到的 FROM/JOIN/INSERT INTO/UPDATE/DELETE FROM 目标抽出来，
-- 与库里的表/视图对一遍，再逐个 has_table_privilege 检查。
-- 结果：**只缺一个** —— `crosswalk`（crosswalk 映射字典，bridge 用它把对方的
-- 题号/概念解析到本库 concept）。
--
-- 为什么 crosswalk 只要 SELECT：它是**映射字典**，与 code_value/field_catalog 同类 ——
-- 摄入时要查它，但不该改它。改映射是运维动作（走迁移），不是摄入路径的职责。
--
-- 这份静态分析值得留下：它把"逐个试错"换成"一次看全"，
-- 而且能反向发现"代码里提到但库里没有的名字"（正则误抓），
-- 避免把英文单词当成表名去授权。
--
-- 可重放：幂等 GRANT。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
              WHERE table_schema='mt' AND table_name='crosswalk') THEN
    EXECUTE 'GRANT SELECT ON mt.crosswalk TO mt_bridge';
    RAISE NOTICE '已给 mt_bridge 授予 crosswalk 的 SELECT';
  ELSE
    RAISE NOTICE 'mt 下没有 crosswalk，跳过';
  END IF;
END $$;

-- 自检：bridge 该读的都能读、该写的都能写，且没有多余能力
DO $$
DECLARE bad text[] := ARRAY[]::text[];
        must_read constant text[] := ARRAY['crosswalk', 'code_value', 'code_table',
                                          'field_catalog', 'entity_catalog', 'occupation',
                                          'job_posting', 'job_requirement', 'tombstone',
                                          'concept', 'attribute_definition'];
        must_write constant text[] := ARRAY['person', 'external_identity', 'sync_event',
                                           'answer', 'consent_record', 'response_session',
                                           'experience_episode', 'skill_assertion',
                                           'preference', 'assertion', 'match_result',
                                           'match_run', 'observation_window',
                                           'talent_deletion_request', 'education_record'];
        never constant text[] := ARRAY['person_pii', 'access_log', 'app_user', 'web_session',
                                       'column_profile', 'access_policy', 'column_policy',
                                       'export_denied', 'change_log'];
        t text;
BEGIN
  FOREACH t IN ARRAY must_read LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema='mt' AND table_name=t)
       AND NOT has_table_privilege('mt_bridge', 'mt.'||t, 'SELECT') THEN
      bad := bad || ('读不到 ' || t);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY must_write LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema='mt' AND table_name=t)
       AND NOT has_table_privilege('mt_bridge', 'mt.'||t, 'INSERT') THEN
      bad := bad || ('写不了 ' || t);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY never LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema='mt' AND table_name=t)
       AND has_table_privilege('mt_bridge', 'mt.'||t, 'SELECT') THEN
      bad := bad || ('不该读到 ' || t);
    END IF;
  END LOOP;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION 'mt_bridge 权限不符合预期：%', array_to_string(bad, '；');
  END IF;
  RAISE NOTICE 'mt_bridge 权限自检通过：字典可读、业务表可写、敏感表不可见';
END $$;

COMMIT;
