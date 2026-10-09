-- ============================================================================
-- 医学生人才信息库 · 030 标识列不得泄露真实身份（披露控制）v1.0.0
--
-- 实测发现的缺陷（不是推测）
-- ---------------------------------------------------------------------------
-- 匿名访客打开 `/field/person/person_id`（person_id 是 **T0 公开**列）时，
-- 页面会列出该列的 Top-K 取值 —— 而库里有三条真实公开人物的记录：
--     per_real_fengtang      冯唐
--     per_real_li_tiantian   李天天
--     per_real_yu_ying       于莺
-- 这些 **id 本身就把姓名编码进去了**，于是：
--   **T2 保护的详细资料，被 T0 的编号列表泄露了身份。**
--
-- 这是"披露控制"的经典形态：**能读一列 ≠ 可以枚举它的取值**。
-- 列级分级只能表达"这一列能不能读"，表达不了"这一列的值会不会identify 一个人"。
--
-- 决策：把 `person.person_id` / `person.subject_code` 调到 **T1**
-- ---------------------------------------------------------------------------
-- 为什么不是"给真实人物换一个不透明的 id"（那也是个好办法，见下）：
--   换 id 会影响既有文档/引用（docs/16 的案例验证、match_run 的产物），
--   属于数据迁移，要单独评估。而**把这两个标识列收进 T1** 是立刻可执行、
--   可强制、可断言的 —— 用已有的 column_policy 机制，不新增机制。
-- 为什么这仍然满足用户"编号可以公开"的意图：
--   编号依然可读，只是**需要登录**（T1 = 注册用户）。匿名访客仍然能拿到
--   **数量**（`count(*)` 不引用任何列，实测不受列级权限限制）——
--   也就是"有多少人"公开，"具体是谁的编号"需要登录。这正是需求原文
--   "编号、年龄等可以是公开"与"其他字段设置某种权限级别"之间的合理落点。
--
-- 顺带记录一个更强的建议（留给后续）：**标识符不应编码姓名**。
-- `per_real_fengtang` 这种 id 一旦出现在任何日志/导出/URL 里，就等于泄露身份。
-- 正确的做法是用不透明随机 id + 单独的 `public_case_label` 存"这是谁"（那一列可以是 T2）。
-- 本迁移不动数据，只把访问面收紧；换 id 需要单独的迁移与影响评估。
--
-- 可重放：刷新函数 + 幂等对账。
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
  FOR r IN
    SELECT c.table_name, c.column_name
      FROM information_schema.columns c
      JOIN pg_class cl ON cl.relname = c.table_name
      JOIN pg_namespace n ON n.oid = cl.relnamespace AND n.nspname = 'mt'
     WHERE c.table_schema = 'mt' AND cl.relkind = 'r'
  LOOP
    v_tier := 'T1'; v_src := 'table_default'; v_note := '默认等级：未命中任何规则';

    -- ⓪ 表级授权对象
    IF r.table_name = 'column_profile' THEN
      v_tier := 'X'; v_src := 'pattern';
      v_note := '表级授权：列级剖析快照含 Top-K 取值，只给 T3（由 apply_role_baseline 授予）。'
                'X 在这里表示"不通过列级机制授出"，不是"谁也读不到"。';

    -- ⓪b **标识列披露控制**（030 新增）：主体标识列的取值会 identify 个体，
    --    所以它们不走"编号一律公开"那条规则，统一收进 T1。
    --    判据：表是 person/person_pii 的标识列。这两张表的 id 直接对应真实个体。
    ELSIF (r.table_name IN ('person', 'person_pii')
           AND r.column_name IN ('person_id', 'subject_code')) THEN
      v_tier := 'T1'; v_src := 'pattern';
      v_note := '披露控制：**主体标识列的取值会 identify 个体**（实测：per_real_fengtang '
                '这类 id 直接编码姓名）。能读一列 ≠ 可以枚举它的取值 —— '
                '匿名的"数量"仍然公开（count(*) 不受列级权限限制），但"具体编号"需要登录。';

    -- ① 公共标识：编号/主键（**不含**上面的主体标识列）
    ELSIF r.column_name IN ('person_id','subject_code','job_id','occupation_id','concept_id',
                            'code_table_id','field_id','entity_id','table_name','column_name',
                            'policy_id','tier','code') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '标识/字典键：公开可读（编号类）';

    -- ② 字典、码表、本体与度量定义
    ELSIF r.table_name IN ('code_table','code_value','field_catalog','entity_catalog',
                           'access_tier','access_policy','category_node','concept',
                           'concept_mapping','concept_relation','concept_ancestor',
                           'metric_definition','attribute_definition','occupation',
                           'dimension','schema_migration') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/本体/定义：公开可读';

    -- ②b 检索索引
    ELSIF r.table_name = 'search_document'
          AND r.column_name IN ('doc_id','title','source_table','source_id','access_tier',
                                'created_at','updated_at','lang','kind') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '检索索引的元数据列：公开';
    ELSIF r.table_name = 'search_document' THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '检索索引的正文列（body/body_tokens/tsv）：可能含原始正文，员工及以上';

    -- ③ 年龄相关
    ELSIF r.column_name IN ('birth_year','age','age_band') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '年龄段：用户明确要求公开';

    -- ④ 可识别身份 / 联系 / 证件
    ELSIF r.column_name ~ '(name|phone|email|contact|id_card|id_hash|passport|wechat|openid|address|real_name)'
          OR r.table_name = 'person_pii' THEN
      v_tier := 'T3'; v_src := 'pattern'; v_note := '可识别个人身份或联系方式：仅管理员，且必须留痕';

    -- ⑤ 健康 / 政治面貌 / 户籍 / 民族
    ELSIF r.column_name ~ '(health|political|hukou|ethnicity|marital|religio|disability)'
          OR r.table_name IN ('consent_record','consent_withdrawal_action','subject_request') THEN
      v_tier := 'T2'; v_src := 'pattern'; v_note := '敏感个人属性或同意记录：员工及以上';

    -- ⑥ 审计、身份与会话记录
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

  -- 字典优先（表级授权对象与披露控制列不被覆盖）
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
       SET min_tier = r.access_tier, source = 'field_catalog',
           note = '字典：' || r.title || '（' || r.field_id || '）'
                  || CASE WHEN r.is_private THEN '，标记为私有' ELSE '' END,
           updated_at = now()
     WHERE table_name = r.table_name AND column_name = r.column_name
       AND table_name <> 'column_profile'
       AND NOT (table_name IN ('person','person_pii')
                AND column_name IN ('person_id','subject_code'))
       AND NOT (min_tier = 'T3' AND r.access_tier IN ('T0','T1'));
  END LOOP;

  -- 人工策略调严
  FOR r IN
    SELECT ap.object_id, ap.min_tier
      FROM access_policy ap
     WHERE ap.object_type = 'field' AND ap.action = 'view_inline'
  LOOP
    UPDATE column_policy cp
       SET min_tier = r.min_tier, source = 'access_policy',
           note = '人工策略（access_policy.view_inline）：' || r.object_id,
           updated_at = now()
      FROM field_catalog f, entity_catalog e
     WHERE f.field_id = r.object_id AND e.entity_id = f.entity_id
       AND cp.table_name = e.table_name
       AND lower(cp.column_name) = lower(regexp_replace(f.field_id, '^F_[A-Za-z0-9]+_', ''))
       AND access_rank(r.min_tier) >= access_rank(cp.min_tier);
  END LOOP;

  RETURN QUERY SELECT cp.min_tier, count(*)::int FROM column_policy cp GROUP BY 1 ORDER BY 1;
END $$;

COMMENT ON FUNCTION refresh_column_policy() IS
  '从 field_catalog / access_policy 推导列级策略，推不出来的按列名模式兜底；'
  '每行的 source 说明依据。030 起含**披露控制**：主体标识列（person_id/subject_code）'
  '固定 T1 —— 因为它们的取值会 identify 个体（实测 per_real_fengtang 编码了姓名）。';

SELECT refresh_column_policy();
SELECT apply_column_grants();

-- 硬断言：匿名绝不能读到主体标识列
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM column_policy
   WHERE table_name IN ('person','person_pii')
     AND column_name IN ('person_id','subject_code')
     AND access_rank(min_tier) <= access_rank('T0');
  IF n > 0 THEN
    RAISE EXCEPTION '仍有 T0 可读的主体标识列 —— 披露控制没生效';
  END IF;
  IF NOT has_column_privilege('mt_t1', 'mt.person', 'person_id', 'SELECT') THEN
    RAISE EXCEPTION 'T1 读不到 person.person_id —— 收得太紧了，注册用户应该能读';
  END IF;
  RAISE NOTICE '披露控制自检通过：标识列对匿名关闭、对 T1 开放';
END $$;

COMMIT;
