-- ============================================================================
-- 医学生人才信息库 · 024 审计日志里的访问者要能读出「是谁」 v1.0.0
--
-- 背景（端到端探针实测发现）
-- ---------------------------------------------------------------------------
-- web_login 建会话时把 actor 传成了 u.user_id，于是 access_log 里记的是
--   actor = u_61220ea0625321bf
-- 而审计日志存在的意义是**事后能回答"谁读过这份数据"**。一串随机十六进制做不到这件事——
-- 要回答它还得再查一次 app_user，而那个账号可能已经被删了（删了就永远查不出来）。
--
-- 修正：actor 记**邮箱**（可读、可追责），user_id 仍然单独存在 web_session.user_id 列里
-- （那是内部主键，用于关联与级联，不承担"给人看"的职责）。
-- 两者分工：给人看的用邮箱，给机器用的用 user_id。
--
-- 顺带一个安全细节：邮箱本身是个人信息，但**它已经在 app_user 表里**，
-- 记进审计日志不增加新的泄露面；而少了它，审计就失去可追责性。
-- 这是"最小必要"在这一处的具体判断，不是随手多存一个字段。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 幂等 GRANT。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

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
  -- actor 用**邮箱**：审计日志要能读出"是谁"，一串随机 id 读不出来。
  -- user_id 仍然单独存在 web_session.user_id（内部关联用）。
  RETURN QUERY SELECT n.session_id, n.actor, n.tier, 'ok'::text
                 FROM web_session_new(u.email, u.tier, u.user_id, p_ttl, p_client_hint) n;
END $$;

COMMENT ON FUNCTION web_login(text, text, interval, text) IS
  '校验口令并建立**服务端**会话（actor 记邮箱，便于审计读出"是谁"）。'
  '口令用 bcrypt 哈希比对；用户不存在与口令错误返回同一个 reason（否则等于提供账号枚举）；'
  '门户角色没有 app_user 的 SELECT 权限，读不到任何哈希。';

GRANT EXECUTE ON FUNCTION web_login(text, text, interval, text) TO mt_portal;

-- 清理：把此前留下的不可读 actor 换成邮箱（只影响测试期间的少量行）
UPDATE web_session s SET actor = u.email
  FROM app_user u WHERE u.user_id = s.user_id AND s.actor = s.user_id;
UPDATE access_log a SET actor = u.email
  FROM app_user u WHERE u.user_id = a.actor;

COMMIT;
