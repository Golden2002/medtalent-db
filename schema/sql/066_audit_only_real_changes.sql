-- ============================================================================
-- 医学生人才信息库 · 066 审计只记"真的变了什么" v1.0.0
--
-- 【怎么发现的：一次 10 倍性能劣化】
-- 一次完整回归里，备份套件从平常的 **664 秒劣化到 7224 秒（2 小时）**。查根因：
--   · 库 255 MB，其中 **change_log 210 MB（82%）**
--   · change_log 932,147 行，其中 **job_requirement + job_posting = 771,715 行 / 148 MB（70%）**
--   · 备份做完整 dump/restore → 成本被审计表主导 → 回归变得不可用
--
-- 【050 已经治了一半，另一半没治】
-- 050 把 `job_requirement`/`job_task` 改成 summary 粒度，**实测有效**：
--   050 生效后 job_requirement 只剩 34 行（之前每天 72,242 行）
-- 但 **`job_posting` 没治**：050 之后仍产生 **31,790 行**。为什么？
--   JD 解析会 UPDATE 每个岗位（690 行 × 每次重算），触发的是
--   `AFTER UPDATE ... FOR EACH ROW` → 逐行写审计。
--
-- 【更严重的是：这些审计行**没有信息量**】
-- 抽查明细，内容只有：
--   {"op":"UPDATE","row":"job_id=job_...","mode":"row","table":"job_posting"}
-- —— **看不出改了哪一列、从什么改成什么**。
-- 也就是说 31,790 行既占 27 MB，又回答不了"到底改了什么"这个问题。
-- 它唯一的含义是"这一行被 UPDATE 过"，而**这恰恰是零信息**。
--
-- 【修法：让审计只记"真的变了什么"】
--   ① **无实质变化的 UPDATE 不写审计**（值全都一样 → 零信息，不该占体量）；
--   ② **有变化时记录变化的列名**（从"某行被更新过"变成"哪几列变了"）；
--   ③ 只改"簿记列"（updated_at / recorded_at 之类）的更新也算无实质变化 ——
--      这类列由 now() 自动写，任何"只碰它"的更新都不代表业务变化。
--      簿记列名单放注册表 `audit_ignore_columns`，可查、可改，不写死在函数里。
--
-- 【为什么不直接删历史噪声行】
-- 那是**数据保留决策**，涉及项目既有的 `retention_policy`（rp_change_log 保留 60 个月），
-- 现有 932k 行**全部在保留期内**。删它等于绕过保留策略 —— 不该由一次性能优化顺手做掉。
-- 本迁移只**向前修**（不再产生噪声），并把历史体量做成可监控的口径，
-- 把"要不要归档历史噪声"留给保留策略的决策者。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 簿记列注册表（只碰这些列的更新不算业务变化）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_ignore_columns (
  table_name  TEXT NOT NULL,
  column_name TEXT NOT NULL,
  reason      TEXT NOT NULL,
  PRIMARY KEY (table_name, column_name)
);
COMMENT ON TABLE audit_ignore_columns IS
  '只改这些列不算"业务变化"，因此不写审计。它们是由 now() 之类自动维护的簿记列：'
  '任何"只碰它"的更新都不代表业务状态变了。放进注册表而不是写死在函数里，'
  '这样读者能自己核对我们忽略了什么，也能按表增补。';

-- 常见簿记列：按表存在与否登记（不存在的列登记了也无害，函数只查名字）
INSERT INTO audit_ignore_columns (table_name, column_name, reason)
SELECT t.table_name, c.col, '自动维护的簿记列（now() 写入），只碰它不代表业务变化'
  FROM (VALUES ('job_posting'), ('job_requirement'), ('person'), ('occupation'),
               ('concept'), ('job_competency_weight'), ('skill_assertion'),
               ('education_record'), ('employment_record'), ('preference'),
               ('match_result'), ('match_run'), ('answer'), ('response_session'),
               ('field_value'), ('assertion'), ('evidence')) t(table_name)
  CROSS JOIN (VALUES ('updated_at'), ('recorded_at'), ('ingested_at'),
                     ('last_seen_at'), ('synced_at')) c(col)
 WHERE EXISTS (SELECT 1 FROM information_schema.columns ic
                WHERE ic.table_schema = 'mt' AND ic.table_name = t.table_name
                  AND ic.column_name = c.col)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. 重写 log_change()：无实质变化不记；有变化记列名
--    （CREATE OR REPLACE：不改已应用的迁移，用新迁移替换函数）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE
  rec     jsonb;
  k       text;
  ident   text := 'unknown';
  v_mode  text;
  v_cols  text[];
  v_n     int := 0;
BEGIN
  -- summary 模式的表：不逐行记（见 audit_mode 的说明）
  SELECT mode INTO v_mode FROM mt.audit_mode WHERE table_name = TG_TABLE_NAME;
  IF v_mode = 'summary' THEN
    RETURN NULL;
  END IF;

  -- ① UPDATE：先看有没有**实质变化**（排除簿记列）
  IF TG_OP = 'UPDATE' THEN
    SELECT array_agg(n.key ORDER BY n.key) INTO v_cols
      FROM jsonb_each(to_jsonb(NEW)) n
      JOIN jsonb_each(to_jsonb(OLD)) o ON o.key = n.key
     WHERE n.value IS DISTINCT FROM o.value
       AND n.key NOT IN (SELECT column_name FROM mt.audit_ignore_columns
                          WHERE table_name = TG_TABLE_NAME);
    -- 一个实质列都没变 → 这条审计为零信息，不写。
    -- 实测：job_posting 每次重算 UPDATE 690 行，绝大多数是"值没变但触发了 UPDATE"。
    IF v_cols IS NULL OR cardinality(v_cols) = 0 THEN
      RETURN NULL;
    END IF;
  END IF;

  rec := to_jsonb(COALESCE(NEW, OLD));
  FOR k IN SELECT jsonb_object_keys(rec) LOOP
    IF k = 'id' OR k LIKE '%\_id' THEN
      ident := k || '=' || COALESCE(rec ->> k, '');
      EXIT;
    END IF;
  END LOOP;

  INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
  VALUES (current_user, TG_TABLE_NAME, ident, lower(TG_OP),
          jsonb_build_object('op', TG_OP, 'table', TG_TABLE_NAME, 'row', ident,
                             'mode', 'row',
                             -- ② 有变化时记**列名**（不记值：值可能含个人信息，
                             --    而且体积会爆）。从"某行被更新过"变成"哪几列变了"。
                             'changed_columns', to_jsonb(COALESCE(v_cols, ARRAY[]::text[])),
                             'changed_count', COALESCE(cardinality(v_cols), 0)));
  RETURN NULL;
END $$;
COMMENT ON FUNCTION log_change() IS
  '行级审计。三条纪律：① summary 模式的表不逐行记（audit_mode）；'
  '② **UPDATE 无实质变化时不写**（只碰簿记列也算无变化）—— '
  '实测 job_posting 每次重算逐行写 690 条、而绝大多数值没变，这些行零信息却占体量；'
  '③ 有变化时记**变化的列名**（不记值，避免体积与个人信息问题）。';

-- ---------------------------------------------------------------------------
-- 3. 口径：把"机械噪声占审计的比例"变成可监控的数字
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_AUDIT_NOISE_RATE', '机械噪声审计占比', '结果', 'ratio',
 '(SELECT count(*) FROM change_log WHERE object_type IN (''job_requirement'',''job_posting'',''job_task''))',
 '(SELECT count(*) FROM change_log)', NULL,
 '%', NULL, '机械重算类表（JD 解析先删后插/逐行更新）产生的审计行占比',
 'JD 解析类表产生的审计行 / 全部审计行。这些行绝大多数是"同一批岗位被重算"造成的，'
 '**信息量极低但体量极大** —— 实测占 change_log 的 70%（771,715 行 / 148 MB），'
 '并把完整回归里的备份步骤从 664 秒拖到 7224 秒。',
 '本口径覆盖**历史累积**，所以它不会因为"新迁移修好了触发逻辑"而下降 —— '
 '下降要靠**归档历史噪声**，而那是保留策略的决策（rp_change_log 保留 60 个月，'
 '现有行全在保留期内）。本库只向前修，不擅自删。', 1),

('M_AUDIT_CHANGELOG_MB', '审计流水体量（MB）', '结果', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(pg_total_relation_size(''mt.change_log'') / 1048576.0, 1))',
 '个', NULL, NULL,
 'change_log 占用的磁盘体量（MB）。**它是回归里备份步骤耗时的主导项**：'
 '库 255 MB 里它占 210 MB（82%），备份做完整 dump 时成本几乎全在这里。',
 '体量大本身不是错（审计是合规资产），错的是**体量被零信息行占据**。'
 '看到它快速增长时，先看 M_AUDIT_NOISE_RATE 判断是"噪声"还是"真实变更"。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 4. 实证：造一次"无实质变化"的更新与一次"真变化"，看审计写不写
-- ---------------------------------------------------------------------------
DO $$
DECLARE r record; n int := 0; n_before bigint; n_after bigint; v numeric;
        v_id text; v_title text; v_col text;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry
            WHERE metric_id IN ('M_AUDIT_NOISE_RATE','M_AUDIT_CHANGELOG_MB') LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  IF n <> 2 THEN RAISE EXCEPTION '两个审计口径没登记全（%）', n; END IF;

  SELECT job_id, title_raw INTO v_id, v_title FROM job_posting LIMIT 1;

  -- ① 无实质变化：把值设成它自己（触发 UPDATE，但值没变）
  SELECT count(*) INTO n_before FROM change_log WHERE object_name = 'job_id=' || v_id;
  UPDATE job_posting SET title_raw = v_title WHERE job_id = v_id;
  SELECT count(*) INTO n_after FROM change_log WHERE object_name = 'job_id=' || v_id;
  IF n_after <> n_before THEN
    RAISE EXCEPTION '无实质变化的 UPDATE 仍然写了审计（% → %）—— 零信息行没被拦住',
      n_before, n_after;
  END IF;
  RAISE NOTICE '① 无实质变化的 UPDATE：审计行数没变（%）—— 零信息行已拦住', n_before;

  -- ② 真变化：改一个有意义的列，必须留下审计**且带列名**
  UPDATE job_posting SET title_normalized = coalesce(title_normalized, '') || ' '
   WHERE job_id = v_id;
  SELECT count(*) INTO n_after FROM change_log WHERE object_name = 'job_id=' || v_id;
  IF n_after <= n_before THEN
    RAISE EXCEPTION '真实变更没有写审计（% → %）—— 拦过头了', n_before, n_after;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM change_log
                  WHERE object_name = 'job_id=' || v_id
                    AND detail->'changed_columns' ? 'title_normalized') THEN
    RAISE EXCEPTION '真实变更的审计里**没有记录变化的列名** —— 还是零信息行';
  END IF;
  RAISE NOTICE '② 真实变更：写了审计且带列名（%）', n_after - n_before;

  -- ③ 只改簿记列的更新，也算无实质变化
  --    注意：簿记列名要**从注册表里取**，不能想当然写 updated_at ——
  --    实测 job_posting 没有 updated_at（它的簿记列是 recorded_at），
  --    我第一版写死 updated_at 直接报 "column does not exist"。
  SELECT column_name INTO v_col FROM audit_ignore_columns
   WHERE table_name = 'job_posting' ORDER BY column_name LIMIT 1;
  IF v_col IS NULL THEN
    RAISE NOTICE '③ job_posting 没有登记簿记列，跳过这一条检验';
  ELSE
    SELECT count(*) INTO n_before FROM change_log WHERE object_name = 'job_id=' || v_id;
    EXECUTE format('UPDATE job_posting SET %I = now() WHERE job_id = %L', v_col, v_id);
    SELECT count(*) INTO n_after FROM change_log WHERE object_name = 'job_id=' || v_id;
    IF n_after <> n_before THEN
      RAISE EXCEPTION '只改簿记列 % 仍写了审计（% → %）—— 簿记列名单没生效',
        v_col, n_before, n_after;
    END IF;
    RAISE NOTICE '③ 只改簿记列（%）：不写审计（%）—— 簿记列名单生效', v_col, n_before;
  END IF;

  SELECT value INTO v FROM metric_value('M_AUDIT_NOISE_RATE');
  RAISE NOTICE '机械噪声审计占比 = %（历史累积，只向前修不会让它下降）', v;
END $$;

COMMIT;
