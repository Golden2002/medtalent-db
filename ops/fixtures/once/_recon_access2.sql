-- 关键治理表的结构与现有内容（决定实施方案是"接线"还是"新建"）
SELECT 'access_log' AS t, string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position) AS cols
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='access_log'
UNION ALL SELECT 'access_policy', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='access_policy'
UNION ALL SELECT 'consent_record', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='consent_record'
UNION ALL SELECT 'subject_request', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='subject_request'
UNION ALL SELECT 'tombstone', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='tombstone'
UNION ALL SELECT 'dataset_release', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='dataset_release';

-- 现有内容
SELECT 'access_log 行数' AS k, count(*)::text AS v FROM mt.access_log
UNION ALL SELECT 'access_policy 行数', count(*)::text FROM mt.access_policy
UNION ALL SELECT 'access_policy 样本', coalesce(string_agg(DISTINCT row_to_json(p)::text, ' | '), '(空)')
        FROM (SELECT * FROM mt.access_policy LIMIT 4) p
UNION ALL SELECT 'consent_record 行数', count(*)::text FROM mt.consent_record
UNION ALL SELECT 'tombstone 行数', count(*)::text FROM mt.tombstone
UNION ALL SELECT 'subject_request 行数', count(*)::text FROM mt.subject_request
UNION ALL SELECT 'dataset_release 行数', count(*)::text FROM mt.dataset_release
UNION ALL SELECT 'RLS 已启用的表数', count(*)::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='mt' AND c.relrowsecurity;
