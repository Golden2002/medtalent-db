-- ============================================================================
-- 医学生人才信息库 · 021 审计日志里记下「数据库认定的真实角色」v1.0.0
--
-- 背景（020 之后剩下的那条失败）
-- ---------------------------------------------------------------------------
-- 020 把 log_access 改成 SECURITY DEFINER 是对的（审计写入只此一条路径、
-- 等级角色不能自己往审计表插行），但带来一个副作用：
--   **SECURITY DEFINER 函数里的 current_user 是函数属主（postgres），不是调用者。**
-- 于是日志里 actor_role 全记成了 postgres，失去了"这份数据是以什么权限被读走的"这一信息。
--
-- 实测探针（ops/fixtures/_lab_definer.sql）给出的答案
-- ---------------------------------------------------------------------------
-- 以 mt_t1 身份调用一个 postgres 属主的 SECURITY DEFINER 函数，函数内看到：
--     current_user = 'postgres'      ← 属主，不能用来记调用者
--     session_user = 'postgres'      ← 会话最初登录的角色
--     current_setting('role', true) = **'mt_t1'**   ← 保留了调用者的 SET ROLE！
-- 也就是说：`SET ROLE` 设置的是 `role` 这个 GUC，SECURITY DEFINER 只改权限检查的
-- 生效角色，**不改这个 GUC**。因此审计里可以用它记下"调用者实际用的是哪个等级"，
-- 而且这个值来自数据库本身，不是应用声明出来的。
--
-- 于是最终形态是"两个都记"，各自的可信度不同、都写清楚：
--   actor       ← 会话变量 mt.actor（应用声明："这是哪个终端用户"）—— 应用理论上可以说谎
--   actor_role  ← current_setting('role')（数据库认定："这是哪个等级角色"）—— 不可伪造
--   detail.db_owner_executed = true  ← 明确标注这次写入是以属主身份执行的
-- 声明与实权不一致时，两者放在一起就能看出来（这正是 017 设计这一列的目的）。
--
-- 可重放：CREATE OR REPLACE + 幂等对账。
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
SECURITY DEFINER                       -- 审计写入只此一条路径；等级角色不能直接 INSERT
SET search_path = mt, public           -- 钉死 search_path，防劫持
AS $$
  INSERT INTO mt.access_log (actor, actor_role, action, target, access_tier,
                             row_count, purpose, detail)
  VALUES (
    mt.session_actor(),
    -- 数据库认定的调用者角色。`role` GUC 在没有 SET ROLE 时是 'none'，退回 login 角色。
    COALESCE(nullif(current_setting('role', true), 'none'), session_user),
    p_action, p_target,
    COALESCE(p_tier, mt.session_tier()),
    p_row_count, p_purpose,
    COALESCE(p_detail, '{}'::jsonb) || jsonb_build_object('db_owner_executed', true)
  )
  RETURNING access_id;
$$;

COMMENT ON FUNCTION log_access(text, text, text, integer, text, jsonb) IS
  '写一条访问日志（SECURITY DEFINER + 钉死 search_path）。'
  'actor = 应用声明的终端用户（可信度有限）；'
  'actor_role = current_setting(''role'')，即数据库认定的调用者等级角色（不可伪造）——'
  '实测 SECURITY DEFINER 不改 role GUC，所以这个值在函数内仍然可用。';

SELECT apply_column_grants();
COMMIT;
