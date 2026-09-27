\pset border 2
\echo '=== 未被任何 mock 主张覆盖的 concept（对照 mt.concept 全量）==='
SELECT c.concept_id, c.concept_type, c.preferred_label
FROM mt.concept c
WHERE c.status = 'active'
  AND NOT EXISTS (SELECT 1 FROM mt.skill_assertion sa
                  WHERE sa.concept_id = c.concept_id
                    AND sa.person_id LIKE 'per_mock_%')
ORDER BY c.concept_id;

\echo ''
\echo '=== v_skill_supply：mock 人才带来的技能供给（Top 12，含岗位侧需求对照）==='
SELECT s.concept_id, s.preferred_label, s.concept_type, s.person_count,
       round(s.avg_level, 2) AS avg_level, d.posting_count, d.family_count
FROM mt.v_skill_supply s
LEFT JOIN mt.v_skill_demand d USING (concept_id)
WHERE s.person_count > 0
ORDER BY s.person_count DESC LIMIT 12;

\echo ''
\echo '=== v_gap_candidates：窗口内的能力缺口候选（mock 人才）==='
SELECT count(*) AS 缺口候选条数, count(DISTINCT person_id) AS 涉及人数
FROM mt.v_gap_candidates WHERE person_id LIKE 'per_mock_%';
