-- ============================================================================
-- 医学生人才信息库 · 044 主体标识的披露控制（修 P0：匿名可枚举全库 person_id）v1.0.0
--
-- 缺陷（独立审查发现；我已用第一手证据复现）
-- ---------------------------------------------------------------------------
-- 030 的披露控制**只按表名写死** `person` / `person_pii` 两张表，紧接着的通用规则
-- 又把 `person_id` 判成 T0 公开。实测结果：
--   · **29 张表**的 `person_id` 是 T0；
--   · 而公开的列级剖析视图 `v_column_profile_public` 把 `top_values`
--     （**Top-K 取值 + 精确频次**）一并交出去：
--         evidence.person_id          → per_real_fengtang 21 / per_real_li_tiantian 21 / per_real_yu_ying 20
--         employment_record.person_id → per_real_yu_ying 6 / per_real_fengtang 5 ...
--         award_honor.person_id       → per_real_fengtang 7 ...
--   · 于是**匿名**在公网上既拿到了真实人物的 id（其 id 编码了姓名，见 030），
--     又拿到了"他产出最多"这种精确统计。
--
-- 根因（一句话）
-- ---------------------------------------------------------------------------
-- **"主体标识"是值的语义，不是表名的属性。** 030 把它实现成了"表名白名单"，
-- 所以只要换个表名（29 张子表），保护就自动失效。
--
-- 修法：两层，缺一不可
-- ---------------------------------------------------------------------------
-- 第 1 层 **策略**：判定改成**语义化 + 动态发现**，不再手写表名：
--     `is_subject_identifier(table, column)` = 列名是 person_id/subject_code
--     **或**该列是指向 `mt.person` 的外键（从 pg_constraint 动态读）。
--     命中即 **T1**。这样新增任何子表都自动被覆盖 —— 不需要记得改白名单。
--
-- 第 2 层 **取值列表**：即使某列被允许公开，**公开它的取值分布仍是另一个更强的动作**。
--     这正是 030 写下的原则"能读一列 ≠ 可以枚举它的取值"，但当时只用在"编号"上，
--     没有用在"Top-K 列表"上。新增 `is_enumeration_risk(table, column)`：
--     主体标识本身，**或**指向"含 person_id 的表"的外键（例如 skill_assertion.evidence_id
--     → evidence，而 evidence 含 person_id）—— 这些列的取值分布都能用来枚举主体集合。
--     `v_column_profile_public` 对命中的列把 `top_values` 置空并写明原因。
--
-- 为什么第 2 层不能省：第 1 层靠"我们记得把新列判成 T1"。第 2 层是**出口处的兜底** ——
-- 即使将来有人误把某列判成 T0，公开视图也不会把它的取值列表交出去。
-- 两层都做成函数而不是复制条件，遵守"一个口径一份实现"。
--
-- 可重放：CREATE OR REPLACE FUNCTION / VIEW + 刷新 + 对账 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 两个判定函数（唯一实现，策略与视图共用）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION is_subject_identifier(p_table text, p_column text)
RETURNS boolean LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT p_column IN ('person_id', 'subject_code')
      OR EXISTS (
           SELECT 1
             FROM pg_constraint con
             JOIN pg_class rel ON rel.oid = con.conrelid
             JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'mt' AND con.contype = 'f'
              AND rel.relname = p_table
              AND con.confrelid = 'mt.person'::regclass
              AND (SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = con.conrelid
                      AND a.attnum = ANY(con.conkey) LIMIT 1) = p_column);
$$;
COMMENT ON FUNCTION is_subject_identifier(text, text) IS
  '这一列是不是**主体标识**？判据：列名是 person_id/subject_code，'
  '或它是指向 mt.person 的外键（动态读 pg_constraint，不靠表名白名单）。'
  '为什么必须动态：030 把它写成"person/person_pii 两张表"，'
  '于是另外 29 张子表的 person_id 全是 T0，匿名可枚举（实测）。'
  '**主体标识是值的语义，不是表名的属性。**';

CREATE OR REPLACE FUNCTION is_enumeration_risk(p_table text, p_column text)
RETURNS boolean LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT is_subject_identifier(p_table, p_column)
      -- 指向"含 person_id 的表"的外键：这些列的取值分布同样能枚举主体集合
      -- （例：skill_assertion.evidence_id → evidence，而 evidence 含 person_id）
      OR EXISTS (
           SELECT 1
             FROM pg_constraint con
             JOIN pg_class rel ON rel.oid = con.conrelid
             JOIN pg_namespace ns ON ns.oid = rel.relnamespace
             JOIN pg_class frel ON frel.oid = con.confrelid
            WHERE ns.nspname = 'mt' AND con.contype = 'f'
              AND rel.relname = p_table
              AND (SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = con.conrelid
                      AND a.attnum = ANY(con.conkey) LIMIT 1) = p_column
              AND EXISTS (SELECT 1 FROM information_schema.columns ic
                           WHERE ic.table_schema = 'mt'
                             AND ic.table_name = frel.relname
                             AND ic.column_name = 'person_id'));
$$;
COMMENT ON FUNCTION is_enumeration_risk(text, text) IS
  '"公开这一列的**取值列表**"是否有枚举主体的风险？比 is_subject_identifier 更宽：'
  '还包括指向"含 person_id 的表"的外键。用于公开视图抑制 top_values —— '
  '因为"能读一列"与"能枚举它的取值"是两个不同强度的动作（030 的原则）。';

-- ---------------------------------------------------------------------------
-- 2. 策略：主体标识一律 T1（语义化规则，覆盖任意张表）
-- ---------------------------------------------------------------------------
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
      v_note := '表级授权：列级剖析快照含 Top-K 取值，只给 T3（由 apply_role_baseline 授予）。';

    -- ⓪b **主体标识的披露控制（044 修正：语义化 + 动态发现）**
    --    030 把它写成"person/person_pii 两张表"，于是另外 29 张子表的 person_id
    --    全是 T0，而公开剖析视图又把它们的 Top-K 取值+频次交出去 ——
    --    匿名因此拿到了编码姓名的真实人物 id 及其精确统计。
    ELSIF is_subject_identifier(r.table_name, r.column_name) THEN
      v_tier := 'T1'; v_src := 'pattern';
      v_note := '主体标识的披露控制：列名是 person_id/subject_code，'
                '或它是指向 mt.person 的外键 —— **取值会 identify 个体**'
                '（实测 per_real_fengtang 这类 id 直接编码姓名）。'
                '判据是语义+动态发现，不靠表名白名单：这样新增子表自动被覆盖。'
                '匿名的"数量"仍公开（走聚合闸门 public_counts）。';

    -- ⓪c 年龄的分级口径（037，用户决策）
    ELSIF r.column_name = 'age_band' THEN
      v_tier := 'T0'; v_src := 'pattern';
      v_note := '年龄段（CT_AGE_BAND）：统计属性，公开。写入时快照，基准年见 mt.age_band_asis。';
    ELSIF r.column_name IN ('birth_year', 'birth_date', 'birth_month', 'age') THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '出生年月 / 精确年龄：可识别个人信息，员工及以上。公开的是年龄段而不是出生年份。';

    -- ⓪d 登录限流的两张表（040）
    ELSIF r.table_name = 'login_attempt' THEN
      v_tier := 'X'; v_src := 'pattern';
      v_note := '登录尝试流水（邮箱+时间）：仍属个人信息，不通过列级机制授出。';
    ELSIF r.table_name = 'login_policy' THEN
      v_tier := 'T0'; v_src := 'pattern';
      v_note := '登录限流参数：策略元数据，公开（不含个人信息）。';

    -- ① 公共标识：编号/主键（**主体标识已在 ⓪b 被拦下**）
    ELSIF r.column_name IN ('job_id','occupation_id','concept_id','code_table_id','field_id',
                            'entity_id','table_name','column_name','policy_id','tier','code') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '标识/字典键：公开可读（非主体标识）';

    -- ② 字典、码表、本体与度量定义
    ELSIF r.table_name IN ('code_table','code_value','field_catalog','entity_catalog',
                           'access_tier','access_policy','category_node','concept',
                           'concept_mapping','concept_relation','concept_ancestor',
                           'metric_definition','attribute_definition','occupation',
                           'dimension','schema_migration','age_band_asis',
                           'export_denied','public_view_registry','public_count_excluded') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/本体/定义/策略元数据：公开可读';

    -- ②b 检索索引
    ELSIF r.table_name = 'search_document'
          AND r.column_name IN ('doc_id','title','source_table','source_id','access_tier',
                                'created_at','updated_at','lang','kind') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '检索索引的元数据列：公开';
    ELSIF r.table_name = 'search_document' THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '检索索引的正文列：可能含原始正文，员工及以上';

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

  -- 字典优先（**主体标识与表级授权对象不被覆盖** —— 044 起用同一个判定函数）
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
       AND table_name NOT IN ('column_profile', 'login_attempt')
       AND NOT is_subject_identifier(r.table_name, r.column_name)
       AND r.column_name NOT IN ('age_band','birth_year','birth_date','birth_month')
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

-- ---------------------------------------------------------------------------
-- 3. 公开剖析视图：对"可枚举主体"的列抑制 Top-K 取值（出口兜底）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_column_profile_public AS
SELECT cp.table_name, cp.column_name, cp.data_type, cp.n_rows, cp.n_not_null,
       cp.n_distinct, cp.null_frac, cp.min_value, cp.max_value, cp.avg_len,
       -- 出口兜底：命中"可枚举主体"的列一律不给取值列表
       CASE WHEN is_enumeration_risk(cp.table_name, cp.column_name)
            THEN NULL::jsonb ELSE cp.top_values END AS top_values,
       CASE WHEN is_enumeration_risk(cp.table_name, cp.column_name)
            THEN '该列的取值列表会对主体集合构成枚举风险（列本身是指向主体或其子表的外键），'
                 '按披露控制不公开取值 —— 能读这一列 ≠ 可以枚举它的取值'
            ELSE cp.top_note END AS top_note,
       cp.is_enum_like, cp.tier, cp.computed_at
  FROM column_profile cp
  JOIN column_policy pol
    ON pol.table_name = cp.table_name AND pol.column_name = cp.column_name
 WHERE access_rank(pol.min_tier) <= access_rank('T0');
COMMENT ON VIEW v_column_profile_public IS
  '只含 T0（公开）列的统计。**044 起对"可枚举主体"的列抑制 top_values** —— '
  '即使某列被判成 T0，公开它的**取值列表**仍是更强的动作：'
  '实测 evidence.person_id 的 Top-K 曾把 per_real_fengtang 及精确条数交给匿名。'
  '两层保护：策略层把主体标识判成 T1（is_subject_identifier），'
  '出口层对可枚举主体的列置空 top_values（is_enumeration_risk）。';

GRANT SELECT ON v_column_profile_public TO mt_t0, mt_t1, mt_t2, mt_t3, mt_portal;

SELECT refresh_column_policy();
SELECT apply_column_grants();
SELECT refresh_age_bands();

-- ---------------------------------------------------------------------------
-- 4. 硬断言：这类泄露不可能再悄悄存在
-- ---------------------------------------------------------------------------
DO $$
DECLARE n int; bad text;
BEGIN
  -- ① 任何表的 person_id / subject_code 都不得 ≤ T0
  SELECT count(*) INTO n FROM column_policy
   WHERE column_name IN ('person_id','subject_code')
     AND access_rank(min_tier) <= access_rank('T0');
  IF n > 0 THEN
    RAISE EXCEPTION '仍有 % 个主体标识列的等级 ≤ T0 —— 披露控制没覆盖全部表', n;
  END IF;

  -- ② 指向 person 的外键列也不得 ≤ T0
  SELECT count(*) INTO n FROM column_policy cp
   WHERE access_rank(cp.min_tier) <= access_rank('T0')
     AND is_subject_identifier(cp.table_name, cp.column_name);
  IF n > 0 THEN
    RAISE EXCEPTION '仍有 % 个主体标识（含外键判定）可被匿名读取', n;
  END IF;

  -- ③ 公开剖析视图里不得出现任何主体标识列的取值
  SELECT string_agg(table_name || '.' || column_name, '、') INTO bad
    FROM v_column_profile_public
   WHERE column_name IN ('person_id','subject_code') AND top_values IS NOT NULL;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION '公开剖析视图仍在交出主体标识的取值：%', bad;
  END IF;

  -- ④ 权限层面也真的生效（不能只是表里写着）
  SELECT count(*) INTO n FROM column_policy
   WHERE column_name = 'person_id' AND table_name <> 'person';
  IF has_column_privilege('mt_t0', 'mt.evidence', 'person_id', 'SELECT') THEN
    RAISE EXCEPTION '匿名仍能读 evidence.person_id';
  END IF;
  IF has_column_privilege('mt_t0', 'mt.award_honor', 'person_id', 'SELECT') THEN
    RAISE EXCEPTION '匿名仍能读 award_honor.person_id';
  END IF;
  IF NOT has_column_privilege('mt_t1', 'mt.evidence', 'person_id', 'SELECT') THEN
    RAISE EXCEPTION 'T1 反而读不到 evidence.person_id —— 收得太紧了';
  END IF;

  RAISE NOTICE '披露控制自检通过：主体标识在全部表上对匿名关闭、对 T1 开放，公开视图已抑制取值';
END $$;

COMMIT;
