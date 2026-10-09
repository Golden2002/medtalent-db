-- ============================================================================
-- 医学生人才信息库 · 022 网页登录与会话（把"访问用户"变成可追责的主体）v1.0.0
--
-- 需求原文：「数据是重要资产，因此需要记录访问用户，并给重要数据加权限」
-- ---------------------------------------------------------------------------
-- 017–021 已经把**字段级权限**建好并实测有效（列级 GRANT：`SELECT *` 会被直接拒绝，
-- 不设等级读不到任何数据）。但缺了另一半：**"访问用户"是谁**。
-- 没有身份，"记录访问用户"只能记成"某个进程读了一行"。
--
-- 本迁移做三件事
-- ---------------------------------------------------------------------------
-- 【1】身份表 app_user：一个终端用户 + 他所属的等级
--      等级直接外键到 access_tier（T0/T1/T2/T3/X），不另建一套分级。
--
-- 【2】会话表 web_session：**服务端**会话，不是把等级放进 cookie
--      为什么不做"签名 cookie 里带等级"：cookie 是客户端可伪造的，
--      而本项目的强制点在数据库（`SET LOCAL ROLE mt_tN`）。
--      若等级来自 cookie，伪造 cookie 就等于伪造 T3 权限 —— 前端一破，后端全开。
--      服务端会话让"等级"由数据库里的行决定。
--
-- 【3】口令只以哈希存在，且校验放在 SECURITY DEFINER 函数里
--      用 pgcrypto 的 `crypt()/gen_salt('bf')`（bcrypt）存哈希，**不存明文**。
--      校验逻辑放进 mt.web_login()：门户角色**永远读不到 password_hash**，
--      只能调用"给我一个会话或什么都不给"这一个函数。
--      同 log_access 的理由：能读哈希的角色，就能离线爆破；
--      不需要读哈希的角色，就不该有那个权限。
--
-- 为什么登录成功返回的是随机 hex 而不是自增 id：
--   session_id 会进 cookie。可猜测的 id 等于可劫持的会话。
--   `encode(gen_random_bytes(32),'hex')` 是 256 位随机量。
--
-- 可重放：CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE FUNCTION。
-- 不修改 001–021；不删任何既有列。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 登录用户
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
  user_id       text PRIMARY KEY,
  email         text NOT NULL UNIQUE,
  display_name  text,
  tier          text NOT NULL REFERENCES access_tier(tier),
  status        text NOT NULL DEFAULT 'active',        -- active / suspended
  password_hash text,                                  -- 只存 bcrypt 哈希，不存明文
  created_at    timestamp with time zone NOT NULL DEFAULT now(),
  last_login_at timestamp with time zone,
  attrs         jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT app_user_status_ck CHECK (status IN ('active','suspended'))
);
COMMENT ON TABLE app_user IS
  '网页登录用户。tier 外键到 access_tier（T0 公开…T3 管理员），不另建一套分级。'
  'password_hash 是 bcrypt 哈希（pgcrypto crypt + gen_salt(''bf'')），永不存明文。';

-- ---------------------------------------------------------------------------
-- 2. 服务端会话
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS web_session (
  session_id   text PRIMARY KEY,                       -- 256 位随机 hex
  user_id      text REFERENCES app_user(user_id),      -- NULL = 匿名访客也发会话
  tier         text NOT NULL REFERENCES access_tier(tier),
  actor        text NOT NULL,                          -- 写进 access_log 的"谁"
  created_at   timestamp with time zone NOT NULL DEFAULT now(),
  last_seen_at timestamp with time zone NOT NULL DEFAULT now(),
  expires_at   timestamp with time zone NOT NULL,
  revoked_at   timestamp with time zone,
  client_hint  text,                                   -- User-Agent 摘要（不含 IP，避免多存个人信息）
  attrs        jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_web_session_expires ON web_session(expires_at);
COMMENT ON TABLE web_session IS
  '服务端会话。等级由**数据库里的行**决定，不放在客户端 cookie 里 —— '
  '否则伪造 cookie 就等于伪造 T3 权限（强制点在数据库，身份就不能来自客户端）。';

-- ---------------------------------------------------------------------------
-- 3. 会话函数：门户角色只能通过它们碰会话，读不到别的
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION web_session_new(
  p_actor text,
  p_tier  text,
  p_user_id text DEFAULT NULL,
  p_ttl   interval DEFAULT interval '8 hours',
  p_client_hint text DEFAULT NULL
) RETURNS TABLE (session_id text, actor text, tier text, expires_at timestamp with time zone)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE
  v_sid text;
  v_tier text;
BEGIN
  -- 等级必须在阶梯表里；未知等级按 T0 处理，绝不放行
  SELECT t.tier INTO v_tier FROM access_tier t WHERE t.tier = p_tier AND t.tier <> 'X';
  IF v_tier IS NULL THEN
    v_tier := 'T0';
  END IF;
  v_sid := encode(gen_random_bytes(32), 'hex');
  INSERT INTO web_session (session_id, user_id, tier, actor, expires_at, client_hint)
  VALUES (v_sid, p_user_id, v_tier, COALESCE(NULLIF(p_actor, ''), 'anonymous'),
          now() + p_ttl, left(COALESCE(p_client_hint, ''), 120));
  RETURN QUERY SELECT v_sid, COALESCE(NULLIF(p_actor,''),'anonymous'), v_tier,
                      (now() + p_ttl);
END $$;

CREATE OR REPLACE FUNCTION web_session_lookup(p_session_id text)
RETURNS TABLE (actor text, tier text, user_id text, ok boolean, expired boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE r RECORD;
BEGIN
  SELECT s.actor, s.tier, s.user_id, s.expires_at, s.revoked_at
    INTO r FROM web_session s WHERE s.session_id = p_session_id;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 'anonymous'::text, 'T0'::text, NULL::text, false, false;
    RETURN;
  END IF;
  IF r.revoked_at IS NOT NULL THEN
    RETURN QUERY SELECT 'anonymous'::text, 'T0'::text, r.user_id, false, false;
    RETURN;
  END IF;
  IF r.expires_at <= now() THEN
    RETURN QUERY SELECT 'anonymous'::text, 'T0'::text, r.user_id, false, true;
    RETURN;
  END IF;
  UPDATE web_session SET last_seen_at = now() WHERE session_id = p_session_id;
  RETURN QUERY SELECT r.actor, r.tier, r.user_id, true, false;
END $$;

CREATE OR REPLACE FUNCTION web_login(
  p_email text, p_password text, p_ttl interval DEFAULT interval '8 hours',
  p_client_hint text DEFAULT NULL
) RETURNS TABLE (session_id text, actor text, tier text, reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE u RECORD;
BEGIN
  SELECT * INTO u FROM app_user WHERE lower(email) = lower(trim(p_email));
  -- 用户不存在、被停用、没有哈希、口令不符 —— 一律返回同一个 reason。
  -- 不区分"用户不存在"与"口令错误"：区分了就等于给出账号枚举接口。
  IF NOT FOUND OR u.status <> 'active' OR u.password_hash IS NULL
     OR crypt(p_password, u.password_hash) <> u.password_hash THEN
    RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, 'bad_credentials'::text;
    RETURN;
  END IF;
  UPDATE app_user SET last_login_at = now() WHERE user_id = u.user_id;
  RETURN QUERY SELECT n.session_id, n.actor, n.tier, 'ok'::text
                 FROM web_session_new(u.user_id, u.tier, u.user_id, p_ttl, p_client_hint) n;
END $$;

COMMENT ON FUNCTION web_login(text, text, interval, text) IS
  '校验口令并建立**服务端**会话。口令用 bcrypt 哈希比对；'
  '用户不存在与口令错误返回同一个 reason（否则等于提供账号枚举）；'
  '门户角色没有 app_user 的 SELECT 权限，读不到任何哈希。';

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
  VALUES (v_uid, lower(trim(p_email)), p_display_name, p_tier, crypt(p_password, gen_salt('bf')));
  RETURN v_uid;
END $$;

COMMENT ON FUNCTION web_user_add(text, text, text, text) IS
  '创建登录用户。强制口令长度下限且**不提供默认口令** —— '
  '默认口令是最常见的后门（外面挂着域名时尤其致命）。';

CREATE OR REPLACE FUNCTION web_session_revoke(p_session_id text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = mt, public AS $$
  UPDATE web_session SET revoked_at = now()
   WHERE session_id = p_session_id AND revoked_at IS NULL
  RETURNING true;
$$;

-- ---------------------------------------------------------------------------
-- 4. 授权：门户角色只拿到"函数执行权"，不拿到表的读写权
-- ---------------------------------------------------------------------------
REVOKE ALL ON app_user, web_session FROM PUBLIC;
REVOKE ALL ON app_user, web_session FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 会话清理是运维动作：只让 postgres 做（门户不该能删审计相关的东西）
CREATE OR REPLACE FUNCTION web_session_sweep() RETURNS integer
LANGUAGE sql SECURITY DEFINER SET search_path = mt, public AS $$
  WITH d AS (DELETE FROM web_session
              WHERE expires_at < now() - interval '7 days'
                 OR (revoked_at IS NOT NULL AND revoked_at < now() - interval '7 days')
              RETURNING 1)
  SELECT count(*)::int FROM d;
$$;
COMMENT ON FUNCTION web_session_sweep() IS
  '清理过期/已撤销超过 7 天的会话。过期会话不立即删，留一段窗口供追责（谁什么时候登录过）。';

-- 门户需要的能力，逐个显式授予（最小权限）
GRANT EXECUTE ON FUNCTION web_session_new(text, text, text, interval, text) TO mt_portal;
GRANT EXECUTE ON FUNCTION web_session_lookup(text) TO mt_portal;
GRANT EXECUTE ON FUNCTION web_session_revoke(text) TO mt_portal;
GRANT EXECUTE ON FUNCTION web_login(text, text, interval, text) TO mt_portal;
GRANT SELECT ON access_tier TO mt_portal;

-- web_user_add 只给管理员角色执行（门户自己不能造账号）
REVOKE ALL ON FUNCTION web_user_add(text, text, text, text) FROM PUBLIC;

-- 健康/审计视图：会话与用户的可观测性
CREATE OR REPLACE VIEW v_web_session_activity AS
SELECT s.session_id,
       s.actor,
       s.tier,
       s.user_id,
       u.email,
       s.created_at,
       s.last_seen_at,
       s.expires_at,
       (s.revoked_at IS NOT NULL) AS revoked,
       (s.expires_at <= now())    AS expired
  FROM web_session s LEFT JOIN app_user u ON u.user_id = s.user_id
 ORDER BY s.last_seen_at DESC;
COMMENT ON VIEW v_web_session_activity IS
  '会话活动视图（供 /quality 与运维查看谁在什么时候登录过）。不含口令哈希任何痕迹。';
GRANT SELECT ON v_web_session_activity TO mt_t3;

COMMIT;
