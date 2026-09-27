-- ============================================================================
-- 医学生人才信息库 · 门禁回归测试
-- 用法：python ops/pg.py sql ops/tests/regression_guards.sql
-- 设计：全部在事务内执行并最终 ROLLBACK，不留残留数据；可重复运行。
-- 约定：每个用例要么 RAISE NOTICE 'PASS: ...'，要么 RAISE EXCEPTION（脚本中止）。
-- ============================================================================
SET search_path TO mt, public;
BEGIN;

-- ---------------------------------------------------------------------------
-- 清理：移除历史测试残留（本事务最终 ROLLBACK，不会影响真实数据）
-- ---------------------------------------------------------------------------
DELETE FROM field_value WHERE value_id LIKE 'fvl_test%';
DELETE FROM observation_window WHERE window_id LIKE 'ow_test%';
DELETE FROM person WHERE person_id LIKE 'per_test%';

-- ---------------------------------------------------------------------------
-- 准备：登记一个扩展属性（机制 B 的正常路径）
-- ---------------------------------------------------------------------------
INSERT INTO attribute_definition (attr_key, entity_id, title, data_type, status)
VALUES ('person_only_child', 'person', '是否独生子女', 'boolean', 'active')
ON CONFLICT (attr_key) DO NOTHING;

-- 用例 1：未登记的 attrs 键必须被拒绝
DO $$
BEGIN
  BEGIN
    INSERT INTO person (person_id, subject_code, attrs)
    VALUES ('per_test0001', 'MT-TEST-1', '{"未登记字段": 1}'::jsonb);
    RAISE EXCEPTION 'FAIL: 未登记的 attrs 键竟被接受';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 1: 未登记 attrs 键被拒绝';
  END;
END $$;

-- 用例 2：已登记的 attrs 键必须被接受
DO $$
BEGIN
  INSERT INTO person (person_id, subject_code, attrs)
  VALUES ('per_test0001', 'MT-TEST-1', '{"person_only_child": true}'::jsonb);
  RAISE NOTICE 'PASS 2: 已登记 attrs 键写入成功';
END $$;

-- 用例 3：field_value 引用未登记字段必须被拒绝
DO $$
BEGIN
  BEGIN
    INSERT INTO field_value (value_id, field_id, subject_type, subject_id, value_num)
    VALUES ('fvl_test_bad', 'F_NOT_EXIST', 'person', 'per_test0001', 1);
    RAISE EXCEPTION 'FAIL: 未登记 field_id 竟被接受';
  EXCEPTION WHEN foreign_key_violation THEN
    RAISE NOTICE 'PASS 3: 未登记 field_id 被拒绝';
  END;
END $$;

-- 用例 4：field_value 引用已登记字段必须成功
DO $$
BEGIN
  INSERT INTO field_value (value_id, field_id, subject_type, subject_id, value_num)
  VALUES ('fvl_test_ok', 'F_PERSON_BIRTH_YEAR', 'person', 'per_test0001', 1999);
  RAISE NOTICE 'PASS 4: 已登记字段写入成功';
END $$;

-- 用例 5：可迁移性 ≥3 但无迁移说明，必须被拒绝
DO $$
BEGIN
  BEGIN
    INSERT INTO skill_assertion (assertion_id, person_id, concept_id, level,
                                 transferability, transfer_note)
    VALUES ('skl_test_bad', 'per_test0001', 'con_test_concept', 3, 4, NULL);
    RAISE EXCEPTION 'FAIL: 高可迁移性缺 transfer_note 竟被接受';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 5: 可迁移性≥3 缺说明被拒绝';
  END;
END $$;

-- 用例 6：薪资区间倒挂必须被拒绝
DO $$
BEGIN
  BEGIN
    INSERT INTO job_posting (job_id, title_raw, raw_text_ref, raw_sha256,
                             salary_min, salary_max)
    VALUES ('job_test_bad', '测试岗位', 'L0://test', 'x', 30000, 10000);
    RAISE EXCEPTION 'FAIL: 薪资倒挂竟被接受';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 6: 薪资区间倒挂被拒绝';
  END;
END $$;

-- 用例 7：观测窗口结束早于开始必须被拒绝
DO $$
BEGIN
  BEGIN
    INSERT INTO observation_window (window_id, person_id, window_type, start_date, end_date)
    VALUES ('ow_test_bad', 'per_test0001', 'W1', DATE '2025-01-01', DATE '2024-01-01');
    RAISE EXCEPTION 'FAIL: 观测窗口时间倒挂竟被接受';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS 7: 观测窗口倒挂被拒绝';
  END;
END $$;

-- 用例 8：合法观测窗口 + 变更流水触发器
DO $$
DECLARE n_before int; n_after int;
BEGIN
  SELECT count(*) INTO n_before FROM change_log;
  INSERT INTO observation_window (window_id, person_id, window_type, start_date, end_date)
  VALUES ('ow_test_ok', 'per_test0001', 'W1', DATE '2020-09-01', DATE '2025-06-30');
  SELECT count(*) INTO n_after FROM change_log;
  IF n_after <= n_before THEN
    RAISE EXCEPTION 'FAIL: change_log 未记录观测窗口插入';
  END IF;
  RAISE NOTICE 'PASS 8: 变更流水已记录（% -> %）', n_before, n_after;
END $$;

-- 用例 9：updated_at 自动维护
-- 注意：now() 在**同一事务内恒定**，所以不能"更新前后各取一次 now() 比较"。
-- 正确做法：INSERT 时显式写一个很旧的 updated_at（touch 只在 BEFORE UPDATE 触发），
-- 再 UPDATE，检查其是否被推进到现在。
DO $$
DECLARE old_ts timestamptz := TIMESTAMPTZ '2000-01-01 00:00:00+08';
        t1 timestamptz;
BEGIN
  INSERT INTO person (person_id, subject_code, updated_at)
  VALUES ('per_test0009', 'MT-TEST-9', old_ts);
  UPDATE person SET status = 'active' WHERE person_id = 'per_test0009';
  SELECT updated_at INTO t1 FROM person WHERE person_id = 'per_test0009';
  IF t1 <= TIMESTAMPTZ '2001-01-01 00:00:00+08' THEN
    RAISE EXCEPTION 'FAIL: updated_at 未被触发器刷新（仍为 %）', t1;
  END IF;
  RAISE NOTICE 'PASS 9: updated_at 已自动刷新（% -> %）', old_ts, t1;
END $$;

-- 用例 10：保留期策略种子可读
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM retention_policy;
  IF n < 6 THEN
    RAISE EXCEPTION 'FAIL: 保留期策略不足（%）', n;
  END IF;
  RAISE NOTICE 'PASS 10: 保留期策略 % 条', n;
END $$;

ROLLBACK;

-- ===========================================================================
-- 第二段：**真实数据**的完整性不变量（不回滚，只读检查）
-- ---------------------------------------------------------------------------
-- 依据 append-only 原则：occupation 节点从不物理删除（退役只改 status），
-- 因此任何"指向不存在节点的引用"都是脏数据，说明有人绕过了正常路径。
-- 这组检查是在问题 #34 之后补的：演化测试清理时删掉了增量节点却留下
-- occupation_change.to_ids 指向它们，表面上像"某职业被拆分过"。
-- ===========================================================================

-- 不变量 1：occupation_change.to_ids 不得悬空
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(change_id, '、') INTO bad FROM occupation_change ch
   WHERE coalesce(array_length(ch.to_ids, 1), 0) > 0
     AND NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = ANY(ch.to_ids));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'FAIL: occupation_change 的 to_ids 悬空（节点从不物理删除）：%', bad;
  END IF;
  RAISE NOTICE 'PASS 11: 变更记录无悬空 to_ids';
END $$;

-- 不变量 2：occupation_migration 的 old_id / new_id 不得悬空
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(migration_id, '、') INTO bad FROM occupation_migration m
   WHERE NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = m.old_id)
      OR NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = m.new_id);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'FAIL: occupation_migration 引用不存在节点：%', bad;
  END IF;
  RAISE NOTICE 'PASS 12: 迁移记录无悬空引用';
END $$;

-- 不变量 3：退役必须留下决策记录（split/merge/retire 都会写 occupation_change）
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(o.occupation_id, '、') INTO bad FROM occupation o
   WHERE o.status <> 'active'
     AND NOT EXISTS (SELECT 1 FROM occupation_change ch
                     WHERE o.occupation_id = ANY(ch.from_ids));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'FAIL: 退役节点缺少变更记录（谁决定的？）：%', bad;
  END IF;
  RAISE NOTICE 'PASS 13: 每个退役节点都有变更记录';
END $$;

DO $$ BEGIN RAISE NOTICE '=== 数据完整性不变量全部通过 ==='; END $$;
