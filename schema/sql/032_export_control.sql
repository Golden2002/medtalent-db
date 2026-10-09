-- ============================================================================
-- 医学生人才信息库 · 032 导出禁令的强制点（把动作级 X 落到物理列）v1.0.0
--
-- 问题（本项目的指标体系一直把它列为空洞）
-- ---------------------------------------------------------------------------
-- `mt.access_policy` 里有 5 条动作级策略：`action='export_row'`、`min_tier='X'`
--   ap_jobraw_row  F_JOB_RAW_REF          job_posting.raw_text_ref
--   ap_hl_row      F_PERSON_HEALTH_LIMITS person_demographics.health_limits
--   ap_pii_cont_r  F_PERSON_PII_CONTACT   person_pii.phone_enc / email_enc / ...
--   ap_pii_id_r    F_PERSON_PII_ID_HASH   person_pii.id_hash
--   ap_pii_name_r  F_PERSON_PII_NAME      person_pii.full_name_enc
-- 但**列级策略（column_policy）里 X 数为 0** —— 也就是说"禁止导出"只是文档，
-- 门户的 CSV 导出（/talent.csv、/t/*.csv、SQL 结果 CSV、图表 CSV）照样会把它导出去。
-- 而导出是真实的泄露渠道：**页面上看得到 ≠ 允许批量拿走**。
--
-- 为什么不能靠"字段 id → 列名"的约定自动推导
-- ---------------------------------------------------------------------------
-- 实测：按约定（去掉 F_<实体缩写>_ 前缀再比对列名）**5 条里只对上 1 条**。
--   · person_pii 的物理列叫 full_name_enc / phone_enc / id_hash，与
--     pii_name / pii_contact / pii_id_hash 对不上；
--   · job_posting 的物理列叫 raw_text_ref，与 raw_ref 对不上。
-- 这与"字段字典只覆盖 8.3% 物理列"是同一个根因。所以这里**显式登记**，
-- 并把"约定能对上几条"这件事写进注释 —— 它本身就是字典覆盖率的证据。
--
-- 设计：显式表 + 一条硬断言
-- ---------------------------------------------------------------------------
--   · `export_denied` 逐列登记"哪个策略禁止导出它"，`because` 引用 policy_id，
--     可审计、可评审（reviewed_at）；
--   · 硬断言：**每一条 export_row=X 策略都必须至少有一个落点列**，
--     否则迁移直接失败 —— 这样"禁令没有强制点"这件事不可能再悄悄存在。
--
-- ⚠ 与列级权限的关系（必须分清）
--   · `column_policy.min_tier` 管的是"**能不能看**"；
--   · `export_denied` 管的是"**能不能批量带走**"。
--   两者是不同的动作，所以同名一列可以"能看、不能导"（这正是这 5 条的意图）。
--   本迁移**不**改列级等级 —— 改了会连"看"一起掐掉，那不是这些策略要表达的。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + 幂等 INSERT。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS export_denied (
  table_name  text NOT NULL,
  column_name text NOT NULL,
  policy_id   text,                    -- 引用 access_policy.policy_id（可审计）
  because     text NOT NULL,           -- 为什么禁止导出（人话）
  reviewed_at timestamp with time zone NOT NULL DEFAULT now(),
  reviewer    text NOT NULL DEFAULT 'migration_032',
  PRIMARY KEY (table_name, column_name)
);
COMMENT ON TABLE export_denied IS
  '导出禁令的**显式落点**：动作级策略 access_policy(action=''export_row'', min_tier=''X'') '
  '在此逐列登记。与 column_policy 的分工：column_policy 管"能不能看"，本表管"能不能带走"。'
  '一列可以"能看、不能导" —— 页面上给你看，但不允许批量拿走。';

-- 逐列登记（5 条策略 → 9 个物理列）
INSERT INTO export_denied (table_name, column_name, policy_id, because) VALUES
  ('person_demographics', 'health_limits', 'ap_hl_row',
   '体检受限项：个体健康信息，允许在页面内查看（view_inline=T3），但不允许批量导出'),
  ('person_pii', 'full_name_enc', 'ap_pii_name_r',
   '姓名密文：允许管理员查看，不允许导出密文（密文一旦离开本库，离线爆破的风险就转嫁给对方）'),
  ('person_pii', 'phone_enc', 'ap_pii_cont_r', '联系方式密文：同上'),
  ('person_pii', 'email_enc', 'ap_pii_cont_r', '联系方式密文：同上'),
  ('person_pii', 'wechat_enc', 'ap_pii_cont_r', '联系方式密文：同上'),
  ('person_pii', 'emergency_contact_enc', 'ap_pii_cont_r', '紧急联系人密文：同上'),
  ('person_pii', 'id_hash', 'ap_pii_id_r',
   '证件号哈希：虽然不可逆，但导出后可用于跨库关联比对，仍属禁止导出'),
  ('job_posting', 'raw_text_ref', 'ap_jobraw_row',
   'JD 原文引用：原文可能含发布方联系方式等第三方信息，禁止批量导出'),
  ('job_posting', 'source_url', 'ap_jobraw_row',
   '原文链接：与原文引用同一理由（点开即达原文），禁止批量导出')
ON CONFLICT (table_name, column_name) DO UPDATE
   SET policy_id = EXCLUDED.policy_id, because = EXCLUDED.because,
       reviewed_at = now(), reviewer = 'migration_032';

-- 硬断言：**每一条 export_row=X 策略都必须有落点**。
-- 这一条把"禁令有空转"变成迁移层面的不可能 —— 指标 I4.4 从此是 5/5 而不是 5/0。
DO $$
DECLARE r RECORD; n int;
BEGIN
  FOR r IN SELECT policy_id, object_id FROM access_policy
            WHERE action = 'export_row' AND min_tier = 'X' LOOP
    SELECT count(*) INTO n FROM export_denied WHERE policy_id = r.policy_id;
    IF n = 0 THEN
      RAISE EXCEPTION '策略 %（%）禁止导出，但在 export_denied 里没有任何落点列 —— '
                      '那就是一条空转的禁令', r.policy_id, r.object_id;
    END IF;
  END LOOP;
  RAISE NOTICE '导出禁令自检通过：每条 export_row=X 策略都有物理落点';
END $$;

-- 可查视图：把"能看"与"能导"摆在一起，让两者的差异一眼可见
CREATE OR REPLACE VIEW v_export_control AS
SELECT ed.table_name, ed.column_name, ed.policy_id,
       cp.min_tier AS 最低可读等级,
       (cp.min_tier IS NOT NULL AND cp.min_tier <> 'X') AS 页面内可读,
       false AS 可导出,
       ed.because, ed.reviewed_at
  FROM export_denied ed
  LEFT JOIN column_policy cp
         ON cp.table_name = ed.table_name AND cp.column_name = ed.column_name;
COMMENT ON VIEW v_export_control IS
  '导出控制一览：同一列"能不能看"（column_policy）与"能不能导"（export_denied）。'
  '"能看不能导"是刻意支持的组合。';

GRANT SELECT ON export_denied, v_export_control TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE INSERT, UPDATE, DELETE ON export_denied FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

COMMIT;
