-- ============================================================================
-- 冒烟测试：用"真实形状"的数据走一遍全链路（事务内执行，最终 ROLLBACK）
-- 验证：多表关联可插入、视图可查询、观测期可建、能力主张与证据可挂接。
-- 用法：python ops/pg.py sql ops/tests/smoke_schema.sql
-- ============================================================================
SET search_path TO mt, public;
BEGIN;

-- 1) 语义层：概念必须先存在
--    skill_assertion.concept_id 与 job_requirement.concept_id 都是指向 concept 的外键，
--    这正是"能力/要求必须落到受控概念上"在数据库层的强制。
INSERT INTO concept (concept_id, concept_type, preferred_label, label_en, definition,
                     level, reusability, status)
VALUES
 ('con_demo_lit_review', 'K1', '文献检索与证据分级', 'Literature Review & Evidence Grading',
  '快速定位相关文献并评估其证据质量', 1, 'RU2', 'active'),
 ('con_demo_evid_comm', 'K3', '循证信息传递', 'Evidence Communication',
  '面向专业人士准确、无偏地传递研究证据', 1, 'RU1', 'active');

-- 2) 来源登记
INSERT INTO source_registry (source_id, name, source_type, base_url, license_note,
                             credibility, evidence_grade, status)
VALUES ('src_demo_careers', '示例企业招聘页', 'SRC2', 'https://example.com/careers',
        '示例数据，仅用于冒烟测试', 0.8, 'C', 'active');

-- 2) 岗位侧：一条真实形状的 JD
--    注意：evidence_grade 不落在 job_posting 上，而是落在 provenance（逐条血缘）。
INSERT INTO job_posting (job_id, source_id, source_url, employer_name_raw,
                         title_raw, title_normalized, job_family, is_campus,
                         city, salary_min, salary_max, salary_period, education_req,
                         raw_text_ref, raw_sha256, parse_version, confidence,
                         verify_status)
VALUES ('job_demo_msl', 'src_demo_careers', 'https://example.com/careers/req-1',
        '某跨国制药企业', '医学科学联络官 MSL - 免疫治疗', '医学科学联络官（MSL）',
        'F02', false, '上海', 300000, 450000, 'SP2', 'D3',
        'L0://demo/msl.html', 'demo_sha256_msl', 'jd_parser@0.1', 0.78, 'V1');

INSERT INTO provenance (provenance_id, record_uid, source_id, source_url, fetched_at,
                        extract_method, extractor_version, evidence_grade, human_verified)
VALUES ('prv_demo_1', 'job_posting:job_demo_msl', 'src_demo_careers',
        'https://example.com/careers/req-1', now(), 'llm_extract',
        'jd_parser@0.1', 'C', false);

INSERT INTO job_task (task_id, job_id, task_order, task_text, importance) VALUES
 ('jbt_demo_1', 'job_demo_msl', 1, '与领域专家建立学术合作关系，开展科学交流', 0.90),
 ('jbt_demo_2', 'job_demo_msl', 2, '向临床医生传递最新临床研究数据与循证信息', 0.95);

INSERT INTO job_requirement (requirement_id, job_id, requirement_kind, requirement_type,
                             concept_id, raw_text, min_level, essentiality, substitutable_by) VALUES
 ('req_demo_1','job_demo_msl','RK1','RT1', NULL,
  '医学相关专业硕士及以上学历', NULL, 1.0, '[]'::jsonb),
 ('req_demo_2','job_demo_msl','RK1','RT5', 'con_demo_lit_review',
  '能快速阅读并准确解读英文文献与临床试验数据', 4, 0.95, '[]'::jsonb),
 ('req_demo_3','job_demo_msl','RK2','RT3', NULL,
  '有临床工作或医药行业经验者优先', NULL, 0.6, '["博士期间深度参与临床研究"]'::jsonb);

-- 3) 人才侧：一份档案 + 学历 + 观测窗口 + 证据 + 能力主张
INSERT INTO person (person_id, subject_code, verify_status, access_tier)
VALUES ('per_demo_001', 'MT-2026-DEMO1', 'V2', 'T1');

INSERT INTO education_record (education_id, person_id, degree_level, degree_name,
                              school_name, major_raw, is_clinical, start_date, end_date,
                              is_graduated, verify_status)
VALUES ('edu_demo_1', 'per_demo_001', 'D3', '临床医学硕士（专业型）',
        '某医科大学', '外科学', true, DATE '2022-09-01', DATE '2025-06-30',
        true, 'V2');

INSERT INTO observation_window (window_id, person_id, window_type, start_date, end_date, coverage_note)
VALUES ('ow_demo_1', 'per_demo_001', 'W1', DATE '2017-09-01', DATE '2025-06-30',
        '覆盖本科至硕士毕业的完整教育与培养经历');

INSERT INTO evidence (evidence_id, person_id, evidence_type, title, uri,
                      verifiability, cel_level, access_tier)
VALUES ('evd_demo_1', 'per_demo_001', 'EV2', '一作论文 DOI:10.1000/demo',
        'https://doi.org/10.1000/demo', 5, 'E3', 'T0');

INSERT INTO skill_assertion (assertion_id, person_id, concept_id, level, level_basis,
                             claim_type, evidence_id, confidence, transferability,
                             transfer_note, verify_status)
VALUES ('skl_demo_1', 'per_demo_001', 'con_demo_lit_review', 4, 'LB4', 'CT1',
        'evd_demo_1', 0.85, 4,
        '能在海量文献中定位证据并评估质量，等价于行业研究中的信息检索与证据分级能力',
        'V3');

-- 4) 验证查询
DO $$
DECLARE n int; t text;
BEGIN
  SELECT count(*) INTO n FROM job_requirement WHERE job_id='job_demo_msl';
  IF n <> 3 THEN RAISE EXCEPTION 'FAIL: 岗位要求条数应为 3，实际 %', n; END IF;
  RAISE NOTICE 'PASS A: 岗位要求 3 条已入库';

  SELECT count(*) INTO n FROM job_task WHERE job_id='job_demo_msl';
  IF n <> 2 THEN RAISE EXCEPTION 'FAIL: 职责条数应为 2，实际 %', n; END IF;
  RAISE NOTICE 'PASS B: 岗位职责 2 条已入库';

  -- 视图可用性
  SELECT count(*) INTO n FROM v_skill_supply;
  RAISE NOTICE 'PASS C: 视图 v_skill_supply 可查询（% 行）', n;
  SELECT count(*) INTO n FROM v_skill_demand;
  RAISE NOTICE 'PASS D: 视图 v_skill_demand 可查询（% 行）', n;
  SELECT count(*) INTO n FROM v_gap_candidates;
  RAISE NOTICE 'PASS E: 视图 v_gap_candidates 可查询（% 行）', n;

  -- 观测期纪律：此人窗口覆盖其全部经历
  SELECT window_type INTO t FROM observation_window WHERE person_id='per_demo_001';
  IF t IS NULL THEN RAISE EXCEPTION 'FAIL: 未建立观测窗口'; END IF;
  RAISE NOTICE 'PASS F: 观测窗口已建立（%）', t;

  -- 原文可回溯：每条要求都必须带 raw_text
  SELECT count(*) INTO n FROM job_requirement WHERE raw_text IS NULL OR raw_text='';
  IF n <> 0 THEN RAISE EXCEPTION 'FAIL: 有 % 条要求缺原文', n; END IF;
  RAISE NOTICE 'PASS G: 岗位要求原文可回溯率 100%%';
END $$;

-- 5) 关联查询演示（客户会问的那类问题）
SELECT p.subject_code, e.degree_name, sa.level, sa.transferability,
       left(sa.transfer_note, 24) AS transfer_note_head
FROM person p
JOIN education_record e ON e.person_id = p.person_id
JOIN skill_assertion sa ON sa.person_id = p.person_id
WHERE p.person_id = 'per_demo_001';

SELECT jp.title_raw, jr.requirement_kind, jr.requirement_type,
       jr.raw_text, jr.substitutable_by
FROM job_posting jp JOIN job_requirement jr ON jr.job_id = jp.job_id
WHERE jp.job_id = 'job_demo_msl' ORDER BY jr.requirement_id;

ROLLBACK;

DO $$ BEGIN RAISE NOTICE '=== 冒烟测试全部通过（已回滚，无残留）==='; END $$;
