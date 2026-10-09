-- ============================================================================
-- 医学生人才信息库 · 020 访问日志只能由固定函数写入（SECURITY DEFINER）v1.0.0
--
-- 背景（019 之后剩下的那条失败）
-- ---------------------------------------------------------------------------
-- `SELECT mt.log_access(...)` 以 mt_t1 身份执行时报
--   **permission denied for table access_log**
-- 但 information_schema 里 mt_t1 明明有 access_log 的 INSERT 权限。
-- 原因很具体：log_access 里写的是 `INSERT ... RETURNING access_id`，
-- 而 **RETURNING 需要被返回列的 SELECT 权限** —— access_id 在策略里是 T2，
-- 所以低等级角色能插但不能"取回"。
--
-- 顺手把这件事做对（这一条比修 bug 更重要）
-- ---------------------------------------------------------------------------
-- 审计表不应该让应用角色直接 INSERT：
--   · 直连 INSERT 意味着应用可以**自己决定写什么行**（跳过 action/target 的约定、
--     甚至伪造 row_count），审计的可信度就没了；
--   · 而"只给出一个函数"能让写入形状被固定下来。
-- 所以把 log_access 改成 SECURITY DEFINER（以属主身份插入），并**收回**四个等级角色
-- 对 access_log 的 INSERT —— 它们只能通过函数追加日志，不能直接写表。
--
-- 必须同时钉住的两件事（SECURITY DEFINER 的经典陷阱）：
--   1) `SET search_path = mt, public` —— 否则调用者可以改 search_path 让函数
--      去操作一张同名的假表（search_path 劫持）。
--   2) 函数体里不执行任何用户输入拼出来的 SQL —— 本函数全是常量与参数绑定，符合。
--
-- 可信度的边界要写清楚：actor 来自会话变量 mt.actor，**应用理论上可以说谎**；
-- 但 actor_role 取 current_user（数据库自己认的），两者都记，不一致就能被发现。
-- 这是这套模型的已知边界，不是被忽略的问题。
--
-- 可重放：CREATE OR REPLACE + 幂等 GRANT/REVOKE。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION log_access(
  p_action     text,
  p_target     text,
  p_tier       text DEFAULT NULL,
  p_row_count  integer DEFAULT NULL,
  p_purpose    text DEFAULT NULL,
  p_detail     jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE sql
SECURITY DEFINER                       -- 以属主身份插入：低权限角色也能留痕
SET search_path = mt, public           -- 钉死 search_path，防劫持
AS $$
  INSERT INTO mt.access_log (actor, actor_role, action, target, access_tier,
                             row_count, purpose, detail)
  VALUES (mt.session_actor(), current_user, p_action, p_target,
          COALESCE(p_tier, mt.session_tier()), p_row_count, p_purpose,
          COALESCE(p_detail, '{}'::jsonb))
  RETURNING access_id;
$$;

COMMENT ON FUNCTION log_access(text, text, text, integer, text, jsonb) IS
  '写一条访问日志。SECURITY DEFINER + 钉死 search_path：审计写入只此一条路径，'
  '等级角色不能直接 INSERT access_log。actor 取自会话变量（应用可声明），'
  'actor_role 取 current_user（数据库认定）——两者都记，不一致即可发现。';

-- 收回直接写入权：审计表只能通过函数追加，不能由应用自己造行
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON access_log FROM mt_t0, mt_t1, mt_t2, mt_t3;

-- 只给 EXECUTE，且只给这四个角色（不要 PUBLIC）
REVOKE ALL ON FUNCTION log_access(text, text, text, integer, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION log_access(text, text, text, integer, text, jsonb)
  TO mt_t0, mt_t1, mt_t2, mt_t3;

-- 基线函数也要据此调整：不再授 INSERT（改由函数负责），避免下次对账又把它加回来
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    -- 审计写入**不**直授表权限：只给函数的执行权（见本迁移开头的说明）
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO %I', r);
    -- 读策略本身：让人能查"我为什么看不到某个字段"
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
  END LOOP;
  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限（模式使用 / 调用日志函数 / 读策略）。列级策略无关，'
  '但 apply_column_grants() 重建授权时必须一并重建（踩过：收回全部会顺手收掉基线）。'
  '注意：审计写入给的是**函数执行权**而不是表权限。';

-- 对账一次，让新规则生效
SELECT apply_column_grants();

COMMIT;
