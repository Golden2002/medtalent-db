-- mock 注册窗口需要的既有资产
SELECT 'talent_deletion_request' AS t, string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position) AS cols
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='talent_deletion_request'
UNION ALL SELECT 'external_identity', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='external_identity'
UNION ALL SELECT 'consent_record', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='consent_record'
UNION ALL SELECT 'experience_episode', string_agg(column_name||':'||data_type, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='experience_episode';

-- profile_json 的签名（门户用它读回画像）
SELECT p.proname, pg_get_function_arguments(p.oid) AS args, pg_get_function_result(p.oid) AS ret
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='mt' AND p.proname IN ('profile_json','delete_instance','form_schema','add_dimension')
 ORDER BY 1;

-- 现有数据规模（用来做"写前/写后"对照）
SELECT 'person' AS t, count(*) FROM mt.person
UNION ALL SELECT 'external_identity', count(*) FROM mt.external_identity
UNION ALL SELECT 'field_value', count(*) FROM mt.field_value
UNION ALL SELECT 'tombstone', count(*) FROM mt.tombstone
UNION ALL SELECT 'talent_deletion_request', count(*) FROM mt.talent_deletion_request
UNION ALL SELECT 'consent_record', count(*) FROM mt.consent_record;
