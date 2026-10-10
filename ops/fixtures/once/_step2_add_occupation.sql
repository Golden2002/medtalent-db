-- ============================================================================
-- 第 2 件事实测：新增一个职业（职业数据库的一行）
-- 直接插入是"最短路径"，但要知道它的代价：**不会留演变记录**
-- （occupation_change 是给 discover/promote 那条证据驱动的路径用的）。
-- 所以这里把审计写进 change_log，保证"谁在什么时候加了什么"可追。
-- ============================================================================
BEGIN;
SET search_path TO mt, public;

-- 先看父节点存不存在（新增前必须确认，否则就是悬挂引用）
SELECT occupation_id, label_zh, level, family FROM occupation WHERE occupation_id = 'OCC-F02-01';

INSERT INTO occupation (
  occupation_id, label_zh, label_en, level, parent_id, family,
  medical_reliance, transition_ease, description,
  code_status, code_source, status)
VALUES (
  'OCC-F02-99', '测试职业（演示用）', 'Demo Occupation', 3, 'OCC-F02-01', 'F02',
  2, 4, '本地测试指南的演示数据，测试完成后请删除', 'N', 'manual', 'active');

-- 审计：谁加的、加在哪、为什么（直接插入不会自动留痕，所以显式写）
INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
VALUES (session_user, 'occupation', 'OCC-F02-99', 'add',
        jsonb_build_object('op', 'INSERT', 'table', 'occupation', 'row',
                           'occupation_id=OCC-F02-99',
                           'note', '测试指南演示：新增职业'));
COMMIT;

-- 验证：它进树了吗？层级/父节点/族对不对？
SELECT occupation_id, label_zh, level, parent_id, family, status
  FROM occupation WHERE occupation_id = 'OCC-F02-99';

-- 验证：它在树里的位置（父 → 自己）
SELECT parent.label_zh AS 父节点, child.label_zh AS 新节点, child.level AS 层级
  FROM occupation child JOIN occupation parent ON parent.occupation_id = child.parent_id
 WHERE child.occupation_id = 'OCC-F02-99';

-- 验证：审计留痕
SELECT actor, object_name, change_type, detail->>'note' AS 说明
  FROM change_log WHERE object_name = 'OCC-F02-99' ORDER BY change_id DESC LIMIT 1;
