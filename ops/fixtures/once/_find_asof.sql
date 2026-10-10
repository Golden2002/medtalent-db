-- occupation_asof 的定义与所有调用点
SELECT '函数定义' AS 位置, p.proname, p.prosecdef AS definer,
       left(pg_get_functiondef(p.oid), 400) AS 片段
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname='mt' AND p.proname = 'occupation_asof'
UNION ALL
SELECT '视图里用到', v.viewname, false, left(v.definition, 300)
  FROM pg_views v
 WHERE v.schemaname='mt' AND v.definition LIKE '%occupation_asof%'
UNION ALL
SELECT '函数体里用到', p.proname, p.prosecdef, left(pg_get_functiondef(p.oid), 200)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname='mt' AND p.proname <> 'occupation_asof'
   AND pg_get_functiondef(p.oid) LIKE '%occupation_asof%';

-- /tree 页面会查哪些视图/表？先看 occupation 相关的公开视图属主
SELECT c.relname AS 视图, pg_get_userbyid(c.relowner) AS 属主,
       has_table_privilege('mt_t3', c.oid, 'SELECT') AS t3可读
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='mt' AND c.relkind='v' AND c.relname LIKE '%occupation%';
