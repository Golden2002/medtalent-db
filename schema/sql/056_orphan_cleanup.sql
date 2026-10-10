-- ============================================================================
-- 医学生人才信息库 · 056 清理孤儿行 + 堵住门禁盲区 v1.0.0
--
-- 【怎么发现的】
-- 055 之后按「数据性质」分层，发现两个口径打架：
--   M_SUPPLY_CONSENT_RATE = 113 人    vs    按 person 分层只剩 29 人
-- 差额 84 → 查下去发现 `consent_record` 有 **179 行孤儿**（指向不存在的 person）。
--
-- 【根因有两层，第二层更值得记】
--   ① 数据层：`consent_record` **没有指向 person 的外键**（只有主键），
--      所以"删了 person 却没删 consent_record"不会报错，只会留下孤儿。
--      它属于项目里"无外键但引用 person"的那类表（mock 窗口的 NO_FK_TO_PERSON 里有它）。
--   ② **门禁层（更严重）**：健康指标 I1.1「指向 person 的孤儿行数」是 MUST=0，
--      但它**只动态发现"有外键"的子表** —— 于是孤儿恰好藏在它看不见的地方，
--      指标长期显示 0，**一个 MUST 指标在说谎**。
--      而清理代码（mock 窗口）**早就知道**这些表。两边对"什么算引用"的定义不一致。
--
-- 【修法】
--   · 门禁：I1.1 改为同时覆盖「外键子表」与「有 person_id 列但无外键的表」
--     （都在 ops/health.py 里动态发现，新增表自动纳入）+ field_value（唯一多态表）；
--     修好后立刻报出 179 —— 这就是"盲区是真的"的证据。
--   · 数据：本迁移清掉历史孤儿，并**留下审计**（删了什么、为什么删）。
--   · 防复发：删除 person 必须走 `portal_mockreg.hardclean_persons()`（它会按
--     NO_FK_TO_PERSON 清理无外键子表）；门禁 I1.1 从此会盯住任何漏网。
--
-- 【为什么不给 consent_record 补外键】
-- 补外键是更彻底的修法，但需要先清数据（本迁移做的事），且要考虑
-- `mt_bridge` 写入路径的权限与顺序。本迁移**不擅自加约束** ——
-- 那会改变写入侧的行为，属于需要单独评估的改动。这里先把"看不见"变成"看得见"。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 清理前先留证：删了什么、多少
CREATE TEMP TABLE _orphan_before ON COMMIT DROP AS
SELECT 'consent_record' AS tbl, count(*) AS n
  FROM consent_record c
 WHERE NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = c.person_id);

DO $$
DECLARE n_before bigint; n_after bigint;
BEGIN
  SELECT n INTO n_before FROM _orphan_before WHERE tbl = 'consent_record';
  RAISE NOTICE '清理前 consent_record 孤儿行：%', n_before;

  DELETE FROM consent_record c
   WHERE NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = c.person_id);
  GET DIAGNOSTICS n_after = ROW_COUNT;
  RAISE NOTICE '已删除：%', n_after;

  -- 审计：删数据必须留痕（否则以后没人知道这些同意记录去哪了）
  INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
  VALUES (session_user, 'consent_record', 'orphan_cleanup_056', 'delete',
          jsonb_build_object(
            'op','DELETE','table','consent_record','rows', n_after,
            'note','清理指向不存在 person 的孤儿同意记录（迁移 056）',
            'root_cause','consent_record 无指向 person 的外键；删除 person 时未清理，'
                         '而门禁 I1.1 只查有外键的子表，因此长期不可见',
            'gate_fix','ops/health.py I1.1 改为同时覆盖无外键的 person_id 引用表 + field_value'));
END $$;

-- 清理后必须为零 —— 否则本迁移没解决问题
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM consent_record c
   WHERE NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = c.person_id);
  IF n > 0 THEN
    RAISE EXCEPTION '清理后仍有 % 行孤儿 —— 本迁移没解决问题', n;
  END IF;
  SELECT count(*) INTO n FROM field_value fv WHERE fv.subject_type='person'
     AND NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = fv.subject_id);
  IF n > 0 THEN
    RAISE NOTICE '注意：field_value 仍有 % 行孤儿（本迁移只清 consent_record）', n;
  END IF;
  RAISE NOTICE '孤儿行清理验证通过（consent_record = 0）';
END $$;

COMMIT;
