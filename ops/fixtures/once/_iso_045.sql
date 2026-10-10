-- 隔离 045 断言里那条 SELECT 的报错
DO $$
DECLARE v RECORD;
BEGIN
  FOR v IN
    SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'mt'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.objid = p.oid AND d.deptype = 'e')
       AND has_function_privilege('public', p.oid, 'EXECUTE')
     LIMIT 3
  LOOP
    RAISE NOTICE '命中：%', v.proname;
  END LOOP;
END $$;
