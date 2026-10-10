-- 清理实验残留（角色有依赖，必须先撤销所有授权）
BEGIN;
REVOKE ALL ON mt.person FROM lab_reader;
REVOKE ALL ON SCHEMA mt FROM lab_reader;
REVOKE lab_reader FROM lab_login;
REVOKE ALL ON SCHEMA mt FROM lab_login;
DROP TABLE IF EXISTS lab_rls CASCADE;
DROP ROLE IF EXISTS lab_login;
DROP ROLE IF EXISTS lab_reader;
COMMIT;
SELECT rolname FROM pg_roles WHERE rolname LIKE 'lab\_%';
