-- 审计触发器：函数是谁、有没有 SECURITY DEFINER
SELECT t.tgname AS 触发器, c.relname AS 表,
       p.proname AS 函数, p.prosecdef AS 是SECURITY_DEFINER,
       coalesce(array_to_string(p.proconfig, ','), '(无 search_path)') AS proconfig
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_proc p ON p.oid = t.tgfoid
 WHERE n.nspname='mt' AND NOT t.tgisinternal
   AND c.relname IN ('person','external_identity','answer','consent_record','sync_event')
 ORDER BY c.relname
 LIMIT 12;

-- 所有会写 change_log 的函数
SELECT p.proname, p.prosecdef,
       pg_get_functiondef(p.oid) LIKE '%change_log%' AS 写审计表
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='mt' AND pg_get_functiondef(p.oid) LIKE '%change_log%'
 ORDER BY 1 LIMIT 10;
