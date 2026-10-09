-- ============================================================================
-- 医学生才信息库 · 040 补上登录限流两张表的列级策略（修 039 的遗漏）v1.0.0
--
-- 问题（039 的遗漏，被回归当场抓住）
-- ---------------------------------------------------------------------------
-- 039 建了 `login_attempt` / `login_policy` 两张表，并直接 GRANT —— 但**没有**
-- 刷新列级策略。后果有两层：
--   1. `column_policy` 里没有这两张表的行 → 破坏"每张表都有列级策略"这条不变量；
--   2. 一旦 `apply_column_grants()`（自愈对账）跑起来，它会
--      `REVOKE ALL ON ALL TABLES ... FROM mt_t0..t3` 再**按 column_policy 授权** ——
--      这两张表没有策略行 → **一条权限都不给**（看起来"拒绝"了，其实是"没策略"）。
--      于是权限状态取决于"对账有没有在 039 之后跑过"，处于**飘**的状态：
--      完整档里门户/可视化/访问控制三个套件因此在同一处报
--      `permission denied for table login_attempt`。
--
-- 这是本项目第 N 次同类教训：**新增对象必须同时进入策略推导**，
-- 否则它的权限既不是"给"也不是"不给"，而是"看对账跑没跑"。
-- （018/019/020/029/030/038 都是同一类，038 已经把视图那侧改成注册表驱动；
--   这里把新表的策略口径补齐。）
--
-- 两张表的分级口径（要写出来，而不是让它落到"默认 T1"）
-- ---------------------------------------------------------------------------
--   · `login_attempt` → **X**（不通过列级机制授出）
--     它只存"邮箱 + 时间"，但**仍属个人信息**（某人何时被尝试登录）。
--     只有属主/运维读它；门户通过 `v_login_lockout` 看"谁被锁着"，
--     那是聚合后的运维视图，不给流水。
--   · `login_policy` → **T0**（公开）
--     限流参数（失败阈值/窗口/保留期）是**策略元数据**，公开它反而有利于
--     "用户知道规则"；且不含任何个人信息。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 刷新 + 对账 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 在 refresh_column_policy() 里补两条**显式口径**（放在最前面，优先于模式兜底）
-- 做法：用 CREATE OR REPLACE 重定义整个函数（它是权威推导，必须整段替换）。
-- 为避免再次整段复制带来抄写风险，这里用**策略后置覆盖**的方式：
-- 先让原函数跑完，再把这两张表的口径写进去，并让 apply_column_grants 用最终结果。
-- 但"后置覆盖"会被下一次 refresh 抹掉 —— 所以必须进函数体。
-- 结论：仍然整段替换（下面这段与 037 版本一致，仅新增 ⓪d 两条规则）。

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

    -- ⓪b 主体标识列的披露控制（030）
    ELSIF (r.table_name IN ('person', 'person_pii')
           AND r.column_name IN ('person_id', 'subject_code')) THEN
      v_tier := 'T1'; v_src := 'pattern';
      v_note := '披露控制：**主体标识列的取值会 identify 个体**（实测 per_real_fengtang '
                '这类 id 直接编码姓名）。数量仍公开（走聚合闸门 public_counts()），但编号要登录。';

    -- ⓪c 年龄的分级口径（037，用户决策）
    ELSIF r.column_name = 'age_band' THEN
      v_tier := 'T0'; v_src := 'pattern';
      v_note := '年龄段（CT_AGE_BAND）：统计属性，公开。写入时快照，基准年见 mt.age_band_asis。';
    ELSIF r.column_name IN ('birth_year', 'birth_date', 'birth_month', 'age') THEN
      v_tier := 'T2'; v_src := 'pattern';
      v_note := '出生年月 / 精确年龄：可识别个人信息，员工及以上。公开的是年龄段而不是出生年份。';

    -- ⓪d 登录限流的两张表（040 新增口径）
    ELSIF r.table_name = 'login_attempt' THEN
      v_tier := 'X'; v_src := 'pattern';
      v_note := '登录尝试流水（邮箱+时间）：虽不存口令/IP，但仍属个人信息 —— '
                '**不通过列级机制授出**，仅属主/运维可读。'
                '门户用 v_login_lockout（聚合）看"谁被锁着"，看不到流水本身。';
    ELSIF r.table_name = 'login_policy' THEN
      v_tier := 'T0'; v_src := 'pattern';
      v_note := '登录限流参数（阈值/窗口/保留期）：**策略元数据，公开** —— '
                '让用户知道规则，且不含个人信息。';

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
                           'export_denied','public_view_registry') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/本体/定义/策略元数据：公开可读';

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

  -- 字典优先（显式口径列与表级授权对象不被覆盖）
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
       AND NOT (table_name IN ('person','person_pii')
                AND column_name IN ('person_id','subject_code'))
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

SELECT refresh_column_policy();
SELECT apply_column_grants();

-- 硬断言
DO $$
DECLARE n_gap int; n_att int;
BEGIN
  -- ① 策略覆盖率：mt 下每张基础表都必须有策略行（这是 039 打破的不变量）
  SELECT count(*) INTO n_gap FROM (
    SELECT c.table_name FROM information_schema.tables c
     WHERE c.table_schema='mt' AND c.table_type='BASE TABLE'
       AND NOT EXISTS (SELECT 1 FROM column_policy cp WHERE cp.table_name = c.table_name)) x;
  IF n_gap > 0 THEN
    RAISE EXCEPTION '有 % 张表没有列级策略 —— 新增对象必须同时进入策略推导', n_gap;
  END IF;

  -- ② login_attempt 仍然对应用角色关闭（X 口径生效）
  --    ⚠ 必须查**列级**权限：本项目是列级授权（column_policy → GRANT SELECT(col)），
  --    `has_table_privilege(...)` 查的是表级权限，列给了它也仍然是 false。
  --    第一版就是用它查 login_policy 而误判成"T0 口径没生效"（实测踩到）。
  IF has_column_privilege('mt_t3', 'mt.login_attempt', 'email', 'SELECT')
     OR has_column_privilege('mt_t3', 'mt.login_attempt', 'at', 'SELECT') THEN
    RAISE EXCEPTION 'mt_t3 能读 login_attempt 的列 —— X 口径没生效';
  END IF;
  IF has_column_privilege('mt_t0', 'mt.login_attempt', 'email', 'SELECT') THEN
    RAISE EXCEPTION '匿名能读 login_attempt 的列 —— X 口径没生效';
  END IF;

  -- ③ login_policy 公开（T0 口径生效）—— 同样查列级
  IF NOT has_column_privilege('mt_t0', 'mt.login_policy', 'max_fail', 'SELECT')
     OR NOT has_column_privilege('mt_t0', 'mt.login_policy', 'window_minutes', 'SELECT') THEN
    RAISE EXCEPTION '匿名读不到 login_policy 的列 —— T0 口径没生效';
  END IF;

  -- ④ 但门户仍然能读"谁被锁着"的聚合视图（否则登录页没法提示）
  IF NOT has_table_privilege('mt_portal', 'mt.v_login_lockout', 'SELECT') THEN
    RAISE EXCEPTION '门户读不到 v_login_lockout';
  END IF;
  RAISE NOTICE '登录限流表的口径自检通过：策略覆盖无缺口、流水不可读、策略参数公开';
END $$;

COMMIT;
