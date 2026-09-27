-- ===========================================================================
-- ops/fixtures/check_talent.sql —— mock 人才档案验收查询（只读）
--
-- 跑法：python ops\pg.py sql ops\fixtures\check_talent.sql
-- 说明：本文件含中文，必须走文件执行——`psql -c "中文"` 在 Windows ANSI argv 下会乱码。
-- ===========================================================================
\pset border 2

\echo ''
\echo '=== 1. 每张人才侧表的行数（per_mock_% only / 全表）==='
SELECT t.table_name AS 表,
       (xpath('/row/c/text()', query_to_xml(
            format('SELECT count(*) AS c FROM mt.%I WHERE person_id LIKE ''per_mock_%%''', t.table_name),
            false, true, '')))[1]::text::bigint AS mock行数,
       (xpath('/row/c/text()', query_to_xml(
            format('SELECT count(*) AS c FROM mt.%I', t.table_name),
            false, true, '')))[1]::text::bigint AS 全表行数
FROM (VALUES ('person'), ('person_demographics'), ('education_record'),
             ('employment_record'), ('clinical_exposure'), ('project_record'),
             ('research_output'), ('award_honor'), ('credential'), ('assessment'),
             ('skill_assertion'), ('evidence'), ('preference'),
             ('observation_window'), ('consent_record')) AS t(table_name);

\echo ''
\echo '=== 2a. 学历分布（每人最高学历）==='
SELECT degree_level AS 学历, count(*) AS 人数,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS 占比
FROM (SELECT DISTINCT ON (person_id) person_id, degree_level
      FROM mt.education_record WHERE person_id LIKE 'per_mock_%'
      ORDER BY person_id, degree_level DESC) t
GROUP BY degree_level ORDER BY degree_level;

\echo ''
\echo '=== 2b. 专业方向分布（Top 10）==='
SELECT major_code AS 专业代码, major_raw AS 专业方向,
       count(DISTINCT person_id) AS 人数
FROM mt.education_record WHERE person_id LIKE 'per_mock_%'
GROUP BY 1, 2 ORDER BY 人数 DESC, 1 LIMIT 10;

\echo ''
\echo '=== 3. skill_assertion 覆盖的不同 concept_id 数（要求 >= 25）==='
SELECT count(DISTINCT concept_id) AS 不同概念数,
       count(*) AS 主张总数,
       count(DISTINCT person_id) AS 覆盖人数,
       round(avg(level), 2) AS 平均等级
FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%';

\echo ''
\echo '=== 4a. 证据等级 cel_level 分布（E0 自述 … E4 履职记录）==='
SELECT cel_level AS 证据分级, count(*) AS 条数,
       count(DISTINCT person_id) AS 涉及人数
FROM mt.evidence WHERE person_id LIKE 'per_mock_%'
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 4b. level_basis × cel_level 交叉（证据口径是否自洽）==='
SELECT sa.level_basis AS 等级依据, ev.cel_level AS 证据分级,
       count(*) AS 条数, round(avg(sa.confidence), 3) AS 平均置信度
FROM mt.skill_assertion sa JOIN mt.evidence ev USING (evidence_id)
WHERE sa.person_id LIKE 'per_mock_%'
GROUP BY 1, 2 ORDER BY 1, 2;

\echo ''
\echo '=== 5. mock 人才数（应约等于 120）==='
SELECT count(*) AS mock_person FROM mt.person WHERE person_id LIKE 'per_mock_%';

\echo ''
\echo '=== 7. 非 mock 人才行数（操作前后必须不变；基线 = 17）==='
SELECT count(*) AS non_mock_person FROM mt.person WHERE person_id NOT LIKE 'per_mock_%';

\echo ''
\echo '=== 附加 1：技能供给 Top 10（不同人数）==='
SELECT sa.concept_id AS 概念, co.preferred_label AS 名称, co.concept_type AS 类型,
       count(DISTINCT sa.person_id) AS 人数, round(avg(sa.level), 2) AS 平均等级,
       round(avg(sa.transferability), 2) AS 平均可迁移性
FROM mt.skill_assertion sa JOIN mt.concept co USING (concept_id)
WHERE sa.person_id LIKE 'per_mock_%'
GROUP BY 1, 2, 3 ORDER BY 人数 DESC, 1 LIMIT 10;

\echo ''
\echo '=== 附加 2：各专业方向的最强概念（技能分布是否有方向差异）==='
SELECT DISTINCT ON (major_raw) major_raw AS 专业方向, concept_id AS 最强概念,
       preferred_label AS 名称, 人数
FROM (
    SELECT e.major_raw, sa.concept_id, co.preferred_label,
           count(DISTINCT sa.person_id) AS 人数
    FROM mt.skill_assertion sa
    JOIN mt.person p USING (person_id)
    JOIN (SELECT DISTINCT ON (person_id) person_id, major_raw, major_code
          FROM mt.education_record WHERE person_id LIKE 'per_mock_%'
          ORDER BY person_id, degree_level DESC) e USING (person_id)
    JOIN mt.concept co USING (concept_id)
    WHERE sa.person_id LIKE 'per_mock_%'
    GROUP BY 1, 2, 3
) x
ORDER BY major_raw, 人数 DESC;

\echo ''
\echo '=== 附加 3：岗位族偏好与技能方向不一致的人数（缺口分析素材）==='
WITH home(major_code, home_families) AS (VALUES
    ('M10001', ARRAY['F01','F04','F10']), ('M600',  ARRAY['F01','F15','F04']),
    ('M30001', ARRAY['F02','F05','F14']), ('M30002', ARRAY['F02','F01','F04']),
    ('M40001', ARRAY['F09','F04','F02']), ('M40002', ARRAY['F04','F08','F16']),
    ('M20001', ARRAY['F05','F10','F02']), ('M20002', ARRAY['F03','F08','F05']),
    ('M500',   ARRAY['F01','F15','F13']), ('M800',   ARRAY['F01','F03','F15']),
    ('M10003', ARRAY['F01','F08','F03']), ('M10002', ARRAY['F01','F03','F04'])),
maj AS (
    SELECT DISTINCT ON (person_id) person_id, major_code
    FROM mt.education_record WHERE person_id LIKE 'per_mock_%'
    ORDER BY person_id, degree_level DESC)
SELECT CASE WHEN pr.value_code = ANY(h.home_families) THEN '一致' ELSE '不一致' END AS 偏好一致性,
       count(*) AS 人数
FROM mt.preference pr
JOIN maj m USING (person_id) JOIN home h USING (major_code)
WHERE pr.pref_type = 'PF3' AND pr.person_id LIKE 'per_mock_%'
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 附加 4：主张越界率（docs/04 质量指标：level>=4 且 basis=LB1 必须为 0）==='
SELECT count(*) AS 越界条数
FROM mt.skill_assertion
WHERE person_id LIKE 'per_mock_%' AND level >= 4 AND level_basis = 'LB1';

\echo ''
\echo '=== 附加 5：能力主张有证据率（docs/04 阈值 >= 70%）==='
SELECT round(100.0 * count(evidence_id) / count(*), 1) AS 有证据率百分比
FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%';

\echo ''
\echo '=== 附加 6：transferability 填写率与 >=3 时的说明完整性 ==='
SELECT transferability AS 可迁移性, count(*) AS 条数,
       count(*) FILTER (WHERE transfer_note IS NOT NULL) AS 有说明
FROM mt.skill_assertion WHERE person_id LIKE 'per_mock_%'
GROUP BY 1 ORDER BY 1;
