-- ============================================================================
-- 医学生人才信息库 · 015 由体检发现的维度注册表修正 v1.0.0
--
-- 本迁移的每一条修改都来自 code/analytics/dimension_check.py 的**实测输出**，
-- 不是设计时的猜测。原样记录证据，方便日后复核：
--
-- 【问题 1】DIM_CREDENTIAL 与 DIM_TRAINING 读的是同一列
--   两者的 person_locator 都是 `credential.credential_type`。
--   实测（120 人，平均 Jaccard）= 1.000 —— 也就是**同一份数据被两个维度
--   各计一次权重**（权重 1.0 与 0.9，合计 1.9 重复计权）。
--   这不是"相关"，是同一列。
--   修法：规培维度只取规培类证照 —— person_locator 改为带值过滤的
--   `credential.credential_type:C02,C03`（C02 住院医师规范化培训合格证、
--   C03 专科医师规范化培训合格证）。
--   为什么用过滤后缀而不是在组装层写 if dimension_id == 'DIM_TRAINING'：
--   过滤条件属于**维度的定义**，定义该留在注册表里；写进代码就等于把
--   数据字典拆成两半，改一个维度要改代码。组装层只负责执行注册表说的口径。
--
-- 【问题 2】DIM_HEALTH_LIMIT 在人侧零方差
--   实测 120 人取值完全相同（信息量为 0）。它是 gate 角色、权重 0.3，
--   但岗位侧 job_locator = unavailable，所以永远不会两侧可评 ——
--   也就是说它既不拦人也不贡献分数，只在报告里占一行。
--   修法：不改权重（合规上它确实是门禁语义），改为在 note 里写明
--   "人侧零方差、岗位侧无落点，当前不产生任何匹配作用"，让读到它的人
--   不必自己重新推一遍。
--
-- 【不改的】其余 30 个人侧有值但岗位侧无落点的维度**保持原样**：
--   它们记录的是"岗位侧应当采集什么"（见 job_gap 列），是待办而不是错误。
--   把权重清零会把"缺口"伪装成"已经不需要了"。
--
-- 可重放：两条 UPDATE 都是幂等的（按 dimension_id 定位，写成确定值）。
-- 不修改 001-014 任何文件；不建表不加列。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 1. 规培维度只取规培类证照，消除与 DIM_CREDENTIAL 的重复计权
UPDATE dimension
   SET person_locator = 'credential.credential_type:C02,C03',
       note = COALESCE(note || ' ', '') ||
              '【015 修正】原 person_locator 为 credential.credential_type，'
              '与 DIM_CREDENTIAL 完全同源（实测平均 Jaccard=1.000，属同一份数据'
              '重复计权）。现按 C02/C03 过滤为规培类证照。',
       version = '1.1.0',
       updated_at = now()
 WHERE dimension_id = 'DIM_TRAINING';

-- 2. 把零方差结论写进 note，避免下一个人重新推一遍
UPDATE dimension
   SET note = COALESCE(note || ' ', '') ||
              '【015 体检结论】人侧实测零方差（120 人取值完全相同，信息量 0）；'
              '岗位侧无落点 → 当前不参与任何匹配判定，仅作合规门禁的语义占位。',
       version = '1.1.0',
       updated_at = now()
 WHERE dimension_id = 'DIM_HEALTH_LIMIT';

-- 3. 记一笔变更，说明这两个维度为什么动过
INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
SELECT session_user, 'dimension', x.did, 'update', '1.1.0',
       jsonb_build_object('reason', x.why, 'found_by', 'code/analytics/dimension_check.py')
  FROM (VALUES
    ('DIM_TRAINING',      '与 DIM_CREDENTIAL 同源（Jaccard=1.000），改为按 C02/C03 过滤'),
    ('DIM_HEALTH_LIMIT',  '人侧零方差、岗位侧无落点，结论写入 note')
  ) AS x(did, why)
 WHERE EXISTS (SELECT 1 FROM dimension d WHERE d.dimension_id = x.did);

COMMIT;
