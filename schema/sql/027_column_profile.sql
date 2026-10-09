-- ============================================================================
-- 医学生人才信息库 · 027 列级剖析的物化载体 v1.0.0
--
-- 需求原文：「我能看到以 sql 存储的数据的字段名，数量，能够分类统计，能够检索」
-- ---------------------------------------------------------------------------
-- "字段名"本库已经有（field_catalog 176 个字段 + 84 张表的物理列），
-- 缺的是**"数量"的完整含义**与**"能不能按这列分类"**：
--   · 行数 ≠ 有值数。只给行数，用户会以为"690 行 = 690 条薪资"。
--   · 一列**能不能当分类维度**取决于它的基数（去重值个数）：基数 17 能 group by，
--     基数 690 是自由文本。不告诉用户这个，他分类统计时会撞墙。
--   · 空值率、Top-K 值、值域、最值，都是"看数据"的基本盘。
--
-- 为什么要**物化**而不是实时算（实测数据，不是估计）
-- ---------------------------------------------------------------------------
--   · job_posting 41 列的空值率剖析：约 3 ms（小表，实时算没问题）
--   · change_log **单列** `count(DISTINCT …)`：**1319 ms**（565k 行，走 external merge sort，temp 26 MB）
--   · change_log 一次全列含 DISTINCT 剖析：**1439 ms**
-- 而 `--check` 与回归会把每个页面渲染一遍。实时剖析 = 直接拖垮回归。
-- 所以：**目录页只读物化表；计算由离线 CLI 做。**
--
-- 为什么用**普通表**而不是物化视图
-- ---------------------------------------------------------------------------
--   · 物化视图 REFRESH 是全量替换，且只保留"最新一份" —— 做不了带版本的历史。
--   · 本库实测 0 个物化视图，引入它是一种新机制，收益不抵维护成本。
--   · 普通表可以按 (表,列) 增量更新、可以记录"什么时候算的"、可以留多份快照。
--
-- 诚实的边界（必须写下来，否则会误导使用者）
-- ---------------------------------------------------------------------------
--   · 剖析结果**是快照**，不是实时值。每行都带 `computed_at`，页面必须显示它。
--   · `n_distinct` 用精确 `count(DISTINCT …)`，不用 `pg_stats.n_distinct`
--     （后者是 ANALYZE 后的**采样估计**，负数表示比例）。
--   · Top-K 只对基数不过大的列有意义；对大基数列记 NULL 并说明原因，
--     **不能拿"前 10 个值"冒充分布**。
--   · 用 `ANALYZE` 得到的 `null_frac` 只作**嫌疑提示**（用于挑选要精确算的列），
--     精确值一律由本表的 count 计算得出。
--
-- 可重放：CREATE TABLE IF NOT EXISTS。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS column_profile (
  table_name    text NOT NULL,
  column_name   text NOT NULL,
  data_type     text,
  computed_at   timestamp with time zone NOT NULL DEFAULT now(),
  n_rows        bigint,            -- 表行数（精确 count(*)）
  n_not_null    bigint,            -- 非空行数（精确 count(col)）
  n_distinct    bigint,            -- 去重值个数（精确 count(DISTINCT col)；NULL 表示未算）
  null_frac     numeric(6,4),      -- 空值率 = 1 - n_not_null/n_rows（精确，不是采样）
  min_value     text,              -- 最小值（文本化，便于统一展示）
  max_value     text,              -- 最大值
  avg_len       numeric(10,2),     -- 文本/字符类型：平均长度
  top_values    jsonb,             -- Top-K：[{"v":..,"n":..}, ...]；大基数列为 null
  top_note      text,              -- 为什么没有 Top-K（诚实说明，而不是留空）
  is_enum_like  boolean,           -- 基数小到可以当分类维度（判据见下）
  enum_hint     text,              -- 若该列有对应码表，给出 code_table_id
  tier          text,              -- 该列的访问等级（冗余自 column_policy，便于目录页一次读完）
  sample_where  text,              -- 计算时实际用的 WHERE（留证据，便于复核）
  elapsed_ms    integer,           -- 本次计算耗时（让"代价"可见）
  PRIMARY KEY (table_name, column_name)
);
COMMENT ON TABLE column_profile IS
  '列级剖析的**物化快照**。目录页/字段页只读它，计算由 ops/profile.py 离线做 —— '
  '实时剖析 change_log 单列就要 1300ms（实测），会在回归里拖垮一切。'
  '每行都带 computed_at：这是快照，不是实时值。';

CREATE INDEX IF NOT EXISTS idx_column_profile_tier ON column_profile(tier);
CREATE INDEX IF NOT EXISTS idx_column_profile_enum ON column_profile(is_enum_like)
  WHERE is_enum_like;

-- 覆盖情况：让人一眼看出"哪些表还没剖析过"（不做，就不能假装目录是完整的）
CREATE OR REPLACE VIEW v_column_profile_coverage AS
SELECT c.table_name,
       count(*) AS n_columns,
       count(cp.column_name) AS n_profiled,
       max(cp.computed_at) AS last_computed,
       coalesce(sum(cp.elapsed_ms), 0) AS total_ms,
       count(*) FILTER (WHERE cp.is_enum_like) AS n_enum_like,
       count(*) FILTER (WHERE cp.null_frac >= 1) AS n_all_null,
       count(*) FILTER (WHERE cp.null_frac = 0) AS n_all_filled
  FROM (SELECT c2.table_name, c2.column_name
          FROM information_schema.columns c2
          JOIN pg_class cl ON cl.relname = c2.table_name
          JOIN pg_namespace n ON n.oid = cl.relnamespace AND n.nspname = 'mt'
         WHERE c2.table_schema = 'mt' AND cl.relkind = 'r') c
  LEFT JOIN column_profile cp
         ON cp.table_name = c.table_name AND cp.column_name = c.column_name
 GROUP BY c.table_name;
COMMENT ON VIEW v_column_profile_coverage IS
  '每张表的剖析覆盖率。n_profiled < n_columns 的表说明目录页对它只能显示"未剖析"，'
  '而不是显示 0 —— 把"没算过"显示成"没有值"是本项目一直在防的误读。';

-- 分类维度的判据集中在一处，避免页面与 CLI 各写一份（口径不一致的老毛病）
CREATE OR REPLACE FUNCTION is_classifiable(p_n_rows bigint, p_n_distinct bigint)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  -- 能当分类维度 = 去重值不太多、也不算太少：
  --   · 上限 200：再多，分组结果就没法读了（下游应该做区间化或码表化）
  --   · 下限 2：只有 1 个值（常量列）没有分组意义，页面要单独提示
  --   · 空表或未算（NULL）一律不算
  SELECT p_n_rows IS NOT NULL AND p_n_distinct IS NOT NULL
         AND p_n_rows > 0 AND p_n_distinct BETWEEN 2 AND 200;
$$;
COMMENT ON FUNCTION is_classifiable(bigint, bigint) IS
  '一列能不能当分类维度（基数 2–200）。判据只写在这里一处：'
  '页面、CLI、测试都调它，避免三份实现漂移。';

-- 权限：目录数据是元数据（不含行内容），四个等级都能读；
-- 但**只给能读该列的人看它的统计**这件事，本表做不到（统计本身是元数据）。
-- 所以策略是：本表对 T0 只暴露 T0 列的统计 —— 由视图做，而不是靠约定。
CREATE OR REPLACE VIEW v_column_profile_public AS
SELECT cp.table_name, cp.column_name, cp.data_type, cp.n_rows, cp.n_not_null,
       cp.n_distinct, cp.null_frac, cp.min_value, cp.max_value, cp.avg_len,
       cp.top_values, cp.top_note, cp.is_enum_like, cp.tier, cp.computed_at
  FROM column_profile cp
  JOIN column_policy pol
    ON pol.table_name = cp.table_name AND pol.column_name = cp.column_name
 WHERE access_rank(pol.min_tier) <= access_rank('T0');
COMMENT ON VIEW v_column_profile_public IS
  '只含 T0（公开）列的统计。**为什么要这个视图**：统计本身是元数据，'
  '但"某个受限列的取值分布"仍然可能泄露信息（例如按"健康受限项"分组能反推个体）。'
  '所以公开入口只给公开列的统计，而不是把整张剖析表摊开。';

GRANT SELECT ON column_profile, v_column_profile_coverage TO mt_t3;
GRANT SELECT ON v_column_profile_public TO mt_t0, mt_t1, mt_t2, mt_t3;
-- 剖析写入是离线 CLI（以 postgres 连接）的事；应用角色一律不能改
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON column_profile
  FROM PUBLIC, mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

COMMIT;
