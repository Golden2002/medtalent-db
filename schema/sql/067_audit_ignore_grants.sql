-- ============================================================================
-- 医学生人才信息库 · 067 补授权与域登记 + 验证 065 的机制 v1.0.0
--
-- 【这个迁移本身就是 065 那次改进的证明】
-- 066 新建了 `audit_ignore_columns`（簿记列名单），但**忘了两件事**：
--   ① 没给它授权  → 门禁 I1.8「无任何等级可读的对象数」**当场检出**（1 个）；
--   ② 没登记业务域 → 会触发门禁 I1.2（未归类的基础表）。
-- 两件事都属于同一类："建表的时候顺手要做的登记没做"。
--
-- 065 的改进就是把 ② 变成"可以在同一个迁移里声明"：
--   `INSERT INTO mt.domain_registry (table_name, domain) VALUES (...)`
-- 于是"建表"与"归类"重新回到一处。本迁移补上这两件，并把 ① 也纳入同一段。
--
-- 【顺带修一个只会在失败路径上出现的 bug】
-- I1.8 的实现里写的是 `r[0]`，而连接用的是 dict_row ——
-- `no_grants` 为空时那行代码根本不执行，所以一直没暴露；
-- 这次真的检出违规，它就先崩了，表现成"体检挂了"而不是"有张表没授权"。
-- 已改为按列名取。**这类 bug 只有"让检查真的失败一次"才能发现** ——
-- 所以每加一条门禁，都要确认它在**违规时**的行为，而不只是通过时的行为。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ① 授权：它是**定义层**元数据（"哪些列算簿记列"），不含个人数据，与其它注册表同级
REVOKE ALL ON audit_ignore_columns FROM PUBLIC;
GRANT SELECT ON audit_ignore_columns TO mt_t0, mt_t1, mt_t2, mt_t3;

-- ② 域登记：建表时本该在同一个迁移里做（见 065）
INSERT INTO domain_registry (table_name, domain, declared_by, note) VALUES
('audit_ignore_columns', '治理与合规', 'migration_067',
 '簿记列名单：只改这些列不算业务变化，因此不写审计（迁移 066 引入）。'
 '归治理与合规域，因为它定义的是"什么算需要留痕的变化"。')
ON CONFLICT (table_name) DO UPDATE
  SET domain = EXCLUDED.domain, declared_by = EXCLUDED.declared_by, updated_at = now();

-- ③ 断言：补完之后两条门禁都必须干净
DO $$
DECLARE n int;
BEGIN
  IF has_table_privilege('mt_t0','mt.audit_ignore_columns','SELECT') IS NOT TRUE THEN
    RAISE EXCEPTION 'audit_ignore_columns 对匿名不可读 —— 它属公开的定义层';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM domain_registry WHERE table_name='audit_ignore_columns') THEN
    RAISE EXCEPTION 'audit_ignore_columns 没登记业务域';
  END IF;
  -- 与 I1.8 同一判据：谁都读不到、且没登记为刻意锁 → 违规
  SELECT count(*) INTO n FROM pg_class c
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname='mt' AND c.relkind IN ('r','v')
     AND c.relname NOT IN (SELECT object_name FROM locked_object_registry)
     AND NOT (has_table_privilege('mt_t0', c.oid,'SELECT')
           OR has_table_privilege('mt_t1', c.oid,'SELECT')
           OR has_table_privilege('mt_t2', c.oid,'SELECT')
           OR has_table_privilege('mt_t3', c.oid,'SELECT')
           OR has_any_column_privilege('mt_t0', c.oid,'SELECT')
           OR has_any_column_privilege('mt_t1', c.oid,'SELECT')
           OR has_any_column_privilege('mt_t2', c.oid,'SELECT')
           OR has_any_column_privilege('mt_t3', c.oid,'SELECT'));
  IF n > 0 THEN
    RAISE EXCEPTION '仍有 % 个对象谁都读不到且未登记为刻意锁', n;
  END IF;
  SELECT count(*) INTO n FROM domain_registry;
  RAISE NOTICE '授权与域登记补齐；域登记共 % 行；"谁都读不到"的对象为 0', n;
END $$;

COMMIT;
