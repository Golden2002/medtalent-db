-- ============================================================================
-- 医学生人才信息库 · 018 修正列级授权函数的"只加不减"缺陷 v1.0.0
--
-- 背景：017 引入的 mt.apply_column_grants() 里，我为了"只把视图授出去"写了一段
--       `GRANT SELECT ON ALL TABLES` 紧跟 `REVOKE ALL ON 每张表` 的处理。
--       那个 REVOKE 把**同一函数前面刚授好的列级权限全部清掉**了：
--       实测 mt_t0 有 265 个列权限，而 mt_t1/t2/t3 只剩 180 —— 阶梯整个塌了。
--       被 ops/tests/access_test.py 的"四个等级列权限严格递增"断言当场抓住。
--
-- 为什么不直接改 017：迁移是追加式的，改已应用的文件会让"别人的库"和"我的库"
--       结构不一致（ops/pg.py 的台账会用内容哈希拒绝重放，这次它确实拦住了我）。
--       所以按规矩新增一个迁移来替换函数 —— 这也正是"迁移"这个机制存在的意义。
--
-- 本次改动只有两条：
--   1) 删掉那段会清空授权的视图处理代码；
--   2) 在函数注释里写清"视图默认以视图属主权限执行"这个安全性质，
--      以及为什么视图授权必须逐个评审而不能批量授出。
--
-- 可重放：CREATE OR REPLACE + 幂等调用。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION apply_column_grants() RETURNS TABLE (
  role_name text, grants integer
) LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  t      RECORD;
  r      RECORD;
BEGIN
  -- 5.1 先全部收回：授权必须是"从策略重新算出来的"，不能在旧授权上累加。
  --     否则删掉一条策略后权限仍在（"只加不减"是最常见的配置类权限事故）。
  FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA mt FROM %I', r.role_name);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA mt FROM %I', r.role_name);
  END LOOP;

  -- 5.2 按列授权：最低层级为 Tn 的列，授予 mt_tn 及**所有更高等级**
  --     （阶梯含义：T2 能看 T0/T1/T2 的列）。X（禁止）永不授出。
  FOR r IN SELECT unnest(ARRAY['T0','T1','T2','T3']) AS tier LOOP
    FOR t IN
      SELECT cp.table_name,
             string_agg(format('%I', cp.column_name), ', ' ORDER BY cp.column_name) AS cols
        FROM column_policy cp
       WHERE access_rank(cp.min_tier) <= access_rank(r.tier)
         AND cp.min_tier <> 'X'
       GROUP BY cp.table_name
    LOOP
      EXECUTE format('GRANT SELECT (%s) ON mt.%I TO mt_%s',
                     t.cols, t.table_name, lower(r.tier));
    END LOOP;
  END LOOP;

  -- 5.3 X（禁止）再单独收一次，防止排序意外放行
  FOR t IN SELECT table_name, column_name FROM column_policy WHERE min_tier = 'X' LOOP
    FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
      EXECUTE format('REVOKE SELECT (%I) ON mt.%I FROM %I',
                     t.column_name, t.table_name, r.role_name);
    END LOOP;
  END LOOP;

  -- 5.4 视图**故意不在这里批量授权**。两条真实的安全性质：
  --     ① 视图默认以**视图属主**的权限执行（PG 15 起有 security_invoker 选项，
  --        本机是 16 可用，但默认仍为 false）。批量 `GRANT SELECT ON ALL TABLES`
  --        会把视图一并授出 → "视图属主能看到的列"就成了绕过列级策略的后门。
  --     ② 视图的列不在 column_policy 里（策略只覆盖 relkind='r' 的表），
  --        所以对它没有任何可依据的判断。
  --     结论：视图授权必须**逐个评审**（开 security_invoker，或只授给 T2/T3
  --     并在视图内显式裁剪列），这是待办而不是可以顺手批量处理的事。
  --     当前状态：四个等级角色**读不到任何视图**。

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
  '把 column_policy 编译成列级 GRANT。每次先全部 REVOKE 再按策略重授。'
  '视图不在批量授权范围内（视图以属主权限执行，会绕过列级策略），需逐个评审。';

-- 重新生成授权（017 那次的结果是坏的：mt_t1/t2/t3 的列权限被清空到 180）
SELECT apply_column_grants();

COMMIT;
