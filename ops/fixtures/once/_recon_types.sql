-- 确认口径要用到的字段类型与取值（写口径前必须核对，否则一跑就报错）
SELECT 'education_record.overseas' AS 项, data_type AS 类型 FROM information_schema.columns
 WHERE table_schema='mt' AND table_name='education_record' AND column_name='overseas'
UNION ALL SELECT 'job_posting.is_campus', data_type FROM information_schema.columns
 WHERE table_schema='mt' AND table_name='job_posting' AND column_name='is_campus'
UNION ALL SELECT 'match_result.target_type 取值', string_agg(DISTINCT target_type, ',')
 FROM mt.match_result
UNION ALL SELECT 'person_demographics.age_band 取值', string_agg(DISTINCT coalesce(age_band,'NULL'), ',')
 FROM mt.person_demographics
UNION ALL SELECT 'education_record.overseas 取值', string_agg(DISTINCT coalesce(overseas::text,'NULL'), ',')
 FROM mt.education_record
UNION ALL SELECT '岗位被匹配的 target_type', string_agg(DISTINCT target_type, ',')
 FROM mt.match_result WHERE target_type = 'job_posting'
UNION ALL SELECT 'consent_record 已撤回数', count(*)::text FROM mt.consent_record WHERE revoked_at IS NOT NULL
UNION ALL SELECT 'skill_assertion 覆盖人数', count(DISTINCT person_id)::text FROM mt.skill_assertion
UNION ALL SELECT 'match_result 覆盖人数', count(DISTINCT person_id)::text FROM mt.match_result
UNION ALL SELECT 'employment_record 覆盖人数', count(DISTINCT person_id)::text FROM mt.employment_record;
