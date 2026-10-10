-- ============================================================================
-- 医学生才信息库 · 048 计数口径统一：数据表公开、活动表不公开 v1.0.0
--
-- 缺陷（独立审查 P1-1 的真正形态）
-- ---------------------------------------------------------------------------
-- 041 给 `public_counts()` 加了排除登记表，把 `login_attempt` 的行数藏了起来 ——
-- 但**公开的 `/schema` 页用 `meta()`（特权连接缓存）照样列出了所有表的精确行数**。
-- 于是同一个问题有**两套实现、两个答案**：
--   · `SELECT * FROM mt.public_counts()` → 不返回 login_attempt；
--   · 匿名打开 `/schema` → 看得到 login_attempt 的行数。
-- 这正是本项目最忌的"一件事两套实现"——而且**看起来两边都对**，最难发现。
--
-- 口径（写下来，只此一份）
-- ---------------------------------------------------------------------------
--   · **数据表**（person / job_posting / credential / evidence / …）：
--     行数**公开**。理由：用户需求原文"数量…可以公开"，而目录页的意义就是
--     "看到数据库真实的样子"。
--   · **身份 / 会话 / 登录表**（app_user / web_session / login_attempt / login_policy）：
--     行数**不公开**。它们反映的是"有多少账号/会话/登录尝试"，属**活动信息**，
--     不是数据规模；对外暴露等于免费给出"这个系统有多少用户、是否正被撞库"。
--   · **审计流水**（access_log / change_log）：
--     行数**不对匿名公开**（同样是活动量），T2（员工）及以上可见。
--
-- 实现原则：判据只有一处 —— `public_count_excluded` 登记表。
--   `public_counts()` 读它（041 已做）；门户的 `/schema`、`/catalog`、`/quality`
--   也读它（本迁移配套的 portal 改动），于是"能不能看到这个数字"只有一个答案。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + upsert。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

INSERT INTO public_count_excluded (table_name, reason) VALUES
  ('app_user',
   '登录账号表。行数 = "这个系统有多少账号"，属活动信息而非数据规模，不对外公开。'),
  ('web_session',
   '会话表。行数 = "当前有多少活跃会话"，属活动信息（也能反映使用高峰），不对外公开。'),
  ('access_log',
   '访问审计流水。行数 = "被访问了多少次"，属活动信息；需要它的人有 T2 权限走页面看。'),
  ('change_log',
   '变更审计流水。行数 = "数据被改了多少次"，属活动信息；T2 及以上可见。')
ON CONFLICT (table_name) DO UPDATE
   SET reason = EXCLUDED.reason, added_at = now(), reviewer = 'migration_048';

-- 判据视图：把"这个表的行数能不能公开"变成一条可查的规则（门户直接读它）
CREATE OR REPLACE VIEW v_count_visibility AS
SELECT c.relname AS table_name,
       NOT EXISTS (SELECT 1 FROM public_count_excluded e WHERE e.table_name = c.relname)
         AS count_is_public,
       (SELECT reason FROM public_count_excluded e WHERE e.table_name = c.relname) AS why_not
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mt' AND c.relkind = 'r';
COMMENT ON VIEW v_count_visibility IS
  '每张表的行数是否**对匿名公开**（判据唯一来源：public_count_excluded）。'
  '门户的表格/目录/质量页都用它，`public_counts()` 也用它 —— 一个口径一份实现，'
  '避免"闸门藏了、页面却还显示"的两套答案（041 之后实际发生过）。';

GRANT SELECT ON v_count_visibility, public_count_excluded
  TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 自检：闸门与登记表必须一致（这正是 041 之后不一致的地方）
DO $$
DECLARE tbl text; n int; bad text[] := ARRAY[]::text[];
BEGIN
  FOR tbl IN SELECT table_name FROM public_count_excluded LOOP
    SELECT count(*) INTO n FROM public_counts() WHERE table_name = tbl;
    IF n <> 0 THEN
      bad := bad || (tbl || '（闸门仍在返回它）');
    END IF;
  END LOOP;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION '闸门与排除登记表不一致：%', array_to_string(bad, '；');
  END IF;
  -- 数据表的行数必须仍然公开（不能收过头）
  IF NOT (SELECT count_is_public FROM v_count_visibility WHERE table_name='person') THEN
    RAISE EXCEPTION 'person 的行数被误判为不公开 —— 与"数量可以公开"的需求冲突';
  END IF;
  SELECT count(*) INTO n FROM public_count_excluded;
  RAISE NOTICE '计数口径自检通过：% 张表的行数不公开，其余公开（person 仍公开）', n;
END $$;

COMMIT;
