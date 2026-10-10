-- ============================================================================
-- 医学生人才信息库 · 043 修复授权自愈入口（042 引入的 P0）v1.0.0
--
-- 缺陷（独立审查发现，实测复现）
-- ---------------------------------------------------------------------------
--   $ SELECT * FROM mt.apply_column_grants();
--   ERROR:  function mt.public_counts() does not exist
--   CONTEXT:  PL/pgSQL function apply_role_baseline() line 12 at EXECUTE
--
-- 042 为了解决"取一张表却把 89 张全数一遍"的性能问题，把无参的
-- `public_counts()` DROP 掉、只保留 `public_counts(text)`。
-- 但 `apply_role_baseline()`（由 031 与 038 定义）里仍然写着：
--     GRANT EXECUTE ON FUNCTION mt.public_counts() TO %I
-- —— 无参签名已经不存在，于是**整个对账函数报错**。
--
-- 为什么这是 P0（不是小毛病）
-- ---------------------------------------------------------------------------
-- `apply_column_grants()` 是本项目**唯一的授权对账入口**（029 自述）。
-- 它一报错，意味着：
--   · 任何 `column_policy` 调整都落不到实际授权上；
--   · 任何列的收回/重建、任何新表接入都只能手工 GRANT；
--   · 而 036/037/038/040/041 全都在末尾调它 —— 那些迁移当时是成功的，
--     所以**缺陷不会在应用时暴露**，只在"下一次需要自愈"时才炸。
-- 更糟的是没有任何指标盯着它：access_test 有一处调用会失败，但门禁不看这个。
-- 所以本迁移除了修签名，还要**加一条 MUST 指标**（见 ops/health.py 的 I4.9），
-- 让"对账入口可用"变成门禁的一部分 —— 否则同类破坏还会再发生一次。
--
-- 教训（写下来）
-- ---------------------------------------------------------------------------
-- **DROP FUNCTION 之后必须全文搜索该函数的调用点。** 我改 042 时只想到
-- "重授权限"，没想到"别的函数体里也 GRANT 了它"—— 函数体里的引用不会出现在
-- `pg_depend` 的普通依赖里，常规的"有无依赖"检查也拦不住。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 调用验证。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 重定义 apply_role_baseline()：把 public_counts 的引用改成**带参签名**。
-- 其余内容与 038 版本一致（视图可见性仍由 public_view_registry 驱动）。
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  n_public int;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO %I', r);
    -- ⚠ 必须是**带参签名** public_counts(text)：042 起无参版本已不存在。
    --   这里正是 042 的破坏点，写死注释防止再改回无参。
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.public_counts(text) TO %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_session_new(text,text,text,interval,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_user_add(text,text,text,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON mt.column_profile FROM %I', r);
  END LOOP;

  EXECUTE 'REVOKE ALL ON mt.column_profile FROM mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT SELECT ON mt.column_profile TO mt_t3';

  -- 视图：注册表驱动（038）
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    IF EXISTS (SELECT 1 FROM public_view_registry pvr WHERE pvr.viewname = v.viewname) THEN
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t0, mt_t1, mt_t2, mt_t3', v.viewname);
    ELSE
      EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
    END IF;
  END LOOP;

  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.public_counts(text) TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.public_counts(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_lookup(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_revoke(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_login(text, text, interval, text) TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.public_view_registry TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants, '
          'mt.v_column_profile_coverage TO mt_portal';

  SELECT count(*) INTO n_public FROM public_view_registry;
  RAISE NOTICE '视图授权已按注册表重建：公开 % 个，其余只给 T3', n_public;
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限。视图可见性由 mt.public_view_registry 驱动（038）。'
  '**043 修正**：public_counts 的 GRANT 必须用带参签名 public_counts(text) —— '
  '042 DROP 了无参版本而没同步改这里，导致整个对账函数报错（P0）。';

-- 现在对账入口必须能真的跑完
DO $$
DECLARE n int;
BEGIN
  PERFORM apply_column_grants();
  SELECT count(*) INTO n FROM column_policy;
  RAISE NOTICE '对账入口已恢复：apply_column_grants() 正常返回，column_policy % 行', n;
END $$;

COMMIT;
