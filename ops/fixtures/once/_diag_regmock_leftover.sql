-- 1) 前几次失败运行留下的测试数据（这就是 M7 基线被污染的原因）
SELECT ei.external_person_id, ei.person_id, ei.status, ei.last_seen_version,
       (SELECT count(*) FROM mt.tombstone t WHERE t.person_id = ei.person_id) AS 墓碑,
       (SELECT count(*) FROM mt.talent_deletion_request r WHERE r.person_id = ei.person_id) AS 注销,
       (SELECT count(*) FROM mt.consent_record cr WHERE cr.person_id = ei.person_id) AS 同意
  FROM mt.external_identity ei
 WHERE ei.source_system = 'regmock'
 ORDER BY ei.linked_at;

-- 2) profile_json 到底返回什么类型
SELECT jsonb_typeof(mt.profile_json('person', p.person_id)) AS 类型,
       left(mt.profile_json('person', p.person_id)::text, 160) AS 片段
  FROM mt.person p
 WHERE p.person_id IN (SELECT person_id FROM mt.external_identity WHERE source_system='regmock')
 LIMIT 2;
