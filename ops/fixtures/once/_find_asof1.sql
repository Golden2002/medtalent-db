-- occupation_asof 的定义与所有调用点（避开 UNION 的类型推断问题，分四条查）
SELECT p.proname AS 函数, p.prosecdef AS definer,
       left(pg_get_functiondef(p.oid), 500) AS 定义片段
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname='mt' AND p.proname = 'occupation_asof';
