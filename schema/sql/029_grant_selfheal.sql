-- ============================================================================
-- 医学生人才信息库 · 029 授权对账必须自愈：策略没覆盖的列不能让授权静默消失 v1.0.0
--
-- 症状（完整档回归实测，不是推理）
-- ---------------------------------------------------------------------------
-- 单独跑 ops/tests/portal_test.py：172/172 通过。
-- 在 run_all 完整档里跑到第 13 套：159/172；第 15 套直接
--   `permission denied for table column_profile`。
-- 实测根因：
--     column_profile 有 18 列，而 mt.column_policy 里**一行都没有**，
--     mt_t3 对它有 **0 个授权**。
-- 机制：
--     `apply_column_grants()` 的设计是"先 REVOKE 全部，再**只按 column_policy** 重授"。
--     这个方向本身是对的（只加不减会留下已废止的权限），但它有个隐含前提：
--     **策略必须覆盖每一列**。迁移 027 新建了 column_profile 却忘了刷新策略，
--     于是此后任何一次对账（`ops/tests/access_test.py` 的 A6 反面用例就会调它）
--     都会把这张表的授权**永久剥光** —— 而第一次跑的时候它还好好地，
--     所以表现为"单独跑通过、序列里失败"，极难定位。
--
-- 这是同一类缺陷的第四次（018 列权限 942→180、019 序列权限、020 审计写入），
-- 所以这次不只补一行，而是把前提本身变成机制的保证。
--
-- 三处修改
-- ---------------------------------------------------------------------------
-- 【1】apply_column_grants() 自愈：对账前先确认策略覆盖，缺了就 refresh。
--      "重建全部授权"的函数必须能保证它重建所依赖的那份数据是完整的 ——
--      否则它的正确性依赖于"没人往库里加过新表"，而那是最不可靠的假设。
--
-- 【2】column_profile 走**表级**授权，不参与列级机制 → 策略里记为 X。
--      为什么：它是列级剖析快照（含 Top-K 取值），028 定的规矩是"只给 T3"。
--      而列级机制是按列的等级授 SELECT 的通用机制，表达不了"整表只给一个等级"。
--      X 的含义在这里是"**不通过列级机制授出**"（apply_column_grants 的 5.3 会把它收掉），
--      而不是"谁也读不到"——它由 apply_role_baseline 按表级决定单独授予 T3。
--      这个语义必须写在 note 里，否则下一个人会以为 X 就是"禁止"。
--
-- 【3】apply_role_baseline() 补上表级授权的重建。
--      与视图同一道理：**凡是不由 column_policy 推导出来的授权，都必须在这里重建**，
--      否则 5.1 的"收回全部"会把它一起收掉。
--
-- 可重放：CREATE OR REPLACE + 幂等刷新。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 【2】策略生成：column_profile 记为 X（表级授权，不参与列级机制）
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

    -- ⓪ 表级授权对象：**不走列级机制**（见本迁移开头）
    IF r.table_name = 'column_profile' THEN
      v_tier := 'X'; v_src := 'pattern';
      v_note := '表级授权：列级剖析快照含 Top-K 取值，只给 T3（由 apply_role_baseline 授予）。'
                'X 在这里表示"不通过列级机制授出"，不是"谁也读不到"。';

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
                           'dimension','schema_migration') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/本体/定义：公开可读';

    -- ②b 检索索引：只公开元数据列
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

  -- 字典优先
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
       AND table_name <> 'column_profile'          -- 表级授权对象不被字典覆盖
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
-- 【1】自愈：对账前先确认策略覆盖了每一列
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_column_grants() RETURNS TABLE (
  role_name text, grants integer
) LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  t RECORD;
  r RECORD;
  n_missing integer;
  n_refreshed integer := 0;
BEGIN
  -- 0. **自愈**：策略没覆盖的列会让 5.1 的"收回全部"把它们永久剥光
  --    （而不是"保持原样"）。这是本轮实测到的真实事故：column_profile 18 列
  --    不在策略里，于是每次对账都清掉它的授权，表现为"单独跑通过、序列里失败"。
  --    所以对账的第一步是**保证它所依赖的策略是完整的**，而不是相信它是完整的。
  SELECT count(*) INTO n_missing
    FROM information_schema.columns c
    JOIN pg_class cl ON cl.relname = c.table_name
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = 'mt'
   WHERE c.table_schema = 'mt' AND cl.relkind = 'r'
     AND NOT EXISTS (SELECT 1 FROM column_policy cp
                      WHERE cp.table_name = c.table_name
                        AND cp.column_name = c.column_name);
  IF n_missing > 0 THEN
    RAISE NOTICE '策略缺少 % 列的覆盖，先刷新策略再对账（否则这些列会被静默剥光）', n_missing;
    SELECT count(*) INTO n_refreshed FROM refresh_column_policy();
  END IF;

  -- 5.1 收回
  FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA mt FROM %I', r.role_name);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA mt FROM %I', r.role_name);
  END LOOP;

  -- 5.2 按列授权（X 永不授出）
  FOR r IN SELECT unnest(ARRAY['T0','T1','T2','T3']) AS tier LOOP
    FOR t IN
      SELECT cp.table_name,
             string_agg(format('%I', cp.column_name), ', ' ORDER BY cp.column_name) AS cols
        FROM column_policy cp
       WHERE access_rank(cp.min_tier) <= access_rank(r.tier) AND cp.min_tier <> 'X'
       GROUP BY cp.table_name
    LOOP
      EXECUTE format('GRANT SELECT (%s) ON mt.%I TO mt_%s',
                     t.cols, t.table_name, lower(r.tier));
    END LOOP;
  END LOOP;

  -- 5.3 X 再收一次
  FOR t IN SELECT table_name, column_name FROM column_policy WHERE min_tier = 'X' LOOP
    FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
      EXECUTE format('REVOKE SELECT (%I) ON mt.%I FROM %I',
                     t.column_name, t.table_name, r.role_name);
    END LOOP;
  END LOOP;

  -- 5.4 补齐基线（含**表级**授权对象，见 029 开头）
  PERFORM apply_role_baseline();

  RETURN QUERY
    SELECT x.role_name, x.n::int
      FROM (SELECT 'mt_t0' AS role_name, count(*) AS n FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 0
            UNION ALL SELECT 'mt_t1', count(*) FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 1
            UNION ALL SELECT 'mt_t2', count(*) FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 2
            UNION ALL SELECT 'mt_t3', count(*) FROM column_policy
             WHERE min_tier <> 'X' AND access_rank(min_tier) <= 3) x;
END $$;

COMMENT ON FUNCTION apply_column_grants() IS
  '唯一授权对账入口：**自愈策略覆盖** → 收回 → 按 column_policy 重建列权限 → 补齐角色基线。'
  '自愈那一步是必须的：策略没覆盖的列会被"收回全部"永久剥光，而不是保持原样。';

-- ---------------------------------------------------------------------------
-- 【3】基线里补上表级授权对象
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    -- 等级角色不能建会话、不能登录、不能创建账号
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_session_new(text,text,text,interval,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_user_add(text,text,text,text) FROM %I', r);
    -- **表级**授权对象：列级机制表达不了"整表只给一个等级"，
    -- 所以它们在这里重建（否则 5.1 的收回全部会连带清掉）
    EXECUTE format('REVOKE ALL ON mt.column_profile FROM %I', r);
    EXECUTE format('GRANT SELECT ON mt.v_column_profile_coverage, '
                   'mt.v_column_profile_public, mt.v_profile_exposure TO %I', r);
  END LOOP;

  -- 列级剖析快照：只给最高等级（含 Top-K 取值）
  EXECUTE 'REVOKE ALL ON mt.column_profile FROM mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT SELECT ON mt.column_profile TO mt_t3';

  -- 视图：只给 T3（视图以属主权限执行，会绕过列级策略；T3 本就拥有全部列）
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
    EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
  END LOOP;
  -- 上面那条统一收口之后，把"本来就该对全部等级开放"的三个剖析元数据视图再授回来
  EXECUTE 'GRANT SELECT ON mt.v_column_profile_coverage, mt.v_column_profile_public, '
          'mt.v_profile_exposure, mt.v_access_baseline_check, mt.v_session_function_grants '
          'TO mt_t0, mt_t1, mt_t2';

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_lookup(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_revoke(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_login(text, text, interval, text) TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants, '
          'mt.v_column_profile_coverage TO mt_portal';
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限：模式使用 / 审计函数执行权 / 读策略 / 视图只给 T3 / 会话函数最小授权 / '
  '**表级授权对象**（column_profile 只给 T3）。'
  '凡是"不由 column_policy 推导出来"的授权都必须在这里重建 —— 否则"收回全部"会清掉它。';

SELECT refresh_column_policy();
SELECT apply_column_grants();

-- 硬断言：对账之后，表级授权必须还在（这正是本轮那个 bug 的回归守卫）
DO $$
BEGIN
  IF NOT has_table_privilege('mt_t3', 'mt.column_profile', 'SELECT') THEN
    RAISE EXCEPTION '对账后 T3 读不到 column_profile —— 表级授权没被重建';
  END IF;
  IF has_table_privilege('mt_t0', 'mt.column_profile', 'SELECT') THEN
    RAISE EXCEPTION '对账后 T0 能读 column_profile —— 列级快照不该对公开等级开放';
  END IF;
  RAISE NOTICE '表级授权自检通过：column_profile 只对 T3 可读，且对账不会丢';
END $$;

COMMIT;
