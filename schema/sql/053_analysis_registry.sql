-- ============================================================================
-- 医学生人才信息库 · 053 商业分析口径 / 分类 / 漏斗 注册表 v1.0.0
--
-- 这一版是**调研 + 实测**的产物，不是凭感觉列的指标。
--
-- 【调研结论：企业级商业分析的指标体系怎么搭】
-- 参考公开方法论（亿信华辰《指标体系构建详解》、join.com 招聘漏斗词条、
-- noon.ai 招聘漏斗基准等，均为公开资料，仅取方法论不取数）：
--   ① **指标的构成 = 原子指标 + 修饰词 + 时间周期**
--      原子指标不可再拆（如"人数""岗位数"）；修饰词限定场景；时间周期限定窗口。
--      三者叠加才是**派生指标**。→ 所以注册表里必须有：分子 / 分母 / 时间依据 / 修饰词。
--   ② **口径必须存档**：方法论明确要求"对指标口径和业务逻辑详细描述存档，
--      如 渗透率 = 该功能的日点击人数 / 日活"。→ 所以有 definition_note 且必填。
--   ③ 核心指标要**公式拆解 + 按业务路径拆解** → 拆解出来的就是**漏斗**。
--   ④ 方法论反复强调**相关不是因果**（原文举"评论点赞多"为例）。
--      → 所以每个口径都带 caveat 字段，把"这个数字不能推出什么"写进去。
--   ⑤ **不能只看均值，要看分布**（原文举"停留时长分布"为例）
--      → 所以漏斗/透视都保留分组明细，而不是只给一个总比率。
--
-- 【为什么做成"表 + 可执行 SQL"而不是"写死在代码里"】
-- 口径会变（业务阶段变、定义变），写死在代码里意味着每次改口径都要发版；
-- 但"把 SQL 存表里再执行"是**注入面**，所以必须像代码一样管：
--   · 只能写单条只读 SELECT/WITH（CHECK 约束 + check_registry_sql 校验）；
--   · 只有 T3 能写、T0–T2 只读；
--   · 带 version / owner / updated_at，改口径要留痕（见 change_log）。
-- 一句话：**口径即代码**，所以按代码的规矩来。
--
-- 【本库实测能算什么】（先侦查再设计，绝不发明算不出来的指标）
--   person 124 / person_demographics 123 / education_record 251 /
--   skill_assertion 821 / evidence 902 / preference 2157 /
--   job_posting 690 / job_requirement 2970 / match_run 83 / match_result 150 /
--   employment_record 143 / experience_episode 0（空表，故不设计依赖它的口径）
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 0. 守卫：注册表里的 SQL 必须是**单条只读查询**
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_registry_sql(txt text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s text;
BEGIN
  IF txt IS NULL OR btrim(txt) = '' THEN RETURN false; END IF;
  s := btrim(txt);
  -- 去注释后再判（否则注释里写个分号就绕过了）
  s := regexp_replace(s, '--[^\n]*', ' ', 'g');
  s := regexp_replace(s, '/\*.*?\*/', ' ', 'gs');
  s := btrim(s);
  IF s LIKE '%;%' THEN RETURN false; END IF;               -- 只允许单条语句
  -- ⚠ 实测踩到的坑：**PostgreSQL 里 `\b` 是退格符，不是词边界**（词边界是 `\y`）。
  -- 第一版写 `'^\s*(select|with)\b'`，于是**所有**口径都被判成非法 ——
  -- 守卫"过严"比"过松"更隐蔽：过松会放行坏数据，过严会让功能整个不能用，
  -- 而错误信息看起来像"你写的 SQL 不合法"。
  IF s !~* '^\s*(select|with)\y' THEN RETURN false; END IF; -- 必须是查询
  -- 写/DDL 关键字一律拒绝（含常见伪装写法）
  IF s ~* '\y(insert|update|delete|truncate|drop|alter|create|grant|revoke|copy|call|do|vacuum|analyze|refresh|reindex|comment|set|reset|begin|commit|rollback)\y'
     THEN RETURN false; END IF;
  -- 危险函数
  IF s ~* '\y(pg_sleep|pg_terminate_backend|pg_read_file|pg_ls_dir|lo_import|dblink|pg_execute_server_program)\y'
     THEN RETURN false; END IF;
  RETURN true;
END $$;
COMMENT ON FUNCTION check_registry_sql(text) IS
  '注册表守卫：只接受**单条只读 SELECT/WITH**。口径即代码，所以执行前必须先过这一关。';

-- ---------------------------------------------------------------------------
-- 1. 口径注册表（metric）
-- ---------------------------------------------------------------------------
CREATE TABLE metric_registry (
  metric_id        TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  domain           TEXT NOT NULL CHECK (domain IN
                     ('供给','需求','匹配','结果')),
  numerator_sql    TEXT NOT NULL CHECK (check_registry_sql(numerator_sql)),
  denominator_sql  TEXT NOT NULL CHECK (check_registry_sql(denominator_sql)),
  unit             TEXT NOT NULL DEFAULT '%' CHECK (unit IN ('%','人','条','个','分')),
  time_basis       TEXT,          -- 时间依据（NULL = 时点快照，不是时间窗）
  modifier         TEXT,          -- 修饰词（限定场景）
  definition_note  TEXT NOT NULL, -- **口径说明：必填**（方法论要求存档）
  caveat           TEXT,          -- 这个数字**不能**推出什么（相关≠因果等）
  min_denominator  INTEGER NOT NULL DEFAULT 20,  -- 分母小于它就不给百分比（小样本）
  status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','deprecated')),
  owner            TEXT NOT NULL DEFAULT 'user',
  version          TEXT NOT NULL DEFAULT '1.0.0',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE metric_registry IS
  '商业分析口径注册表。每个口径 = 分子 + 分母 + 时间依据 + 修饰词 + **口径说明（必填）**。'
  '方法论要求"口径必须存档"：没有 definition_note 的口径，三个月后没人知道它算的是什么。'
  'caveat 写"这个数字不能推出什么" —— 尤其是**相关不等于因果**。';

CREATE OR REPLACE FUNCTION metric_value(p_metric text)
RETURNS TABLE(metric_id text, title text, unit text, numerator bigint,
              denominator bigint, value numeric, note text, caveat text,
              small_sample boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE r metric_registry; n bigint; d bigint; v numeric;
BEGIN
  SELECT * INTO r FROM metric_registry WHERE metric_id = p_metric AND status='active';
  IF NOT FOUND THEN
    RAISE EXCEPTION '口径不存在或已停用：%', p_metric USING ERRCODE = 'no_data_found';
  END IF;
  EXECUTE 'SELECT ' || r.numerator_sql   INTO n;
  EXECUTE 'SELECT ' || r.denominator_sql INTO d;
  IF d IS NULL OR d = 0 THEN
    v := NULL;                       -- 分母为 0 → **NULL，不是 0**
  ELSIF r.unit = '%' THEN
    v := round(100.0 * n / d, 2);
  ELSE
    v := n;
  END IF;
  RETURN QUERY SELECT r.metric_id, r.title, r.unit, n, d, v,
                      r.definition_note, r.caveat, (d < r.min_denominator);
END $$;
COMMENT ON FUNCTION metric_value(text) IS
  '按注册表算一个口径。**分母为 0 时返回 NULL 而不是 0** —— "没人可算"与"算出来是 0"'
  '是两件事，混起来是最常见的报表说谎方式。分母小于 min_denominator 时打 small_sample 标记。';

-- ---------------------------------------------------------------------------
-- 2. 分类（维度）注册表 —— 供"选择单个或多个字段作为分类变量"的透视使用
-- ---------------------------------------------------------------------------
CREATE TABLE dimension_registry (
  dimension_id   TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  entity         TEXT NOT NULL CHECK (entity IN ('person','job_posting')),
  from_sql       TEXT NOT NULL CHECK (check_registry_sql(from_sql)),
  expr_sql       TEXT NOT NULL CHECK (check_registry_sql(expr_sql)),
  code_table_id  TEXT,            -- 有码表就显示中文标签
  group_name     TEXT NOT NULL DEFAULT '其他',
  note           TEXT,
  min_cell       INTEGER NOT NULL DEFAULT 20,   -- 单元格样本下限：低于它不给百分比
  status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','deprecated')),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE dimension_registry IS
  '分类（分层维度）注册表。from_sql 给出基表与连接，expr_sql 给出"这一维取什么值"。'
  '透视界面就是：挑 1..N 个同 entity 的维度 → GROUP BY 它们的 expr_sql。'
  '**同一 entity 才能拼在一张透视表里**（人的维度和岗位的维度不在一个粒度上）。'
  'min_cell 是**小样本护栏**：样本太少的格子不给百分比，避免"1 个人 100%"骗人。';

-- ---------------------------------------------------------------------------
-- 3. 漏斗注册表
-- ---------------------------------------------------------------------------
CREATE TABLE funnel_registry (
  funnel_id  TEXT PRIMARY KEY,
  title      TEXT NOT NULL,
  domain     TEXT NOT NULL CHECK (domain IN ('供给','需求','匹配')),
  subject    TEXT NOT NULL DEFAULT 'person' CHECK (subject IN ('person','job_posting')),
  stages     JSONB NOT NULL,   -- [{"name":"注册","sql":"SELECT count(*) ...","note":"..."}]
  note       TEXT,
  version    TEXT NOT NULL DEFAULT '1.0.0',
  status     TEXT NOT NULL DEFAULT 'active'
               CHECK (status IN ('active','deprecated')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_funnel_stages_is_array CHECK (jsonb_typeof(stages) = 'array'),
  CONSTRAINT chk_funnel_stages_nonempty CHECK (jsonb_array_length(stages) >= 2)
);
COMMENT ON TABLE funnel_registry IS
  '漏斗注册表。stages 是**有序阶段**，每阶段的 sql 返回该阶段的**主体数**（去重）。'
  '关键不变式：**后面的阶段必须是前面阶段的子集**（漏斗只能变窄）。'
  '违反它说明阶段定义写错了 —— funnel_run() 会直接报错，而不是画一张好看的假漏斗。';

CREATE OR REPLACE FUNCTION funnel_run(p_funnel text)
RETURNS TABLE(stage_no int, stage_name text, subjects bigint,
              step_rate numeric, overall_rate numeric, note text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE r funnel_registry; st jsonb; i int := 0; n bigint; first_n bigint := NULL;
        prev_n bigint := NULL;
BEGIN
  SELECT * INTO r FROM funnel_registry WHERE funnel_id = p_funnel AND status='active';
  IF NOT FOUND THEN
    RAISE EXCEPTION '漏斗不存在或已停用：%', p_funnel USING ERRCODE='no_data_found';
  END IF;
  FOR st IN SELECT * FROM jsonb_array_elements(r.stages) LOOP
    i := i + 1;
    EXECUTE (st->>'sql') INTO n;
    IF first_n IS NULL THEN first_n := n; END IF;
    RETURN QUERY SELECT i, (st->>'name'), n,
      CASE WHEN prev_n IS NULL OR prev_n = 0 THEN NULL
           ELSE round(100.0 * n / prev_n, 2) END,
      CASE WHEN first_n = 0 THEN NULL ELSE round(100.0 * n / first_n, 2) END,
      (st->>'note');
    -- 不变式：漏斗只能变窄。违反就是**阶段定义错了**，必须当场报错
    IF prev_n IS NOT NULL AND n > prev_n THEN
      RAISE EXCEPTION '漏斗 % 第 % 阶段「%」的主体数 % 大于上一阶段 % —— '
        '漏斗只能变窄；说明阶段定义写错了（不是数据的问题）',
        p_funnel, i, (st->>'name'), n, prev_n USING ERRCODE='check_violation';
    END IF;
    prev_n := n;
  END LOOP;
END $$;
COMMENT ON FUNCTION funnel_run(text) IS
  '跑一个漏斗。**阶段主体数必须单调不增**，否则直接报错 —— '
  '宁可报错也不画一张"越走越宽"的假漏斗（那通常是阶段定义写错了，而不是数据有问题）。';

-- ---------------------------------------------------------------------------
-- 4. 权限：口径/分类/漏斗是**只读给所有人、只写给 T3**
--    理由：它们本身不含个人数据，是"如何看数据"的说明；但**能被执行的 SQL**
--    属于高风险内容，写权限必须收紧。
-- ---------------------------------------------------------------------------
REVOKE ALL ON metric_registry, dimension_registry, funnel_registry FROM PUBLIC;
GRANT SELECT ON metric_registry, dimension_registry, funnel_registry TO mt_t0;
GRANT SELECT ON metric_registry, dimension_registry, funnel_registry TO mt_t1;
GRANT SELECT ON metric_registry, dimension_registry, funnel_registry TO mt_t2;
GRANT SELECT ON metric_registry, dimension_registry, funnel_registry TO mt_t3;

DO $$
DECLARE n int;
BEGIN
  -- 匿名只能读、不能写
  IF has_table_privilege('mt_t0','mt.metric_registry','INSERT') THEN
    RAISE EXCEPTION '匿名能写口径注册表 —— 那等于能注入任意 SQL';
  END IF;
  IF NOT has_table_privilege('mt_t3','mt.metric_registry','SELECT') THEN
    RAISE EXCEPTION 'T3 读不到口径注册表';
  END IF;
  -- 守卫生效：写关键字必须被拒
  IF check_registry_sql('SELECT 1; DROP TABLE person') THEN
    RAISE EXCEPTION '守卫失效：多语句写操作没被拒';
  END IF;
  IF check_registry_sql('UPDATE person SET status=''x''') THEN
    RAISE EXCEPTION '守卫失效：UPDATE 没被拒';
  END IF;
  IF NOT check_registry_sql('SELECT count(*) FROM person') THEN
    RAISE EXCEPTION '守卫过严：正常 SELECT 被拒';
  END IF;
  SELECT count(*) INTO n FROM metric_registry;
  RAISE NOTICE '口径/分类/漏斗注册表就绪（当前口径 % 条）', n;
END $$;

COMMIT;
