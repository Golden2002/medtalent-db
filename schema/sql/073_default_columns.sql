-- ============================================================================
-- 医学生人才信息库 · 073 默认列：按任务决定"先看哪几列" v1.0.0
--
-- 【评审优先级 5 的收窄项】"基础视觉：改为补导航组织、**默认列**、移动端、真实任务验证"
--
-- 【先核对运行状态】
--   · 只有**人才库**有默认列机制（`TALENT_COLUMNS`，且它是因为用户实测反馈
--     "注册后看不到自己写的编号"才加上的"来源编号"列）；
--   · 通用表浏览页 `/t/<表>` 是 `SELECT *` 把**所有列**铺开 ——
--     `occupation` 25 列、`job_posting` 几十列，前几列往往是内部 id 与时间戳。
--   · 用户的任务是"看数据库内容、字段名、数量、分类统计、检索"，
--     所以默认应当先给**能识别记录、能判断状态、能区分来源**的列。
--
-- 【设计：注册表 + 规则解析，而不是逐表手写清单】
-- 逐表手写会重演"两处清单必然漏"（本库在域划分上已经漏过四次）：
-- 新表建好了，默认列清单没人补，于是它又退回"全列铺开"。
-- 所以做成**规则 → 解析 → 注册表**：
--   `default_column_rule` 写规则（按列名模式 + 表模式）
--   `refresh_default_columns()` 把规则**解析到每一张表的每一列**
--   `table_default_columns` 存解析结果（可查、可人工覆盖）
-- 新表只要跑一次 refresh 就有默认列 —— 不依赖谁记得改代码。
--
-- 【一个必须守住的纪律：默认列是"先看这些"，不是"只能看这些"】
-- 隐藏的列**必须仍然可达**（页面上有"显示全部列"）。否则就是本库最怕的
-- 「看不到 ≠ 没有」在界面上的翻版 —— 用户会以为那一列不存在。
-- 所以本迁移只决定"顺序与默认可见性"，**不删除任何列的访问路径**。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 规则表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS default_column_rule (
  rule_id     TEXT PRIMARY KEY,
  table_pat   TEXT NOT NULL,        -- 表名正则（^...$ 风格）
  column_pat  TEXT NOT NULL,        -- 列名正则
  visible     BOOLEAN NOT NULL,     -- 是否默认显示
  position    INT NOT NULL,         -- 越小越靠前
  reason      TEXT NOT NULL,
  note        TEXT
);
COMMENT ON TABLE default_column_rule IS
  '默认列的**规则**（表名模式 × 列名模式 → 是否默认显示 + 排序位 + 理由）。'
  '做成规则而不是逐表手写清单，是因为逐表清单会重演"新表没人补"——'
  '本库在业务域划分上已经漏过四次（053/062/063/064）。'
  '规则由 `refresh_default_columns()` 解析到**每一张表的每一列**。';

INSERT INTO default_column_rule (rule_id, table_pat, column_pat, visible, position, reason, note) VALUES
-- ① 主键/标识：永远第一位（"这是哪条记录"）
('pk_id',        '.*', '^(id|.*_id)$',                true,  10,
 '标识列：先能说出"这是哪条记录"，后面所有讨论才有对象', NULL),
-- ② 编码/码值：检索与对照最常用
('code',         '.*', '^(code|.*_code|code_status|status)$', true, 20,
 '码值/状态：用户检索与判断当前状态最常用的两类', NULL),
-- ③ 人可读的名称类：中文标签、标题、名称
('label',        '.*', '^(label.*|.*_label|title.*|.*_title|name|.*_name|label_zh)$', true, 30,
 '人可读的名称：没有它就只能看码值，等于逼用户自己翻译', NULL),
-- ④ 业务时间（**发布/生效**类的，不是簿记类的）
('biz_time',     '.*', '^(effective_from|effective_date|published_at|start_date|end_date|deadline|.*_date)$', true, 40,
 '业务时间：判断"这条还作不作数"的依据', '簿记时间（recorded_at/created_at）另有规则默认隐藏'),
-- ⑤ 数量/计数类：用户要"数量"
('count',        '.*', '^(n_|count|.*_count|.*_size|weight|score.*|importance)$', true, 50,
 '数量/分值与权重：用户明确要"数量"，且这类列短、信息密度高', NULL),
-- ⑥ 短文本说明
('note',         '.*', '^(note|reason|comment|description|definition_note|caveat)$', true, 60,
 '说明类短文本：口径与理由都在这里，是"这个数字怎么来的"的答案', NULL),
-- ⑦ 簿记列：默认隐藏（对业务判断没用，但占了最前面）
('bookkeeping',  '.*', '^(recorded_at|created_at|updated_at|added_at|collected_at|at|valid_from|valid_to|expires_at|pruned_at|decided_at|annotated_at)$',
 false, 900,
 '簿记列：自动维护的写入/生效时间。**对"这条数据是什么"没有帮助，却总排在前面**',
 '页面仍可展开查看；查询与排序也仍可用'),
-- ⑧ 内部载荷：默认隐藏（长 JSON / 长文本会撑爆表格）
('payload',      '.*', '^(attrs|detail|payload|payload_ref|record_set_jsonld|raw_text|duties|achievements|evidence_sample|.*_json|.*_jsonb)$',
 false, 910,
 '内部载荷：长 JSON / 长文本。铺在列表里会让表格无法阅读',
 '这些列在**详情页**里仍有完整展示，不是被删掉'),
-- ⑨ 校验/技术字段：默认隐藏
('technical',    '.*', '^(checksum.*|sha256|hash.*|parse_version|mapping_version|rule_version|algo_version|.*_version)$',
 false, 920,
 '技术校验字段：排障时才需要，日常浏览是噪声', NULL)
ON CONFLICT (rule_id) DO UPDATE
  SET table_pat = EXCLUDED.table_pat, column_pat = EXCLUDED.column_pat,
      visible = EXCLUDED.visible, position = EXCLUDED.position,
      reason = EXCLUDED.reason, note = EXCLUDED.note;

-- ---------------------------------------------------------------------------
-- 2. 解析结果表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS table_default_columns (
  table_name  TEXT NOT NULL,
  column_name TEXT NOT NULL,
  visible     BOOLEAN NOT NULL,
  position    INT NOT NULL,
  reason      TEXT NOT NULL,
  rule_id     TEXT,
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (table_name, column_name)
);
COMMENT ON TABLE table_default_columns IS
  '默认列的**解析结果**（每张表的每一列都有且仅有一行）。'
  '由 `refresh_default_columns()` 从 `default_column_rule` 生成；'
  '`rule_id` 记录它命中哪条规则，便于回答"为什么这一列被藏起来了"。';

-- ---------------------------------------------------------------------------
-- 3. 解析函数：规则 → 每张表的每一列
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION refresh_default_columns() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE n integer;
BEGIN
  -- 规则按 position 升序取**第一条命中的**（位置小的优先），
  -- 所以"永远第一位的主键"不会被后面的规则覆盖。
  DELETE FROM table_default_columns;
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

  -- 没命中任何规则的列：**默认可见**（宁可多显示一列，也不要让用户以为它不存在），
  -- 但排在最后。
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

  SELECT count(*) INTO n FROM table_default_columns;
  RETURN n;
END $$;
COMMENT ON FUNCTION refresh_default_columns() IS
  '把 `default_column_rule` 解析到**每一张表的每一列**。新表建好后跑一次就有默认列 —— '
  '不依赖谁记得改代码（本库在域划分上因"两处清单"漏过四次，这里是同一个教训）。'
  '未命中规则的列**默认可见**并排在最后：宁可多显示一列，也不要让人以为它不存在。';

-- ---------------------------------------------------------------------------
-- 4. 口径：默认列到底省了多少
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_UI_DEFAULT_COL_COVERAGE', '默认列解析覆盖率', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(100.0 * (SELECT count(*) FROM table_default_columns) '
 '/ greatest((SELECT count(*) FROM information_schema.columns c '
 '   JOIN pg_class cl ON cl.relname = c.table_name '
 '   JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = ''mt'' '
 '  WHERE c.table_schema = ''mt'' AND cl.relkind = ''r''), 1), 1))',
 '%', NULL, '解析结果行数 / 基础表列数；**必须为 100**',
 '默认列解析**覆盖率**：每张表的每一列都必须有一行解析结果。'
 '它的作用是**防呆** —— 必须保持 100%。'
 '⚠ 这不是"可见列占比"：可见占比是另一条口径（M_UI_HIDDEN_COL_SHARE）。'
 '**第一版我把这条口径的值写成 `visible/total`，于是算出 85.8 并在断言处报错** —— '
 '断言是对的、口径写错了：我们要求的是"每一列都被解析过（隐藏的仍可达）"，'
 '不是"每一列都默认显示"。',
 '如果这个数字小于 100，说明有列**既没被解析也没被兜底** —— '
 '那才是真问题（用户会以为那一列不存在）。', 1),

('M_UI_HIDDEN_COL_SHARE', '默认隐藏列占比', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(100.0 * count(*) FILTER (WHERE NOT visible) / greatest(count(*),1), 1) '
 'FROM table_default_columns)',
 '%', NULL, '隐藏＝默认不铺开，但仍可通过「显示全部列」展开',
 '默认不铺开的列占比。它衡量"表格被簿记列与内部载荷挤占"的程度。',
 '这个数字**高不等于好**：隐藏太多会把有价值的列也藏起来。'
 '判据是"任务需不需要它"，见 `default_column_rule.reason`。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 5. 域登记 + 授权 + 解析 + 断言
-- ---------------------------------------------------------------------------
INSERT INTO domain_registry (table_name, domain, declared_by, note) VALUES
('default_column_rule', '字典与语义', 'migration_073', '默认列规则（表模式 × 列模式 → 可见性与排序）。'),
('table_default_columns', '字典与语义', 'migration_073', '默认列解析结果（每表每列一行）。')
ON CONFLICT (table_name) DO UPDATE
  SET domain = EXCLUDED.domain, declared_by = EXCLUDED.declared_by, updated_at = now();

SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE n int; n_cols int; n_tabs int; v numeric; r record;
BEGIN
  n := refresh_default_columns();
  RAISE NOTICE '默认列解析完成：% 行', n;

  -- ① 必须覆盖**每一张基础表的每一列** —— 少一列就意味着"那一列在默认视图里不存在"
  SELECT count(*) INTO n_cols FROM information_schema.columns c
    JOIN pg_class cl ON cl.relname = c.table_name
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'mt'
   WHERE c.table_schema = 'mt' AND cl.relkind = 'r';
  IF n <> n_cols THEN
    RAISE EXCEPTION '默认列解析 % 行 ≠ 基础表列数 % —— 有列没被覆盖（用户会以为它不存在）', n, n_cols;
  END IF;

  SELECT count(DISTINCT table_name) INTO n_tabs FROM table_default_columns;
  RAISE NOTICE '覆盖 % 张表 / % 列', n_tabs, n_cols;

  -- ② 每张表都必须有**默认可见**的列（否则默认视图是空表）
  SELECT count(*) INTO n FROM (
    SELECT table_name FROM table_default_columns
     GROUP BY table_name HAVING count(*) FILTER (WHERE visible) = 0) x;
  IF n > 0 THEN RAISE EXCEPTION '有 % 张表默认一列都不显示 —— 默认视图会是空的', n; END IF;

  -- ③ 主键类列必须默认可见且排在前（"先能说出这是哪条记录"）
  SELECT count(*) INTO n FROM information_schema.columns c
    JOIN pg_class cl ON cl.relname = c.table_name
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'mt'
    LEFT JOIN table_default_columns t
           ON t.table_name = c.table_name AND t.column_name = c.column_name
   WHERE c.table_schema='mt' AND cl.relkind='r' AND c.column_name = 'id'
     AND (t.visible IS NOT TRUE OR t.position > 100);
  IF n > 0 THEN RAISE EXCEPTION '有 % 个 id 列没有默认可见且靠前', n; END IF;

  -- ④ 防呆口径必须是 100%（隐藏 ≠ 不可达）
  SELECT value INTO v FROM metric_value('M_UI_DEFAULT_COL_COVERAGE');
  IF v <> 100.0 THEN
    RAISE EXCEPTION '默认列解析覆盖率 = % ≠ 100 —— 有列既没解析也没兜底', v;
  END IF;

  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id LIKE 'M_UI\_%' LOOP
    PERFORM * FROM metric_value(r.metric_id);
  END LOOP;
  RAISE NOTICE '默认列就绪：% 张表 / % 列；默认隐藏占比见 M_UI_HIDDEN_COL_SHARE', n_tabs, n_cols;
END $$;

COMMIT;
