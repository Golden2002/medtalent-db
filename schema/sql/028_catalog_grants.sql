-- ============================================================================
-- 医学生人才信息库 · 028 目录页需要的两处授权 v1.0.0
--
-- 背景：/catalog（数据目录）要显示每张表的"已剖析列 / 可分类列 / 整列为空"三列，
--      数据来自 v_column_profile_coverage。027 只把它授给了 mt_t3，
--      于是 T0–T2 打开目录页会读不到这三列。
--
-- 判断：**这是表级聚合，不是列级取值，可以给全部等级。**
-- ---------------------------------------------------------------------------
-- 027 里我把"公开入口只给公开列的统计"落成了 v_column_profile_public（按列过滤），
-- 那个判断针对的是**列级取值分布**（Top-K 取值可能反推个体，例如按"健康受限项"分组）。
-- 而 v_column_profile_coverage 里只有：
--     表的列数、已剖析列数、剖析时间、合计耗时、可分类列数、整列为空数、全非空列数
-- 全是**表级计数**，既不涉及具体取值，也不点名单个受限列。
-- 所以它可以给全部等级 —— 目录页要在任何人打开时都显示真实的覆盖率。
--
-- 另一个理由：不给它，页面就只能显示 0，而"没算过"显示成"没有"正是
-- 本项目一直在防的误读（docs/20 把这条列为"用户没想到但必须有"的第 7 条）。
--
-- 同时把 column_profile 本身的授权收紧一次（027 已 REVOKE，这里再确认一次）：
-- 列级快照含 Top-K 取值，只给 T3；T0–T2 走 v_column_profile_public。
--
-- 可重放：GRANT/REVOKE 均幂等。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 表级聚合覆盖率：全部等级（含门户登录角色）
GRANT SELECT ON v_column_profile_coverage TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 列级快照（含 Top-K 取值）：只给 T3
REVOKE ALL ON column_profile FROM PUBLIC, mt_portal, mt_t0, mt_t1, mt_t2;
GRANT SELECT ON column_profile TO mt_t3;

-- 公开列的统计：全部等级
GRANT SELECT ON v_column_profile_public TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 自检：把"谁能看到什么粒度的统计"变成可查的事实
CREATE OR REPLACE VIEW v_profile_exposure AS
SELECT 'column_profile（列级快照，含 Top-K 取值）' AS object,
       has_table_privilege('mt_t0', 'mt.column_profile', 'SELECT') AS t0_can,
       has_table_privilege('mt_t1', 'mt.column_profile', 'SELECT') AS t1_can,
       has_table_privilege('mt_t3', 'mt.column_profile', 'SELECT') AS t3_can
UNION ALL
SELECT 'v_column_profile_coverage（表级聚合）',
       has_table_privilege('mt_t0', 'mt.v_column_profile_coverage', 'SELECT'),
       has_table_privilege('mt_t1', 'mt.v_column_profile_coverage', 'SELECT'),
       has_table_privilege('mt_t3', 'mt.v_column_profile_coverage', 'SELECT')
UNION ALL
SELECT 'v_column_profile_public（仅 T0 列的统计）',
       has_table_privilege('mt_t0', 'mt.v_column_profile_public', 'SELECT'),
       has_table_privilege('mt_t1', 'mt.v_column_profile_public', 'SELECT'),
       has_table_privilege('mt_t3', 'mt.v_column_profile_public', 'SELECT');
COMMENT ON VIEW v_profile_exposure IS
  '剖析数据的三级暴露面：表级聚合给全部等级，T0 列统计给全部等级，'
  '列级快照（含 Top-K）只给 T3。判据是"聚合会不会反推个体"。';
GRANT SELECT ON v_profile_exposure TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 硬断言：T0 绝不能读到列级快照（Top-K 取值可能反推个体）
DO $$
BEGIN
  IF has_table_privilege('mt_t0', 'mt.column_profile', 'SELECT') THEN
    RAISE EXCEPTION 'T0 能读 column_profile —— 列级 Top-K 取值不该对公开等级开放';
  END IF;
  RAISE NOTICE '剖析暴露面自检通过';
END $$;

COMMIT;
