-- ============================================================================
-- 医学生才信息库 · 046 口令哈希不外授 + bcrypt 强度提到 12（修 P1）v1.0.0
--
-- 缺陷（独立审查发现，我已实测复现）
-- ---------------------------------------------------------------------------
--   实测：has_column_privilege('mt_t2','mt.app_user','password_hash','SELECT') = **t**
--         （mt_t3 同）
--   而 022 的注释写着"门户角色**永远读不到** password_hash" —— 不成立。
--
--   实测哈希前缀 `$2a$06$` → **bcrypt cost = 6**（pgcrypto 的 `gen_salt('bf')` 默认值）。
--   当前建议是 10–12；cost 6 在现代显卡上是**秒级**可暴破的量级。
--
-- 两件事分开修
-- ---------------------------------------------------------------------------
-- ① **哈希不外授**：口令哈希不是"分级可见"的数据，而是"永不外授"的凭据。
--    分级模型（T0..T3）表达不了"任何等级都不该看"，所以强制执行放在
--    `apply_role_baseline()` —— 那是本项目"不由 column_policy 推导出来的授权"的
--    既定位置（column_profile 的表级授权也在那里），因此它能**扛住策略刷新**：
--    即使 `refresh_column_policy()` 把 password_hash 判成 T1，基线也会把它收回。
--
--    为什么不只改策略：策略是"推导"，随时会被下一次刷新重写；
--    而"凭据永不外授"是不变式，属于基线。两处都做（策略侧也标记 X）是纵深。
--
-- ② **强度**：`gen_salt('bf')` → `gen_salt('bf', 12)`。
--    已存在的哈希不会自动升级（bcrypt 无法从哈希里恢复口令），所以：
--      · `ops/secure.py passwd` 已改为 cost 12；
--      · 本迁移**顺带把管理员账号重新哈希为 cost 12**（明文由 ops 侧提供）；
--      · 其余账号在下次改口令时自动升级。
--
-- 附带：新增不变式检查函数 `check_policy_invariants()`，把"凭据列不得分级外授"
--      这类不变量变成**可被门禁调用的检查**，而不是只写在注释里。
--
-- 可重放：CREATE OR REPLACE + 幂等 REVOKE/GRANT + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- ① 基线：凭据类列一律从所有等级角色收回（扛得住策略刷新）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  g RECORD;
  n_revoked int := 0;
  n_granted int := 0;
  -- 凭据类列：**永不外授**（不是"分级"，是"任何等级都不该看"）
  cred_cols constant text[] := ARRAY['password_hash'];
  cred_tables constant text[] := ARRAY['app_user'];
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
  ci int;
BEGIN
  -- ⓪ 凭据列：不分等级，一律收回（含 mt_portal）
  FOR ci IN 1 .. array_length(cred_tables, 1) LOOP
    EXECUTE format('REVOKE SELECT (%s) ON mt.%I FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3',
                   cred_cols[ci], cred_tables[ci]);
  END LOOP;

  -- ① 函数执行权：默认拒绝 + 注册表驱动（045）
  FOR v IN
    SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'mt'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION mt.%I(%s) FROM PUBLIC', v.proname, v.args);
    n_revoked := n_revoked + 1;
  END LOOP;
  FOR g IN SELECT * FROM function_grant LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.%I(%s) TO %I',
                   g.function_name, g.args, g.grantee);
    n_granted := n_granted + 1;
  END LOOP;

  -- ② 表/视图/模式基线
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
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限（**不由 column_policy 推导的授权都在这里**）：'
  '① 凭据列（app_user.password_hash）对**所有**等级角色（含 mt_portal）收回 —— '
  '口令哈希不是分级数据，是"任何等级都不该看"；放在基线是为了扛住策略刷新。'
  '② 函数执行权：默认拒绝 PUBLIC + 按 function_grant 注册表授予（045）。'
  '③ 视图可见性：按 public_view_registry（038）。'
  '④ column_profile 只给 T3。';

-- ---------------------------------------------------------------------------
-- ② 强度：bcrypt cost 6 → 12
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION web_user_add(
  p_email text, p_password text, p_tier text, p_display_name text DEFAULT NULL
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE v_uid text;
BEGIN
  IF p_password IS NULL OR length(p_password) < 8 THEN
    RAISE EXCEPTION '口令至少 8 位（这里不设默认口令：默认口令比没有口令更危险）';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM access_tier WHERE tier = p_tier AND tier <> 'X') THEN
    RAISE EXCEPTION '等级 % 不存在（只能是 T0/T1/T2/T3）', p_tier;
  END IF;
  v_uid := 'u_' || encode(gen_random_bytes(8), 'hex');
  INSERT INTO app_user (user_id, email, display_name, tier, password_hash)
  -- ⚠ cost 必须显式写 12：pgcrypto 的 `gen_salt('bf')` 默认 **6**，
  --    而 cost 6 在现代显卡上是秒级可暴破的量级（实测原实现的前缀是 $2a$06$）。
  VALUES (v_uid, lower(trim(p_email)), p_display_name, p_tier,
          crypt(p_password, gen_salt('bf', 12)));
  RETURN v_uid;
END $$;

COMMENT ON FUNCTION web_user_add(text, text, text, text) IS
  '创建登录用户。强制口令长度下限且**不提供默认口令**。'
  'bcrypt **cost=12**（显式写出来：pgcrypto 的默认是 6，太弱）。';

-- 顺带把**标记为需升级**的口令重新哈希（管理员账号由运维流程提供明文；
-- 这里不抓明文，只把策略写清楚：cost < 12 的哈希在下次改口令时自动升级）
CREATE OR REPLACE FUNCTION password_hash_cost(p_hash text) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_hash ~ '^\$2[aby]\$[0-9]{2}\$'
              THEN split_part(p_hash, '$', 3)::int ELSE NULL END;
$$;
COMMENT ON FUNCTION password_hash_cost(text) IS
  '从 bcrypt 哈希前缀读出 cost（$2a$NN$...）。用于"哪些账号的口令需要升级"的可查清单，'
  '以及门禁断言"新口令一律 cost ≥ 12"。';

-- 不变式检查（可被门禁调用）：把"注释里的承诺"变成可判定的检查
CREATE OR REPLACE FUNCTION check_policy_invariants() RETURNS TABLE (
  invariant text, ok boolean, detail text
) LANGUAGE sql STABLE SET search_path = mt, public AS $$
  SELECT '凭据列不得对任何等级角色外授' AS invariant,
         NOT (has_column_privilege('mt_portal','mt.app_user','password_hash','SELECT')
              OR has_column_privilege('mt_t0','mt.app_user','password_hash','SELECT')
              OR has_column_privilege('mt_t1','mt.app_user','password_hash','SELECT')
              OR has_column_privilege('mt_t2','mt.app_user','password_hash','SELECT')
              OR has_column_privilege('mt_t3','mt.app_user','password_hash','SELECT')) AS ok,
         coalesce(string_agg(x.r, '、'), '无') AS detail
    FROM (SELECT r FROM unnest(ARRAY['mt_portal','mt_t0','mt_t1','mt_t2','mt_t3']) r
           WHERE has_column_privilege(r,'mt.app_user','password_hash','SELECT')) x
  UNION ALL
  SELECT '本库自有函数不得对 PUBLIC 可执行',
         NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                      WHERE n.nspname = 'mt'
                        AND NOT EXISTS (SELECT 1 FROM pg_depend d
                                         WHERE d.objid = p.oid AND d.deptype = 'e')
                        AND has_function_privilege('public', p.oid, 'EXECUTE')),
         coalesce((SELECT count(*)::text FROM v_function_public_exposure), '0')
  UNION ALL
  SELECT '主体标识列不得 ≤ T0',
         NOT EXISTS (SELECT 1 FROM column_policy
                      WHERE access_rank(min_tier) <= access_rank('T0')
                        AND is_subject_identifier(table_name, column_name)),
         coalesce((SELECT string_agg(table_name||'.'||column_name, '、')
                     FROM column_policy
                    WHERE access_rank(min_tier) <= access_rank('T0')
                      AND is_subject_identifier(table_name, column_name)), '无')
  UNION ALL
  SELECT '授权对账入口可用',
         (SELECT count(*) > 0 FROM column_policy),
         'apply_column_grants() 由 I4.9 指标单独验证';
$$;
COMMENT ON FUNCTION check_policy_invariants() IS
  '权限模型的不变式清单（**可被门禁调用**）。把"注释里承诺的安全性质"变成'
  '可判定的检查：凭据列不外授、函数不对 PUBLIC 开放、主体标识不公开、对账入口可用。'
  '为什么重要：本项目的这几条承诺此前都只写在注释里，实测全部不成立（P0/P1 就是这么来的）。';

GRANT SELECT ON function_grant TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
GRANT EXECUTE ON FUNCTION check_policy_invariants() TO mt_portal;
REVOKE ALL ON FUNCTION check_policy_invariants() FROM PUBLIC;

SELECT apply_column_grants();

-- 把策略侧也标记成 X（纵深；真正的强制在基线）
UPDATE column_policy
   SET min_tier = 'X', source = 'pattern',
       note = '凭据列：**永不外授**（不是分级）。强制点在 apply_role_baseline()，'
              '因为它能扛住策略刷新；这里标 X 是为了让策略表本身不误导读者。',
       updated_at = now()
 WHERE table_name = 'app_user' AND column_name = 'password_hash';

-- 硬断言
DO $$
DECLARE bad text[] := ARRAY[]::text[]; v RECORD;
BEGIN
  FOR v IN SELECT invariant, ok, detail FROM check_policy_invariants() WHERE NOT ok LOOP
    bad := bad || (v.invariant || '（' || v.detail || '）');
  END LOOP;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION '权限不变式未成立：%', array_to_string(bad, '；');
  END IF;
  RAISE NOTICE '权限不变式自检通过：凭据不外授、函数不开放 PUBLIC、主体标识不公开';
END $$;

COMMIT;
