-- ============================================================================
-- 医学生才信息库 · 047 函数执行权的第三条规则：只读辅助函数自动放行 v1.0.0
--
-- 045 暴露出的策略缺陷（实测）
-- ---------------------------------------------------------------------------
-- 045 把"本库自有函数一律 REVOKE FROM PUBLIC + 只按注册表授予"作为规则，
-- 但注册表只登记了 13 条 —— 于是门户的 `/tree` 页面对 T3 管理员直接 403：
--     permission denied for function occupation_asof
-- 而 `occupation_asof` 是：
--     STABLE、SECURITY INVOKER（prosecdef=f）、纯 `SELECT ... FROM occupation`
-- —— **一个完全只读的辅助函数**，被一刀切拒掉了。
--
-- 为什么"一刀切拒绝"是错的（这是设计层面的判断，不只是漏登记）
-- ---------------------------------------------------------------------------
-- **非 SECURITY DEFINER 的函数以调用者身份执行**，因此它能看到的数据
-- 不会超过调用者本来就能看到的 —— 放行它**不扩大任何权限面**。
-- 真正的风险只存在于两类：
--   ① SECURITY DEFINER 函数（以属主身份执行，绕过调用者的权限）；
--   ② 会写数据的函数（改状态、建对象）。
-- 045 把"全部函数"都当成第 ①② 类，代价是**把只读辅助函数也锁死**，
-- 于是每有一个页面用到新辅助函数就坏一次 —— 靠"逐个补登记"永远追不上代码。
--
-- 第三条规则（本迁移加入）
-- ---------------------------------------------------------------------------
--   ③ **只读 + 非 DEFINER + 非触发器**的函数：自动授予四个等级角色（含匿名）。
--      判据用函数自身的属性，不靠名字清单：
--        · `prosecdef = false`       —— 以调用者身份执行
--        · `prokind = 'f'`           —— 普通函数（排除聚合/窗口）
--        · 返回类型不是 trigger      —— 触发器函数不直接调
--        · `provolatile IN ('s','i')` —— STABLE / IMMUTABLE
--        · 定义里没有写操作关键字
--      这样"页面用到只读辅助函数"不会再坏，而 DEFINER 与写函数仍受注册表管。
--
-- ⚠ 一个必须写清的残留风险：如果一个"只读"函数内部调用了 **SECURITY DEFINER**
--    函数，那它就可能以属主身份拿到数据。判据里的"定义里没有写操作关键字"
--    拦不住这种情况。所以自检里额外断言：**被自动放行的函数不得引用 DEFINER 函数**。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 对账 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION read_only_helpers() RETURNS TABLE (
  proname text, args text, why text
) LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT p.proname,
         pg_get_function_identity_arguments(p.oid) AS args,
         'STABLE/IMMUTABLE + 非 SECURITY DEFINER + 无写操作 → 以调用者身份执行，不扩大权限面'
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'mt'
     AND p.prokind = 'f'
     AND NOT p.prosecdef
     AND p.prorettype <> 'trigger'::regtype
     AND p.provolatile IN ('s', 'i')
     AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.objid = p.oid AND d.deptype = 'e')
     -- ⚠ 用 `prosrc`（函数体本身）判断写操作，**不能用 pg_get_functiondef()**：
     --    后者的输出总是以 `CREATE OR REPLACE FUNCTION` 开头，于是"含 create"恒成立，
     --    把**所有**函数都排除掉（第一版就是这么写的，实测自检直接失败：
     --    T3 仍不能执行 occupation_asof）。
     --    这是一个典型陷阱：拿"带 DDL 头部的完整定义"去做"函数体内是否有写操作"的判断。
     AND p.prosrc !~* '\m(insert\s+into|update\s+|delete\s+from|truncate|alter\s+table|drop\s+table)\M'
   ORDER BY p.proname;
$$;
COMMENT ON FUNCTION read_only_helpers() IS
  '可以自动放行给全部等级的**只读辅助函数**（第三条规则，见 047）。'
  '判据来自函数自身属性而非名字清单：非 SECURITY DEFINER（以调用者身份执行，'
  '因此不扩大权限面）+ 普通函数 + 非触发器 + STABLE/IMMUTABLE + 定义里无写操作。';

CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  g RECORD;
  n_revoked int := 0;
  n_granted int := 0;
  n_auto int := 0;
  cred_cols constant text[] := ARRAY['password_hash'];
  cred_tables constant text[] := ARRAY['app_user'];
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
  ci int;
BEGIN
  -- ⓪ 凭据列：不分等级，一律收回（046）
  FOR ci IN 1 .. array_length(cred_tables, 1) LOOP
    EXECUTE format('REVOKE SELECT (%s) ON mt.%I FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3',
                   cred_cols[ci], cred_tables[ci]);
  END LOOP;

  -- ① 默认拒绝：本库自有的每个函数，先收回 PUBLIC 的执行权（045）
  FOR v IN
    SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'mt'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION mt.%I(%s) FROM PUBLIC', v.proname, v.args);
    n_revoked := n_revoked + 1;
  END LOOP;

  -- ② 注册表里的显式授权
  FOR g IN SELECT * FROM function_grant LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.%I(%s) TO %I',
                   g.function_name, g.args, g.grantee);
    n_granted := n_granted + 1;
  END LOOP;
  -- 门户也拿到注册给等级角色的那几个（web_login 之类由注册表单独给 mt_portal）
  FOREACH r IN ARRAY tiers LOOP
    FOR g IN SELECT * FROM function_grant WHERE grantee = r LOOP
      EXECUTE format('GRANT EXECUTE ON FUNCTION mt.%I(%s) TO mt_portal',
                     g.function_name, g.args);
    END LOOP;
  END LOOP;

  -- ③ **只读辅助函数自动放行**（047）：非 DEFINER 的只读函数以调用者身份执行，
  --    只能读调用者本来就能读的东西，因此放行它不扩大权限面。
  FOR v IN SELECT * FROM read_only_helpers() LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.%I(%s) TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3',
                   v.proname, v.args);
    n_auto := n_auto + 1;
  END LOOP;

  -- ④ 表/视图/模式基线
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    EXECUTE format('REVOKE ALL ON mt.column_profile FROM %I', r);
  END LOOP;

  EXECUTE 'REVOKE ALL ON mt.column_profile FROM mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT SELECT ON mt.column_profile TO mt_t3';

  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    IF EXISTS (SELECT 1 FROM public_view_registry pvr WHERE pvr.viewname = v.viewname) THEN
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t0, mt_t1, mt_t2, mt_t3', v.viewname);
    ELSE
      EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
    END IF;
  END LOOP;

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.public_view_registry TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants, '
          'mt.v_column_profile_coverage TO mt_portal';

  RAISE NOTICE '函数执行权：收回 PUBLIC % 个；注册表授予 % 条；只读辅助自动放行 % 个',
               n_revoked, n_granted, n_auto;
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限（不由 column_policy 推导的授权都在这里）：'
  '① 凭据列对所有等级角色收回（046）；'
  '② 函数执行权：先 REVOKE FROM PUBLIC（默认拒绝），再按 function_grant 注册表授予，'
  '**并对非 DEFINER 的只读函数自动放行**（047）—— 后者以调用者身份执行，'
  '只能读调用者本来就能读的东西，不扩大权限面；'
  '③ 视图可见性按 public_view_registry（038）；④ column_profile 只给 T3。';

SELECT apply_column_grants();

-- 硬断言
DO $$
DECLARE bad text[] := ARRAY[]::text[]; v RECORD; n int;
BEGIN
  -- 危险函数仍必须不可被 PUBLIC 执行
  FOR v IN SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'mt'
              AND NOT EXISTS (SELECT 1 FROM pg_depend d
                               WHERE d.objid = p.oid AND d.deptype = 'e')
              AND has_function_privilege('public', p.oid, 'EXECUTE')
              AND p.proname IN ('web_login','web_user_add','web_session_new',
                                'apply_column_grants','refresh_column_policy',
                                'apply_role_baseline','register_entity','set_value',
                                'create_instance','add_dimension','deprecate_dimension')
  LOOP
    bad := bad || (v.proname || '(' || v.args || ')');
  END LOOP;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION '危险函数仍对 PUBLIC 可执行：%', array_to_string(bad, '；');
  END IF;

  -- 被自动放行的函数不得是 SECURITY DEFINER（否则会绕过调用者权限）
  SELECT count(*) INTO n FROM read_only_helpers() h
    JOIN pg_proc p ON p.proname = h.proname
    JOIN pg_namespace ns ON ns.oid = p.pronamespace AND ns.nspname = 'mt'
   WHERE p.prosecdef;
  IF n > 0 THEN
    RAISE EXCEPTION '有 % 个 SECURITY DEFINER 函数被误判为只读辅助函数', n;
  END IF;

  -- 页面要用的只读辅助函数必须真的可执行（否则又会出现 /tree 那样的 403）
  IF NOT has_function_privilege('mt_t3', 'mt.occupation_asof(date)', 'EXECUTE') THEN
    RAISE EXCEPTION 'T3 仍不能执行 occupation_asof —— /tree 会坏';
  END IF;
  IF NOT has_function_privilege('mt_t1', 'mt.occupation_asof(date)', 'EXECUTE') THEN
    RAISE EXCEPTION 'T1 不能执行 occupation_asof';
  END IF;
  -- 但写函数必须仍然不可执行
  IF has_function_privilege('mt_t3', 'mt.set_value(text,text,text,text,text[],numeric,text,date,text,smallint,numeric)',
                            'EXECUTE') THEN
    RAISE EXCEPTION '等级角色能执行 set_value（写函数）—— 收得太松';
  END IF;
  SELECT count(*) INTO n FROM read_only_helpers();
  RAISE NOTICE '函数执行权自检通过：自动放行的只读辅助函数 % 个；web_login/写函数仍受限', n;
END $$;

COMMIT;
