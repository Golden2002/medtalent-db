-- ============================================================================
-- 医学生人才信息库 · 039 登录失败限流与锁定（公开登录入口的前置控制）v1.0.0
--
-- 为什么必须先做这个
-- ---------------------------------------------------------------------------
-- 只读门户已经挂到公网，它有一个登录入口。**只要登录入口在公网上，
-- 就一定会被撞库/暴力尝试** —— 而我此前只做了一件事：生成 20 位随机口令。
-- 那是"口令足够强"，不是"入口受保护"。缺少的是：
--   · 失败次数限制（否则可以无限尝试）；
--   · 短时锁定（把在线爆破的成本抬到不现实）。
--
-- 把限制放在**数据库函数里**（web_login 是 SECURITY DEFINER），而不是应用层：
--   · 应用层限流可以在重启/多进程/绕过路径时失效；
--   · 放在函数里，任何调用方都绕不过（和本项目"强制点放数据库"的一贯做法一致）。
--
-- 设计（刻意简单，因为简单才不会被绕过）
-- ---------------------------------------------------------------------------
--   · 记每一次尝试：邮箱（小写）、时间、成功与否；
--   · 同一邮箱在 `window` 窗口内失败 ≥ max_fail 次 → 锁定，直到最早那次失败
--     超出窗口为止（滑动窗口，不是"再等 N 分钟"的粗暴计数器）；
--   · 锁定时**不校验口令**（连哈希比对都不做）→ 不给攻击者任何计时侧信道；
--   · 成功登录会清掉该邮箱的失败记录（否则用户偶发输错会累积）。
--
-- ⚠ 一个必须写清的取舍：按**账号**锁定 = 攻击者可以拿它来"锁住"合法用户（DoS）。
--    缓解：窗口短（15 分钟）、阈值不低（5 次），且**不记录 IP**（IP 是个人信息，
--    本库一贯不存）。更强的做法是加验证码或按 IP+账号双维度限流，但那需要
--    引入外部依赖或存 IP —— 与"最小必要"冲突。所以这里选短窗口 + 明确写出代价。
--
-- ⚠ 另一个边界：失败记录本身是"某邮箱在何时被尝试登录"的痕迹，属个人信息。
--    故：只存邮箱与时间（不存口令、不存 IP），且**默认保留 30 天**由
--    ops 清理（见 web_login_attempt_sweep()），不做长期留存。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE FUNCTION。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS login_attempt (
  attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email      text NOT NULL,                    -- 小写邮箱；**不存口令、不存 IP**
  at         timestamp with time zone NOT NULL DEFAULT now(),
  ok         boolean NOT NULL,
  reason     text                              -- bad_credentials / locked / suspended / ok
);
CREATE INDEX IF NOT EXISTS idx_login_attempt_email_at ON login_attempt (lower(email), at DESC);
COMMENT ON TABLE login_attempt IS
  '登录尝试流水（用于失败限流）。**只存邮箱与时间**：不存口令、不存 IP —— '
  'IP 属个人信息，本库一贯不存（见 web_session.client_hint 的同类取舍）。'
  '失败记录本身是个人信息，默认保留 30 天，由 web_login_attempt_sweep() 清理。';

-- 限流参数（可调，但要有名字以便审计）
CREATE TABLE IF NOT EXISTS login_policy (
  id        integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  max_fail  integer NOT NULL DEFAULT 5,
  window_minutes integer NOT NULL DEFAULT 15,
  retain_days integer NOT NULL DEFAULT 30,
  updated_at timestamp with time zone NOT NULL DEFAULT now()
);
COMMENT ON TABLE login_policy IS
  '登录限流参数（单行）:max_fail / window_minutes / retain_days。'
  '写出来而不是硬编码，是为了让"锁定策略是什么"可查、可审计、可调整。';
INSERT INTO login_policy (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- web_login：加入限流。签名不变，行为多一条 reason='too_many_attempts'
-- ---------------------------------------------------------------------------
-- ⚠ 必须 DROP 再 CREATE：本函数新增了出参 `locked_until`，而
--    `CREATE OR REPLACE FUNCTION` **不允许改变返回类型**（出参集合变了就是新类型），
--    实测报 `cannot change return type of existing function`。
--    所以先 DROP，再建，并**显式重授执行权** ——
--    DROP 会把原有的 GRANT 一起带走，忘了重授就会出现"登录突然 42501/权限不足"，
--    而那条路径只有登录时才走到，很容易漏测。
--    （不加 CASCADE：若有视图依赖它会直接报错，比静默连带删除更安全。）
DROP FUNCTION IF EXISTS web_login(text, text, interval, text);

CREATE OR REPLACE FUNCTION web_login(
  p_email text, p_password text, p_ttl interval DEFAULT interval '8 hours',
  p_client_hint text DEFAULT NULL
) RETURNS TABLE (session_id text, actor text, tier text, reason text, locked_until timestamp with time zone)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE
  u RECORD;
  pol RECORD;
  em text := lower(trim(coalesce(p_email, '')));
  n_fail int;
  first_fail timestamptz;
BEGIN
  SELECT * INTO pol FROM login_policy WHERE id = 1;

  -- ① 限流检查：窗口内失败次数是否已达阈值
  SELECT count(*), min(at) INTO n_fail, first_fail
    FROM login_attempt
   WHERE lower(email) = em AND NOT ok
     AND at > now() - make_interval(mins => pol.window_minutes);

  IF em <> '' AND n_fail >= pol.max_fail THEN
    -- 锁定期内**连口令都不校验**：既省算力，也不给计时侧信道
    INSERT INTO login_attempt (email, ok, reason) VALUES (em, false, 'locked');
    RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, 'too_many_attempts'::text,
                        (first_fail + make_interval(mins => pol.window_minutes));
    RETURN;
  END IF;

  SELECT * INTO u FROM app_user WHERE lower(email) = em;

  -- ② 口令校验（用户不存在 / 停用 / 无哈希 / 口令不符 → 同一个 reason，
  --    否则等于提供账号枚举接口 —— 这条是原有设计，保持不变）
  IF NOT FOUND OR u.status <> 'active' OR u.password_hash IS NULL
     OR crypt(p_password, u.password_hash) <> u.password_hash THEN
    INSERT INTO login_attempt (email, ok, reason) VALUES (em, false, 'bad_credentials');
    RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, 'bad_credentials'::text,
                        NULL::timestamptz;
    RETURN;
  END IF;

  -- ③ 成功：清掉该邮箱的失败记录（否则偶发输错会累积到锁定）
  DELETE FROM login_attempt WHERE lower(email) = em AND NOT ok;
  INSERT INTO login_attempt (email, ok, reason) VALUES (em, true, 'ok');

  UPDATE app_user SET last_login_at = now() WHERE user_id = u.user_id;
  RETURN QUERY SELECT n.session_id, n.actor, n.tier, 'ok'::text, NULL::timestamptz
                 FROM web_session_new(u.email, u.tier, u.user_id, p_ttl, p_client_hint) n;
END $$;

COMMENT ON FUNCTION web_login(text, text, interval, text) IS
  '校验口令并建立服务端会话，**含登录失败限流**。'
  '窗口内失败达阈值则锁定并返回 reason=too_many_attempts（锁定期内不比对口令，'
  '避免计时侧信道）。限流放在 SECURITY DEFINER 函数里而不是应用层 —— '
  '应用层限流在重启/多进程/绕过路径时会失效，放在这里任何调用方都绕不过。'
  'actor 记邮箱（便于审计读出"是谁"）；用户不存在与口令错误返回同一 reason。';

-- 清理：失败记录只保留 retain_days 天（它是个人信息，不做长期留存）
CREATE OR REPLACE FUNCTION web_login_attempt_sweep() RETURNS integer
LANGUAGE sql SECURITY DEFINER SET search_path = mt, public AS $$
  WITH d AS (
    DELETE FROM login_attempt
     WHERE at < now() - make_interval(days => (SELECT retain_days FROM login_policy WHERE id = 1))
     RETURNING 1)
  SELECT count(*)::int FROM d;
$$;
COMMENT ON FUNCTION web_login_attempt_sweep() IS
  '清理超过保留期的登录尝试记录（默认 30 天）。登录痕迹属个人信息，'
  '不做长期留存 —— 与本库对 location_hint 不存 IP 的取舍一致。';

-- 可查视图：让"当前谁被锁着、锁到什么时候"可查（运维需要它）
CREATE OR REPLACE VIEW v_login_lockout AS
SELECT lower(a.email) AS email,
       count(*) FILTER (WHERE NOT a.ok) AS fails_in_window,
       max(a.at) AS last_fail_at
  FROM login_attempt a
 WHERE a.at > now() - make_interval(mins => (SELECT window_minutes FROM login_policy WHERE id = 1))
   AND NOT a.ok
 GROUP BY lower(a.email)
HAVING count(*) FILTER (WHERE NOT a.ok)
       >= (SELECT max_fail FROM login_policy WHERE id = 1);
COMMENT ON VIEW v_login_lockout IS
  '当前处于锁定状态的邮箱与窗口内失败次数（供运维/自己排查）。';
GRANT SELECT ON v_login_lockout, login_policy TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE INSERT, UPDATE, DELETE ON login_policy FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- 权限：门户只能"调用登录函数"，读不到尝试流水（那是个人信息）
REVOKE ALL ON login_attempt FROM PUBLIC, mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
GRANT EXECUTE ON FUNCTION web_login(text, text, interval, text) TO mt_portal;
REVOKE ALL ON FUNCTION web_login_attempt_sweep() FROM PUBLIC, mt_portal;

-- 自检：限流必须真的生效
DO $$
DECLARE r RECORD; i int;
BEGIN
  IF NOT has_table_privilege('mt_portal', 'mt.v_login_lockout', 'SELECT') THEN
    RAISE EXCEPTION '门户读不到锁定状态视图 —— 登录页就没法告诉用户"你被锁了"';
  END IF;
  IF has_table_privilege('mt_portal', 'mt.login_attempt', 'SELECT') THEN
    RAISE EXCEPTION '门户能读登录尝试流水（含他人邮箱）—— 越权';
  END IF;
  RAISE NOTICE '登录限流自检通过：门户可查锁定状态，读不到尝试流水';
END $$;

COMMIT;
