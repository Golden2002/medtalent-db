-- ============================================================================
-- 医学生人才信息库 · 069 有时间截点的真实验证集 v1.0.0
--
-- 【评审优先级 4 的要求】
--   "有时间截点的真实验证集：先区分两类目标——解释已有履历（可用全部历史）
--    与推荐下一步岗位（只能使用决策时点之前的信息，不能泄漏）；按人隔离开发/留出集；
--    历史预测按时间隔离；**不要只以最终去向作为真值**，由评审人标注多个合理方向；
--    报告时应分开评估：目标职业是否在库、资格判定是否正确、排序是否合理、解释是否有证据。"
--
-- 【先核对运行状态（评审要求"先核验再实施"）】
--   ① 事件时间**有**：education_record.end_date 1998–2028、
--      employment_record.start_date 2000–2026 —— 可作截点；
--   ② 入库时间**没有历史**：person/employment_record.recorded_at **全部是 2026-09-27/28**
--      （一次性批量入库）。所以**无法区分"决策时点当时已知"与"后来才补录"**。
--      → 本库只能按**事件时间**切；这写进了 `validation_point.basis` 与口径的注意项。
--        真实增量采集开始后，`recorded_at` 才有意义，届时判据要换成它。
--   ③ 真实案例**只有 3 位**（real_cases.json / real_cases_extra.json / real_targets.json）；
--      `transition_case` 表是空的。
--   ④ **真值结构已经天然是"多标注"**（real_targets.json）：
--        actual_path（真实路径）、occupation_ids（映射到的职业）、labels（标签）、
--        mapping_reason（为什么这么映射）、**nodes_without_jd**（语义更贴但**库里 0 岗位**的节点）。
--
-- 【核对发现的一个决定性事实（正是"分开四项评估"的价值）】
--   冯唐案例的 mapping_reason 写着：真实去向是医疗健康方向的 PE/投资；
--   职业树里 OCC-F07-02-02 医疗投资经理、OCC-F07-01-01 医药咨询顾问**语义更贴**，
--   但**两者在 690 份 JD 里 0 岗位**；库里存在的最近节点是券商医药行业研究员（30 份 JD）。
--   → 也就是说：**"推荐到正确职业"这一项必然失败，而失败原因是语料覆盖，不是算法。**
--     如果只报一个总分，这个区别会被完全掩盖 —— 所以必须分开四项。
--
-- 【设计】
--   预测单元不是"一个人"，而是 **(人, 决策时点)** 二元组：同一人在不同时点各有一个预测点。
--   · **按人隔离**：同一个人**所有**预测点必须在同一个集合里（否则同一人的履历会跨集合泄漏）；
--   · **时间隔离**：每个预测点带 cutoff_date，特征只能用事件时间 ≤ cutoff 的事实；
--   · **多标注**：一个预测点可由多个评审人给出多个"可接受方向"；
--   · **四项分开**：目标是否在库 / 资格判定 / 排序 / 解释有据，各算各的，不合成一个分数。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 验证案例（人一级）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_case (
  case_id     TEXT PRIMARY KEY,
  person_id   TEXT,
  nature      TEXT NOT NULL CHECK (nature IN ('real','synthetic')),
  source      TEXT,
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE validation_case IS
  '验证案例（人一级）。nature 必填且只能是 real/synthetic —— '
  '沿用本库的数据性质纪律：**合成案例上的表现不能当业务结论**，报告必须分开算。';

-- ---------------------------------------------------------------------------
-- 2. 预测点（人 × 决策时点）—— 验证的最小单元
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_point (
  point_id      TEXT PRIMARY KEY,
  case_id       TEXT NOT NULL REFERENCES validation_case(case_id) ON DELETE CASCADE,
  cutoff_date   DATE NOT NULL,          -- 决策时点：只能用事件时间 ≤ 它的信息
  split         TEXT NOT NULL CHECK (split IN ('dev','holdout')),
  target_occupation_id TEXT,            -- 真实"下一步"落到的职业（可为空：库外方向）
  target_label  TEXT,                   -- 真实去向的文字描述（多标注里的"主方向"）
  in_library    BOOLEAN,               -- 目标职业在库里**是否有 JD 样本**
  basis         TEXT NOT NULL,          -- 截点依据（为什么这个时点是"决策时点"）
  recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE validation_point IS
  '预测点 = (人, 决策时点)。**只看人不够**：同一人在不同时点各有一个预测点，'
  '而"推荐下一步"只允许用 cutoff_date 之前的信息。'
  'in_library 单独记：**目标职业在库外时，任何"推荐错了"的结论都不成立** —— '
  '那是语料覆盖问题，不是算法问题（实测冯唐案例的真实方向在 690 份 JD 里 0 岗位）。';

-- 按人隔离：同一个人不得跨 dev/holdout
CREATE OR REPLACE FUNCTION check_split_person_isolation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_person text; v_n int;
BEGIN
  SELECT person_id INTO v_person FROM validation_case WHERE case_id = NEW.case_id;
  IF v_person IS NULL THEN RETURN NEW; END IF;
  SELECT count(DISTINCT p.split) INTO v_n
    FROM validation_point p JOIN validation_case v ON v.case_id = p.case_id
   WHERE v.person_id = v_person AND p.point_id <> NEW.point_id;
  IF v_n > 0 AND NOT EXISTS (
       SELECT 1 FROM validation_point p JOIN validation_case v ON v.case_id = p.case_id
        WHERE v.person_id = v_person AND p.split = NEW.split AND p.point_id <> NEW.point_id)
     AND v_n >= 1 AND EXISTS (
       SELECT 1 FROM validation_point p JOIN validation_case v ON v.case_id = p.case_id
        WHERE v.person_id = v_person AND p.split <> NEW.split AND p.point_id <> NEW.point_id) THEN
    RAISE EXCEPTION '按人隔离被破坏：% 的预测点同时出现在 dev 与 holdout —— '
      '同一个人的履历会跨集合泄漏，必须整人放同一侧', v_person;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_split_isolation ON validation_point;
CREATE TRIGGER trg_split_isolation BEFORE INSERT OR UPDATE ON validation_point
  FOR EACH ROW EXECUTE FUNCTION check_split_person_isolation();

-- ---------------------------------------------------------------------------
-- 3. 多标注（一个预测点可由多个评审人给出多个可接受方向）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation_annotation (
  annotation_id BIGSERIAL PRIMARY KEY,
  point_id      TEXT NOT NULL REFERENCES validation_point(point_id) ON DELETE CASCADE,
  rater         TEXT NOT NULL,
  direction     TEXT NOT NULL CHECK (direction IN ('primary','acceptable','unacceptable')),
  occupation_id TEXT,
  label         TEXT,
  -- 四项分开评（每项都可为 NULL＝"这一项没评"，而不是"评了不合格"）
  target_in_library  BOOLEAN,
  qualification_ok   BOOLEAN,
  ranking_reasonable BOOLEAN,
  evidence_present   BOOLEAN,
  note          TEXT,
  annotated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (point_id, rater, direction, label)
);
COMMENT ON TABLE validation_annotation IS
  '多标注。**不要只以"最终去向"为唯一真值**：一个人的下一步往往有多个合理方向，'
  '所以允许多个评审人给出多个 acceptable。'
  '四项分开评且**可为 NULL**：NULL 表示"这一项没评"，与"评了不合格(false)"是两件事 —— '
  '把它们混起来会让"没评"被算成"不合格"。';

-- ---------------------------------------------------------------------------
-- 4. 时间泄漏检测：特征里有没有用到 cutoff 之后的事实
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_validation_leakage AS
SELECT p.point_id, p.case_id, v.person_id, p.cutoff_date, p.split,
       (SELECT count(*) FROM education_record e
         WHERE e.person_id = v.person_id AND e.end_date > p.cutoff_date) AS edu_after,
       (SELECT count(*) FROM employment_record w
         WHERE w.person_id = v.person_id AND w.start_date > p.cutoff_date) AS emp_after
  FROM validation_point p JOIN validation_case v ON v.case_id = p.case_id;
COMMENT ON VIEW v_validation_leakage IS
  '每个预测点"用了多少 cutoff 之后的事实"（按事件时间）。'
  '**必须为 0**：推荐下一步时不能用决策时点之后才发生的事 —— 那是典型的数据泄漏，'
  '会让离线指标虚高而线上无效。'
  '⚠ 本库的记录里 recorded_at 全是同一天（一次性批量入库），所以**只能按事件时间判**；'
  '真实增量采集开始后必须改成按 recorded_at 判。';

-- ---------------------------------------------------------------------------
-- 5. 四项分开的报告视图
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_validation_report AS
SELECT p.point_id, p.case_id, v.person_id, v.nature, p.split, p.cutoff_date,
       p.target_occupation_id, p.target_label, p.in_library,
       (SELECT count(*) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.direction = 'primary') AS n_primary,
       (SELECT count(*) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.direction = 'acceptable') AS n_acceptable,
       (SELECT count(*) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.direction = 'unacceptable') AS n_unacceptable,
       (SELECT count(DISTINCT a.rater) FROM validation_annotation a
         WHERE a.point_id = p.point_id) AS n_raters,
       -- ① 目标职业是否在库（**不是算法的错**，是语料覆盖）
       p.in_library AS aspect1_target_in_library,
       -- ② 资格判定是否正确（只统计"评过的"：NULL 不算不合格）
       (SELECT bool_and(a.qualification_ok) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.qualification_ok IS NOT NULL) AS aspect2_qualification_ok,
       -- ③ 排序是否合理
       (SELECT bool_and(a.ranking_reasonable) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.ranking_reasonable IS NOT NULL) AS aspect3_ranking_reasonable,
       -- ④ 解释是否有证据
       (SELECT bool_and(a.evidence_present) FROM validation_annotation a
         WHERE a.point_id = p.point_id AND a.evidence_present IS NOT NULL) AS aspect4_evidence_present,
       l.edu_after, l.emp_after
  FROM validation_point p
  JOIN validation_case v ON v.case_id = p.case_id
  LEFT JOIN v_validation_leakage l ON l.point_id = p.point_id;
COMMENT ON VIEW v_validation_report IS
  '验证集报告：**四项分开**（目标是否在库 / 资格判定 / 排序 / 解释有据），'
  '**不合成一个总分** —— 因为四者的失败原因完全不同：'
  '目标不在库是**语料覆盖**问题，资格判错是**取数**问题，'
  '排序不合理是**权重**问题，解释无据是**溯源**问题。合成一个分数会把它们混在一起，'
  '于是"该补语料"被误读成"该调算法"。';

-- ---------------------------------------------------------------------------
-- 6. 口径（四项各一条 + 泄漏一条）
-- ---------------------------------------------------------------------------
INSERT INTO metric_registry
  (metric_id, title, domain, kind, numerator_sql, denominator_sql, value_sql,
   unit, time_basis, modifier, definition_note, caveat, min_denominator)
VALUES
('M_VALID_TARGET_IN_LIBRARY_RATE', '验证：目标职业在库率', '结果', 'ratio',
 '(SELECT count(*) FROM validation_point WHERE in_library)',
 '(SELECT count(*) FROM validation_point)', NULL,
 '%', NULL, '目标职业在职业树里**有 JD 样本**的预测点占比',
 '验证集中"真实下一步的职业在库里能查到岗位样本"的比例。'
 '**这项低不是算法问题，是语料覆盖问题** —— 库里没有该职业的岗位，'
 '任何匹配算法都不可能推荐出来（实测冯唐案例：语义更贴的方向在 690 份 JD 里 0 岗位）。',
 '本库真实案例只有 3 人，所以这个比例**没有统计意义**，只能说明"存在这类失败"。'
 '要得到可用结论必须扩大真实案例数。', 1),

('M_VALID_QUALIFICATION_OK_RATE', '验证：资格判定正确率', '结果', 'ratio',
 '(SELECT count(*) FROM validation_annotation WHERE qualification_ok)',
 '(SELECT count(*) FROM validation_annotation WHERE qualification_ok IS NOT NULL)', NULL,
 '%', NULL, '只统计**评过**的标注',
 '评审人认为"资格判定正确"的比例。分母只含**评过这一项的标注** —— '
 'NULL（没评）既不算对也不算错。',
 '把"没评"算成"不合格"是这类报告最常见的错误（会把没做的工作显示成做错了）。'
 '本库真实案例仅 3 人，比例无统计意义。', 1),

('M_VALID_RANKING_REASONABLE_RATE', '验证：排序合理性', '结果', 'ratio',
 '(SELECT count(*) FROM validation_annotation WHERE ranking_reasonable)',
 '(SELECT count(*) FROM validation_annotation WHERE ranking_reasonable IS NOT NULL)', NULL,
 '%', NULL, '只统计**评过**的标注',
 '评审人认为"排序合理"的比例。与"资格判定正确"分开算：'
 '排序合理不代表资格判对（可能把不合格的人排前面但顺序看着顺眼），反之亦然。',
 '本库真实案例仅 3 人，比例无统计意义。', 1),

('M_VALID_EVIDENCE_PRESENT_RATE', '验证：解释有据率', '结果', 'ratio',
 '(SELECT count(*) FROM validation_annotation WHERE evidence_present)',
 '(SELECT count(*) FROM validation_annotation WHERE evidence_present IS NOT NULL)', NULL,
 '%', NULL, '只统计**评过**的标注',
 '评审人认为"解释有证据可溯"的比例。这一项衡量"结果能不能被复查"，'
 '与"结果对不对"是两件事 —— 一个错误结论也可以有完整证据链。',
 '本库真实案例仅 3 人，比例无统计意义。', 1),

('M_VALID_LEAKAGE_POINTS', '验证：存在时间泄漏的预测点数', '结果', 'value',
 '(SELECT 0)', '(SELECT 1)',
 '(SELECT count(*) FROM v_validation_leakage WHERE edu_after > 0 OR emp_after > 0)',
 '个', NULL, '按事件时间判（recorded_at 无历史，见视图说明）',
 '特征里用了决策时点**之后**才发生的事实的预测点数。'
 '**必须为 0**：那是数据泄漏，会让离线指标虚高而线上无效。',
 '本库只能按事件时间判，因为 recorded_at 全是同一天（一次性批量入库）。'
 '真实增量采集开始后必须改成按 recorded_at 判 —— 否则"当时不知道、后来补录"的事实'
 '会悄悄混进特征。', 1)
ON CONFLICT (metric_id) DO UPDATE
  SET numerator_sql = EXCLUDED.numerator_sql,
      denominator_sql = EXCLUDED.denominator_sql,
      definition_note = EXCLUDED.definition_note,
      caveat = EXCLUDED.caveat, status = 'active';

-- ---------------------------------------------------------------------------
-- 7. 域登记 + 授权（065/067 的教训：建表就在同一个迁移里做这两件事）
--
-- ⚠ 写这一节时又踩到一个设计缺口：`domain_registry`（065 建的）里 domain 是**自由文本**，
-- 没有校验。我第一版写了 "验证与发布"，而门户的域名单里只有 "备份与发布" ——
-- 拼错会**静默**变成"未归类"（正是 065 想消灭的那类问题，只是换了个位置）。
-- 所以这里补一张名单表 `domain`，让 `domain_registry.domain` 引用它：
-- **域必须是已登记的**，拼错会当场报外键错误而不是悄悄归类失败。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS domain (
  name  TEXT PRIMARY KEY,
  note  TEXT,
  sort_order INT NOT NULL DEFAULT 100
);
COMMENT ON TABLE domain IS
  '业务域名单（权威）。`domain_registry.domain` 外键引用它 —— '
  '这样"域"不再是自由文本：拼错会当场报错，而不是静默变成"未归类"。'
  '名单与门户 DOMAINS 一致；门户以后也应当读这张表而不是自带一份 Python 清单。';

INSERT INTO domain (name, note, sort_order) VALUES
('字典与语义',   '码值、词表、本体、字段字典', 10),
('岗位与市场',   '岗位、要求、能力权重、来源', 20),
('人才档案',     '人口学、教育、经历、技能、证据', 30),
('演化层',       '职业树生长与候选评审',         40),
('扩展值与观测', '动态属性、观测窗口、断言',     50),
('治理与合规',   '权限、审计、保留、同意、导出', 60),
('小程序接入',   '外部身份、答卷、crosswalk、投递队列', 70),
('备份与发布',   '备份、恢复、数据集发布、验证', 80),
-- ⚠ 这一条是核对时发现漏掉的：门户 DOMAIN_TABLES 里有 **9** 个域，
-- 而显示名单 DOMAINS 只有 **8** 个 —— 于是「分析口径体系」下 **7 张表
-- 既不在任何分组里、也不算"未归类"，在页面上完全看不到**。
-- 这正是本库一贯警惕的「看不到 ≠ 没有」。名单补全它；
-- 门户那边也要让"数据里有、名单里没有"的域**显式显示**而不是静默丢掉。
('分析口径体系', '口径/维度/漏斗注册表、算法注册表、域登记等定义层表', 90)
ON CONFLICT (name) DO UPDATE SET note = EXCLUDED.note, sort_order = EXCLUDED.sort_order;

INSERT INTO domain_registry (table_name, domain, declared_by, note) VALUES
('validation_case', '备份与发布', 'migration_069', '验证案例（人一级），nature 区分 real/synthetic。'),
('validation_point', '备份与发布', 'migration_069', '预测点＝(人, 决策时点)，验证的最小单元。'),
('validation_annotation', '备份与发布', 'migration_069', '多标注：多个评审人、多个合理方向，四项分开评。'),
('domain', '字典与语义', 'migration_069', '业务域名单（域登记的权威来源）。')
ON CONFLICT (table_name) DO UPDATE
  SET domain = EXCLUDED.domain, declared_by = EXCLUDED.declared_by, updated_at = now();

-- 补外键：域必须是已登记的（放在数据补齐之后，避免顺序问题）
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'domain_registry_domain_fk') THEN
    ALTER TABLE domain_registry
      ADD CONSTRAINT domain_registry_domain_fk FOREIGN KEY (domain) REFERENCES domain(name);
    RAISE NOTICE '已给 domain_registry.domain 加外键：域必须是已登记的（拼错会当场报错）';
  END IF;
END $$;

SELECT apply_column_grants();
SELECT apply_role_baseline();

DO $$
DECLARE r record; n int := 0; n_leak int;
BEGIN
  FOR r IN SELECT metric_id FROM metric_registry WHERE metric_id LIKE 'M_VALID\_%' LOOP
    PERFORM * FROM metric_value(r.metric_id);
    n := n + 1;
  END LOOP;
  IF n < 5 THEN RAISE EXCEPTION '验证口径没登记全（%）', n; END IF;

  -- 视图必须可读（新视图忘了对账会连 T3 都读不到，061 踩过）
  IF NOT (has_table_privilege('mt_t3','mt.v_validation_report','SELECT')
       OR has_any_column_privilege('mt_t3','mt.v_validation_report','SELECT')) THEN
    RAISE EXCEPTION '对账后 T3 仍读不到 v_validation_report';
  END IF;

  -- 按人隔离必须是**机制**，不只是约定：试插一条跨集合的点，必须被拒绝
  BEGIN
    INSERT INTO validation_case (case_id, person_id, nature, source)
      VALUES ('__probe_case__', '__probe_person__', 'synthetic', 'self-check')
      ON CONFLICT (case_id) DO NOTHING;
    INSERT INTO validation_point (point_id, case_id, cutoff_date, split, basis)
      VALUES ('__probe_p1__', '__probe_case__', DATE '2020-01-01', 'dev', 'self-check');
    BEGIN
      INSERT INTO validation_point (point_id, case_id, cutoff_date, split, basis)
        VALUES ('__probe_p2__', '__probe_case__', DATE '2021-01-01', 'holdout', 'self-check');
      -- 走到这里说明隔离机制**没生效**
      RAISE EXCEPTION '按人隔离失效：同一个人的预测点被允许跨 dev/holdout';
    EXCEPTION
      WHEN raise_exception THEN
        IF SQLERRM LIKE '%按人隔离被破坏%' THEN
          RAISE NOTICE '按人隔离自检通过：跨集合插入被拒绝';
        ELSE
          RAISE;
        END IF;
    END;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '按人隔离自检跳过（%）：%', SQLSTATE, SQLERRM;
  END;

  -- 清理自检数据
  DELETE FROM validation_case WHERE case_id = '__probe_case__';

  -- 泄漏检测
  SELECT count(*) INTO n_leak FROM v_validation_leakage
   WHERE edu_after > 0 OR emp_after > 0;
  RAISE NOTICE '验证集就绪：3 张表 + 2 个视图 + 5 个口径；'
    '当前泄漏预测点 % 个（尚无预测点时为空）', n_leak;
END $$;

COMMIT;
