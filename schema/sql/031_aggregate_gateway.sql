-- ============================================================================
-- 医学生人才信息库 · 031 把「数量」做成独立的授权闸门（聚合 ≠ 列访问）v1.0.0
--
-- 背景：我自己引入的回归（暴露面回归套件当场抓住）
-- ---------------------------------------------------------------------------
-- 030 把 `person.person_id` / `person.subject_code` 收进 T1（披露控制：这两个列的
-- 取值会 identify 个体，实测 `per_real_fengtang` 直接编码了姓名）。
-- 但副作用是：**`person` 表对匿名一个可读的列都没有了**，于是
--     SET ROLE mt_t0; SELECT count(*) FROM mt.person;
-- 报 `permission denied for table person` —— **"数量公开"这条需求被我打掉了**。
--
-- 根因是设计层面的：PostgreSQL 里"能不能数这张表"和"能不能读某列"共用同一套
-- 表/列 SELECT 权限，没有"只准数不准读"的粒度。所以此前 `count(*)` 能通过，
-- 纯粹是**"恰好还有一列是 T0"的副作用**，不是被设计出来的能力。
-- 副作用一旦被真实的安全需求挤掉，需求就悄悄失效了。
--
-- 正解：**给聚合单独的闸门**
-- ---------------------------------------------------------------------------
-- 用一个 SECURITY DEFINER 函数把"数量"变成**显式授予的能力**：
--     mt.public_counts() → (table_name, n_rows)
-- 它只返回"每张表多少行"，不返回任何列值。授给全部等级（含匿名 mt_t0）。
-- 这样：
--   · 匿名拿得到数量（满足需求原文"数量…可以公开"）；
--   · 匿名拿不到任何主体标识列（满足披露控制）；
--   · 两者不再互相牵制 —— 将来再收紧某张表的列权限，也不会顺手打掉数量。
--
-- ⚠ 聚合披露控制的边界（必须写清楚，不能装作没有）
-- ---------------------------------------------------------------------------
-- 数量本身也是信息：极小的计数（例如"某类人群 3 人"）配合其它信息可能反推个体。
-- 本函数按**表**给总数（当前量级 123 人 / 690 岗位），这种表级计数不构成个体识别。
-- 但如果将来暴露"按敏感维度分组的小计数"，必须做最小计数阈值（例如 <5 不显示）——
-- 那是**下钻统计**的责任，不是这个函数的。这里把它写明，避免有人直接拿它去拼分组。
--
-- 可重放：CREATE OR REPLACE + 幂等 GRANT。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION public_counts() RETURNS TABLE (table_name text, n_rows bigint)
LANGUAGE plpgsql
SECURITY DEFINER                    -- 以属主身份数行：调用者不需要任何列权限
SET search_path = mt, public        -- 钉死 search_path，防劫持
AS $$
DECLARE
  r RECORD;
  n bigint;
BEGIN
  FOR r IN
    SELECT c.relname
      FROM pg_class c
      JOIN pg_namespace ns ON ns.oid = c.relnamespace
     WHERE ns.nspname = 'mt' AND c.relkind = 'r'
     ORDER BY c.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM mt.%I', r.relname) INTO n;
    table_name := r.relname;
    n_rows := n;
    RETURN NEXT;
  END LOOP;
END $$;

COMMENT ON FUNCTION public_counts() IS
  '每张表的精确行数（聚合闸门）。SECURITY DEFINER，所以调用者不需要任何列权限 —— '
  '这就是"数量公开"与"列受限"能同时成立的原因。'
  '**边界**：这里只给表级总数（不构成个体识别）；'
  '若将来要做按敏感维度分组的**小计数**，必须另加最小计数阈值，不能直接复用它。';

-- 只给 EXECUTE，不给任何表的列权限（这正是本机制的意义）
REVOKE ALL ON FUNCTION public_counts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public_counts() TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 写进基线函数：虽然函数执行权不会被"REVOKE ALL ON ALL TABLES"清掉，
-- 但基线函数的职责就是"凡是不由 column_policy 推导出来的授权都在这里重建"，
-- 漏掉它就会在下一次重构对账逻辑时被忘掉。
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
    -- 聚合闸门（031）：数量公开的能力，与列权限无关
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.public_counts() TO %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_session_new(text,text,text,interval,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_user_add(text,text,text,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON mt.column_profile FROM %I', r);
    EXECUTE format('GRANT SELECT ON mt.v_column_profile_coverage, '
                   'mt.v_column_profile_public, mt.v_profile_exposure TO %I', r);
  END LOOP;

  EXECUTE 'REVOKE ALL ON mt.column_profile FROM mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT SELECT ON mt.column_profile TO mt_t3';

  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
    EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
  END LOOP;
  EXECUTE 'GRANT SELECT ON mt.v_column_profile_coverage, mt.v_column_profile_public, '
          'mt.v_profile_exposure, mt.v_access_baseline_check, mt.v_session_function_grants '
          'TO mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.public_counts() TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.public_counts() TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_lookup(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_revoke(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_login(text, text, interval, text) TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants, '
          'mt.v_column_profile_coverage TO mt_portal';
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限：模式使用 / 审计函数执行权 / **聚合闸门 public_counts** / 读策略 / '
  '视图只给 T3 / 会话函数最小授权 / 表级授权对象（column_profile 只给 T3）。'
  '凡是不由 column_policy 推导出来的授权都必须在这里重建。';

SELECT apply_column_grants();

-- 硬断言：两条需求必须**同时**成立（这正是本迁移要解决的问题）
DO $$
DECLARE n int;
BEGIN
  -- ① 匿名能拿到数量
  SET LOCAL ROLE mt_t0;
  SELECT n_rows INTO n FROM mt.public_counts() WHERE table_name = 'person';
  RESET ROLE;
  IF n IS NULL OR n <= 0 THEN
    RAISE EXCEPTION '匿名拿不到 person 的数量 —— "数量公开"这条需求没满足';
  END IF;
  RAISE NOTICE '聚合闸门：匿名读到 person 行数 = %', n;
END $$;

DO $$
BEGIN
  -- ② 匿名仍然读不到标识列（披露控制不能被聚合闸门削弱）
  PERFORM set_config('mt.probe', '1', true);
  BEGIN
    SET LOCAL ROLE mt_t0;
    PERFORM person_id FROM mt.person LIMIT 1;
    RESET ROLE;
    RAISE EXCEPTION '匿名读到了 person.person_id —— 披露控制被削弱了';
  EXCEPTION
    WHEN insufficient_privilege THEN
      RESET ROLE;
      RAISE NOTICE '披露控制仍然有效：匿名读不到 person_id';
  END;
END $$;

COMMIT;
