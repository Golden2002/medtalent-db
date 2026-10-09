-- ============================================================================
-- 医学生人才信息库 · 038 公开视图改成「注册表驱动」，不再靠改清单 v1.0.0
--
-- 问题（实测，就在 036 之后立刻出现）
-- ---------------------------------------------------------------------------
-- 036 建了 `v_age_band_public`（公开的年龄段分布）并 GRANT 给 mt_t0，
-- 但 037 结尾调用的 `apply_column_grants()` → `apply_role_baseline()` 里有一条规则：
--     「视图默认只给 T3」（因为视图以**属主**权限执行，会绕过列级策略）
-- 它把新视图从 mt_t0/t1/t2 上收回去了，只有少数几个视图在"白名单"里被重新授回。
-- 实测：`has_table_privilege('mt_t0','mt.v_age_band_public','SELECT')` = **false**。
--
-- 这是同一个漏洞类的**第四次**：
--   018 列权限 942→180、019 序列权限、020 审计写入、029 表级授权对象 ——
-- 每次都是"重建全部授权的函数没有重建全部"。前三次的修法是"往清单里补一行"，
-- 而清单会一直漏（每加一个对象就漏一次）。所以这次改结构：
--
--   **视图的对外可见性由一张注册表决定，而不是由函数里的字面清单决定。**
--
-- 机制
-- ---------------------------------------------------------------------------
--   · `mt.public_view_registry(viewname, reason)`：登记"对全部等级开放"的视图；
--   · `apply_role_baseline()` 遍历**全部**视图：在注册表里的 → 授给 t0/t1/t2/t3；
--     不在的 → 只授 T3（安全默认：视图以属主权限执行，绕过列级策略）；
--   · 于是新增公开视图只需 INSERT 一行注册，**不需要改函数**；
--   · 自检：注册表里不能有不存在或非视图的名字（防止登记了个错名字还以为生效了）。
--
-- 为什么需要"公开视图"这个概念本身
-- ---------------------------------------------------------------------------
-- 视图以属主身份执行，所以给视图就等于给它能看到的一切列 —— 对列级策略是个后门。
-- 但如果视图**本身只含公开信息**（例如"各年龄段人数"这种聚合，不含任何主体标识），
-- 那它对匿名开放是合理且必要的（不然"数量/分类统计公开"就没法实现）。
-- 注册表强制这个判断被**显式做一次、写下来、可复核**，而不是随手 GRANT。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + 幂等 upsert + 对账。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS public_view_registry (
  viewname  text PRIMARY KEY,
  reason    text NOT NULL,          -- 为什么这个视图对**匿名**开放（必须写清）
  added_at  timestamp with time zone NOT NULL DEFAULT now(),
  reviewer  text NOT NULL DEFAULT 'migration_038'
);
COMMENT ON TABLE public_view_registry IS
  '登记"对全部等级（含匿名）开放"的视图。判据：视图**只含公开信息**'
  '（聚合、字典、策略元数据），不含任何主体标识或个人信息。'
  '未登记的视图一律只给 T3 —— 因为视图以属主权限执行，对列级策略是后门。';

-- 登记现有确实是"只含公开信息"的视图，并逐条写明理由
INSERT INTO public_view_registry (viewname, reason) VALUES
  ('v_column_policy_coverage',
   '每个表的**列级策略分布**（各等级多少列）+ 依据来源分布。纯计数，不含取值。'),
  ('v_column_profile_public',
   '**只含 T0 公开列**的统计（已按 column_policy 过滤）。注意与 column_profile 的区别：'
   '后者含全部列的 Top-K 取值，只给 T3。'),
  ('v_profile_exposure',
   '剖析数据的三级暴露面（谁能看到什么粒度）。策略元数据，公开可读。'),
  ('v_access_baseline_check',
   '各等级角色是否拿到模式使用/审计写入/列权限/视图授权。基线自检的可查形式。'),
  ('v_session_function_grants',
   '会话与审计函数的执行权分布。安全元数据，公开可读（判据见迁移 026）。'),
  ('v_age_band_public',
   '**年龄段分布**（各段人数，AG1..AG6/AG9）。刻意不含 person_id —— '
   '逐人年龄段需要主体标识才能对上人，而主体标识已按披露控制收进 T1（030）。'
   '这个视图是"年龄可公开"（037 用户决策）的落地形式。'),
  ('v_export_control',
   '导出控制一览（哪些列禁导、其最低可读等级）。策略元数据，公开可读。'),
  ('v_bridge_privileges',
   '小程序接入角色的权限一览。安全元数据，公开可读。'),
  ('v_web_session_activity',
   '会话活动（谁什么时候登录过）。**注意：它含 email**，属于 T2 级信息 —— '
   '所以**不在**本表里；见下面的自检它会拒绝登记。')
ON CONFLICT (viewname) DO UPDATE
   SET reason = EXCLUDED.reason, added_at = now(), reviewer = 'migration_038';

-- 上一条演示用的记录必须删掉：v_web_session_activity 含 email，不该对匿名开放。
-- （把它写进 INSERT 再删掉，是为了在迁移里留下"这个判断被想过"的痕迹。）
DELETE FROM public_view_registry WHERE viewname = 'v_web_session_activity';

-- 视图可见性由注册表驱动的基线函数
CREATE OR REPLACE FUNCTION apply_role_baseline() RETURNS void
LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  r text;
  v RECORD;
  n_public int;
  tiers constant text[] := ARRAY['mt_t0','mt_t1','mt_t2','mt_t3'];
BEGIN
  FOREACH r IN ARRAY tiers LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA mt TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON mt.access_log FROM %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO %I', r);
    EXECUTE format('GRANT EXECUTE ON FUNCTION mt.public_counts() TO %I', r);
    EXECUTE format('GRANT SELECT ON mt.access_tier, mt.column_policy, '
                   'mt.v_column_policy_coverage TO %I', r);
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON mt.access_tier, mt.column_policy FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_session_new(text,text,text,interval,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON FUNCTION mt.web_user_add(text,text,text,text) FROM %I', r);
    EXECUTE format('REVOKE ALL ON mt.column_profile FROM %I', r);
  END LOOP;

  -- 列级剖析快照（含 Top-K 取值）：只给最高等级
  EXECUTE 'REVOKE ALL ON mt.column_profile FROM mt_t0, mt_t1, mt_t2';
  EXECUTE 'GRANT SELECT ON mt.column_profile TO mt_t3';

  -- 视图：**注册表驱动**
  --   在 public_view_registry 里的 → 全部等级（含匿名）
  --   未登记的 → 只给 T3（安全默认：视图以属主权限执行，绕过列级策略）
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'mt' LOOP
    IF EXISTS (SELECT 1 FROM public_view_registry pvr WHERE pvr.viewname = v.viewname) THEN
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t0, mt_t1, mt_t2, mt_t3', v.viewname);
    ELSE
      EXECUTE format('REVOKE ALL ON mt.%I FROM mt_t0, mt_t1, mt_t2', v.viewname);
      EXECUTE format('GRANT SELECT ON mt.%I TO mt_t3', v.viewname);
    END IF;
  END LOOP;

  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.public_counts() TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';

  EXECUTE 'GRANT USAGE ON SCHEMA mt TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.access_tier, mt.column_policy, mt.v_column_policy_coverage TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.log_access(text, text, text, integer, text, jsonb) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.public_counts() TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_lookup(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_session_revoke(text) TO mt_portal';
  EXECUTE 'GRANT EXECUTE ON FUNCTION mt.web_login(text, text, interval, text) TO mt_portal';
  EXECUTE 'GRANT SELECT ON mt.public_view_registry TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3';
  -- 门户还需要这两个视图（它自己读会话与策略）
  EXECUTE 'GRANT SELECT ON mt.v_web_session_activity, mt.v_session_function_grants, '
          'mt.v_column_profile_coverage TO mt_portal';

  SELECT count(*) INTO n_public FROM public_view_registry;
  RAISE NOTICE '视图授权已按注册表重建：公开 % 个，其余只给 T3', n_public;
END $$;

COMMENT ON FUNCTION apply_role_baseline() IS
  '角色基线权限。视图可见性由 **mt.public_view_registry** 驱动（038 起）：'
  '登记的授给全部等级，未登记的只给 T3。'
  '为什么改成注册表：此前是函数里的字面清单，每加一个公开视图就要改函数一次，'
  '而"忘了改"的表现是新视图被静默收回（036 建 v_age_band_public 时实测踩到）。'
  '这也是同类缺陷第四次（018/019/020/029），所以这次改结构而不是补清单。';

-- 自检
DO $$
DECLARE bad text[] := ARRAY[]::text[]; v text;
BEGIN
  -- ① 注册表里的名字必须真实存在且是视图（否则"登记了以为生效了"）
  FOR v IN SELECT viewname FROM public_view_registry LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_views WHERE schemaname='mt' AND viewname = v) THEN
      bad := bad || ('登记了不存在的视图 ' || v);
    END IF;
  END LOOP;
  -- ② 含 email 的会话活动视图**不能**在注册表里（它是 T2 信息）
  IF EXISTS (SELECT 1 FROM public_view_registry WHERE viewname='v_web_session_activity') THEN
    bad := bad || 'v_web_session_activity 含 email，不该对匿名开放';
  END IF;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION '公开视图注册表不自洽：%', array_to_string(bad, '；');
  END IF;
  RAISE NOTICE '公开视图注册表自检通过';
END $$;

SELECT apply_column_grants();

-- 对账之后必须真的生效（这正是 036 那次失败的地方）
DO $$
BEGIN
  IF NOT has_table_privilege('mt_t0', 'mt.v_age_band_public', 'SELECT') THEN
    RAISE EXCEPTION '对账后匿名仍读不到 v_age_band_public —— 注册表机制没生效';
  END IF;
  IF has_table_privilege('mt_t0', 'mt.v_web_session_activity', 'SELECT') THEN
    RAISE EXCEPTION '匿名读到了 v_web_session_activity（含 email）—— 安全默认失效';
  END IF;
  RAISE NOTICE '对账后自检通过：公开视图对匿名可见，含个人信息的视图仍只给 T3';
END $$;

COMMIT;
