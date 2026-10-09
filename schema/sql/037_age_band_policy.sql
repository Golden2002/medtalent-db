-- ============================================================================
-- 医学生人才信息库 · 037 年龄字段的分级口径：年龄段公开、出生年收进 T2 v1.0.0
--
-- 用户决策：「公开年龄段（age_band）、birth_year 保持 T2」
-- ---------------------------------------------------------------------------
-- 036 已经把 `person_demographics.age_band` 落成物理列并回填。本迁移改**策略规则**，
-- 让分级真的按这个口径生效：
--   · `age_band` → **T0**（公开）：年龄段是统计属性；
--   · `birth_year` / `age` → **T2**：出生年份（或直接年龄数字）属可识别个人信息。
--
-- 为什么必须改函数而不是直接 UPDATE column_policy
-- ---------------------------------------------------------------------------
-- `refresh_column_policy()` 是**权威推导**：它会重写整张 column_policy 表。
-- 手工 UPDATE 一行会在下一次刷新时被覆盖掉（这个项目已经反复踩过"只改数据、
-- 没改生成规则"的坑）。所以规则必须写进生成函数里。
--
-- 之前的口径为什么是错的（诚实记录）
-- ---------------------------------------------------------------------------
-- 030 之前，模式规则 ③ 把 `birth_year`/`age`/`age_band` **一起**判成 T0 ——
-- 那是照搬用户"编号、年龄等可以是公开"这句话的字面意思。
-- 但"年龄可公开"与"**精确出生年份**可公开"是两件事：
-- 出生年月配合其它信息足以 identify 一个人，而年龄段不会。
-- 现在按用户决策拆开，两者的差异也写进了 note，便于将来复核时看懂为什么这样分。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 刷新 + 对账。
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

    -- ⓪b 主体标识列的披露控制（030）
    ELSIF (r.table_name IN ('person', 'person_pii')
           AND r.column_name IN ('person_id', 'subject_code')) THEN
      v_tier := 'T1'; v_src := 'pattern';
      v_note := '披露控制：**主体标识列的取值会 identify 个体**（实测：per_real_fengtang '
                '这类 id 直接编码姓名）。能读一列 ≠ 可以枚举它的取值 —— '
                '匿名的"数量"仍然公开（走聚合闸门 mt.public_counts()），但"具体编号"需要登录。';

    -- ⓪c 年龄的分级口径（037，用户决策）
    ELSIF r.column_name = 'age_band' THEN
      v_tier := 'T0'; v_src := 'pattern';
      v_note := '年龄段（CT_AGE_BAND）：**统计属性，公开**。用户决策「公开年龄段」。'
                '注意它是写入时快照，基准年见 mt.age_band_asis，会随时间过期。';
    ELSIF r.column_name IN ('birth_year', 'birth_date', 'birth_month', 'age') THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '出生年月 / 精确年龄：**可识别个人信息**，员工及以上。'
                '用户决策「birth_year 保持 T2」—— 与"年龄可公开"不矛盾：'
                '公开的是年龄段（age_band），不是精确出生年份。';

    -- ① 公共标识：编号/主键
    ELSIF r.column_name IN ('person_id','subject_code','job_id','occupation_id','concept_id',
                            'code_table_id','field_id','entity_id','table_name','column_name',
                            'policy_id','tier','code') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '标识/字典键：公开可读（编号类）';

    -- ② 字典、码表、本体与度量定义
    ELSIF r.table_name IN ('code_table','code_value','field_catalog','entity_catalog',
                           'access_tier','access_policy','category_node','concept',
                           'concept_mapping','concept_relation','concept_ancestor',
                           'metric_definition','attribute_definition','occupation',
                           'dimension','schema_migration','age_band_asis',
                           'export_denied') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/本体/定义：公开可读';

    -- ②b 检索索引
    ELSIF r.table_name = 'search_document'
          AND r.column_name IN ('doc_id','title','source_table','source_id','access_tier',
                                'created_at','updated_at','lang','kind') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '检索索引的元数据列：公开';
    ELSIF r.table_name = 'search_document' THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '检索索引的正文列（body/body_tokens/tsv）：可能含原始正文，员工及以上';

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

  -- 字典优先（表级授权对象与披露控制列、年龄口径列不被覆盖）
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
       AND table_name NOT IN ('column_profile')
       AND NOT (table_name IN ('person','person_pii')
                AND column_name IN ('person_id','subject_code'))
       -- 年龄口径由用户决策确定，不被字典的旧值覆盖
       AND column_name NOT IN ('age_band','birth_year','birth_date','birth_month')
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
  '每行的 source 说明依据。含三处**显式口径**：主体标识列披露控制（030）、'
  '年龄段公开 / 出生年 T2（037，用户决策）、column_profile 表级授权（029）。';

SELECT refresh_column_policy();
SELECT apply_column_grants();

-- 硬断言：用户决策的两条必须同时成立
DO $$
DECLARE t_age text; t_birth text;
BEGIN
  SELECT min_tier INTO t_age FROM column_policy
   WHERE table_name='person_demographics' AND column_name='age_band';
  SELECT min_tier INTO t_birth FROM column_policy
   WHERE table_name='person_demographics' AND column_name='birth_year';
  IF t_age IS DISTINCT FROM 'T0' THEN
    RAISE EXCEPTION 'age_band 的等级是 %，应为 T0（用户决策：公开年龄段）', t_age;
  END IF;
  IF access_rank(t_birth) < access_rank('T2') THEN
    RAISE EXCEPTION 'birth_year 的等级是 %，应不低于 T2（用户决策：保持 T2）', t_birth;
  END IF;
  -- 权限层面也要真的生效，不能只是表里写着
  IF NOT has_column_privilege('mt_t0', 'mt.person_demographics', 'age_band', 'SELECT') THEN
    RAISE EXCEPTION '匿名读不到 age_band —— 决策没落到授权上';
  END IF;
  IF has_column_privilege('mt_t0', 'mt.person_demographics', 'birth_year', 'SELECT') THEN
    RAISE EXCEPTION '匿名能读 birth_year —— 决策没落到授权上';
  END IF;
  IF NOT has_column_privilege('mt_t2', 'mt.person_demographics', 'birth_year', 'SELECT') THEN
    RAISE EXCEPTION 'T2 读不到 birth_year —— 收得太紧了';
  END IF;
  RAISE NOTICE '年龄口径自检通过：age_band=T0（匿名可读）、birth_year=%（T2 起可读）', t_birth;
END $$;

COMMIT;
