-- ============================================================================
-- 医学生才信息库 · 045 函数执行权：从「默认对 PUBLIC 开放」改为「注册表驱动」v1.0.0
--
-- 缺陷（独立审查发现，实测）
-- ---------------------------------------------------------------------------
-- 实测 `mt` 模式下有 **82 个函数对 PUBLIC 可执行**，包括：
--   · `web_login`     —— 任何角色都能反复调它猜口令（限流因此形同虚设，
--                        因为限流是"按邮箱"的，攻击者换账号即可继续）；
--   · `apply_column_grants` / `refresh_column_policy` / `apply_role_baseline`
--                     —— 权限对账与策略推导入口；
--   · `register_entity` / `set_value` / `create_instance` —— 动态建模的写入口。
--
-- 根因：**PostgreSQL 建函数时默认把 EXECUTE 授给 PUBLIC**。本项目只显式收回了
-- `web_session_new` 与 `web_user_add` 两个（026），其余全靠"没人想到去调"——
-- 那不是权限设计，是运气。
--
-- 为什么必须在**基线函数**里系统化处理，而不是再补几个 REVOKE
-- ---------------------------------------------------------------------------
-- 补 REVOKE 是"逐个记得"的路子：每加一个函数就漏一次（本项目已经因此栽过
-- 018/019/020/029/038/042 六次）。所以这次采用与 038（公开视图注册表）、
-- 041（计数排除登记表）同一个模式：**默认拒绝 + 显式登记**。
--
--     for 每个 mt 自己拥有的函数:
--         REVOKE EXECUTE FROM PUBLIC          ← 默认拒绝
--     然后按 function_grant 登记表逐条授予    ← 显式、可复核、有理由
--
-- ⚠ 一个必须写清的边界：**扩展自带的函数不动**
-- ---------------------------------------------------------------------------
-- pgcrypto 被装进了 mt 模式（`crypt`/`gen_salt`/`pgp_*`），它们属于扩展、
-- 由扩展的安装脚本管理权限。对它们动手等于跟扩展的升级路径打架。
-- 所以用 `pg_depend.deptype='e'` 把它们排除掉，只处理**本库自己创建的**函数。
-- 应用角色本来也不需要直接调 `crypt`：`web_login` / `web_user_add` 都是
-- SECURITY DEFINER，以属主身份执行（属主是超级用户，不受 EXECUTE 限制）。
--
-- 影响面：超级用户连接（ops/*、code/db.py、开发者模式、迁移）**不受影响**；
-- 应用角色只保留登记表里明确授予的那几个。测试会立刻暴露任何漏授。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + upsert + 基线函数重定义 + 对账 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS function_grant (
  function_name text NOT NULL,
  args          text NOT NULL DEFAULT '',      -- 身份参数（空串=无参）
  grantee       text NOT NULL,
  reason        text NOT NULL,
  added_at      timestamp with time zone NOT NULL DEFAULT now(),
  PRIMARY KEY (function_name, args, grantee)
);
COMMENT ON TABLE function_grant IS
  '函数执行权的**显式登记**（谁可以执行哪个函数、为什么）。'
  '基线函数对 mt 自有的每个函数先 REVOKE EXECUTE FROM PUBLIC（默认拒绝），'
  '再按本表逐条 GRANT。为什么要登记表而不是在函数里写死清单：'
  '清单会漏（本项目在权限基线上一共栽过六次），而登记表漏了会在自检里报出来。';

-- 登记合法授权（每条都要能说出理由）
INSERT INTO function_grant (function_name, args, grantee, reason) VALUES
  ('web_login', 'text, text, interval, text', 'mt_portal',
   '门户的登录入口；只给门户登录角色（不是四个等级角色 —— "能登录"与"能读哪个等级"是两件事）'),
  ('web_session_lookup', 'text', 'mt_portal', '会话校验：每个请求都要用它把 cookie 换成身份'),
  ('web_session_revoke', 'text', 'mt_portal', '退出登录时吊销会话'),
  ('log_access', 'text, text, text, integer, text, jsonb', 'mt_portal',
   '审计写入（SECURITY DEFINER）：应用只走这一条路，且对 access_log 本身零权限'),
  ('public_counts', 'text', 'mt_portal', '聚合闸门：数量公开，且不需要任何列权限'),
  ('log_access', 'text, text, text, integer, text, jsonb', 'mt_t0', '匿名访问也要留痕'),
  ('log_access', 'text, text, text, integer, text, jsonb', 'mt_t1', '同上'),
  ('log_access', 'text, text, text, integer, text, jsonb', 'mt_t2', '同上'),
  ('log_access', 'text, text, text, integer, text, jsonb', 'mt_t3', '同上'),
  ('public_counts', 'text', 'mt_t0', '匿名可读数量（需求："数量可以公开"）'),
  ('public_counts', 'text', 'mt_t1', '同上'),
  ('public_counts', 'text', 'mt_t2', '同上'),
  ('public_counts', 'text', 'mt_t3', '同上')
ON CONFLICT (function_name, args, grantee) DO UPDATE
   SET reason = EXCLUDED.reason, added_at = now();

-- 基线函数：默认拒绝 + 按登记表授予
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  g RECORD;
  n_revoked int := 0;
  n_granted int := 0;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  -- ① **默认拒绝**：本库自有的每个函数，先收回 PUBLIC 的执行权。
  --    排除扩展自带的函数（deptype='e'）—— 那是扩展的地盘，动它会跟升级打架。
  FOR v IN
    SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'mt'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION mt.%I(%s) FROM PUBLIC',
                   v.proname, v.args);
    n_revoked := n_revoked + 1;
  END LOOP;

  -- ② 按登记表显式授予
  FOR g IN SELECT * FROM function_grant LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.%I(%s) TO %I',
                   g.function_name, g.args, g.grantee);
    n_granted := n_granted + 1;
  END LOOP;

  -- ③ 表/视图/模式层面的基线（与 038/043 一致）
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    EXECUTE format('REVOKE ALL ON mt.column_profile FROM %I', r);
  END LOOP;

  EXECUTE 'REVOKE ALL ON mt.column_profile FROM mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT SELECT ON mt.column_profile TO mt_t3';

  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    IF EXISTS (SELECT 1 FROM public_view_registry pvr WHERE pvr.viewname = v.viewname) THEN
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t0, mt_t1, mt_t2, mt_t3', v.viewname);
    ELSE
      EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
    END IF;
  END LOOP;

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.public_view_registry TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants, '
          'mt.v_column_profile_coverage TO mt_portal';

  RAISE NOTICE '函数执行权已按注册表重建：收回 PUBLIC % 个，显式授予 % 条', n_revoked, n_granted;
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限。**045 起函数执行权改为"默认拒绝 + 注册表驱动"**：'
  '对 mt 自有的每个函数先 REVOKE EXECUTE FROM PUBLIC（PostgreSQL 建函数时默认授 PUBLIC，'
  '实测曾有 82 个函数对 PUBLIC 可执行，含 web_login / apply_column_grants / set_value），'
  '再按 mt.function_grant 逐条授予。扩展自带函数（deptype=e）不动。'
  '视图可见性由 public_view_registry 驱动（038）。';

-- 可查视图：谁可以执行什么（安全复核用）
CREATE OR REPLACE VIEW v_function_public_exposure AS
SELECT p.proname AS 函数,
       pg_get_function_identity_arguments(p.oid) AS 参数,
       has_function_privilege('public', p.oid, 'EXECUTE') AS PUBLIC可执行,
       p.prosecdef AS SECURITY_DEFINER,
       (SELECT count(*) FROM function_grant g
         WHERE g.function_name = p.proname
           AND g.args = pg_get_function_identity_arguments(p.oid)) AS 登记授权数
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mt'
   AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
   AND has_function_privilege('public', p.oid, 'EXECUTE')
 ORDER BY p.proname;
COMMENT ON VIEW v_function_public_exposure IS
  '仍然对 PUBLIC 可执行的本库自有函数（**期望为空**）。'
  '不为空说明基线对账没跑过，或者有函数是绕过基线新建的。';
GRANT SELECT ON v_function_public_exposure, function_grant TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE INSERT, UPDATE, DELETE ON function_grant FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

SELECT apply_column_grants();

-- 硬断言：危险函数必须不可被 PUBLIC 执行
DO $$
DECLARE bad text[] := ARRAY[]::text[]; v RECORD; n int;
BEGIN
  FOR v IN SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'mt'
              AND NOT EXISTS (SELECT 1 FROM pg_depend d
                               WHERE d.objid = p.oid AND d.deptype = 'e')
              AND has_function_privilege('public', p.oid, 'EXECUTE')
              AND p.proname IN ('web_login','web_user_add','web_session_new',
                                'apply_column_grants','refresh_column_policy',
                                'apply_role_baseline','register_entity','set_value',
                                'create_instance','add_dimension','deprecate_dimension',
                                'web_session_revoke','web_session_sweep')
  LOOP
    bad := bad || (v.proname || '(' || v.args || ')');
  END LOOP;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION '这些危险函数仍对 PUBLIC 可执行：%', array_to_string(bad, '；');
  END IF;

  -- 反向：合法的调用路径必须还在（不能收过头）
  IF NOT has_function_privilege('mt_portal', 'mt.web_login(text,text,interval,text)',
                                'EXECUTE') THEN
    RAISE EXCEPTION '收过头了：门户连 web_login 都不能执行 —— 登录会直接坏掉';
  END IF;
  IF NOT has_function_privilege('mt_t0', 'mt.public_counts(text)', 'EXECUTE') THEN
    RAISE EXCEPTION '收过头了：匿名不能执行聚合闸门 —— 数量公开会坏掉';
  END IF;
  IF NOT has_function_privilege('mt_t3', 'mt.log_access(text,text,text,integer,text,jsonb)',
                                'EXECUTE') THEN
    RAISE EXCEPTION '收过头了：审计写入不可用';
  END IF;
  SELECT count(*) INTO n FROM v_function_public_exposure;
  RAISE NOTICE '函数执行权自检通过：本库自有函数对 PUBLIC 的暴露 = % 个（期望 0）', n;
END $$;

COMMIT;
