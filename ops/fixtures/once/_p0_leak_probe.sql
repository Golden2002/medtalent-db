-- 复现独立审查报告里的 P0-1：匿名能否枚举全库 person_id、并拿到精确频次？
-- 全部以 mt_t0 身份执行。

-- ① 有多少张表的 person_id 是 T0？
SELECT '① person_id 为 T0 的表数' AS 检查,
       count(*)::text AS 结果
  FROM mt.column_policy
 WHERE column_name = 'person_id' AND min_tier = 'T0';

SELECT table_name, min_tier FROM mt.column_policy
 WHERE column_name = 'person_id' AND min_tier = 'T0'
 ORDER BY table_name LIMIT 12;

-- ② 公开的列级剖析视图里，有没有把 person_id 的 Top-K 取值交出去？
SELECT table_name, column_name, left(top_values::text, 120) AS 取值样例
  FROM mt.v_column_profile_public
 WHERE column_name = 'person_id'
 ORDER BY table_name LIMIT 12;
