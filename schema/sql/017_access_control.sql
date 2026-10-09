-- ============================================================================
-- 医学生人才信息库 · 017 字段级访问控制（把"标签"变成"拦截"）v1.0.0
--
-- 为什么要有这个迁移（问题陈述）
-- ---------------------------------------------------------------------------
-- docs/06 与 README 早就写了自我批评：**访问分级目前"是标签不是拦截"**。
-- 实测确认了这句话：
--   · mt.access_policy 有 10 条真实策略（字段 × 动作 × 最低层级，X = 禁止）
--   · mt.access_log 表建好了，但 **0 行** —— 从来没记过谁读过什么
--   · RLS 启用的表：**0 张**
--   · mt.field_catalog.access_tier 有 T0/T1/T2/T3 四级（T0=53 T1=105 T2=13 T3=5）
-- 也就是说：分级模型设计得很完整，但**没有任何一处会拒绝访问**。
--
-- 本迁移做的四件事
-- ---------------------------------------------------------------------------
-- 【1】把"等级"变成真正的数据库角色（能力阶梯）
--      T0 公开 < T1 注册用户 < T2 员工 < T3 管理员；X = 禁止。
--      注意：这不是"给角色起个名字"，而是**列级 GRANT 的载体**。
--
-- 【2】把"每个字段权限不同"变成列级授权（关键机制）
--      新增 mt.column_policy（表.列 → 最低层级），再从 `field_catalog.access_tier`
--      与 `access_policy.view_inline.min_tier` 推导；推不出来的按**列名模式**兜底，
--      并且**每一行都记 source**，让你一眼看出哪些是字典依据、哪些是约定。
--      然后 mt.apply_column_grants() 把它编译成 `GRANT SELECT (列) ON 表 TO 角色`。
--
--      为什么用列级 GRANT 而不是只在应用层过滤（本机的实测结论，见 ops/fixtures/_lab_access.py）：
--        · 只授权部分列时，`SELECT *` **直接被数据库拒绝** —— 应用层写错也漏不出去
--        · 读未授权列 **被拒绝**；`count(未授权列)` 也被拒绝
--        · 但 `count(*)`（不引用任何列）**仍然可用** —— 所以"数量公开、字段受限"能同时成立，
--          正好满足需求里的"编号、年龄公开，其他字段限权"
--      这是"能被证明的受限"，而不是"相信应用层会过滤"。
--
-- 【3】会话身份与访问日志
--      mt.session_actor() / mt.session_tier() 从会话变量取身份；
--      mt.log_access() 把"谁、以什么角色、对什么对象、做了什么、读到多少行"写进 access_log。
--      用 `SET LOCAL` 传身份，事务结束自动失效 —— 连接池复用连接时不会串身份。
--
-- 【4】一个必须说清楚的架构前提（实测）
--      **超级用户读 RLS 表会绕过策略，连 `FORCE ROW LEVEL SECURITY` 都拦不住**
--      （实测：postgres 读带 RLS 的表仍然看到全部行）。
--      所以门户现在"用 postgres 连接"这件事，会让任何权限设计都变成装饰。
--      本迁移因此建了专用登录角色 mt_portal（非超级、非 bypassrls），
--      它自己**没有任何表权限**，必须先 `SET ROLE mt_tN` 才能读到东西 ——
--      "忘了设等级就读不到任何数据"，这个默认方向才是安全的。
--
-- 可重放：所有 CREATE 都带 IF NOT EXISTS / DROP ... IF EXISTS；
--         角色创建用 DO 块判断存在性（CREATE ROLE 没有 IF NOT EXISTS）。
-- 不修改 001-016；不删任何既有列。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 等级阶梯：把"等级"变成有顺序、有名字、有说明的一等实体
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_tier (
  tier        text PRIMARY KEY,
  rank        integer NOT NULL UNIQUE,     -- 数值越大权限越高；比较用 rank，不用字符串
  title_zh    text NOT NULL,
  title_en    text,
  audience    text,                        -- 这个等级对应"谁"
  description text NOT NULL,
  is_public   boolean NOT NULL DEFAULT false
);
COMMENT ON TABLE access_tier IS
  '访问等级阶梯。比较权限一律用 rank，不要用 tier 字符串直接比大小（T10 会小于 T2）。';

INSERT INTO access_tier (tier, rank, title_zh, title_en, audience, description, is_public) VALUES
  ('T0', 0, '公开',   'Public',       '任何人（含未登录）',
   '可公开的标识与统计信息：编号、年龄、字典与代码表、表结构本身。不含任何个人联系或健康信息。', true),
  ('T1', 1, '注册用户', 'Registered', '已在小程序完成注册并同意数据使用的人',
   '默认等级：普通档案字段（教育、经历、能力、产出）。个人联系与健康信息不在此级。', false),
  ('T2', 2, '员工',   'Staff',        '本机构工作人员（运营、招聘顾问）',
   '内部运营需要的字段：人口学、户籍、审计流水、访问日志本体。', false),
  ('T3', 3, '管理员', 'Admin',        '数据管理者与合规负责人',
   '可识别个人身份的字段（姓名、证件哈希、联系方式）与健康受限项。每一次读取都必须留痕。', false),
  ('X',  99, '禁止',  'Denied',       '没有人（保留给合规红线）',
   '任何角色都不可读。用于"即使管理员也不该导出"的场景（例如原始 JD 原文引用、禁止导出的字段）。', false)
ON CONFLICT (tier) DO UPDATE
   SET rank = EXCLUDED.rank, title_zh = EXCLUDED.title_zh, title_en = EXCLUDED.title_en,
       audience = EXCLUDED.audience, description = EXCLUDED.description,
       is_public = EXCLUDED.is_public;

-- ---------------------------------------------------------------------------
-- 2. 角色：一个"什么都不能做"的登录角色 + 四个等级组角色
--    mt_portal 自己没有任何表权限；它必须先 SET ROLE mt_tN。
--    "忘了设等级 ⇒ 读不到任何数据" 是刻意选的默认方向。
-- ---------------------------------------------------------------------------
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
    -- 组角色不继承任何东西，也不许建对象
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', r);
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mt_portal') THEN
    CREATE ROLE mt_portal LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
  -- mt_portal 只能"切换到"四个等级角色；它本身不拥有任何权限
  FOREACH r IN ARRAY ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'] LOOP
    EXECUTE format('GRANT %I TO mt_portal', r);
  END LOOP;
  EXECUTE 'REVOKE ALL ON SCHEMA mt FROM mt_portal';
  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_t0, mt_t1, mt_t2, mt_t3, mt_portal';
END $$;

COMMENT ON ROLE mt_portal IS
  '只读门户（:8082）的登录角色。它自身没有任何表权限，必须 SET LOCAL ROLE mt_tN 才能读；'
  '这样"忘记设等级"的结果是读不到数据，而不是读到全部数据。';

-- ---------------------------------------------------------------------------
-- 3. column_policy：表.列 → 最低可读层级
--    source 列是刻意留的：它区分"字典依据"与"列名约定"，让覆盖率的成色可见。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS column_policy (
  table_name  text NOT NULL,
  column_name text NOT NULL,
  min_tier    text NOT NULL REFERENCES access_tier(tier),
  source      text NOT NULL,          -- field_catalog / access_policy / pattern / table_default
  note        text,
  updated_at  timestamp with time zone NOT NULL DEFAULT now(),
  PRIMARY KEY (table_name, column_name)
);
COMMENT ON TABLE column_policy IS
  '字段级访问策略：每一列的最低可读层级。由 mt.refresh_column_policy() 从字典与策略推导，'
  '推不出来的按列名模式兜底（source=pattern），全部可见、可审。';

-- 刷新函数：把"字典里的等级"翻译到"物理列"
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

    -- ② 字典与代码表本身是"用来被读的"，公开
    ELSIF r.table_name IN ('code_table','code_value','field_catalog','entity_catalog',
                           'access_tier','access_policy','category_node','concept',
                           'concept_mapping','metric_definition','schema_migration',
                           'attribute_definition','occupation','dimension') THEN
      v_tier := 'T0'; v_src := 'pattern'; v_note := '语义层/字典/元数据：公开可读';

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

    -- ⑥ 审计与访问日志本体
    ELSIF r.table_name IN ('change_log','access_log','tombstone','dataset_release') THEN
      v_tier := 'T2'; v_src := 'pattern'; v_note := '审计/发布记录：员工及以上';
    END IF;

    INSERT INTO column_policy (table_name, column_name, min_tier, source, note, updated_at)
    VALUES (r.table_name, r.column_name, v_tier, v_src, v_note, now())
    ON CONFLICT (table_name, column_name) DO UPDATE
       SET min_tier = EXCLUDED.min_tier, source = EXCLUDED.source,
           note = EXCLUDED.note, updated_at = now();
  END LOOP;

  -- 3.2 用字典里的字段等级**覆盖**兜底值（字典依据优先于列名约定）
  --     field_id 的约定形态是 F_<实体缩写>_<列名>，去掉前缀后与列名比对。
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
       -- 字典不得把列**放宽**到比兜底更公开？不：字典是权威依据，允许调宽也允许调严。
       -- 但绝不能把已经是 T3 的敏感列放宽成 T0 —— 这类冲突要留下痕迹而不是静默生效。
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
       -- 只允许调严，不允许人工策略**放宽**已经判定为敏感的列
       AND access_rank(r.min_tier) >= access_rank(cp.min_tier);
  END LOOP;

  RETURN QUERY
    SELECT cp.min_tier, count(*)::int FROM column_policy cp GROUP BY 1 ORDER BY 1;
END $$;

COMMENT ON FUNCTION refresh_column_policy() IS
  '从 field_catalog / access_policy 推导列级策略，推不出来的按列名模式兜底；'
  '每一行的 source 说明它的依据。可反复执行（幂等）。';

-- ---------------------------------------------------------------------------
-- 4. 等级比较：用 rank 而不是字符串
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION access_rank(p_tier text) RETURNS integer
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT COALESCE((SELECT rank FROM access_tier WHERE tier = p_tier), -1);
$$;
COMMENT ON FUNCTION access_rank(text) IS
  '等级 → 序数。未知等级返回 -1（比 T0 还低），这样"写错的等级"是拒绝而不是放行。';

CREATE OR REPLACE FUNCTION tier_allows(p_have text, p_need text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT access_rank(p_have) >= access_rank(p_need) AND access_rank(p_need) >= 0;
$$;

-- ---------------------------------------------------------------------------
-- 5. 把策略编译成列级授权（这一句是"拦截"真正生效的地方）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_column_grants() RETURNS TABLE (
  role_name text, grants integer
) LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  t      RECORD;
  r      RECORD;
  n      integer;
BEGIN
  -- 5.1 先全部收回：授权必须是"从策略重新算出来的"，不能在旧授权上累加。
  --     否则删掉一条策略后权限仍在（这类"只加不减"的配置错误很常见）。
  FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
    FOR t IN SELECT table_name FROM column_policy GROUP BY 1 LOOP
      EXECUTE format('REVOKE ALL ON mt.%I FROM %I', t.table_name, r.role_name);
    END LOOP;
    -- 同时确保这四个角色拿不到别的东西
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA mt FROM %I', r.role_name);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA mt FROM %I', r.role_name);
  END LOOP;

  -- 5.2 按列授权：tier 为 Tn 的列，授予 mt_tn 及**所有更高等级**
  --     （阶梯的含义：T2 能看 T0/T1/T2 的列）
  FOR r IN SELECT unnest(ARRAY['T0','T1','T2','T3']) AS tier LOOP
    FOR t IN
      SELECT cp.table_name,
             string_agg(format('%I', cp.column_name), ', ' ORDER BY cp.column_name) AS cols,
             count(*) AS ncol
        FROM column_policy cp
       WHERE access_rank(cp.min_tier) <= access_rank(r.tier)
         AND cp.min_tier <> 'X'
       GROUP BY cp.table_name
    LOOP
      EXECUTE format('GRANT SELECT (%s) ON mt.%I TO mt_%s',
                     t.cols, t.table_name, lower(r.tier));
    END LOOP;
  END LOOP;

  -- 5.3 X（禁止）单独再收一次，防止上面某一步的排序意外放行
  FOR t IN SELECT table_name, column_name FROM column_policy WHERE min_tier = 'X' LOOP
    FOR r IN SELECT unnest(ARRAY['mt_t0','mt_t1','mt_t2','mt_t3']) AS role_name LOOP
      EXECUTE format('REVOKE SELECT (%I) ON mt.%I FROM %I',
                     t.column_name, t.table_name, r.role_name);
    END LOOP;
  END LOOP;

  -- 5.4 视图**不在此处批量授权**。原因是一条真实的安全性质：
  --     视图默认以**视图属主**的权限执行（PG 15 起才有 security_invoker 选项，
  --     本机是 16，可用但默认仍为 false）。批量 `GRANT SELECT ON ALL TABLES`
  --     会把视图一并授出，于是"视图属主能看到的列"就成了绕过列级策略的后门。
  --     视图的授权必须逐个评审（要么把视图建在等级角色名下并开 security_invoker，
  --     要么只把视图授给 T2/T3 并在视图里显式裁剪列）。
  --     ⚠ 第一版这里写的是 `GRANT ALL TABLES` 再 `REVOKE` 每张表 —— 那个 REVOKE
  --       把上面 5.2 刚授好的**列级权限全清了**（实测 mt_t1 的列权限从 942 掉到 180），
  --       被 ops/tests/access_test.py 的"阶梯严格递增"断言抓住。

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
  '把 column_policy 编译成列级 GRANT。每次先全部 REVOKE 再按策略重授 —— '
  '"只加不减"会让被废止的策略继续生效。执行后可用 ops/tests/access_test.py 验证。';

-- ---------------------------------------------------------------------------
-- 6. 会话身份与访问日志
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION session_actor() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(nullif(current_setting('mt.actor', true), ''), 'anonymous');
$$;

CREATE OR REPLACE FUNCTION session_tier() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(nullif(current_setting('mt.tier', true), ''), 'T0');
$$;

-- 当前会话**实际能读**到的最高等级：取"声明的等级"与"角色实际持有的权限"中较低者。
-- 为什么要这个：只信声明就是把安全建立在"应用不会说谎"上。
-- 记录日志时用它，才能看出"声明的等级"和"实际生效的等级"是否一致。
CREATE OR REPLACE FUNCTION effective_tier() RETURNS text
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT COALESCE(
    (SELECT t.tier FROM access_tier t
      WHERE t.tier <> 'X' AND pg_has_role(current_user, 'mt_' || lower(t.tier), 'USAGE')
      ORDER BY t.rank DESC LIMIT 1), 'T0');
$$;

CREATE OR REPLACE FUNCTION log_access(
  p_action     text,
  p_target     text,
  p_tier       text DEFAULT NULL,
  p_row_count  integer DEFAULT NULL,
  p_purpose    text DEFAULT NULL,
  p_detail     jsonb DEFAULT '{}'::jsonb
) RETURNS bigint LANGUAGE sql SET search_path = mt, public AS $$
  INSERT INTO access_log (actor, actor_role, action, target, access_tier,
                          row_count, purpose, detail)
  VALUES (session_actor(), current_user, p_action, p_target,
          COALESCE(p_tier, session_tier()), p_row_count, p_purpose, p_detail)
  RETURNING access_id;
$$;

COMMENT ON FUNCTION log_access(text, text, text, integer, text, jsonb) IS
  '写一条访问日志。actor 取自会话变量 mt.actor，actor_role 取 current_user（实际生效的角色）——'
  '两者都记，才能发现"声明的身份"与"实际权限"不一致的情况。';

-- 让 mt_t0..mt_t3 能调用日志函数（它们是 SECURITY INVOKER，需要 INSERT 权限）
GRANT INSERT ON access_log TO mt_t0, mt_t1, mt_t2, mt_t3;
GRANT USAGE ON SEQUENCE access_log_access_id_seq TO mt_t0, mt_t1, mt_t2, mt_t3;
GRANT SELECT ON access_tier, column_policy TO mt_t0, mt_t1, mt_t2, mt_t3, mt_portal;

-- ---------------------------------------------------------------------------
-- 7. 覆盖视图：让人一眼看出策略的成色（多少有字典依据、多少是列名约定）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_column_policy_coverage AS
SELECT cp.table_name,
       count(*) AS n_columns,
       count(*) FILTER (WHERE cp.min_tier = 'T0') AS t0,
       count(*) FILTER (WHERE cp.min_tier = 'T1') AS t1,
       count(*) FILTER (WHERE cp.min_tier = 'T2') AS t2,
       count(*) FILTER (WHERE cp.min_tier = 'T3') AS t3,
       count(*) FILTER (WHERE cp.min_tier = 'X')  AS denied,
       count(*) FILTER (WHERE cp.source = 'field_catalog')  AS by_field_catalog,
       count(*) FILTER (WHERE cp.source = 'access_policy')  AS by_access_policy,
       count(*) FILTER (WHERE cp.source = 'pattern')        AS by_pattern,
       count(*) FILTER (WHERE cp.source = 'table_default')  AS by_default
  FROM column_policy cp GROUP BY cp.table_name;
COMMENT ON VIEW v_column_policy_coverage IS
  '每个表的列级策略分布 + 依据来源分布。by_pattern/by_default 占比高说明"约定"多于"字典"，'
  '那是要补字典的信号，不是可以忽略的细节。';

GRANT SELECT ON v_column_policy_coverage TO mt_t0, mt_t1, mt_t2, mt_t3, mt_portal;

-- 初次生成策略与授权
SELECT refresh_column_policy();
SELECT apply_column_grants();

COMMIT;
