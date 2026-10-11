-- ============================================================================
-- 医学生人才信息库 · 062 补齐新视图的授权 + 让"忘记对账"再也藏不住 v1.0.0
--
-- 【怎么发现的】
-- 把"数据性质卡"放到首页之后，**登录用户也拿到"需要登录"的降级卡**。
-- 查下去：`v_job_nature`（061 新建）**一个授权都没有**，t3 读不到、t0 也读不到。
--
-- 【这是第三次踩同一个坑】
--   048：新建视图没进 public_view_registry → 授权飘（"取决于对账有没有跑过"）
--   052：DROP + CREATE 视图带走 GRANT → 显式重授
--   061：新建视图**完全没跑对账** → 连 T3 都读不到
-- 根因是同一件事：**本库的授权由对账（refresh_column_policy + apply_column_grants
-- + apply_role_baseline）统一生成，新建对象如果不在对账范围内，就等于没授权。**
-- 而且它的表现是"页面降级/空白"，看起来像功能没做，**不像权限问题**。
--
-- 【所以这一版除了补授权，还要让这个坑不可能再静默发生】
-- 给健康指标加一条 **I1.6「无任何等级可读的视图/表」= MUST 0**：
--   扫 mt 下所有视图与基础表，找出"对 mt_t0..mt_t3 全都不可读"的对象。
--   这种对象要么是忘了对账，要么是刻意收紧 —— 无论哪种都必须**显式说明**，
--   不能靠人去想起来。加了这条之后，061 那次会被门禁当场抓住。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 1. 跑对账（视图与列级授权都由它生成）
SELECT apply_column_grants();
SELECT apply_role_baseline();

-- ---------------------------------------------------------------------------
-- 2. "刻意锁住"的对象要有**显式登记**，不能靠人去想起来
--
-- 扫完之后只剩 1 个对象对任何等级都不可读：`login_attempt`（登录失败限流流水，
-- 含邮箱）。它是**刻意锁的** —— 任何数据等级都不该读它，只有门户应用角色在用。
-- 但"刻意锁"和"忘了授权"在数据库里长得一模一样，
-- 所以必须有一份登记表把**意图**写下来：
--   未登记 + 谁都读不到 = 违规（门禁报错）
--   已登记 + 谁都读不到 = 明确的设计（并写明理由）
-- 这与 `public_view_registry`（登记的视图对所有人开放）是同一个思路的另一半。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS locked_object_registry (
  object_name TEXT PRIMARY KEY,
  reason      TEXT NOT NULL,
  owner       TEXT NOT NULL DEFAULT 'user',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE locked_object_registry IS
  '"刻意为任何数据等级都不可读"的对象登记表。'
  '存在的意义：**"刻意锁"与"忘了授权"在数据库里长得一模一样**，'
  '所以必须把意图写下来。门禁规则：未登记且谁都读不到 → 违规。';

INSERT INTO locked_object_registry (object_name, reason) VALUES
('login_attempt', '登录失败限流流水，含邮箱与时间。**刻意不对任何数据等级开放** —— '
                  '只有门户应用角色（mt_portal）在登录流程里读写它（见迁移 039/040）。'
                  '它属"运行时安全数据"，不是业务数据，任何等级都不该能查。')
ON CONFLICT (object_name) DO UPDATE SET reason = EXCLUDED.reason;

-- 登记表本身是定义层，给 T0 起可读（它是"怎么读数据"的说明，不含个人数据）
GRANT SELECT ON locked_object_registry TO mt_t0, mt_t1, mt_t2, mt_t3;

-- 3. 硬断言：T3 能读（分析要用）、T0 不能读（含岗位/来源明细，不是公开定义层）
DO $$
DECLARE n int;
BEGIN
  IF NOT has_table_privilege('mt_t3', 'mt.v_job_nature', 'SELECT') THEN
    RAISE EXCEPTION '对账之后 T3 仍读不到 v_job_nature —— 视图不在对账范围内';
  END IF;
  IF has_table_privilege('mt_t0', 'mt.v_job_nature', 'SELECT') THEN
    RAISE EXCEPTION '匿名能读 v_job_nature —— 它不是公开的定义层视图';
  END IF;

  -- 3. 把"无任何等级可读的对象"扫一遍（这条检查同时进 ops/health.py 做常驻门禁）
  --
  -- ⚠ 判据必须同时看**表级**与**列级**权限（实测踩到的坑）：
  -- 本库的基础表用的是**列级授权**，此时 `has_table_privilege` 返回 false
  -- 而表其实是可读的。第一版只查表级，于是误报 92 个"谁都读不到"的对象，
  -- 断言失败 → **整个迁移事务回滚** → 连刚授的视图权限一起被撤销
  -- （表现是"修了反而更坏"，很难查）。改用 `has_any_column_privilege`
  -- 判断"有没有任意一列可读"，这才是"这张表读不读得到"的正确判据。
  SELECT count(*) INTO n FROM (
    SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
     WHERE ns.nspname = 'mt' AND c.relkind IN ('r', 'v')
       AND c.relname NOT IN (SELECT object_name FROM locked_object_registry)
       AND NOT (
         has_table_privilege('mt_t0', c.oid, 'SELECT')
         OR has_table_privilege('mt_t1', c.oid, 'SELECT')
         OR has_table_privilege('mt_t2', c.oid, 'SELECT')
         OR has_table_privilege('mt_t3', c.oid, 'SELECT')
         OR has_any_column_privilege('mt_t0', c.oid, 'SELECT')
         OR has_any_column_privilege('mt_t1', c.oid, 'SELECT')
         OR has_any_column_privilege('mt_t2', c.oid, 'SELECT')
         OR has_any_column_privilege('mt_t3', c.oid, 'SELECT'))) t;
  IF n > 0 THEN
    RAISE EXCEPTION '有 % 个对象对任何等级都不可读、且没在 locked_object_registry 里登记 —— '
      '要么忘了对账，要么需要把"刻意锁"写进登记表', n;
  END IF;
  SELECT count(*) INTO n FROM locked_object_registry;
  RAISE NOTICE '视图授权补齐；"谁都读不到"的对象 % 个，全部已显式登记（含理由）', n;
END $$;

COMMIT;
