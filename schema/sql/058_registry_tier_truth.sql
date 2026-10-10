-- ============================================================================
-- 医学生人才信息库 · 058 分析注册表的访问等级：把"两处说法不一致"改成一致
--
-- 【怎么发现的】
-- 给暴露面测试加"匿名打开 /pivot、/funnel、/factors"的断言时，发现匿名拿到的是
-- **没有选项的空表单** —— 说明它读不到注册表。查下发现在 053 里我写的
-- `GRANT SELECT ON metric_registry ... TO mt_t0` **已经被撤销了**。
--
-- 【为什么会撤销（这不是 bug，是机制）】
-- 本库的列级授权由 `refresh_column_policy()` + `apply_column_grants()` 统一生成：
-- 它**为每张基础表的每一列重新生成策略**，默认 `table_default = T1`，
-- 然后按规则逐条覆盖。新表没有命中任何规则 → 落在默认 T1。
-- 于是 053 里的表级 GRANT 会被对账**收回去**（对账管的是列级授权，
-- 表级授权属于"绕过机制"，会被重置）。
--
-- **真正的问题是"两处说法不一致"**：
--   · 053 说：这三张注册表 T0 可读；
--   · 对账说：T1（默认）。
-- 谁对？—— 在这个库里，**列级等级的唯一权威是 column_policy / 对账**。
-- 所以 053 那句话是错的（它假设自己能越过对账），本迁移把事实写清楚。
--
-- 【设计判断：注册表到底该给谁看】
-- 结论：**T1（登录后可见）**，理由有三条：
--   ① 它们是**分析层**的定义（口径、分类、漏斗），与"数据目录"（码表 CT_*、
--      字段字典 field_catalog，那两个确实是 T0）不是同一层：
--      数据目录回答"库里有什么"，注册表回答"该怎么算、算了什么"；
--   ② 里面的 SQL 片段会暴露表名与列名组合（虽然 /schema 已公开表结构，
--      但"哪些列被用来做分层"本身就是分析口径的一部分）；
--   ③ 更重要的是**机制一致性**：让对账说了算，而不是靠某个迁移"偷偷"授一次权
--      —— 否则今天能用、明天对账一跑又没了（本项目在 048 已经踩过
--      "取决于对账有没有跑过"的飘）。
--
-- 【顺带修的两件事】
--   ① 界面：匿名打开这三个页面时，要**明确说"注册表需要登录"**，
--      而不是给一个没有选项的空表单（那看起来像坏了）；
--   ② 暴露面断言：改成断言"匿名拿不到定义，且拿到明确的登录提示"。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 1. 显式收回 053 留下的、与机制冲突的表级授权，让"事实"和"机制"一致
REVOKE ALL ON metric_registry, dimension_registry, funnel_registry FROM mt_t0;
REVOKE ALL ON metric_registry, dimension_registry, funnel_registry FROM mt_t1;
REVOKE ALL ON metric_registry, dimension_registry, funnel_registry FROM mt_t2;
REVOKE ALL ON metric_registry, dimension_registry, funnel_registry FROM mt_t3;
-- 再交给对账按 column_policy 授（T1+：登录后可见）
SELECT apply_column_grants();

-- 2. 把"等级由谁决定"写进表注释，免得下一个人又去改 GRANT
COMMENT ON TABLE metric_registry IS
  '商业分析口径注册表。每个口径 = 分子 + 分母 + 时间依据 + 修饰词 + **口径说明（必填）**。'
  '⚠ **访问等级由 column_policy 决定（默认 T1，登录后可见），不要直接 GRANT** —— '
  '本库的列级授权由 refresh_column_policy()/apply_column_grants() 统一生成，'
  '表级 GRANT 会被对账收回（迁移 058 记录的实测）。';
COMMENT ON TABLE dimension_registry IS
  '分类（分层维度）注册表。from_sql 给基表与连接，expr_sql 给"这一维取什么值"。'
  '⚠ 访问等级由 column_policy 决定（默认 T1），不要直接 GRANT（见迁移 058）。';
COMMENT ON TABLE funnel_registry IS
  '漏斗注册表。stages 是有序阶段，每阶段返回主体数；**阶段只能变窄**，'
  'funnel_run() 会直接报错而不是画假漏斗。'
  '⚠ 访问等级由 column_policy 决定（默认 T1），不要直接 GRANT（见迁移 058）。';

-- 3. 硬断言：把"事实"钉住（T0 读不到、T1+ 读得到），并**对账两次**确认不飘
DO $$
DECLARE n int;
BEGIN
  IF has_table_privilege('mt_t0','mt.metric_registry','SELECT')
     OR has_column_privilege('mt_t0','mt.metric_registry','metric_id','SELECT') THEN
    RAISE EXCEPTION '匿名能读口径注册表 —— 与"分析层需登录"的设计不符';
  END IF;
  -- 以 T1 身份实测：能读
  SET LOCAL ROLE mt_t1;
  BEGIN
    PERFORM count(*) FROM metric_registry;
    RAISE NOTICE 'T1 可以读口径注册表（符合设计）';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE EXCEPTION 'T1 读不到口径注册表 —— 门户的透视/多因子页面会拿不到可选维度';
  END;
  RESET ROLE;

  -- 对账两次，确认等级不飘（这正是 048 踩过的坑）
  PERFORM refresh_column_policy();
  PERFORM apply_column_grants();
  PERFORM refresh_column_policy();
  PERFORM apply_column_grants();
  IF has_column_privilege('mt_t0','mt.metric_registry','metric_id','SELECT') THEN
    RAISE EXCEPTION '两次对账后匿名反而能读了 —— 等级在飘';
  END IF;
  SELECT count(*) INTO n FROM column_policy
   WHERE table_name IN ('metric_registry','dimension_registry','funnel_registry')
     AND min_tier = 'T1';
  RAISE NOTICE '三张注册表共 % 列落在 T1（默认等级），已与对账机制一致', n;
END $$;

COMMIT;
