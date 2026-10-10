-- consent_record 是否有孤儿行（指向 person 里不存在的人）
SELECT 'consent_record 有有效授权的去重人数' AS 口径, count(DISTINCT person_id)::text AS 值
  FROM mt.consent_record WHERE revoked_at IS NULL
UNION ALL SELECT '其中 person 里存在的',
  count(DISTINCT c.person_id)::text FROM mt.consent_record c
   WHERE c.revoked_at IS NULL AND EXISTS (SELECT 1 FROM mt.person p WHERE p.person_id=c.person_id)
UNION ALL SELECT '**孤儿行（指向不存在的人）**',
  count(DISTINCT c.person_id)::text FROM mt.consent_record c
   WHERE c.revoked_at IS NULL AND NOT EXISTS (SELECT 1 FROM mt.person p WHERE p.person_id=c.person_id)
UNION ALL SELECT 'consent_record 总行数', count(*)::text FROM mt.consent_record
UNION ALL SELECT 'consent_record 孤儿行数',
  count(*)::text FROM mt.consent_record c
   WHERE NOT EXISTS (SELECT 1 FROM mt.person p WHERE p.person_id=c.person_id);

\echo '=== consent_record 有没有外键？ ==='
SELECT conname, pg_get_constraintdef(oid) AS 定义
  FROM pg_constraint WHERE conrelid='mt.consent_record'::regclass;

\echo '=== 全库孤儿体检：每张引用 person 的表 ==='
SELECT t.table_name AS 表, t.col AS 列,
       (SELECT count(*) FROM mt.consent_record c WHERE NOT EXISTS (SELECT 1 FROM mt.person p WHERE p.person_id=c.person_id)) AS 孤儿_consent_参考
  FROM (SELECT 'consent_record' AS table_name, 'person_id' AS col) t
 WHERE false;
