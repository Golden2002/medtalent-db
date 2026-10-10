-- 清理界面验收测试留下的数据（含审计），用完即净
BEGIN;
SET search_path TO mt, public;

-- 职业演示行 + 它们的审计
DELETE FROM change_log WHERE object_name IN ('OCC-F02-96','OCC-F02-98','OCC-F02-99');
DELETE FROM occupation WHERE occupation_id IN ('OCC-F02-96','OCC-F02-98','OCC-F02-99');
-- 界面导入留下的审计
DELETE FROM change_log WHERE object_name = 'xlsimport'
   AND detail->>'via' = 'portal_admin';

-- 报告清理结果
SELECT (SELECT count(*) FROM occupation WHERE occupation_id LIKE 'OCC-F02-9%') AS 剩余演示职业,
       (SELECT count(*) FROM change_log WHERE object_name = 'xlsimport') AS 剩余导入审计;
COMMIT;
