-- ============================================================================
-- 医学生人才信息库 · 054 口径 / 分类 / 漏斗 种子数据 v1.0.0
--
-- 053 建了注册表，这一版把它**填成能算的**。每个口径都在本库实跑验证过
-- （见文件末尾的 DO 块：逐条执行，算不出来就直接报错）。
--
-- 【调研结论怎么落到这里的】
-- 公开方法论（亿信华辰《指标体系构建详解》等）讲指标 = 原子指标 + 修饰词 + 时间周期，
-- 并列了四步法：厘清业务阶段 → 定核心指标 → **公式拆解 + 按业务路径拆解** → 宣贯存档。
-- 本库的业务路径是**人才供给 → 岗位需求 → 匹配 → 结果**，所以：
--   · 口径按四个域组织（供给/需求/匹配/结果）—— 对应"业务阶段"；
--   · 每个口径都写清**分子/分母**（等于公式拆解），并**存档口径说明**；
--   · 按路径拆出来的阶段序列就是**漏斗**（funnel_registry）；
--   · 方法论反复强调"相关不是因果"，所以每个口径都有 caveat。
--
-- 【实测踩到的坑：写错一个词，算出 0 且不报错】
-- `match_result.target_type` 的**实际取值是 `'job'`**，不是 `'job_posting'`。
-- 若按直觉写 `target_type='job_posting'`，分母正常、分子恒为 0，
-- 报表会安静地显示"岗位被匹配率 0%" —— 看起来像"业务没做"，其实是**口径写错了**。
-- 所以下面每条口径都在末尾的 DO 块里**实跑一遍**，并且对"本该 >0 的"加了断言。
--
-- 【平均值类口径】
-- 053 只支持"比率型"（分子/分母）。平均分这类**没有天然分母**，
-- 硬套会写出"分母=1"这种骗人的东西，所以这里补 `kind` 与 `value_sql`：
-- 比率型给分子分母（能看出 3/124），数值型直接给一个值（并说明它是什么）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 0. 修正 053 的守卫：它**对"被括号包起来的子查询"过严**
--
-- 实测：053 的锚点写的是 `^\s*(select|with)\y`，而注册表里的 SQL 片段
-- 全都要**嵌进更大的查询**里，所以一律写成 `(SELECT count(*) ...)` ——
-- 以 `(` 开头，锚点匹配不上，于是**每一条口径的 CHECK 都失败**。
--
-- 这是"守卫过严"的第二种表现（第一种是 053 里的 `\b` 当成词边界）：
-- 过松会放行坏数据，过严会让功能整个不能用，而且错误信息看起来像
-- "你写的 SQL 不合法" —— 排查方向会被带偏。
--
-- 修法：判"是不是查询"之前先剥掉**开头的括号与空白**。
-- 这不削弱防护：多语句仍被 `;` 拦、写关键字仍被关键字表拦。
-- （053 已应用，按纪律不改它；修正放在依赖它的 054 开头。）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_registry_sql(txt text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s text;
BEGIN
  IF txt IS NULL OR btrim(txt) = '' THEN RETURN false; END IF;
  s := btrim(txt);
  s := regexp_replace(s, '--[^\n]*', ' ', 'g');
  s := regexp_replace(s, '/\*.*?\*/', ' ', 'gs');
  s := btrim(s);
  IF s LIKE '%;%' THEN RETURN false; END IF;                -- 只允许单条语句
  s := regexp_replace(s, '^[\s(]+', '');                    -- 允许被括号包起来
  -- ⚠ PostgreSQL 里 `\b` 是**退格符**不是词边界，词边界是 `\y`（053 踩过）
  IF s !~* '^\s*(select|with)\y' THEN RETURN false; END IF;  -- 必须是查询
  IF s ~* '\y(insert|update|delete|truncate|drop|alter|create|grant|revoke|copy|call|do|vacuum|analyze|refresh|reindex|comment|set|reset|begin|commit|rollback)\y'
     THEN RETURN false; END IF;
  IF s ~* '\y(pg_sleep|pg_terminate_backend|pg_read_file|pg_ls_dir|lo_import|dblink|pg_execute_server_program)\y'
     THEN RETURN false; END IF;
  RETURN true;
END $$;
COMMENT ON FUNCTION check_registry_sql(text) IS
  '注册表守卫：只接受**单条只读 SELECT/WITH**（允许被括号包裹，因为片段要嵌进更大的查询）。'
  '只用于**完整查询**类字段（num/den/value_sql）。口径即代码，所以执行前必须先过这一关。';

-- ---------------------------------------------------------------------------
-- 0b. 第二种守卫：给"片段"用（from_sql 是 FROM 片段、expr_sql 是表达式）
--
-- 实测暴露的**我自己的设计错误**：053 把"必须是完整查询"的守卫
-- （`check_registry_sql`）套在了 `dimension_registry.from_sql` 与 `expr_sql` 上，
-- 而这两个字段**本来就不是完整查询** —— `from_sql` 是 `FROM` 后面的片段
-- （`person p LEFT JOIN ...`），`expr_sql` 是一个表达式（`coalesce(d.age_band,'…')`）。
-- 于是每一条维度都建不进来。
--
-- 教训：**守卫要按"这个字段到底是什么"来选，不是"越严越好"。**
-- 严到把正常内容也拦住，等于功能不可用，而且错误信息会指向"你写错了"。
--
-- 弱守卫仍然有实际作用：禁止多语句（`;`）、禁止写/DDL 关键字、禁止危险函数。
-- 它拦不住"表达式本身写得恶意" —— 这一点**如实说明**：注册表只有 T3 能写，
-- 且表达式最终嵌进门户的**只读事务**里执行，这两道才是真正的边界。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_registry_fragment(txt text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s text;
BEGIN
  IF txt IS NULL OR btrim(txt) = '' THEN RETURN false; END IF;
  s := btrim(txt);
  s := regexp_replace(s, '--[^\n]*', ' ', 'g');
  s := regexp_replace(s, '/\*.*?\*/', ' ', 'gs');
  s := btrim(s);
  IF s LIKE '%;%' THEN RETURN false; END IF;
  IF s ~* '\y(insert|update|delete|truncate|drop|alter|create|grant|revoke|copy|call|vacuum|refresh|reindex)\y'
     THEN RETURN false; END IF;
  IF s ~* '\y(pg_sleep|pg_terminate_backend|pg_read_file|pg_ls_dir|lo_import|dblink|pg_execute_server_program)\y'
     THEN RETURN false; END IF;
  RETURN true;
END $$;
COMMENT ON FUNCTION check_registry_fragment(text) IS
  '注册表"片段"守卫：用于 from_sql（FROM 片段）与 expr_sql（表达式）。'
  '禁多语句、禁写/DDL 关键字、禁危险函数；**不要求**是完整查询（它们本来就不是）。';

-- 把 053 建错的约束换成正确的那一个（053 已应用，按纪律不改它，这里替换）
ALTER TABLE dimension_registry DROP CONSTRAINT IF EXISTS dimension_registry_from_sql_check;
ALTER TABLE dimension_registry DROP CONSTRAINT IF EXISTS dimension_registry_expr_sql_check;
ALTER TABLE dimension_registry
  ADD CONSTRAINT chk_dim_from_sql CHECK (check_registry_fragment(from_sql));
ALTER TABLE dimension_registry
  ADD CONSTRAINT chk_dim_expr_sql CHECK (check_registry_fragment(expr_sql));

-- ---------------------------------------------------------------------------
-- 1. 扩展：支持"数值型"口径（平均值/总数），而不是硬塞进比率
-- ---------------------------------------------------------------------------
ALTER TABLE metric_registry
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'ratio'
    CHECK (kind IN ('ratio','value'));
ALTER TABLE metric_registry
  ADD COLUMN IF NOT EXISTS value_sql TEXT;
ALTER TABLE metric_registry
  DROP CONSTRAINT IF EXISTS chk_metric_value_sql;
ALTER TABLE metric_registry
  ADD CONSTRAINT chk_metric_value_sql
    CHECK (value_sql IS NULL OR check_registry_sql(value_sql));
COMMENT ON COLUMN metric_registry.kind IS
  'ratio = 比率型（看分子/分母，如 3/124）；value = 数值型（平均值/总数，无天然分母）。'
  '把平均值硬写成比率（例如"分母=1"）是报表最常见的骗人方式，所以分开。';

CREATE OR REPLACE FUNCTION metric_value(p_metric text)
RETURNS TABLE(metric_id text, title text, unit text, numerator bigint,
              denominator bigint, value numeric, note text, caveat text,
              small_sample boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE r metric_registry; n bigint; d bigint; v numeric;
BEGIN
  -- ⚠ 必须写 mr.metric_id：本函数的返回列里也叫 metric_id，
  -- 不限定就会出现 `column reference "metric_id" is ambiguous`。
  -- （这个错在 053 里就埋着了，只是当时没调用过这个函数 —— **没被调用的代码等于没验证**。）
  SELECT mr.* INTO r FROM metric_registry mr
   WHERE mr.metric_id = p_metric AND mr.status = 'active';
  IF NOT FOUND THEN
    RAISE EXCEPTION '口径不存在或已停用：%', p_metric USING ERRCODE='no_data_found';
  END IF;
  IF r.kind = 'value' THEN
    EXECUTE 'SELECT ' || r.value_sql INTO v;
    RETURN QUERY SELECT r.metric_id, r.title, r.unit, NULL::bigint, NULL::bigint, v,
                        r.definition_note, r.caveat, false;
    RETURN;
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

-- ---------------------------------------------------------------------------
-- 2. 口径（metrics）—— 供给 / 需求 / 匹配 / 结果
-- ---------------------------------------------------------------------------
DELETE FROM metric_registry WHERE metric_id LIKE 'M\_%';

INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
-- ===== 供给域：人才这一侧 =====
('M_SUPPLY_PERSON', '入库人才数', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)', '(SELECT count(*) FROM person)',
 '人', NULL, NULL,
 'person 表的行数（全部状态，含停用）。这是**分母口径**，其它供给类比率都以它为分母。',
 '含已停用/墓碑记录；要"在库可用人数"应另加 status=''active'' 的修饰词。', 1),

('M_SUPPLY_PROFILE_RATE', '基础档案覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM person_demographics)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '有基础人口学档案（person_demographics 有行）的人 / 全部入库人才。'
 '人口学档案是分层的**前提**：没有它，年龄段/性别这些维度就是"未填"。',
 '覆盖率高不等于数据对；且它衡量的是"有没有填"，不是"填得准不准"。', 20),

('M_SUPPLY_EDU_RATE', '教育记录覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM education_record)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '有至少一条教育记录的人 / 全部入库人才。教育记录是学历/院校/专业维度与大部分岗位硬性要求的比对依据。',
 '没有教育记录**不等于**低学历，只是"没采到"——按此维度分层时必须把"未填"单列。', 20),

('M_SUPPLY_SKILL_RATE', '技能断言覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM skill_assertion)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '有至少一条技能断言的人 / 全部入库人才。技能断言是匹配算法的输入。',
 '断言可能是**自述**（claim_type）而未核验；覆盖率不能解读为"能力达标率"。', 20),

('M_SUPPLY_PREF_RATE', '求职偏好覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM preference)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '有至少一条求职偏好的人 / 全部入库人才。偏好是"推荐"类分析的必要输入。',
 '偏好是**意愿**不是事实；用偏好解释结果时注意它会被"想去的"而非"能去的"扭曲。', 20),

('M_SUPPLY_CONSENT_RATE', '有效授权覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM consent_record WHERE revoked_at IS NULL)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '有未被撤回的授权记录的人 / 全部入库人才。**这是合规底线口径**：'
 '没有个人分析授权的人，其分析产物在写入侧就会被授权门拒绝（见 bridge 的 CONSENT_REQUIRED）。',
 '已撤回的历史授权不在分子里，但撤回记录仍在库；做合规审计要看 consent_withdrawal_action。', 20),

('M_SUPPLY_MATCHED_RATE', '被匹配覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM match_result)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '至少出现在一条匹配结果里的人 / 全部入库人才。反映"人才有没有进入匹配流程"。',
 '覆盖率低可能是"没跑过匹配"而不是"匹配不上"——要先看 match_run 的次数与时间。', 20),

('M_SUPPLY_EMPLOYED_RATE', '就业记录覆盖率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM employment_record)',
 '(SELECT count(*) FROM person)', NULL,
 '%', NULL, NULL,
 '有至少一条就业记录的人 / 全部入库人才。本库的"结果变量"之一。',
 '就业记录来自采集，**缺失不等于未就业**；把它当"失业率"用会系统性高估失业。', 20),

('M_SUPPLY_OVERSEAS_RATE', '海外经历率', '供给', 'ratio',
 '(SELECT count(DISTINCT person_id) FROM education_record WHERE overseas)',
 '(SELECT count(DISTINCT person_id) FROM education_record)', NULL,
 '%', NULL, '限定在"有教育记录的人"',
 '有海外教育经历（education_record.overseas = true）的人 / **有教育记录的人**。'
 '注意分母不是全部人才——没有教育记录的人无从判断是否有海外经历，'
 '把他算进分母等于把"不知道"当成"没有"。',
 '海外经历常与家庭条件、院校层次同时作用（混杂）。'
 '**看到它与就业率的差异，不能直接说因果**，必须分层控制后再看。', 20),

('M_SUPPLY_SKILL_PER_PERSON', '人均技能断言数', '供给', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(count(*)::numeric / NULLIF(count(DISTINCT person_id),0), 2) FROM skill_assertion)',
 '条', NULL, NULL,
 '技能断言总数 / 有断言的人数。衡量档案的**信息密度**。',
 '断言多不等于能力强（自述类断言容易堆量）；它衡量的是档案丰富度。', 1),

('M_SUPPLY_OUTCOME_EMPLOYED_RATE', '已就业率（求职结果口径）', '供给', 'ratio',
 '(SELECT count(*) FROM field_value WHERE field_id=''F_PSN_RES_JOB_OUTCOME'' '
 'AND subject_type=''person'' AND value_code=''JO1'')',
 '(SELECT count(*) FROM field_value WHERE field_id=''F_PSN_RES_JOB_OUTCOME'' '
 'AND subject_type=''person'')', NULL,
 '%', NULL, '限定在"填了求职结果的人"',
 '求职结果字段填了"已就业"的人 / **填了该字段的人**。'
 '注意：该字段当前的值是**合成演示数据**（见 ops/seed_demo_analysis.py），'
 '真实数据接入前，这个口径只能用来验证算得对不对，**不能当业务结论**。',
 '分母是"填了的人"，不是全部人才。缺失的人不进分母 —— 这正是"缺失≠空值"的用处。', 20),

-- ===== 需求域：岗位这一侧 =====
('M_DEMAND_JOB', '入库岗位数', '需求', 'value',
 '(SELECT 0)', '(SELECT 1)', '(SELECT count(*) FROM job_posting)',
 '个', NULL, NULL,
 'job_posting 的行数。需求侧的分母口径。',
 '含过期/重复来源的岗位；本库有 raw_sha256 去重，但不保证业务上去重。', 1),

('M_DEMAND_REQ_PER_JOB', '岗位要求密度', '需求', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(count(*)::numeric / NULLIF((SELECT count(*) FROM job_posting),0), 2) '
 'FROM job_requirement)',
 '条', NULL, NULL,
 '岗位要求条目数 / 岗位数。衡量**岗位描述的细化程度**，也是 JD 解析质量的间接指标。',
 '要求条数多可能是"真的要求多"，也可能是**解析把一段话切碎了**；'
 '判断前要先看 job_requirement.requirement_kind 的分布。', 1),

('M_DEMAND_OCC_COVERED_RATE', '岗位归类职业率', '需求', 'ratio',
 '(SELECT count(*) FROM job_posting WHERE occupation_id IS NOT NULL)',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, NULL,
 '已归到职业树的岗位 / 全部岗位。**这是需求侧最关键的可用性口径**：'
 '没归类的岗位无法参与按职业维度的供需比对。',
 '归类率高不代表归类对；要抽查 parse_confidence 与 occupation 的层级是否合理。', 20),

('M_DEMAND_MATCHED_RATE', '岗位被匹配率', '需求', 'ratio',
 '(SELECT count(DISTINCT target_id) FROM match_result WHERE target_type = ''job'')',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, NULL,
 '至少出现在一条匹配结果里的岗位 / 全部岗位。'
 '⚠ 这里 `target_type` 的**实际取值是 `''job''`**（实测确认），不是 `''job_posting''`；'
 '写错会算出安静的 0%。',
 '与"被匹配覆盖率"配对看：只算一侧会误判是需求不足还是供给不足。', 20),

('M_DEMAND_CAMPUS_RATE', '校招岗位占比', '需求', 'ratio',
 '(SELECT count(*) FROM job_posting WHERE is_campus)',
 '(SELECT count(*) FROM job_posting)', NULL,
 '%', NULL, NULL,
 '标记为校招的岗位 / 全部岗位。学生群体主要进校招通道，所以这个比例决定'
 '"人才库和岗位库在同一个池子里"的程度。',
 'is_campus 是解析推断的，可能有误判；跨年比较前先确认解析版本一致。', 20),

-- ===== 匹配域 =====
('M_MATCH_RUN', '匹配运行次数', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)', '(SELECT count(*) FROM match_run)',
 '个', 'started_at', NULL,
 'match_run 的行数。每次运行有 algo_version 与 params，**算法变了结论就不能混着比**。',
 '不同 algo_version 的结果不可直接比较；跨版本趋势要按版本分组。', 1),

('M_MATCH_RESULT_PER_RUN', '每次运行产生结果数', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(count(*)::numeric / NULLIF((SELECT count(*) FROM match_run),0), 2) '
 'FROM match_result)',
 '条', NULL, NULL,
 '匹配结果总数 / 运行次数。衡量单次匹配的产出量。',
 '产出量高可能是"匹配面宽"，也可能是**阈值太低**；要配合得分分布一起看。', 1),

('M_MATCH_SCORE_AVG', '平均匹配得分', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)', '(SELECT round(avg(score_total), 2) FROM match_result)',
 '分', NULL, NULL,
 'match_result.score_total 的平均值。',
 '**均值会掩盖分布**：均值 60 可能是"都 60"，也可能是"一半 90 一半 30"——'
 '方法论明确要求看分布而不只看均值，所以分析界面必须能同时看分组明细与分布。', 1),

('M_MATCH_SCORE_MEDIAN', '匹配得分中位数', '匹配', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT round(percentile_cont(0.5) WITHIN GROUP (ORDER BY score_total)::numeric, 2) '
 'FROM match_result)',
 '分', NULL, NULL,
 'match_result.score_total 的中位数。与均值一起看可以判断偏斜。',
 '中位数不受极端值影响，但也不能说明"匹配得好"——它是相对分不是绝对标准。', 1),

-- ===== 结果域 =====
('M_OUTCOME_EMPLOYED_PERSON', '有就业记录的人数', '结果', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(DISTINCT person_id) FROM employment_record)',
 '人', NULL, NULL,
 'employment_record 覆盖的去重人数。',
 '采集来源偏斜（有就业记录的人往往更好采集），因此**不能当总体就业率**。', 1),

('M_OUTCOME_EMPLOYER_COVERAGE', '雇主信息覆盖率', '结果', 'ratio',
 '(SELECT count(*) FROM employment_record WHERE employer_id IS NOT NULL)',
 '(SELECT count(*) FROM employment_record)', NULL,
 '%', NULL, NULL,
 '就业记录里已关联到雇主主数据（employer_id 非空）的比例。'
 '关联不上就没法做"同一雇主的人才流动"分析。',
 'employer 表当前为空，所以这个口径现在必然很低 —— 那不是数据质量问题，是主数据还没建。', 20);

-- ---------------------------------------------------------------------------
-- 3. 分类（维度）—— 供"选单个或多个字段作为分类变量"的透视使用
-- ---------------------------------------------------------------------------
DELETE FROM dimension_registry WHERE dimension_id LIKE 'D\_%';

INSERT INTO dimension_registry
  (dimension_id, title, entity, from_sql, expr_sql, code_table_id, group_name,
   note, min_cell)
VALUES
-- ===== 人的维度（entity = person）=====
('D_AGE_BAND', '年龄段', 'person',
 'person p LEFT JOIN person_demographics d ON d.person_id = p.person_id',
 'coalesce(d.age_band, ''（未填）'')', 'CT_AGE_BAND', '人口学',
 '由 birth_year 分段而来（见迁移 036）。**这是公开字段**（T2）。', 20),
('D_SEX', '性别', 'person',
 'person p LEFT JOIN person_demographics d ON d.person_id = p.person_id',
 'coalesce(d.sex, ''（未填）'')', 'CT_SEX', '人口学', NULL, 20),
('D_HUKOU_TYPE', '户籍类型', 'person',
 'person p LEFT JOIN person_demographics d ON d.person_id = p.person_id',
 'coalesce(d.hukou_type, ''（未填）'')', NULL, '人口学', NULL, 20),
('D_CITY', '户籍城市', 'person',
 'person p LEFT JOIN person_demographics d ON d.person_id = p.person_id',
 'coalesce(d.hukou_province, ''（未填）'')', NULL, '地域',
 '列名是历史遗留：hukou_province 里实际存的是**城市名**（实测去重值全是城市），'
 '不要当省用。', 20),
('D_ENROLL_CHANNEL', '入组渠道', 'person',
 'person p', 'coalesce(p.enroll_channel, ''（未填）'')', 'CT_ENROLL_CHANNEL',
 '来源', '渠道差异是"拉新质量"分析的核心维度。', 20),
('D_DEGREE', '最高学历', 'person',
 'person p', '(SELECT e.degree_level FROM education_record e '
 'WHERE e.person_id = p.person_id ORDER BY e.degree_level DESC NULLS LAST LIMIT 1)',
 'CT_DEGREE_LEVEL', '教育',
 '取**最高**一条学历记录。同一人有多个学历时只保留最高，避免重复计数。', 20),
('D_EDU_OVERSEAS', '海外经历（教育记录）', 'person',
 'person p',
 'CASE WHEN EXISTS (SELECT 1 FROM education_record e '
 'WHERE e.person_id = p.person_id AND e.overseas) THEN ''有'' '
 'WHEN EXISTS (SELECT 1 FROM education_record e WHERE e.person_id = p.person_id) '
 'THEN ''无'' ELSE ''（无教育记录）'' END',
 NULL, '教育',
 '**三态而不是两态**：有 / 无 / 无教育记录。把"不知道"并进"没有"是最常见的分析错误。', 20),
('D_EDU_CLINICAL', '是否临床医学类', 'person',
 'person p',
 '(SELECT CASE WHEN bool_or(e.is_clinical) THEN ''是'' ELSE ''否'' END '
 'FROM education_record e WHERE e.person_id = p.person_id)',
 NULL, '教育', NULL, 20),
('D_SKILL_CNT', '技能断言条数分档', 'person',
 'person p',
 'CASE WHEN (SELECT count(*) FROM skill_assertion s WHERE s.person_id=p.person_id) = 0 '
 'THEN ''0 条'' WHEN (SELECT count(*) FROM skill_assertion s WHERE s.person_id=p.person_id) <= 5 '
 'THEN ''1-5 条'' WHEN (SELECT count(*) FROM skill_assertion s WHERE s.person_id=p.person_id) <= 10 '
 'THEN ''6-10 条'' ELSE ''10 条以上'' END',
 NULL, '能力', '分档而不是直接用条数：条数是准连续变量，直接透视会碎成几百个格子。', 20),
('D_CONSENT', '授权状态', 'person',
 'person p',
 'CASE WHEN EXISTS (SELECT 1 FROM consent_record c WHERE c.person_id=p.person_id '
 'AND c.revoked_at IS NULL) THEN ''有效授权'' '
 'WHEN EXISTS (SELECT 1 FROM consent_record c WHERE c.person_id=p.person_id) '
 'THEN ''已撤回'' ELSE ''（无授权记录）'' END',
 NULL, '合规', '合规分层：分析产物只能来自有有效授权的人。', 20),
('D_MATCHED', '是否被匹配', 'person',
 'person p',
 'CASE WHEN EXISTS (SELECT 1 FROM match_result m WHERE m.person_id=p.person_id) '
 'THEN ''被匹配过'' ELSE ''未匹配'' END',
 NULL, '匹配', NULL, 20),
('D_EMPLOYED', '是否有就业记录', 'person',
 'person p',
 'CASE WHEN EXISTS (SELECT 1 FROM employment_record e WHERE e.person_id=p.person_id) '
 'THEN ''有就业记录'' ELSE ''无就业记录'' END',
 NULL, '结果',
 '⚠ "无就业记录"**不等于未就业**（可能是没采到）。做结果分析时这句话要写在图表旁边。', 20),
('D_OUTCOME_FIELD', '求职结果（动态字段）', 'person',
 'person p',
 'coalesce((SELECT fv.value_code FROM field_value fv WHERE fv.subject_type=''person'' '
 'AND fv.subject_id=p.person_id AND fv.field_id=''F_PSN_RES_JOB_OUTCOME'' LIMIT 1), ''（未填）'')',
 'CT_F_PSN_RES_JOB_OUTCOME', '结果',
 '当前值来自**合成演示数据**（ops/seed_demo_analysis.py），仅用于验证口径。', 20),
('D_OVERSEAS_FIELD', '海外经历（动态字段）', 'person',
 'person p',
 'coalesce((SELECT fv.value_code FROM field_value fv WHERE fv.subject_type=''person'' '
 'AND fv.subject_id=p.person_id AND fv.field_id=''F_PSN_RES_OVERSEAS'' LIMIT 1), ''（未填）'')',
 'CT_YES_NO', '教育',
 '与 D_EDU_OVERSEAS 是**两个不同口径**：这个来自动态建模字段（自己填的），'
 '那个来自教育记录（采集的）。两者不一致本身就是有价值的分析发现。', 20),

-- ===== 岗位的维度（entity = job_posting）=====
('D_JOB_FAMILY', '岗位族', 'job_posting',
 'job_posting j', 'coalesce(j.job_family, ''（未填）'')', NULL, '岗位',
 '需求侧最主要的分类维度。', 20),
('D_JOB_CITY', '岗位城市', 'job_posting',
 'job_posting j', 'coalesce(j.city, ''（未填）'')', NULL, '岗位', NULL, 20),
('D_JOB_LEVEL', '岗位层级', 'job_posting',
 'job_posting j', 'coalesce(j.level, ''（未填）'')', NULL, '岗位', NULL, 20),
('D_JOB_CAMPUS', '招聘类型', 'job_posting',
 'job_posting j',
 'CASE WHEN j.is_campus THEN ''校招'' ELSE ''社招'' END', NULL, '岗位',
 'is_campus 由解析推断，可能有误判。', 20),
('D_JOB_EDU_REQ', '学历要求', 'job_posting',
 'job_posting j', 'coalesce(j.education_req, ''（未填）'')', NULL, '要求',
 '需求侧硬性条件，用于与供给侧学历做**供需错配**分析。', 20),
('D_JOB_OCC', '是否已归类职业', 'job_posting',
 'job_posting j',
 'CASE WHEN j.occupation_id IS NOT NULL THEN ''已归类'' ELSE ''未归类'' END',
 NULL, '质量', '未归类的岗位不参与按职业的供需比对。', 20),
('D_JOB_MATCHED', '岗位是否被匹配', 'job_posting',
 'job_posting j',
 'CASE WHEN EXISTS (SELECT 1 FROM match_result m WHERE m.target_type=''job'' '
 'AND m.target_id = j.job_id) THEN ''被匹配过'' ELSE ''未匹配'' END',
 NULL, '匹配', NULL, 20),
('D_JOB_REQ_CNT', '岗位要求条数分档', 'job_posting',
 'job_posting j',
 'CASE WHEN (SELECT count(*) FROM job_requirement r WHERE r.job_id=j.job_id) = 0 THEN ''0 条'' '
 'WHEN (SELECT count(*) FROM job_requirement r WHERE r.job_id=j.job_id) <= 3 THEN ''1-3 条'' '
 'WHEN (SELECT count(*) FROM job_requirement r WHERE r.job_id=j.job_id) <= 8 THEN ''4-8 条'' '
 'ELSE ''8 条以上'' END',
 NULL, '要求', '要求条数分档，用于看"描述详细度"与"被匹配率"的关系。', 20);

-- ---------------------------------------------------------------------------
-- 4. 漏斗（funnel）—— 每阶段都是前一阶段的**收窄**
--    按"业务路径拆解"设计；阶段之间用 AND 递进，保证单调不增。
-- ---------------------------------------------------------------------------
DELETE FROM funnel_registry WHERE funnel_id LIKE 'F\_%';

INSERT INTO funnel_registry (funnel_id, title, domain, subject, stages, note) VALUES
('F_SUPPLY', '人才供给漏斗（注册 → 可匹配 → 有结果）', '供给', 'person',
 jsonb_build_array(
  jsonb_build_object('name','入库', 'sql','SELECT count(*) FROM person',
    'note','人才库起点。'),
  jsonb_build_object('name','有基础档案', 'sql',
    'SELECT count(*) FROM person p WHERE EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id)',
    'note','没有档案就无法做人口学分层。'),
  jsonb_build_object('name','有教育记录', 'sql',
    'SELECT count(*) FROM person p '
    'WHERE EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id)',
    'note','教育记录是岗位硬性要求的比对依据。'),
  jsonb_build_object('name','有技能断言', 'sql',
    'SELECT count(*) FROM person p '
    'WHERE EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM skill_assertion s WHERE s.person_id=p.person_id)',
    'note','技能断言是匹配算法的输入。'),
  jsonb_build_object('name','有有效授权', 'sql',
    'SELECT count(*) FROM person p '
    'WHERE EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM skill_assertion s WHERE s.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM consent_record c WHERE c.person_id=p.person_id AND c.revoked_at IS NULL)',
    'note','**合规关卡**：没有个人分析授权就不能产出分析结果，所以它在漏斗里是一道真实的门。'),
  jsonb_build_object('name','被匹配过', 'sql',
    'SELECT count(*) FROM person p '
    'WHERE EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM skill_assertion s WHERE s.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM consent_record c WHERE c.person_id=p.person_id AND c.revoked_at IS NULL) '
    'AND EXISTS (SELECT 1 FROM match_result m WHERE m.person_id=p.person_id)',
    'note','进入匹配流程。'),
  jsonb_build_object('name','有就业记录', 'sql',
    'SELECT count(*) FROM person p '
    'WHERE EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM skill_assertion s WHERE s.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM consent_record c WHERE c.person_id=p.person_id AND c.revoked_at IS NULL) '
    'AND EXISTS (SELECT 1 FROM match_result m WHERE m.person_id=p.person_id) '
    'AND EXISTS (SELECT 1 FROM employment_record w WHERE w.person_id=p.person_id)',
    'note','⚠ "这一阶段人数"不是就业率：漏斗只统计**全程走完且每步都有记录**的人。'))
 ,
 '按"业务路径拆解"设计（方法论四步法的第 3 步）：每一阶段都是前一阶段的收窄，'
 '所以人数必然单调不增。**注意读法**：末阶段人数除以首阶段人数得到的是'
 '"全链路带记录转化率"，它会同时被"真的没走完"和"没采到记录"压低 —— 二者在这张图里分不开，'
 '要结合各阶段的记录覆盖率（见口径 M_SUPPLY_*_RATE）一起解释。'),

('F_DEMAND', '岗位需求漏斗（入库 → 解析 → 归类 → 被匹配）', '需求', 'job_posting',
 jsonb_build_array(
  jsonb_build_object('name','岗位入库', 'sql','SELECT count(*) FROM job_posting',
    'note','需求侧起点。'),
  jsonb_build_object('name','有要求解析', 'sql',
    'SELECT count(*) FROM job_posting j '
    'WHERE EXISTS (SELECT 1 FROM job_requirement r WHERE r.job_id=j.job_id)',
    'note','没有解析出要求的岗位无法参与胜任比对。'),
  jsonb_build_object('name','已归类职业', 'sql',
    'SELECT count(*) FROM job_posting j '
    'WHERE EXISTS (SELECT 1 FROM job_requirement r WHERE r.job_id=j.job_id) '
    'AND j.occupation_id IS NOT NULL',
    'note','归类后才能按职业做供需比对。'),
  jsonb_build_object('name','被匹配过', 'sql',
    'SELECT count(*) FROM job_posting j '
    'WHERE EXISTS (SELECT 1 FROM job_requirement r WHERE r.job_id=j.job_id) '
    'AND j.occupation_id IS NOT NULL '
    'AND EXISTS (SELECT 1 FROM match_result m WHERE m.target_type=''job'' AND m.target_id=j.job_id)',
    'note','进入了匹配流程。'))
 ,
 '需求侧漏斗。它能回答"岗位是**没解析出来**、**没归类**、还是**没被匹配**" —— '
 '这三种原因的处置完全不同（改解析器 / 补职业树 / 跑匹配）。'),

('F_MATCH_QUALITY', '匹配质量漏斗（被匹配 → 得分达标 → 有就业记录）', '匹配', 'person',
 jsonb_build_array(
  jsonb_build_object('name','被匹配过', 'sql',
    'SELECT count(DISTINCT person_id) FROM match_result',
    'note','至少有一条匹配结果的人。'),
  jsonb_build_object('name','最高得分 ≥ 60', 'sql',
    'SELECT count(*) FROM (SELECT person_id, max(score_total) AS best FROM match_result '
    'GROUP BY person_id HAVING max(score_total) >= 60) t',
    'note','60 是**本例的阈值约定**，不是行业标准；改阈值要改这条 SQL 并留痕。'),
  jsonb_build_object('name','且有就业记录', 'sql',
    'SELECT count(*) FROM (SELECT person_id, max(score_total) AS best FROM match_result '
    'GROUP BY person_id HAVING max(score_total) >= 60) t '
    'WHERE EXISTS (SELECT 1 FROM employment_record e WHERE e.person_id = t.person_id)',
    'note','⚠ **这一列不能解读为"匹配得好所以就业了"**：'
    '高得分的人可能本来就更容易就业（混杂），而且就业记录缺失会把人数压低。'
    '要下因果结论必须做分层或对照。'))
 ,
 '匹配质量漏斗。**它是本库最容易被误读的一张图**：从"被匹配"到"有就业记录"的转化率'
 '混合了算法质量、人才本身条件、以及采集覆盖三件事，不能单独当算法效果的证据。');

-- ---------------------------------------------------------------------------
-- 5. 逐条实跑验证：算不出来、或算出"安静的 0"的，直接报错
-- ---------------------------------------------------------------------------
DO $$
DECLARE r record; n_ok int := 0; n_metric int; n_dim int; n_funnel int; v numeric;
BEGIN
  -- ① 每个口径都要能算（不能只登记不执行）
  FOR r IN SELECT metric_id FROM metric_registry WHERE status='active' ORDER BY 1 LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n_ok := n_ok + 1;
  END LOOP;
  RAISE NOTICE '口径实跑通过：% 条', n_ok;

  -- ② 关键口径**不能是安静的 0**：这些在本库本该 > 0
  SELECT value INTO v FROM metric_value('M_SUPPLY_PERSON');
  IF v IS NULL OR v <= 0 THEN RAISE EXCEPTION '入库人才数为 0 —— 口径或库是空的'; END IF;
  SELECT value INTO v FROM metric_value('M_DEMAND_JOB');
  IF v IS NULL OR v <= 0 THEN RAISE EXCEPTION '入库岗位数为 0'; END IF;
  -- 这条最要紧：target_type 写错就会是 0 且不报错（实测踩过）
  SELECT value INTO v FROM metric_value('M_DEMAND_MATCHED_RATE');
  IF v IS NULL THEN
    RAISE EXCEPTION '岗位被匹配率为 NULL —— 检查分母';
  END IF;
  IF v = 0 THEN
    RAISE EXCEPTION '岗位被匹配率为 0%% —— 极可能是 target_type 写错（实际值是 job）';
  END IF;
  RAISE NOTICE '岗位被匹配率 = %%%（非 0，说明 target_type 口径正确）', v;
  -- ③ 每个维度都要能算（用它的 from_sql + expr_sql 真跑一次分组）
  FOR r IN SELECT dimension_id, from_sql, expr_sql, entity
             FROM dimension_registry WHERE status='active' ORDER BY 1 LOOP
    EXECUTE format('SELECT count(*) FROM (SELECT %s AS cat FROM %s GROUP BY 1) t',
                   r.expr_sql, r.from_sql) INTO v;
    IF v IS NULL OR v = 0 THEN
      RAISE EXCEPTION '维度 % 一个分组都算不出来', r.dimension_id;
    END IF;
  END LOOP;
  RAISE NOTICE '维度实跑通过';

  -- ④ 每个漏斗都要能跑，且**单调不增**（funnel_run 内部会断言）
  FOR r IN SELECT funnel_id FROM funnel_registry WHERE status='active' ORDER BY 1 LOOP
    PERFORM * FROM funnel_run(r.funnel_id);
  END LOOP;
  RAISE NOTICE '漏斗实跑通过（含单调性断言）';

  SELECT count(*) INTO n_metric FROM metric_registry WHERE status='active';
  SELECT count(*) INTO n_dim FROM dimension_registry WHERE status='active';
  SELECT count(*) INTO n_funnel FROM funnel_registry WHERE status='active';
  RAISE NOTICE '分析注册表就绪：口径 % 条 / 分类 % 个 / 漏斗 % 个', n_metric, n_dim, n_funnel;
END $$;

COMMIT;
