-- 现状侦察：访问控制与审计相关的既有资产
SELECT 'version' AS k, version() AS v
UNION ALL SELECT 'db', current_database() || ' / ' || current_user
UNION ALL SELECT 'tables', count(*)::text FROM information_schema.tables
        WHERE table_schema='mt' AND table_type='BASE TABLE'
UNION ALL SELECT 'views', count(*)::text FROM information_schema.views WHERE table_schema='mt';

-- 1) 已有的"访问策略"类表
SELECT table_name, (SELECT count(*) FROM information_schema.columns c
                     WHERE c.table_schema='mt' AND c.table_name=t.table_name) AS cols
  FROM information_schema.tables t
 WHERE t.table_schema='mt'
   AND (t.table_name ILIKE '%access%' OR t.table_name ILIKE '%policy%'
        OR t.table_name ILIKE '%consent%' OR t.table_name ILIKE '%audit%'
        OR t.table_name ILIKE '%release%' OR t.table_name ILIKE '%log%'
        OR t.table_name ILIKE '%role%' OR t.table_name ILIKE '%user%'
        OR t.table_name ILIKE '%subject_request%' OR t.table_name ILIKE '%tombstone%')
 ORDER BY 1;

-- 2) 数据库里已有哪些角色（非系统）
SELECT rolname, rolsuper, rolcanlogin, rolbypassrls
  FROM pg_roles WHERE rolname NOT LIKE 'pg\_%' ORDER BY rolname;

-- 3) access_tier 在字段字典里的分布（这是"每个字段权限不同"的现有抓手）
SELECT access_tier, count(*) FROM mt.field_catalog GROUP BY 1 ORDER BY 2 DESC;

-- 4) 哪些表有 access_tier 列，分布如何（抽样）
SELECT 'person' AS t, access_tier, count(*) FROM mt.person GROUP BY 2
UNION ALL SELECT 'job_posting', access_tier, count(*) FROM mt.job_posting GROUP BY 2;
