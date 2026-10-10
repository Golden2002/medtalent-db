-- 实验：列级权限到底能强制到什么程度？(全程在事务里，结束 ROLLBACK，不留痕)
-- 要回答四个问题：
--   1) 只给部分列的 SELECT 权限后，SELECT * 会怎样？
--   2) 没有列权限时，count(*) 还能不能算？（这决定"数量统计"能不能对受限角色开放）
--   3) 表属主/超级用户是否绕过 RLS？（本项目门户现在用 postgres 连接，这是关键）
--   4) SET LOCAL ROLE 能否在会话内切换身份？（多租户/多等级共用一个连接池的做法）
BEGIN;

CREATE ROLE lab_reader NOLOGIN;
CREATE ROLE lab_login LOGIN;
GRANT lab_reader TO lab_login;
GRANT USAGE ON SCHEMA mt TO lab_reader, lab_login;
-- 只授权两列
GRANT SELECT (person_id, subject_code) ON mt.person TO lab_reader;

\echo '--- Q1: 受限角色 SELECT * ---'
SET LOCAL ROLE lab_reader;
SELECT * FROM mt.person LIMIT 1;
RESET ROLE;

\echo '--- Q2: 受限角色读授权列 ---'
SET LOCAL ROLE lab_reader;
SELECT person_id, subject_code FROM mt.person ORDER BY person_id LIMIT 2;
SELECT count(*) AS 允许的列参与计数 FROM mt.person;
RESET ROLE;

\echo '--- Q3: 受限角色读未授权列（应被拒） ---'
SET LOCAL ROLE lab_reader;
SELECT status FROM mt.person LIMIT 1;
RESET ROLE;

\echo '--- Q4: 受限角色 count(*) 不引用任何列 ---'
SET LOCAL ROLE lab_reader;
SELECT count(*) AS 裸计数 FROM mt.person;
RESET ROLE;

ROLLBACK;
