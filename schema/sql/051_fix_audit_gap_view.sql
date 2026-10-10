-- ============================================================================
-- 医学生才信息库 · 051 修正审计缺口视图（050 的 v_audit_gap 算法错误）v1.0.0
--
-- 缺陷（被我自己写的断言当场抓住）
-- ---------------------------------------------------------------------------
-- 050 的 `v_audit_gap` 取"最近汇总快照"时写的是：
--     (SELECT sum(coalesce(counter_snapshot...)) FROM change_log c2
--       WHERE c2.object_type = c1.object_type AND c2.detail->>'mode' = 'summary')
-- 它对**所有**汇总行的快照**求和**，而正确做法是取**最近一条**的快照。
-- 后果：汇总行越多，"快照"越大 → 缺口算出**负数**。
-- 实测：写第一条汇总行时缺口 0（只有一个快照，求和恰好等于它），
--       写第三条之后变成 **-259704** —— 断言 `缺口 == 0` 因此失败。
--
-- 为什么这个 bug 值得单独记
-- ---------------------------------------------------------------------------
-- 它是"用一个聚合掩盖了时序"的典型：**"最近一次快照"是一个取最大/最近的操作，
-- 不是求和**。求和看起来"把信息都用上了"，实际是把"时点"混成了"总量"。
-- 而且它在只有一条汇总行时**恰好正确**（sum of one = itself），
-- 所以最简单的冒烟测试发现不了 —— 是"多写一条汇总行"才让它暴露。
-- 教训：用累计计数器做差值的检测，**必须精确定义"基准时点"**，并在断言里
--       制造"多条快照"的情形（我第一版断言只写了 0 缺口，没制造多条）。
--
-- 另一个必须处理的现实：`pg_stat_user_tables` 的累计计数器在**实例重启**或
-- 统计重置后会归零。若计数器 < 快照，差值会是负数 —— 那不是"负缺口"，
-- 而是"基准失效"。所以：
--   · 缺口用 GREATEST(当前 − 快照, 0)，只报正增量；
--   · 另外暴露一列 `基准是否失效`，让运维知道"该重新写一条汇总行以重建基准"。
--
-- 可重放：CREATE OR REPLACE VIEW。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ⚠ 必须 DROP 再 CREATE：本次把「最近汇总快照」从 numeric（sum 的结果）改成 bigint，
--   而 `CREATE OR REPLACE VIEW` **不允许改列类型**（实测报
--   `cannot change data type of view column "最近汇总快照" from numeric to bigint`）。
--   与 039 改函数返回类型、042 改函数签名是同一类约束 ——
--   凡是"改了输出结构"就必须 DROP/CREATE，而 **DROP 会带走 GRANT，必须显式重授**。
DROP VIEW IF EXISTS v_audit_gap;

CREATE VIEW v_audit_gap AS
WITH last AS (
  -- **每个表取最近一条**汇总行（按 change_id 降序取第一）。第一版误用 sum()，
  -- 把"时点"混成了"总量"，多条汇总行时算出负数缺口。
  SELECT DISTINCT ON (object_type)
         object_type AS table_name,
         at          AS last_summary_at,
         change_id   AS last_summary_id,
         coalesce((detail->'counter_snapshot'->>'n_tup_ins')::bigint, 0)
       + coalesce((detail->'counter_snapshot'->>'n_tup_upd')::bigint, 0)
       + coalesce((detail->'counter_snapshot'->>'n_tup_del')::bigint, 0) AS snap_total
    FROM change_log
   WHERE detail->>'mode' = 'summary'
   ORDER BY object_type, change_id DESC
)
SELECT m.table_name,
       s.n_tup_ins + s.n_tup_upd + s.n_tup_del AS 当前计数器,
       coalesce(l.snap_total, 0)               AS 最近汇总快照,
       -- 只报正增量：计数器被重置（实例重启/统计重置）时差值为负，
       -- 那是"基准失效"，不是"负缺口"
       GREATEST((s.n_tup_ins + s.n_tup_upd + s.n_tup_del)
                - coalesce(l.snap_total, 0), 0) AS 未覆盖增量,
       (l.snap_total IS NOT NULL
        AND (s.n_tup_ins + s.n_tup_upd + s.n_tup_del) < l.snap_total) AS 基准是否失效,
       l.last_summary_at                       AS 最近汇总时间
  FROM audit_mode m
  JOIN pg_stat_user_tables s ON s.schemaname = 'mt' AND s.relname = m.table_name
  LEFT JOIN last l ON l.table_name = m.table_name
 WHERE m.mode = 'summary';

COMMENT ON VIEW v_audit_gap IS
  'summary 粒度表的**审计缺口检测**：PostgreSQL 累计行计数器 − **最近一条**汇总行里的快照。'
  '050 第一版误用 sum() 把所有汇总行的快照求和（把"时点"混成"总量"），'
  '多条汇总行时算出负数缺口 —— 051 改为 DISTINCT ON 取最近一条。'
  '计数器在实例重启/统计重置后归零，此时差值为负：用 GREATEST(...,0) 只报正增量，'
  '并用「基准是否失效」提示需要重写一条汇总行来重建基准。';

-- 自检：**制造多条汇总行**的情形（第一版正是只在单条时才正确）
DO $$
DECLARE g_before bigint; g_after bigint; n int;
BEGIN
  SELECT 未覆盖增量 INTO g_before FROM v_audit_gap WHERE table_name='job_requirement';

  -- 连写两条汇总行：如果视图仍用 sum()，第二条会让缺口变成大负数
  PERFORM audit_bulk('job_requirement', 'rebuild', 0, '051 自检：第 1 条');
  PERFORM audit_bulk('job_requirement', 'rebuild', 0, '051 自检：第 2 条');
  SELECT 未覆盖增量 INTO g_after FROM v_audit_gap WHERE table_name='job_requirement';

  IF g_after <> g_before THEN
    RAISE EXCEPTION '多条汇总行后缺口变了（% → %）—— 说明仍在用聚合而不是取最近一条',
                    g_before, g_after;
  END IF;
  IF g_after < 0 THEN
    RAISE EXCEPTION '缺口为负（%）—— 基准算错了', g_after;
  END IF;
  SELECT count(*) INTO n FROM v_audit_gap WHERE 未覆盖增量 < 0;
  IF n > 0 THEN
    RAISE EXCEPTION '有 % 行的缺口为负 —— 视图仍在算总量', n;
  END IF;
  RAISE NOTICE '缺口视图自检通过：连写两条汇总行后缺口不变（%），且不为负', g_after;
END $$;

-- DROP 带走了原授权，显式重授（039/042 都踩过"DROP 后忘了重授"）
GRANT SELECT ON v_audit_gap TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 它也必须在公开视图注册表里 —— 否则下一次对账会按"视图默认只给 T3"把它收回，
-- 于是又是一个"取决于对账有没有跑过"的飘（048 刚踩过，见 049）。
INSERT INTO public_view_registry (viewname, reason) VALUES
  ('v_audit_gap',
   '审计粒度表的缺口检测（哪些表有改动但没写汇总行）。治理元数据，'
   '公开它让"审计没漏"这件事可被读者自己核对 —— 与 v_count_visibility 同类。')
ON CONFLICT (viewname) DO UPDATE
   SET reason = EXCLUDED.reason, added_at = now(), reviewer = 'migration_051';

SELECT apply_column_grants();

DO $$
BEGIN
  IF NOT has_table_privilege('mt_t0', 'mt.v_audit_gap', 'SELECT') THEN
    RAISE EXCEPTION '登记为公开后匿名仍读不到 v_audit_gap（授权没生效）';
  END IF;
  -- 再对账一次，确认状态稳定（不是"这次恰好有"）
  PERFORM apply_column_grants();
  IF NOT has_table_privilege('mt_t0', 'mt.v_audit_gap', 'SELECT') THEN
    RAISE EXCEPTION '第二次对账后匿名又读不到了 —— 登记没生效（仍在飘）';
  END IF;
  RAISE NOTICE 'v_audit_gap 已登记为公开视图；两次对账后状态稳定';
END $$;

COMMIT;
