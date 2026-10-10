-- 真实行数（不用 reltuples：它是估算，会骗人）+ 匹配链路结构
SELECT 'person' AS 表, count(*) AS 行数 FROM mt.person
UNION ALL SELECT 'person_demographics', count(*) FROM mt.person_demographics
UNION ALL SELECT 'education_record',   count(*) FROM mt.education_record
UNION ALL SELECT 'experience_episode', count(*) FROM mt.experience_episode
UNION ALL SELECT 'skill_assertion',    count(*) FROM mt.skill_assertion
UNION ALL SELECT 'evidence',           count(*) FROM mt.evidence
UNION ALL SELECT 'preference',         count(*) FROM mt.preference
UNION ALL SELECT 'consent_record',     count(*) FROM mt.consent_record
UNION ALL SELECT 'job_posting',        count(*) FROM mt.job_posting
UNION ALL SELECT 'job_requirement',    count(*) FROM mt.job_requirement
UNION ALL SELECT 'match_run',          count(*) FROM mt.match_run
UNION ALL SELECT 'match_result',       count(*) FROM mt.match_result
UNION ALL SELECT 'employment_record',  count(*) FROM mt.employment_record
ORDER BY 2 DESC;

-- match_run 与 match_result 的列（漏斗的骨架在这里）
SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position) AS 列
  FROM information_schema.columns
 WHERE table_schema='mt' AND table_name IN ('match_run','match_result','employment_record')
 GROUP BY table_name;
