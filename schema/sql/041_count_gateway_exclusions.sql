-- ============================================================================
-- 医学生人才信息库 · 041 计数闸门排除登录流水 + 质量页改走闸门 v1.0.0
--
-- 两个问题，一个根因
-- ---------------------------------------------------------------------------
-- 现象：`/quality` 对 T3 会话返回 403「需要 T2，你当前是 T3」—— 提示自相矛盾。
-- 原话是 `permission denied for table login_attempt`。
--
-- 根因在质量页的一段代码：
--     empty = [t for t in sorted(meta()["by_name"])
--              if q1(c, 'SELECT count(*) AS n FROM mt."%s"' % ...) == 0]
-- 它对**每一张表**（含视图）用**会话连接**跑 `count(*)`。而迁移 039 的
-- `login_attempt` 刻意对应用角色零授权，于是这一张表把整页打成"权限不足"。
--
-- 这暴露两件事：
--   ① **计数与列权限被混在一起**了。项目在 031 已经建立了"聚合走独立闸门
--      public_counts()"的做法，但质量页还在用逐表 count(*) —— 同一件事两套实现，
--      结果就是"新收紧一张表，一个无关页面挂掉"。
--   ② 闸门本身需要**排除清单**：`public_counts()` 会把 login_attempt 的行数也
--      交出去，而"有多少条登录尝试"属运维信息，不该公开。
--
-- 修法
-- ---------------------------------------------------------------------------
--   · 新增 `public_count_excluded(table_name, reason)` 登记表：闸门跳过这些表；
--   · `public_counts()` 读这个登记表（而不是硬编码 if）——
--     与 038 的 public_view_registry 同一个思路：**判断显式登记、可复核**；
--   · 质量页改用 `public_counts()`：一次调用拿到全部精确行数，
--     不再逐表 count(*)，因此对任何等级都可用（这正是闸门存在的意义）。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE FUNCTION。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS public_count_excluded (
  table_name text PRIMARY KEY,
  reason     text NOT NULL,
  added_at   timestamp with time zone NOT NULL DEFAULT now(),
  reviewer   text NOT NULL DEFAULT 'migration_041'
);
COMMENT ON TABLE public_count_excluded IS
  '**不通过聚合闸门公开行数**的表及理由。默认全部公开（"有多少人/多少岗位"按需求是公开信息）；'
  '登记在这里的是"行数本身也属运维/敏感信息"的表。'
  '用登记表而不是在函数里写 if —— 与 038 的 public_view_registry 同一个思路：'
  '判断必须显式写下来、可复核，而不是散在代码里。';

INSERT INTO public_count_excluded (table_name, reason) VALUES
  ('login_attempt',
   '登录尝试流水。虽然闸门只给行数，但"有多少次登录尝试"属运维信息，'
   '可能被用来推断"是否正在被攻击/有多少人在用"——不公开。'
   '需要它的人走运维路径（属主连接）。'),
  ('login_policy',
   '限流参数表（单行）。它的**内容**是公开的（T0），但行数没有意义，'
   '列在这里只为避免在页面上出现一行"1"的噪声。')
ON CONFLICT (table_name) DO UPDATE
   SET reason = EXCLUDED.reason, added_at = now(), reviewer = 'migration_041';

-- 闸门：跳过登记表
CREATE OR REPLACE FUNCTION public_counts() RETURNS TABLE (table_name text, n_rows bigint)
LANGUAGE plpgsql
SECURITY DEFINER                    -- 以属主身份数行：调用者不需要任何列权限
SET search_path = mt, public        -- 钉死 search_path，防劫持
AS $$
DECLARE
  r RECORD;
  n bigint;
BEGIN
  FOR r IN
    SELECT c.relname
      FROM pg_class c
      JOIN pg_namespace ns ON ns.oid = c.relnamespace
     WHERE ns.nspname = 'mt' AND c.relkind = 'r'
       AND NOT EXISTS (SELECT 1 FROM public_count_excluded e
                        WHERE e.table_name = c.relname)
     ORDER BY c.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM mt.%I', r.relname) INTO n;
    table_name := r.relname;
    n_rows := n;
    RETURN NEXT;
  END LOOP;
END $$;

COMMENT ON FUNCTION public_counts() IS
  '每张表的精确行数（聚合闸门）。SECURITY DEFINER，所以调用者不需要任何列权限 —— '
  '这就是"数量公开"与"列受限"能同时成立的原因（见 031）。'
  '041 起跳过 public_count_excluded 里登记的表（登录流水等：行数也属运维信息）。'
  '**边界**：只给表级总数；按敏感维度分组的**小计数**必须另加最小计数阈值，不能复用它。';

REVOKE ALL ON FUNCTION public_counts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public_counts() TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
GRANT SELECT ON public_count_excluded TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE INSERT, UPDATE, DELETE ON public_count_excluded FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 自检
DO $$
DECLARE n int; hit int;
BEGIN
  SELECT count(*) INTO n FROM public_counts() WHERE table_name = 'person';
  IF n <> 1 THEN
    RAISE EXCEPTION '闸门里没有 person 的行数（聚合闸门坏了）';
  END IF;
  SELECT count(*) INTO hit FROM public_counts() WHERE table_name = 'login_attempt';
  IF hit <> 0 THEN
    RAISE EXCEPTION '闸门仍然公开 login_attempt 的行数';
  END IF;
  -- 权限口径：这些登记表必须在 column_policy 里有行（否则又回到"看对账跑没跑"）
  SELECT count(*) INTO n FROM (SELECT table_name FROM public_count_excluded) e
   WHERE NOT EXISTS (SELECT 1 FROM column_policy cp WHERE cp.table_name = e.table_name);
  IF n > 0 THEN
    RAISE EXCEPTION '有 % 张登记表没有列级策略 —— 需要补 refresh_column_policy 的口径', n;
  END IF;
  RAISE NOTICE '聚合闸门自检通过：person 可数、login_attempt 已排除、登记表都有策略行';
END $$;

SELECT refresh_column_policy();
SELECT apply_column_grants();

COMMIT;
