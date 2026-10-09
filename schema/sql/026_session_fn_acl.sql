-- ============================================================================
-- 医学生人才信息库 · 026 会话函数不得对 PUBLIC 可执行（堵上自造 T3 会话）v1.0.0
--
-- 漏洞（本轮诊断实测发现）
-- ---------------------------------------------------------------------------
-- PostgreSQL 的 `CREATE FUNCTION` **默认把 EXECUTE 授予 PUBLIC**。
-- 022 建函数时我只记得 `REVOKE ALL ON FUNCTION web_user_add ... FROM PUBLIC`，
-- 漏了 web_session_new / web_session_lookup / web_session_revoke / web_login。
--
-- 危害有多大（实测确认，不是推测）：
--     SELECT mt.web_session_new('attacker', 'T3');
-- 会返回一个**合法的新会话 id**，而 web_session_new 是 SECURITY DEFINER、
-- 且等级是**参数**。于是任何能连到数据库的角色，都能给自己造一个 T3 会话；
-- 把这个 id 放进 cookie，门户就会 `SET LOCAL ROLE mt_t3` —— 读走全部列。
-- 这是本项目最严重的一类问题：**不是读了不该读的，而是自己给自己发了权限。**
--
-- 为什么之前没被发现：`ops/tests/access_test.py` 测的是"角色能不能读列"，
-- 而这一条是"角色能不能**造身份**"——是不同的攻击面。
-- 本迁移同时给 access_test 加一条断言（见 ops/tests/access_test.py 的 A8）。
--
-- 正确的权限模型
-- ---------------------------------------------------------------------------
--   web_login          —— 门户需要调用（登录入口），给 mt_portal
--   web_session_lookup —— 门户需要调用（每次请求解析 cookie），给 mt_portal
--   web_session_revoke —— 门户需要调用（退出登录），给 mt_portal
--   web_session_new    —— **门户不需要**！它只被 web_login 内部调用。
--                         而 DEFINER 函数内部调用同属主（postgres）的函数时，
--                         走的是属主权限，所以收回 PUBLIC 也不影响登录。
--   web_user_add       —— 只该由运维工具调用（已收；这里再收一次并显式只给 postgres）
--
-- 教训（写下来，因为这个默认值很容易忘）：
--   **在 PostgreSQL 里，"我没授权" 不等于 "没人能用"——函数默认对 PUBLIC 开放。**
--   凡是 SECURITY DEFINER 且参数里含"权限级别/身份"的函数，必须显式 REVOKE。
--
-- 可重放：REVOKE/GRANT 均幂等。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 先全部收回（幂等），再按最小权限逐条授予。
-- ⚠ 必须同时收回**应用角色**：022 里我显式把 web_session_new 授给了 mt_portal，
--   所以只 REVOKE PUBLIC 是不够的 —— 本迁移第一版的自检当场拒绝了登记，
--   报"角色 mt_portal 仍能执行 web_session_new"。那个断言写对了。
REVOKE ALL ON FUNCTION web_session_new(text, text, text, interval, text)
  FROM PUBLIC, mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE ALL ON FUNCTION web_session_lookup(text) FROM PUBLIC, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE ALL ON FUNCTION web_session_revoke(text) FROM PUBLIC, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE ALL ON FUNCTION web_login(text, text, interval, text) FROM PUBLIC, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE ALL ON FUNCTION web_user_add(text, text, text, text)
  FROM PUBLIC, mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE ALL ON FUNCTION log_access(text, text, text, integer, text, jsonb) FROM PUBLIC;

-- 门户真正需要的三个 + 登录入口
GRANT EXECUTE ON FUNCTION web_login(text, text, interval, text) TO mt_portal;
GRANT EXECUTE ON FUNCTION web_session_lookup(text) TO mt_portal;
GRANT EXECUTE ON FUNCTION web_session_revoke(text) TO mt_portal;

-- web_session_new 只留给属主（web_login 内部以属主身份调用）
-- web_user_add 只留给属主（由 ops/secure.py 以 postgres 连接调用）
-- log_access 已在 020/023 授给四个等级角色与门户；PUBLIC 必须没有

-- 023 的基线函数也必须改：否则下一次 apply_column_grants() 会把 web_session_new
-- 重新授给 mt_portal（"重建授权"的函数要重建的是**正确**的那一版）
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    -- 等级角色**不能**建会话、不能登录：那是门户（mt_portal）的职责。
    -- 能建会话 = 能给自己发任意等级的身份证。
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_session_new(text,text,text,interval,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_user_add(text,text,text,text) FROM %I', r);
  END LOOP;

  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
    EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
  END LOOP;

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO mt_portal';
  -- 只授"登录/查会话/撤销会话"三个；**不授 web_session_new**（见本迁移开头）
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_lookup(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_revoke(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_login(text, text, interval, text) TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants TO mt_portal';
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限（模式使用 / 审计函数执行权 / 读策略 / 视图只给 T3 / 会话函数最小授权）。'
  '必须由 apply_column_grants() 一并重建。'
  '关键约束：web_session_new 与 web_user_add **绝不给任何应用角色** —— '
  '它们能造出任意等级的会话，等于跳过全部权限设计。';

-- 自检：把"谁能造会话"变成可查的事实
CREATE OR REPLACE VIEW v_session_function_grants AS
SELECT p.proname AS function_name,
       coalesce(array_to_string(p.proacl, ' | '), '(默认：PUBLIC 可执行)') AS acl,
       has_function_privilege('mt_portal', p.oid, 'EXECUTE') AS portal_can,
       has_function_privilege('mt_t0', p.oid, 'EXECUTE') AS t0_can,
       has_function_privilege('mt_t3', p.oid, 'EXECUTE') AS t3_can
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mt'
   AND p.proname IN ('web_login','web_session_new','web_session_lookup',
                     'web_session_revoke','web_user_add','log_access')
 ORDER BY p.proname;
COMMENT ON VIEW v_session_function_grants IS
  '会话与审计函数的执行权分布。判据：**除了 web_login/lookup/revoke 对 mt_portal，'
  '其余一律不给任何应用角色** —— 尤其是 web_session_new 绝不能可被调用（能调用就能自造 T3 会话）。';
GRANT SELECT ON v_session_function_grants TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 硬断言：web_session_new 绝不能被任何应用角色执行
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles
            WHERE rolname IN ('mt_portal','mt_t0','mt_t1','mt_t2','mt_t3') LOOP
    IF has_function_privilege(r.rolname, 'mt.web_session_new(text,text,text,interval,text)',
                              'EXECUTE') THEN
      RAISE EXCEPTION '角色 % 仍能执行 web_session_new —— 它可以自造任意等级的会话', r.rolname;
    END IF;
  END LOOP;
  RAISE NOTICE '会话函数权限自检通过：只有属主能执行 web_session_new';
END $$;

COMMIT;
