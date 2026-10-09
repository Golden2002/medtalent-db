-- ============================================================================
-- 医学生人才信息库 · 034 审计写入不要求应用拥有审计表权限（SECURITY DEFINER）v1.0.0
--
-- 问题：最小权限把审计触发器打回了（迁移 033 之后实测）
-- ---------------------------------------------------------------------------
-- 把 bridge 换成最小权限角色 mt_bridge 后，第一次写入就报：
--     permission denied for table change_log
-- 原因是一条 PostgreSQL 的基本语义：**触发器函数以调用者的身份执行**。
-- `person` / `consent_record` / `external_identity` 等表上挂着 `trg_audit_*`
-- → 触发 `mt.log_change()` → 它 INSERT `change_log` → 以 mt_bridge 身份执行
-- → 而 mt_bridge **没有** change_log 的 INSERT 权限（我也不想给）。
--
-- 为什么"给 mt_bridge 开 change_log 的 INSERT"是错的解法
-- ---------------------------------------------------------------------------
-- 一旦写入者拥有审计表的 INSERT 权限，它就能**伪造审计记录**
-- （写入任何它想写的 actor/object/detail）。审计的全部价值建立在
-- "被审计者不能自己写审计"之上。这与 020 迁移对 `log_access` 的处理是同一个道理：
-- **审计写入只此一条路径，且这条路径以属主身份执行**。
--
-- 正解：把 log_change 改成 SECURITY DEFINER
-- ---------------------------------------------------------------------------
-- 函数以**属主**（postgres）身份插入 change_log，于是：
--   · 任何角色写业务表都会正常产生审计记录，**不需要**审计表的任何权限；
--   · 应用角色对 change_log 依然零权限（不能伪造、不能删、不能改）。
-- 同时保留已有的 `SET search_path TO 'mt','public'` —— SECURITY DEFINER 的经典陷阱
-- 就是 search_path 被调用者劫持（本项目在 020 已经踩过一次并钉死）。
--
-- 顺带：guard_attrs 需要读 attribute_definition（属性白名单字典）
-- ---------------------------------------------------------------------------
-- 它是**校验**函数，不是审计写入，所以不需要 DEFINER ——
-- 字典表本来就是"用来被读的"（只读侧对 T0 都开放）。
-- 给 mt_bridge 加一个 SELECT 即可，比把校验函数提权更小的面。
--
-- 可重放：CREATE OR REPLACE FUNCTION + 幂等 GRANT。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE FUNCTION log_change() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER                     -- 以属主身份写审计：写入者不需要 change_log 的任何权限
SET search_path TO 'mt', 'public'    -- 钉死 search_path（DEFINER 的必做项）
AS $function$
DECLARE
  rec   jsonb;
  k     text;
  ident text := 'unknown';
BEGIN
  rec := to_jsonb(COALESCE(NEW, OLD));
  FOR k IN SELECT jsonb_object_keys(rec) LOOP
    IF k = 'id' OR k LIKE '%\_id' THEN
      ident := k || '=' || COALESCE(rec ->> k, '');
      EXIT;
    END IF;
  END LOOP;

  INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
  VALUES (current_user, TG_TABLE_NAME, ident, lower(TG_OP),
          jsonb_build_object('op', TG_OP, 'table', TG_TABLE_NAME, 'row', ident));
  RETURN NULL;   -- AFTER 触发器返回值被忽略
END;
$function$;

COMMENT ON FUNCTION log_change() IS
  '审计触发器：把业务表的变更写进 change_log。**SECURITY DEFINER** —— '
  '触发器以调用者身份执行，若不以属主身份写审计，则任何写入者都必须拥有审计表的 '
  'INSERT 权限，而那等于允许它伪造审计记录（020 对 log_access 是同一个道理）。'
  '注意 actor 记的是 `current_user`：在 DEFINER 下它是**函数属主**，'
  '所以这一列表达的是"由审计机制写入"，而不是"谁改的"——'
  '"谁改的"要看业务表自己的 updated_by/来源字段。这个语义边界必须写明，'
  '否则会把 actor 误读成操作者。';

-- 校验函数需要读的属性白名单字典：给 bridge 只读权限（字典本就是用来读的）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
              WHERE table_schema='mt' AND table_name='attribute_definition') THEN
    EXECUTE 'GRANT SELECT ON mt.attribute_definition TO mt_bridge';
  END IF;
END $$;

-- 自检：把"审计不依赖写入者权限"变成可查的事实
DO $$
DECLARE before_n bigint; after_n bigint;
BEGIN
  -- ① mt_bridge 确实**没有** change_log 的写权限
  IF has_table_privilege('mt_bridge', 'mt.change_log', 'INSERT') THEN
    RAISE EXCEPTION 'mt_bridge 拥有 change_log 的 INSERT —— 它可以伪造审计记录';
  END IF;

  -- ② 以 mt_bridge 身份真实写一行，审计必须照样产生
  --    ⚠ 第一版这里写成"插入后立刻 RAISE 回滚"，结果 after_n == before_n 而断言失败 ——
  --    因为审计触发器是 **AFTER** 触发器，它写的审计行**在同一个事务里**，
  --    回滚数据的同时也回滚了审计。这不是缺陷，反而说明了审计与业务数据是同事务的
  --    （"改了但没留痕"不可能发生）。所以这里改成"真写 → 测量 → 显式清理"。
  SELECT count(*) INTO before_n FROM change_log;
  SET LOCAL ROLE mt_bridge;
  INSERT INTO person (person_id, subject_code, enroll_channel, verify_status,
                      access_tier, status)
  VALUES ('per_audit_probe_034', 'MT-PROBE-034', 'EC3', 'V1', 'T1', 'active');
  RESET ROLE;
  SELECT count(*) INTO after_n FROM change_log;

  IF after_n <= before_n THEN
    RAISE EXCEPTION '以 mt_bridge 身份写入没有产生审计记录（before=% after=%）',
                    before_n, after_n;
  END IF;
  RAISE NOTICE '审计自检通过：bridge 无审计表权限，但写入仍然留痕（% → %）',
               before_n, after_n;

  -- 清理探测痕迹（审计行也要清，否则留下的是一条指向不存在对象的记录）
  DELETE FROM change_log WHERE object_type = 'person'
                         AND detail->>'row' = 'person_id=per_audit_probe_034';
  DELETE FROM person WHERE person_id = 'per_audit_probe_034';
END $$;

COMMIT;
