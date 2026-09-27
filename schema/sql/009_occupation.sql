-- ============================================================================
-- 医学生人才信息库 · 职业树扩展 v0.6.0（T06）
-- occupation 表补两列：code_status / code_source。
--
-- 为什么要这两列：职业分类的外部码（ISCO-08 / O*NET-SOC / 大典）一旦写错，
-- 下游所有跨库映射都会错。项目纪律是"不写未核验的事实"，但码值又确实需要
-- 一个起步值。折中做法：把核验状态变成**数据**，下游分析只允许用 V（已核验）。
--   V = 已核验（有权威来源，写在 code_source）
--   E = 待核验（估计值，仅供检索线索，禁止用于对外结论）
--   N = 无（该职业在国际标准中没有对应单元组，如 MSL 这类新兴岗位）
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

ALTER TABLE occupation ADD COLUMN IF NOT EXISTS code_status TEXT NOT NULL DEFAULT 'N';
ALTER TABLE occupation ADD COLUMN IF NOT EXISTS code_source TEXT;

ALTER TABLE occupation DROP CONSTRAINT IF EXISTS chk_occ_code_status;
ALTER TABLE occupation ADD CONSTRAINT chk_occ_code_status
  CHECK (code_status IN ('V','E','N'));

-- 受控词表：CT_CODE_STATUS
INSERT INTO code_table (code_table_id, name, description, version, status, owner)
VALUES ('CT_CODE_STATUS', '外部码核验状态', '职业/概念外部标准码的可信度', '1.0.0', 'active', 'research')
ON CONFLICT (code_table_id) DO NOTHING;
INSERT INTO code_value (code_table_id, code, label_zh, label_en, level, sort_order, definition) VALUES
 ('CT_CODE_STATUS','V','已核验','Verified',1,10,'有权威来源，可用于对外结论'),
 ('CT_CODE_STATUS','E','待核验','Estimated',1,20,'估计值，仅作检索线索，禁止用于对外结论'),
 ('CT_CODE_STATUS','N','无对应','None',1,30,'国际标准中无对应单元组（多为新兴岗位）')
ON CONFLICT (code_table_id, code) DO UPDATE SET label_zh=EXCLUDED.label_zh;

-- 职业树主用查询：只取已核验映射
CREATE OR REPLACE VIEW v_occupation_verified AS
SELECT occupation_id, family, label_zh, label_en, level, parent_id,
       medical_reliance, transition_ease, isco08_code, onet_soc_code, cn_occode,
       code_status, code_source, license_required, degree_typical, entry_paths
FROM occupation
WHERE status = 'active' AND code_status = 'V';

-- 待核验清单（工作项）
CREATE OR REPLACE VIEW v_occupation_code_todo AS
SELECT family, occupation_id, label_zh, label_en,
       isco08_code, onet_soc_code, cn_occode, code_status
FROM occupation
WHERE status = 'active' AND code_status <> 'V' AND level >= 3
ORDER BY family, occupation_id;

COMMIT;
