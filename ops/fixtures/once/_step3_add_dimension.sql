-- ============================================================================
-- 第 3 件事实测：新增信息分类变量（= 新增一"列"）
-- 用 mt.add_dimension()：**零 DDL** —— 它在 field_catalog 里登记字段、
-- 自动建词表（CT_*）、并把值存进 field_value。等价于给 person 加了一列，
-- 但不需要 ALTER TABLE，也不需要停服。
-- ============================================================================
BEGIN;
SET search_path TO mt, public;

SELECT mt.add_dimension(
  'person',                                   -- 实体
  'F_PSN_RES_JOB_OUTCOME',                    -- 字段 id（沿用 F_PSN_ 前缀）
  '求职结果',                                  -- 中文标题（表格里就用这个表头）
  '[{"code":"JO1","label":"已就业"},
    {"code":"JO2","label":"待业中"},
    {"code":"JO3","label":"升学深造"},
    {"code":"JO4","label":"出国出境"},
    {"code":"JO9","label":"不愿透露"}]'::jsonb,
  'code',                                     -- 有选项 → 自动建词表并置为 code
  false,                                      -- 非多选
  false,                                      -- 非必填 **（调查必然有人不填，所以不设必填）**
  '毕业时的求职结果（用于分析分类变量的影响）',
  '求职与结果',                                -- 表单分组
  'T1',                                       -- 访问等级：注册用户可见
  'user'
) AS 新字段id;

COMMIT;

-- 验证：字段已登记 + 词表自动建好
SELECT f.field_id, f.title, f.data_type, f.code_table_id, f.access_tier, f.status
  FROM mt.field_catalog f WHERE f.field_id = 'F_PSN_RES_JOB_OUTCOME';

SELECT code, label_zh, sort_order FROM mt.code_value
 WHERE code_table_id = 'CT_F_PSN_RES_JOB_OUTCOME' ORDER BY sort_order;
