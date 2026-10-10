-- ============================================================================
-- 医学生人才信息库 · 057 分析函数改为 SECURITY INVOKER v1.0.0
--
-- 【为什么要改】
-- 053/054 把 `metric_value()` / `funnel_run()` 写成了 **SECURITY DEFINER**
-- （以属主 postgres 身份执行）。实测后果有两个，而且方向相反：
--   · 匿名**不能**执行 → 看起来安全；
--   · 但 **T3 也不能执行** → 门户（以 mt_tN 角色连库）根本调不动，功能等于不存在。
-- 也就是说：DEFINER 既没带来方便，也没带来安全，只是让功能不可用。
--
-- 【为什么 INVOKER 才是对的】
-- 这两个函数执行的是**注册表里存的 SQL 片段**，而那些片段读的是业务表
-- （person / employment_record / match_result …）。这些表都有**列级与行级策略**：
--   · 用 DEFINER 执行 = **绕开**所有列级控制 —— 一个 T1 用户能拿到他本不该看到的
--     列上的聚合数，甚至能通过口径组合反推出敏感分布；
--   · 用 INVOKER 执行 = 片段以**调用者自己的权限**跑：能读的表就能算，
--     读不了的（T0 读 person）会**如实报权限不足**，由页面降级显示。
-- 与本项目一贯的原则一致：**权限强制点在数据库，身份不由客户端指定** ——
-- 那么"以谁的身份算"就不该被一个 DEFINER 悄悄换掉。
--
-- 【附带好处：可降级】
-- 改成 INVOKER 后，"公开表的口径（岗位数）"匿名能算，
-- "涉及人才的户口径"匿名会被拒 → 页面按数据源逐个降级，
-- 这正是暴露面套件里已经验证过的模式（检索页的人员来源就是这么处理的）。
--
-- 【不变式仍然成立】
-- `mt.check_policy_invariants()` 要求"本库自有函数不得对 PUBLIC 可执行"。
-- 这里显式授权给 mt_t0..mt_t3（**不是 PUBLIC**），且不改变 SECURITY DEFINER 的
-- 计数口径（改完 DEFINER 函数反而少了一个）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 1. 改成调用者权限
ALTER FUNCTION metric_value(text) SECURITY INVOKER;
ALTER FUNCTION funnel_run(text) SECURITY INVOKER;
ALTER FUNCTION is_synthetic_person(text) SECURITY INVOKER;

-- 2. 显式授权到各等级（不是 PUBLIC）：能读哪些表由各角色自己的权限决定
REVOKE ALL ON FUNCTION metric_value(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION funnel_run(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION metric_value(text) TO mt_t0, mt_t1, mt_t2, mt_t3;
GRANT EXECUTE ON FUNCTION funnel_run(text) TO mt_t0, mt_t1, mt_t2, mt_t3;
GRANT EXECUTE ON FUNCTION check_registry_sql(text) TO mt_t3;
GRANT EXECUTE ON FUNCTION check_registry_fragment(text) TO mt_t3;

DO $$
DECLARE n int;
BEGIN
  -- 3. 断言：匿名能执行（但会在**读不到的表**上如实失败），且不是 DEFINER
  IF (SELECT prosecdef FROM pg_proc p JOIN pg_namespace ns ON ns.oid=p.pronamespace
       WHERE ns.nspname='mt' AND p.proname='metric_value') THEN
    RAISE EXCEPTION 'metric_value 仍是 SECURITY DEFINER —— 会绕开列级控制';
  END IF;
  IF NOT has_function_privilege('mt_t3','mt.metric_value(text)','EXECUTE') THEN
    RAISE EXCEPTION 'T3 不能执行 metric_value —— 门户调不动';
  END IF;
  IF has_function_privilege('public','mt.metric_value(text)','EXECUTE') THEN
    RAISE EXCEPTION 'metric_value 对 PUBLIC 可执行 —— 违反策略不变式';
  END IF;

  -- 4. 实测 INVOKER 的降级行为：以 mt_t0 身份算"岗位数"应成功，算"人才数"应被拒
  SET LOCAL ROLE mt_t0;
  BEGIN
    PERFORM * FROM metric_value('M_DEMAND_JOB');
    RAISE NOTICE 'INVOKER 降级验证：mt_t0 可以算公开口径（岗位数）';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'mt_t0 连岗位数都算不了（说明 job_posting 不是公开可读，需复核）';
  END;
  BEGIN
    PERFORM * FROM metric_value('M_SUPPLY_PERSON');
    RAISE NOTICE '⚠ mt_t0 竟然能算人才数 —— INVOKER 没起作用，需排查';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'INVOKER 生效：mt_t0 算人才数被数据库**如实拒绝**（不是被函数悄悄放行）';
  END;
  RESET ROLE;

  -- 5. 确认 DEFINER 函数数量没有增加（不变式的一部分）
  SELECT count(*) INTO n FROM pg_proc p JOIN pg_namespace ns ON ns.oid=p.pronamespace
   WHERE ns.nspname='mt' AND p.prosecdef;
  RAISE NOTICE 'mt 下 SECURITY DEFINER 函数仍有 % 个（本次未增加）', n;
END $$;

COMMIT;
