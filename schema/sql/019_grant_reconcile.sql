-- ============================================================================
-- 医学生人才信息库 · 019 授权对账要自洽：列权限 + 角色基线一起重建 v1.0.0
--
-- 背景（018 之后剩下的那条失败）
-- ---------------------------------------------------------------------------
-- apply_column_grants() 的开头是"先把四个角色的表权限全部收回，再按策略重授"。
-- 这个方向是对的（只加不减会留下已废止的权限），但它**收得太宽**：
-- 017 里单独授予的 `INSERT ON access_log`（写访问日志要用）也被一并收掉了，
-- 于是每次重放授权，访问日志就写不进去 —— 实测 access_test 的 A5 直接失败。
--
-- 教训：一个"重建全部授权"的函数必须**重建全部**，不能只重建它自己关心的那一部分。
-- 否则它的正确性依赖于"没人碰过其它授权"，而那正是最不可靠的假设。
--
-- 本迁移把这层关系显式化：
--   · apply_role_baseline()      —— 角色基线权限（模式使用、日志写入、策略表只读）
--   · apply_column_grants()      —— 先收回、再按 column_policy 重建列权限、
--                                   最后调用 apply_role_baseline() 补齐基线
--   两者都是幂等且可反复执行的；apply_column_grants() 是唯一的对账入口。
--
-- 可重放：CREATE OR REPLACE + 幂等调用。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 角色基线权限：与"具体哪个列是什么等级"无关，但少了它角色就干不了活
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;                                   -- FOREACH ... IN ARRAY 要标量，不能是 RECORD
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    -- 模式可见
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    -- 写访问日志（这是"记录访问用户"能落地的前提；列权限模型不覆盖 INSERT）
    EXECUTE format('GRANT INSERT ON mt.access_log TO %I', r);
    EXECUTE format('GRANT USAGE ON SEQUENCE mt.access_log_access_id_seq TO %I', r);
    -- 读策略本身：让人能查"我为什么看不到某个字段"
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    -- 只读，不许改策略（改策略是管理动作，走 postgres/开发者模式）
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
  END LOOP;
  -- 门户登录角色只读策略（它必须先 SET ROLE 才能读数据）
  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限（模式使用 / 写访问日志 / 读策略）。'
  '与列级策略无关，但 apply_column_grants() 重建授权时**必须**一并重建，'
  '否则"收回全部"会顺手收掉这些权限（踩过一次：访问日志写不进去）。';

CREATE OR REPLACE FUNCTION apply_column_grants() RETURNS TABLE (
  role_name text, grants integer
) LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  t RECORD;
  r RECORD;
BEGIN
  -- 5.1 收回：授权必须从策略重新算出来，不能在旧授权上累加
  FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA mt FROM %I', r.role_name);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA mt FROM %I', r.role_name);
  END LOOP;

  -- 5.2 按列授权（最低层级 Tn 的列 → mt_tn 及所有更高等级）；X 永不授出
  FOR r IN SELECT unnest(ARRAY['T0','T1','T2','T3']) AS tier LOOP
    FOR t IN
      SELECT cp.table_name,
             string_agg(format('%I', cp.column_name), ', ' ORDER BY cp.column_name) AS cols
        FROM column_policy cp
       WHERE access_rank(cp.min_tier) <= access_rank(r.tier) AND cp.min_tier <> 'X'
       GROUP BY cp.table_name
    LOOP
      EXECUTE format('GRANT SELECT (%s) ON mt.%I TO mt_%s',
                     t.cols, t.table_name, lower(r.tier));
    END LOOP;
  END LOOP;

  -- 5.3 X（禁止）再单独收一次
  FOR t IN SELECT table_name, column_name FROM column_policy WHERE min_tier = 'X' LOOP
    FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
      EXECUTE format('REVOKE SELECT (%I) ON mt.%I FROM %I',
                     t.column_name, t.table_name, r.role_name);
    END LOOP;
  END LOOP;

  -- 5.4 补齐基线权限（**这一步不能省**：5.1 的"收回全部"会连它一起收掉）
  PERFORM apply_role_baseline();

  -- 5.5 视图不在批量授权范围内：视图默认以**属主**权限执行，批量授出会绕过列级策略。
  --     当前状态：四个等级角色读不到任何视图（待逐个评审）。

  RETURN QUERY
    SELECT x.role_name, x.n::int
      FROM (SELECT 'mt_t0' AS role_name, count(*) AS n FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 0
            UNION ALL SELECT 'mt_t1', count(*) FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 1
            UNION ALL SELECT 'mt_t2', count(*) FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 2
            UNION ALL SELECT 'mt_t3', count(*) FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 3) x;
END $$;

COMMENT ON FUNCTION apply_column_grants() IS
  '唯一的授权对账入口：收回 → 按 column_policy 重建列权限 → 补齐角色基线。'
  '幂等，可反复执行；每次重放都会把授权拉回与策略一致的状态。';

-- 重放，把日志写入权限补回来
SELECT apply_column_grants();

COMMIT;
