-- ============================================================================
-- 医学生人才信息库 · 023 门户角色的最小授权与视图的可控开放 v1.0.0
--
-- 背景：门户从 023 起改用 mt_portal 连接（见 code/demo/portal.py 的 PORTAL_DSN）。
--      它自身**没有任何权限**，必须 SET LOCAL ROLE mt_tN。本迁移补齐它真正需要的两样东西。
--
-- 【1】日志写入：mt_portal 需要 log_access 的执行权
--      log_access 是 SECURITY DEFINER（迁移 020），所以调用者不需要 access_log 的表权限，
--      只需要 EXECUTE。022 给了 web_login 等，忘了给 log_access —— 于是
--      "记录访问用户"这条需求在门户里会静默失败（审计写不进去但不影响页面，
--      这正是 log_visit 里"失败也要打印 stderr"的原因：不让它变成查不出来的洞）。
--
-- 【2】视图：只开放给 T3，不给 T0–T2
--      这条要写清理由，因为它是本次唯一一处"看起来像放宽"的决定：
--        · 视图默认以**视图属主**的权限执行（PG 16 有 security_invoker 选项，默认 false），
--          属主是 postgres。所以给谁视图，谁就能读到视图里的**全部列**，
--          **绕过 column_policy 的列级授权**。实测确认过这个后门：
--          限制列权限后，应用仍能通过视图读到本无权读的列。
--        · T3 是最高等级，本来就已经拥有全部 1029 列（`apply_column_grants` 给它的）。
--          所以对 T3 授视图**不构成额外泄露** —— 它只是让 T3 能渲染页面。
--        · T0/T1/T2 一律不授视图。它们要读数据必须走列的授权，
--          于是"视图后门"对它们不存在。
--      ⚠ 这也意味着：**页面用的视图在低等级下会报权限不足**。这不是 bug，
--        而是"这个页面的数据对当前等级不可见"的正确答案 —— 门户把它渲染成
--        「权限不足」页（见 portal.py 的 view_no_permission），而不是 500。
--
-- 【3】把这两条写进 apply_role_baseline()
--      必须写进去，否则下一次 `apply_column_grants()` 的"先收回全部"会把它们清掉 ——
--      这个坑本项目已经踩过三次（018 列权限 942→180、019 序列权限、020 审计写入）。
--      **"重建全部授权"的函数必须重建全部**，不能只重建它自己关心的那部分。
--
-- 可重放：CREATE OR REPLACE + 幂等调用。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    -- 审计写入：给的是**函数执行权**，不是 access_log 的表权限
    -- （表权限已在 020 收回：审计表只能由 SECURITY DEFINER 函数追加，应用不能自己造行）
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO %I', r);
    -- 读策略本身：让人能查"我为什么看不到某个字段"
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
  END LOOP;

  -- 视图只给最高等级（理由见本迁移开头；对 T3 不构成额外泄露）
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
    EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
  END LOOP;

  -- 门户登录角色：只读策略 + 执行认证/会话/审计函数
  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_new(text, text, text, interval, text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_lookup(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_revoke(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_login(text, text, interval, text) TO mt_portal';
  -- 会话活动视图需要看会话；给 mt_portal 一份只读的（它要用它判断"谁在线"）
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity TO mt_portal';
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限（模式使用 / 审计函数执行权 / 读策略 / 视图只给 T3）。'
  '必须由 apply_column_grants() 一并重建 —— "先收回全部"会把它清掉（已踩过三次）。'
  '视图只授 T3：视图以属主权限执行，会绕过列级策略；T3 本就拥有全部列，故不构成额外泄露。';

-- 对账
SELECT apply_column_grants();

-- 自检：把"基线是否真的建立"变成可查的事实
CREATE OR REPLACE VIEW v_access_baseline_check AS
SELECT x.role_name,
       has_schema_privilege(x.role_name, 'mt', 'USAGE') AS schema_usage,
       has_function_privilege(x.role_name,
         'mt.log_access(text,text,text,integer,text,jsonb)', 'EXECUTE') AS can_log,
       (SELECT count(*) FROM information_schema.column_privileges cp
         WHERE cp.grantee = x.role_name AND cp.privilege_type = 'SELECT') AS n_columns,
       (SELECT count(*) FROM information_schema.role_table_grants g
         JOIN pg_views v ON v.viewname = g.table_name AND v.schemaname = 'mt'
         WHERE g.grantee = x.role_name AND g.privilege_type = 'SELECT') AS n_views
  FROM (VALUES ('mt_t0'),('mt_t1'),('mt_t2'),('mt_t3')) x(role_name);
COMMENT ON VIEW v_access_baseline_check IS
  '基线自检：每个等级角色是否真的拿到了模式使用、审计写入、列权限、视图授权。'
  '这两样少一个都会静默失效（审计写不进去 / 页面渲染不出来），所以必须可查。';
GRANT SELECT ON v_access_baseline_check TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

COMMIT;
