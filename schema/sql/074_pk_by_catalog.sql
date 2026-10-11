-- ============================================================================
-- 医学生人才信息库 · 074 主键判据改为「目录里的事实」（修正 073 的规则）v1.0.0
--
-- 【073 哪里错了】默认列排序用列名模式 `^(id|.*_id)$` 认"标识列"，
-- 于是 `job_posting` 里 `employer_id / ingest_run_id / job_id / occupation_id / source_id`
-- **全部**落到同一档（position 10），再按字母排序 ——
-- 结果**真正的 id 被排到第三位**，最前面显示的是外键：
--     employer_id | ingest_run_id | job_id | occupation_id | source_id | ...
--
-- 【这不是笔误，是我违反了自己反复强调的原则】
-- 本库的口径纪律里写着「**判据是事实，不是名字**」——
-- 我在 061 判数据性质时专门用了 `source_registry.is_synthetic` 而不用名称匹配，
-- 说"按命名判断一定会误伤"；结果转头在默认列上用列名猜主键。
-- 主键在系统目录里是**确定的事实**（`pg_index.indisprimary`），根本不需要猜。
--
-- 【修法】
--   ① 列名规则降级为"外键/引用列"（position 15），不再与主键同档；
--   ② `refresh_default_columns()` **从系统目录读出真主键并置顶（position 1）**；
--   ③ 断言：每张有主键的表，其主键列 position 必须是 1。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ① 列名规则降级为"引用列"，不再冒充主键
UPDATE default_column_rule
   SET position = 15,
       reason = '引用列（外键）：指向别的表，通常要跟着看目标表才知道含义',
       note = '⚠ 073 里它与主键同档（position 10），同档后按字母排序，'
              '**把真主键挤到了后面**（job_posting 的第一列变成了 employer_id）。'
              '主键改由系统目录判定，见 refresh_default_columns()。'
 WHERE rule_id = 'pk_id';

-- ② 解析函数：真主键从 `pg_index` 读，置顶
CREATE OR REPLACE FUNCTION refresh_default_columns() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE n integer; n_pk integer;
BEGIN
  DELETE FROM table_default_columns;

  -- 规则按 position 升序取**第一条命中的**
  INSERT INTO table_default_columns (table_name, column_name, visible, position, reason, rule_id)
  SELECT DISTINCT ON (c.table_name, c.column_name)
         c.table_name, c.column_name, r.visible, r.position, r.reason, r.rule_id
    FROM information_schema.columns c
    JOIN pg_class cl ON cl.relname = c.table_name
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'mt'
    JOIN default_column_rule r
      ON c.table_name ~ r.table_pat AND c.column_name ~ r.column_pat
   WHERE c.table_schema = 'mt' AND cl.relkind = 'r'
   ORDER BY c.table_name, c.column_name, r.position;

  -- 未命中任何规则的列：默认可见、排在最后（宁可多给一列，也不要让人以为它不存在）
  INSERT INTO table_default_columns (table_name, column_name, visible, position, reason, rule_id)
  SELECT c.table_name, c.column_name, true, 800,
         '未命中任何规则 → 默认显示（宁可多给一列，也不要让人以为它不存在）', NULL
    FROM information_schema.columns c
    JOIN pg_class cl ON cl.relname = c.table_name
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'mt'
   WHERE c.table_schema = 'mt' AND cl.relkind = 'r'
     AND NOT EXISTS (SELECT 1 FROM table_default_columns t
                      WHERE t.table_name = c.table_name AND t.column_name = c.column_name)
  ON CONFLICT DO NOTHING;

  -- **主键置顶：判据是目录里的事实（pg_index.indisprimary），不是列名**
  WITH pk AS (
    SELECT c.relname AS table_name, a.attname AS column_name
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indrelid
      JOIN pg_namespace ns ON ns.oid = c.relnamespace AND ns.nspname = 'mt'
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
     WHERE i.indisprimary)
  UPDATE table_default_columns t
     SET position = 1, visible = true,
         reason = '主键（判据来自系统目录 pg_index.indisprimary，不是列名匹配）：'
                  '先能说出"这是哪条记录"，后面所有讨论才有对象',
         rule_id = 'pk_catalog'
    FROM pk WHERE pk.table_name = t.table_name AND pk.column_name = t.column_name;
  GET DIAGNOSTICS n_pk = ROW_COUNT;

  SELECT count(*) INTO n FROM table_default_columns;
  RAISE NOTICE '默认列解析：% 行，其中主键置顶 % 列', n, n_pk;
  RETURN n;
END $$;

COMMENT ON FUNCTION refresh_default_columns() IS
  '把 `default_column_rule` 解析到每一张表的每一列，并**从系统目录把真主键置顶**。'
  '⚠ 主键判据必须是 `pg_index.indisprimary`（事实），**不是列名模式**（猜测）—— '
  '073 第一版用 `^(id|.*_id)$` 认主键，把外键也当成主键，于是 '
  '`job_posting` 的第一列变成了 `employer_id` 而真 id 排第三。'
  '本库在口径纪律里写着「判据是事实，不是名字」，这里是同一个要求。';

-- ③ 重新解析 + 断言
DO $$
DECLARE n int; n_bad int; n_pk int; r record;
BEGIN
  n := refresh_default_columns();

  -- 每张有主键的表，主键列必须 position=1
  SELECT count(*) INTO n_bad FROM (
    SELECT c.relname AS t, a.attname AS col
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indrelid
      JOIN pg_namespace ns ON ns.oid = c.relnamespace AND ns.nspname = 'mt'
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
     WHERE i.indisprimary) pk
   LEFT JOIN table_default_columns t
          ON t.table_name = pk.t AND t.column_name = pk.col
   WHERE t.position IS DISTINCT FROM 1 OR t.visible IS NOT TRUE;
  IF n_bad > 0 THEN
    RAISE EXCEPTION '有 % 个主键列没有排在第一位 —— 默认视图第一列不是"这是哪条记录"', n_bad;
  END IF;

  -- 覆盖率仍须 100%（隐藏 ≠ 不可达）
  PERFORM * FROM metric_value('M_UI_DEFAULT_COL_COVERAGE');

  -- 抽查 job_posting：第一列必须是 job_id
  SELECT column_name INTO r FROM table_default_columns
   WHERE table_name = 'job_posting' AND position = 1 LIMIT 1;
  IF r IS NULL OR r.column_name <> 'job_id' THEN
    RAISE EXCEPTION 'job_posting 第一列应是 job_id，实得 %',
      coalesce(r.column_name, '（空）');
  END IF;
  RAISE NOTICE '主键置顶已生效：job_posting 第一列 = %', r.column_name;
END $$;

COMMIT;
