-- 确认口径/分类/漏斗要用到的列真的存在（口径不能凭空发明）
SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position) AS 列
  FROM information_schema.columns
 WHERE table_schema='mt'
   AND table_name IN ('person','person_demographics','education_record',
                      'skill_assertion','preference','match_result','match_run',
                      'employment_record','job_posting','consent_record')
 GROUP BY table_name ORDER BY table_name;
