-- ============================================================================
-- 医学生人才信息库 · 042 聚合闸门支持按表查询（修 031 引入的性能缺陷）v1.0.0
--
-- 实测到的缺陷（我自己引入的）
-- ---------------------------------------------------------------------------
--   SELECT n_rows FROM mt.public_counts() WHERE table_name='person';   → 689 ms
--   SELECT count(*) FROM mt.person;                                    →   5 ms
--
-- 原因：`public_counts()` 是**集合返回函数**，它的 plpgsql 循环会先把**全部 89 张表**
-- 数一遍，外层 `WHERE table_name='person'` **不会下推**进循环（plpgsql 的 FOR 循环
-- 是全量执行的）。而那 89 张里包含 `change_log`（70 万行 / 100MB+），单价最高。
--
-- 后果（真正的危害不在单次 689ms，而在它出现的位置）：
--   · 公开落地页要为 7 个指标各调一次 → 匿名打开一次首页 ≈ **4.8 秒**的数据库开销；
--   · `/quality` 调一次也是 690ms；
--   · 而这是**公网**页面 —— 任何人都能反复触发。等于把最贵的查询做成了公开接口。
--
-- 修法：给闸门加**可选参数**，让调用方能精确指定要数哪张表。
--   public_counts()              → 全部表（保留原行为，供"清空空表"这类全量场景）
--   public_counts('person')      → 只数 person（约 5ms）
-- 不做"结果缓存"作为主修法：缓存会引入"数字过期"这个新的语义问题
-- （本项目已经因为派生数据过期吃过亏，见 age_band）。**让查询本身变便宜**更干净。
--
-- 兼容性：新增带默认值的参数会产生一个**新的函数签名**，而 `public_counts()` 这个
-- 零参调用会优先匹配到新函数（默认参数）—— 所以必须 DROP 旧的无参版本，
-- 否则 `public_counts()` 会因"两个候选都匹配"而报 ambiguous。
-- DROP 会带走 GRANT，因此下面显式重授（020/039 都踩过这个坑，这里写明）。
--
-- 可重放：DROP IF EXISTS + CREATE + 重授 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

DROP FUNCTION IF EXISTS public_counts();

CREATE OR REPLACE FUNCTION public_counts(p_table text DEFAULT NULL)
RETURNS TABLE (table_name text, n_rows bigint)
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
       -- 只数被指定的表（若指定）：避免"取一张表却把 89 张全数一遍"
       AND (p_table IS NULL OR c.relname = p_table)
       -- 跳过登记为"行数不公开"的表（041）
       AND NOT EXISTS (SELECT 1 FROM public_count_excluded e
                        WHERE e.table_name = c.relname)
     ORDER BY c.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM mt.%I', r.relname) INTO n;
    table_name := r.relname;
    n_rows := n;
    RETURN NEXT;
  END LOOP;
END $$;

COMMENT ON FUNCTION public_counts(text) IS
  '每张表的精确行数（聚合闸门）。SECURITY DEFINER，调用者不需要任何列权限 —— '
  '这就是"数量公开"与"列受限"能同时成立的原因（031）。'
  '**042 起支持参数**：public_counts(''person'') 只数 person（约 5ms），'
  'public_counts() 数全部表（约 600ms，因为含 change_log 这类大表）。'
  '为什么要加参数：集合返回函数里的循环**不会**被外层 WHERE 下推，'
  '实测"取一张表"要 689ms 而不是 5ms —— 而调用它的是**公网页面**。'
  '041 起跳过 public_count_excluded 里登记的表（行数也属运维信息）。';

REVOKE ALL ON FUNCTION public_counts(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public_counts(text) TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 硬断言：单表查询必须显著快于全量（用真实计时，不是看代码猜）
DO $$
DECLARE t0 timestamptz; t1 timestamptz; t2 timestamptz;
        ms_one numeric; ms_all numeric; n int;
BEGIN
  SELECT count(*) INTO n FROM public_counts('person');
  IF n <> 1 THEN
    RAISE EXCEPTION '带参数调用没有返回且只返回 person 一行（实得 % 行）', n;
  END IF;

  t0 := clock_timestamp();
  PERFORM count(*) FROM public_counts('person');
  t1 := clock_timestamp();
  PERFORM count(*) FROM public_counts();
  t2 := clock_timestamp();
  ms_one := EXTRACT(EPOCH FROM (t1 - t0)) * 1000;
  ms_all := EXTRACT(EPOCH FROM (t2 - t1)) * 1000;

  RAISE NOTICE '单表 % ms；全量 % ms（比值 %）',
               round(ms_one, 1), round(ms_all, 1), round(ms_all / GREATEST(ms_one, 0.001), 1);
  IF ms_one > ms_all THEN
    RAISE EXCEPTION '单表查询反而更慢（% ms vs % ms）—— 参数没生效', ms_one, ms_all;
  END IF;
  -- 不设绝对阈值（机器快慢不同），只要求"单表明显更快"
  IF ms_all > 100 AND ms_one > ms_all / 3 THEN
    RAISE EXCEPTION '单表 % ms 相对全量 % ms 没有明显优势 —— 疑似没有下推', ms_one, ms_all;
  END IF;
END $$;

COMMIT;
