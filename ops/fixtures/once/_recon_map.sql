-- 把"字段策略"翻译成"物理列授权"需要知道：字段 → 表.列 怎么映射
SELECT 'field_catalog 列' AS k, string_agg(column_name, ', ' ORDER BY ordinal_position) AS v
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='field_catalog'
UNION ALL SELECT 'entity_catalog 列', string_agg(column_name, ', ' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='mt' AND table_name='entity_catalog';

-- 抽样看 field_catalog 里到底有没有"物理列名"
SELECT field_id, entity_id, title, data_type, access_tier, related_fields, applies_to
  FROM mt.field_catalog ORDER BY field_id LIMIT 12;

-- entity → 表名 的映射全不全
SELECT e.entity_id, e.table_name, count(f.field_id) AS 字段数
  FROM mt.entity_catalog e LEFT JOIN mt.field_catalog f ON f.entity_id = e.entity_id
 GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;

-- 已有的 10 条策略长什么样（这是"用户等级"的现有表达）
SELECT policy_id, object_type, object_id, action, min_tier, condition_note
  FROM mt.access_policy ORDER BY object_id, action;
