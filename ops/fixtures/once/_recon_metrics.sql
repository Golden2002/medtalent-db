-- 侦查：本库到底能算出哪些口径与漏斗（口径不能凭空发明，必须可执行）
-- 只看"供给（人）— 需求（岗位）— 匹配 — 结果"这条业务路径上的表
SELECT c.relname AS 表名,
       c.reltuples::bigint AS 估行数,
       obj_description(c.oid, 'pg_class') AS 说明
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mt' AND c.relkind = 'r'
   AND c.relname ~ 'person|match|job|employ|skill|assert|education|experience|prefer|consent|session|source|evidence'
 ORDER BY c.reltuples DESC;
