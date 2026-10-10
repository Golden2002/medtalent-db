-- 动作级禁令（export_row = X）到底约束哪些物理列？现在有没有强制点？
SELECT ap.policy_id, ap.object_id, ap.action, ap.min_tier,
       f.title AS 字段中文名,
       e.table_name AS 实体表,
       lower(regexp_replace(f.field_id, '^F_[A-Za-z0-9]+_', '')) AS 约定列名,
       -- 该约定列名在该表里是否真的存在物理列
       EXISTS (SELECT 1 FROM information_schema.columns c
                WHERE c.table_schema='mt' AND c.table_name=e.table_name
                  AND lower(c.column_name) =
                      lower(regexp_replace(f.field_id, '^F_[A-Za-z0-9]+_', ''))) AS 物理列存在
  FROM mt.access_policy ap
  LEFT JOIN mt.field_catalog f ON f.field_id = ap.object_id
  LEFT JOIN mt.entity_catalog e ON e.entity_id = f.entity_id
 WHERE ap.action = 'export_row'
 ORDER BY ap.object_id;

-- 门户里所有能导出 CSV 的入口（人工核对用）
SELECT 'access_policy 动作分布' AS k,
       string_agg(DISTINCT action || '=' || min_tier, ', ') AS v
  FROM mt.access_policy
UNION ALL
SELECT '列级策略里的 X 数', count(*)::text FROM mt.column_policy WHERE min_tier='X'
UNION ALL
SELECT 'column_profile（表级给 T3）', count(*)::text FROM mt.column_policy
        WHERE table_name='column_profile' AND min_tier='X';
