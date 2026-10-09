-- ============================================================================
-- 医学生人才信息库 · 025 本体结构属于公开字典（修正策略规则漏项）v1.0.0
--
-- 背景（端到端探针实测发现）
-- ---------------------------------------------------------------------------
-- 匿名访客（T0）打开首页被数据库拒绝：
--     **permission denied for table concept_relation**
-- 原因：017 的 `refresh_column_policy()` 里，"字典/语义层一律公开（T0）"那张表清单
-- 列了 concept / concept_mapping / code_table / code_value / occupation / category_node …
-- 但**漏了 concept_relation 与 concept_ancestor** —— 于是它们掉进兜底值 T1，
-- 而首页要画概念关系图，T0 就读不到。
--
-- 判断：这是**规则漏项**，不是"首页不该看这个概念"。
-- 本体（概念之间的层级与关系）和码表同类 —— 它是"受控的词表结构"，
-- 目的就是让人理解本库在说什么；它不包含任何个人信息。
-- 把它判成 T1 会让最需要字典的匿名访客看不到字典，与需求"编号、年龄等可以公开"的方向相反。
--
-- 顺带把同一类漏项一次性补全（同类东西不该因为清单没列全而分到不同等级）：
--   · concept_relation / concept_ancestor —— 本体结构
--   · metric_definition / attribute_definition —— 度量与扩展属性的**定义**（不含值）
--   · search_document —— 检索索引条目（它索引的正文**内容**另行判定，见下方说明）
--   · dimension —— 画像维度注册表（已在元数据层，但清单里写作 'dimension' 而已覆盖）
--
-- ⚠ search_document 的特别说明：它自身的 body 列**可能含原始正文**，
--   所以它不该整表 T0。这里只把它的**元数据列**（标题、来源、时间）放进公开清单，
--   正文列保持受限 —— 这是"同一个表里不同列的等级可以不同"的正常用法，
--   也正是本套机制要表达的东西（column_policy 是按列记的，不是按表记的）。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 幂等刷新。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION refresh_column_policy() RETURNS TABLE (
  tier text, n integer
) LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r RECORD;
  v_tier text;
  v_src  text;
  v_note text;
BEGIN
  -- 3.1 先按列名模式给出**兜底**（最保守的那一版），保证每一列都有归属
  FOR r IN
    SELECT c.table_name, c.column_name
      FROM information_schema.columns c
      JOIN pg_class cl ON cl.relname = c.table_name
      JOIN pg_namespace n ON n.oid = cl.relnamespace AND n.nspname = 'mt'
     WHERE c.table_schema = 'mt' AND cl.relkind = 'r'
  LOOP
    v_tier := 'T1';
    v_src  := 'table_default';
    v_note := '默认等级：未命中任何规则';

    -- ① 公共标识：编号/主键。用户明确说过"编号可以是公开"。
    IF r.column_name IN ('person_id','subject_code','job_id','occupation_id','concept_id',
                         'code_table_id','field_id','entity_id','table_name','column_name',
                         'policy_id','tier','code') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '标识/字典键：公开可读（编号类）';

    -- ② 字典、码表、本体与度量定义 —— 它们是"用来被读的"，公开
    --    （025 补全了 017 漏掉的 concept_relation / concept_ancestor / metric_definition /
    --      attribute_definition / search_document 等）
    ELSIF r.table_name IN ('code_table','code_value','field_catalog','entity_catalog',
                           'access_tier','access_policy','category_node','concept',
                           'concept_mapping','concept_relation','concept_ancestor',
                           'metric_definition','attribute_definition','occupation',
                           'dimension','schema_migration') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/本体/定义：公开可读';

    -- ②b 检索索引：只公开它的**元数据列**；正文列保持受限
    ELSIF r.table_name = 'search_document'
          AND r.column_name IN ('doc_id','title','source_table','source_id','access_tier',
                                'created_at','updated_at','lang','kind') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '检索索引的元数据列：公开';
    ELSIF r.table_name = 'search_document' THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '检索索引的正文列（body/body_tokens/tsv）：可能含原始正文，员工及以上';

    -- ③ 年龄相关：用户明确说"年龄可以是公开"
    ELSIF r.column_name IN ('birth_year','age','age_band') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '年龄段：用户明确要求公开';

    -- ④ 可识别身份 / 联系 / 证件 —— 最高等级，且导出动作另受 access_policy 约束
    ELSIF r.column_name ~ '(name|phone|email|contact|id_card|id_hash|passport|wechat|openid|address|real_name)'
          OR r.table_name = 'person_pii' THEN
      v_tier := 'T3'; v_src := 'pattern'; v_note := '可识别个人身份或联系方式：仅管理员，且必须留痕';

    -- ⑤ 健康 / 政治面貌 / 户籍 / 民族 —— 敏感个人属性
    ELSIF r.column_name ~ '(health|political|hukou|ethnicity|marital|religio|disability)'
          OR r.table_name IN ('consent_record','consent_withdrawal_action','subject_request') THEN
      v_tier := 'T2'; v_src := 'pattern'; v_note := '敏感个人属性或同意记录：员工及以上';

    -- ⑥ 审计与访问日志本体、以及登录用户与会话
    ELSIF r.table_name IN ('change_log','access_log','tombstone','dataset_release',
                           'app_user','web_session') THEN
      v_tier := 'T2'; v_src := 'pattern'; v_note := '审计/发布/身份记录：员工及以上';
    END IF;

    INSERT INTO column_policy (table_name, column_name, min_tier, source, note, updated_at)
    VALUES (r.table_name, r.column_name, v_tier, v_src, v_note, now())
    ON CONFLICT (table_name, column_name) DO UPDATE
       SET min_tier = EXCLUDED.min_tier, source = EXCLUDED.source,
           note = EXCLUDED.note, updated_at = now();
  END LOOP;

  -- 3.2 用字典里的字段等级**覆盖**兜底值（字典依据优先于列名约定）
  FOR r IN
    SELECT cc.table_name, cc.column_name, f.access_tier, f.field_id, f.title, f.is_private
      FROM field_catalog f
      JOIN entity_catalog e ON e.entity_id = f.entity_id
      JOIN information_schema.columns cc
        ON cc.table_schema = 'mt' AND cc.table_name = e.table_name
       AND lower(cc.column_name) = lower(regexp_replace(f.field_id, '^F_[A-Za-z0-9]+_', ''))
     WHERE f.access_tier IS NOT NULL AND f.access_tier <> ''
  LOOP
    UPDATE column_policy
       SET min_tier = r.access_tier,
           source = 'field_catalog',
           note = '字典：' || r.title || '（' || r.field_id || '）'
                  || CASE WHEN r.is_private THEN '，标记为私有' ELSE '' END,
           updated_at = now()
     WHERE table_name = r.table_name AND column_name = r.column_name
       -- 字典不得把已判定为 T3 的敏感列放宽
       AND NOT (min_tier = 'T3' AND r.access_tier IN ('T0','T1'));
  END LOOP;

  -- 3.3 用 access_policy 里 view_inline 的最低等级**调严**（人工策略优先于自动推导）
  FOR r IN
    SELECT ap.object_id, ap.min_tier
      FROM access_policy ap
     WHERE ap.object_type = 'field' AND ap.action = 'view_inline'
  LOOP
    UPDATE column_policy cp
       SET min_tier = r.min_tier,
           source = 'access_policy',
           note = '人工策略（access_policy.view_inline）：' || r.object_id,
           updated_at = now()
      FROM field_catalog f, entity_catalog e
     WHERE f.field_id = r.object_id
       AND e.entity_id = f.entity_id
       AND cp.table_name = e.table_name
       AND lower(cp.column_name) = lower(regexp_replace(f.field_id, '^F_[A-Za-z0-9]+_', ''))
       AND access_rank(r.min_tier) >= access_rank(cp.min_tier);
  END LOOP;

  RETURN QUERY
    SELECT cp.min_tier, count(*)::int FROM column_policy cp GROUP BY 1 ORDER BY 1;
END $$;

COMMENT ON FUNCTION refresh_column_policy() IS
  '从 field_catalog / access_policy 推导列级策略，推不出来的按列名模式兜底；'
  '每一行的 source 说明它的依据。可反复执行（幂等）。'
  '025 修正：本体（concept_relation/concept_ancestor）与度量/属性定义属公开字典；'
  'search_document 只公开元数据列，正文列保持受限。';

SELECT refresh_column_policy();
SELECT apply_column_grants();

-- 自检：本体表必须对 T0 可读（这条断言防的是"同类的表被分到不同等级"这类规则漏项）
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM column_policy
   WHERE table_name IN ('concept_relation','concept_ancestor') AND min_tier = 'T0';
  IF n = 0 THEN
    RAISE EXCEPTION '本体表仍是受限的：规则漏项没修好';
  END IF;
  RAISE NOTICE '本体表 T0 列数 = %', n;
END $$;

COMMIT;
