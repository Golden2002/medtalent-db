-- ============================================================================
-- 医学生人才信息库 · 049 计数可见性视图登记为公开（消除"授了又被收回"的飘动）v1.0.0
--
-- 问题（我在 048 里自己引入的）
-- ---------------------------------------------------------------------------
-- 048 给四个等级角色授权了 `v_count_visibility`，但**没有登记进 public_view_registry**。
-- 而 038 建立的规则是"视图默认只给 T3，除非登记" —— 于是：
--   · 048 刚跑完：等级角色**能**读；
--   · 下一次 `apply_column_grants()`：被收回 → 等级角色**不能**读。
-- 同一个对象两个答案，取决于"对账有没有跑过" —— 这正是我在 041/042 反复批评的"飘动"，
-- 而这次是我自己制造的。**加了新视图就必须同时决定它的可见性**，不能只 GRANT 一句就完事。
--
-- 裁定：登记为**公开**
-- ---------------------------------------------------------------------------
-- 它的内容是"哪些表的行数不公开 + 为什么"，属**策略元数据** ——
-- 与 `export_denied`（导出禁令，已公开）、`v_column_policy_coverage`（策略分布，已公开）
-- 同类：告诉读者"规则是什么"，不含任何个人数据或内部实现细节。
-- 公开它还有一个好处：用户看到某个表显示"不公开"时，可以自己去查判据与理由，
-- 而不是只能相信页面。
--
-- 可重放：INSERT ... ON CONFLICT + 对账 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

INSERT INTO public_view_registry (viewname, reason) VALUES
  ('v_count_visibility',
   '每张表的行数是否对匿名公开 + 不公开的理由（策略元数据）。'
   '公开它让"某个表显示不公开"这件事可被读者自己核对，而不是只能相信页面。'
   '与 export_denied 同类：只说明规则，不含个人数据。')
ON CONFLICT (viewname) DO UPDATE
   SET reason = EXCLUDED.reason, added_at = now(), reviewer = 'migration_049';

SELECT apply_column_grants();

-- 断言：登记之后，等级角色**稳定地**能读（不再取决于对账有没有跑过）
DO $$
DECLARE n int;
BEGIN
  IF NOT has_table_privilege('mt_t0', 'mt.v_count_visibility', 'SELECT') THEN
    RAISE EXCEPTION '登记为公开后匿名仍读不到 v_count_visibility';
  END IF;
  IF NOT has_table_privilege('mt_t3', 'mt.v_count_visibility', 'SELECT') THEN
    RAISE EXCEPTION 'T3 读不到 v_count_visibility';
  END IF;
  -- 再对账一次，确认不是"这次恰好有"
  PERFORM apply_column_grants();
  IF NOT has_table_privilege('mt_t0', 'mt.v_count_visibility', 'SELECT') THEN
    RAISE EXCEPTION '第二次对账后匿名又读不到了 —— 说明登记没生效（仍在飘）';
  END IF;
  SELECT count(*) INTO n FROM public_view_registry;
  RAISE NOTICE '计数可见性视图已登记为公开；公开视图共 % 个（两次对账后状态稳定）', n;
END $$;

COMMIT;
