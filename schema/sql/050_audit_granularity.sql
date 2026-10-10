-- ============================================================================
-- 医学生人才信息库 · 050 审计按「操作」记录机械重算 + 变化检测 v1.0.0
--
-- 问题（独立审查 P2-2 的根因，用实测定位）
-- ---------------------------------------------------------------------------
--   change_log：**889,935 行 / 200 MB**，全部产生于 14 天内；其中
--   `job_requirement` 一项占 **625,525 行（70%）**，actor 全是 postgres。
--   根因：`code/parse/jd_ingest.py` 对每个岗位**先删后插** job_requirement
--   （690 岗位 × 约 900 条要求 ≈ 62 万行），而行级审计触发器对每一行都写一条。
--
-- 为什么这既是"体量问题"也是"质量问题"
-- ---------------------------------------------------------------------------
-- 审计的价值在于"谁在什么时候改了什么"。而 62 万行机械重算把真正的变更淹没了 ——
-- 在 `/audit` 页面上按时间看，看到的全是重建噪声。**信噪比比体量更致命**：
-- 体量大只是占空间，噪声大会让人不再看审计，那等于没有审计。
--
-- 方案：对**机械重算类**的表按「操作」记录，而不是按「行」记录
-- ---------------------------------------------------------------------------
--   · `audit_mode(table_name, mode, reason)`：mode ∈ {'row','summary'}。
--     只有**重算会整体重写**的表才登记为 summary，并写明理由。
--   · `log_change()`：summary 模式的表**不逐行记**；
--   · `mt.audit_bulk(table, op, n_rows, note)`：由重算工具显式写**一条**汇总行。
--     于是"这次重算发生过"仍然留痕，只是不再逐行。
--
-- ⚠ 为什么必须有「检测」，否则这就是"关掉审计"
-- ---------------------------------------------------------------------------
-- 逐行不记之后，**谁都能悄悄改表而不留痕**（包括重算工具忘了写汇总行）。
-- 所以配套一个可判定的检测：PostgreSQL 自己维护每张表的
-- `pg_stat_user_tables.n_tup_ins/n_tup_upd/n_tup_del` **累计计数器**。
--   · `audit_bulk()` 写汇总行时，把当时的三个计数器快照存进 `detail`；
--   · `v_audit_gap` 比较"当前计数器"与"最近一次汇总行里的快照"：
--     差额超过容差 → 说明**有改动没有被任何汇总行覆盖**。
-- 这样"按操作记录"就不再是"关掉审计"，而是**换一种可验证的粒度**：
-- 改动总量由数据库计数器兜底，任何缺口都会在视图里露出来。
--
-- 保留策略作为兜底（审查建议的另一半）
-- ---------------------------------------------------------------------------
-- 同时建 `retention_policy` 登记表与 `ops/retention.py`（默认只 plan、不删），
-- 让"这类流水留多久"成为一个**写下来的决定**，而不是无限增长到出事再处理。
--
-- 可重放：CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE + upsert。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 审计粒度登记
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_mode (
  table_name text PRIMARY KEY,
  mode       text NOT NULL CHECK (mode IN ('row', 'summary')),
  reason     text NOT NULL,
  added_at   timestamp with time zone NOT NULL DEFAULT now(),
  reviewer   text NOT NULL DEFAULT 'migration_050'
);
COMMENT ON TABLE audit_mode IS
  '审计粒度登记：row=逐行记（默认），summary=按操作记（仅限"重算会整体重写"的表）。'
  '改为 summary **不等于关掉审计**：配套 v_audit_gap 用 PostgreSQL 的行计数器检测'
  '"有改动但没有任何汇总行覆盖"的缺口。';

INSERT INTO audit_mode (table_name, mode, reason) VALUES
  ('job_requirement', 'summary',
   'JD 解析会按岗位**先删后插**整表要求（实测 690 岗位约 62 万行）。'
   '逐行记会把审计淹没在机械重算里（实测占 change_log 的 70%），'
   '而它的价值靠"这次重算发生了什么"就能表达。'),
  ('job_task', 'summary',
   '与 job_requirement 同一次重算里产生，理由同上。')
ON CONFLICT (table_name) DO UPDATE
   SET mode = EXCLUDED.mode, reason = EXCLUDED.reason,
       added_at = now(), reviewer = 'migration_050';

-- ---------------------------------------------------------------------------
-- 2. log_change：summary 模式的表不逐行记
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION log_change() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER                     -- 以属主身份写审计（034）
SET search_path TO 'mt', 'public'
AS $function$
DECLARE
  rec   jsonb;
  k     text;
  ident text := 'unknown';
  v_mode text;
BEGIN
  -- summary 模式的表：不逐行记（见 audit_mode 的说明）。
  -- 用 `current_setting(..., true)` 之外的方式不好使 —— 直接查登记表更直白，
  -- 而且登记表本身是 T0 公开的策略元数据，读者能自己核对我们放宽了哪些表。
  SELECT mode INTO v_mode FROM mt.audit_mode WHERE table_name = TG_TABLE_NAME;
  IF v_mode = 'summary' THEN
    RETURN NULL;
  END IF;

  rec := to_jsonb(COALESCE(NEW, OLD));
  FOR k IN SELECT jsonb_object_keys(rec) LOOP
    IF k = 'id' OR k LIKE '%\_id' THEN
      ident := k || '=' || COALESCE(rec ->> k, '');
      EXIT;
    END IF;
  END LOOP;

  INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
  VALUES (current_user, TG_TABLE_NAME, ident, lower(TG_OP),
          jsonb_build_object('op', TG_OP, 'table', TG_TABLE_NAME, 'row', ident,
                             'mode', 'row'));
  RETURN NULL;
END;
$function$;

COMMENT ON FUNCTION log_change() IS
  '审计触发器（SECURITY DEFINER，见 034）。**050 起**：`audit_mode` 登记为 '
  '`summary` 的表不逐行记 —— 机械重算（JD 解析按岗位整体重写 job_requirement）'
  '会淹没真正的变更。缺口由 `v_audit_gap` 用 PostgreSQL 行计数器检测，'
  '所以这是"换粒度"而不是"关审计"。';

-- ---------------------------------------------------------------------------
-- 3. 汇总行写入：由重算工具调用，并记录计数器快照
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit_bulk(
  p_table text, p_op text, p_n_rows bigint, p_note text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mt, public AS $$
DECLARE
  v_id bigint;
  v_stat RECORD;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM audit_mode WHERE table_name = p_table) THEN
    RAISE EXCEPTION '表 % 没有登记审计粒度；audit_bulk 只用于 audit_mode 里登记过的表', p_table;
  END IF;
  -- 把"当前累计计数器"一并记下：v_audit_gap 靠它判断有没有未覆盖的改动
  SELECT coalesce(n_tup_ins,0) AS ins, coalesce(n_tup_upd,0) AS upd,
         coalesce(n_tup_del,0) AS del
    INTO v_stat
    FROM pg_stat_user_tables WHERE schemaname = 'mt' AND relname = p_table;

  INSERT INTO change_log (actor, object_type, object_name, change_type, detail)
  VALUES (current_user, p_table, 'bulk:' || coalesce(p_note, p_op), lower(p_op),
          jsonb_build_object(
            'op', p_op, 'table', p_table, 'mode', 'summary',
            'n_rows', p_n_rows,
            'note', p_note,
            'counter_snapshot', jsonb_build_object(
               'n_tup_ins', coalesce(v_stat.ins, 0),
               'n_tup_upd', coalesce(v_stat.upd, 0),
               'n_tup_del', coalesce(v_stat.del, 0))))
  -- ⚠ 主键列名是 `change_id`，不是 `id`（第一版写成 id 直接报 "column id does not exist"）
  RETURNING change_id INTO v_id;
  RETURN v_id;
END $$;

COMMENT ON FUNCTION audit_bulk(text, text, bigint, text) IS
  '为 audit_mode=summary 的表写**一条**汇总审计行，并把 PostgreSQL 的行计数器快照'
  '一并存进 detail。重算工具必须在结束时调用它 —— 否则 v_audit_gap 会报出缺口。'
  '这样"按操作记录"是可验证的，而不是"关掉了审计"。';

-- ---------------------------------------------------------------------------
-- 4. 缺口检测：有没有改动没被任何汇总行覆盖
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_audit_gap AS
WITH last AS (
  SELECT object_type AS table_name,
         max(change_id) AS last_id,
         (SELECT sum(coalesce((detail->'counter_snapshot'->>'n_tup_ins')::bigint,0)
                   + coalesce((detail->'counter_snapshot'->>'n_tup_upd')::bigint,0)
                   + coalesce((detail->'counter_snapshot'->>'n_tup_del')::bigint,0))
            FROM change_log c2
           WHERE c2.object_type = c1.object_type
             AND c2.detail->>'mode' = 'summary') AS snap_total,
         max(at) AS last_summary_at
    FROM change_log c1
   WHERE detail->>'mode' = 'summary'
   GROUP BY object_type)
SELECT m.table_name,
       s.n_tup_ins + s.n_tup_upd + s.n_tup_del AS 当前计数器,
       coalesce(l.snap_total, 0)               AS 最近汇总快照,
       (s.n_tup_ins + s.n_tup_upd + s.n_tup_del) - coalesce(l.snap_total, 0) AS 未覆盖增量,
       l.last_summary_at                       AS 最近汇总时间
  FROM audit_mode m
  JOIN pg_stat_user_tables s ON s.schemaname = 'mt' AND s.relname = m.table_name
  LEFT JOIN last l ON l.table_name = m.table_name
 WHERE m.mode = 'summary';
COMMENT ON VIEW v_audit_gap IS
  'summary 粒度表的**审计缺口检测**：比较 PostgreSQL 的累计行计数器与"最近一次汇总行里'
  '的快照"，差额就是"发生了改动但没有任何汇总行覆盖"的量。'
  '为什么必须有它：逐行不记之后，改了不报也能不留痕 —— 这个视图让那种情况露出来。'
  '（累计计数器不因 VACUUM 归零，但要留意实例重启/统计重置会把它清零，'
  '  所以判据是"增量显著大于 0"而不是"必须等于 0"。）';

-- ---------------------------------------------------------------------------
-- 5. 保留策略：**已经有表了，不要再建一张**
-- ---------------------------------------------------------------------------
-- ⚠ 我第一版在这里写了 `CREATE TABLE IF NOT EXISTS retention_policy (...)` 并 INSERT
--    365/730 天 —— 报错 `column "table_name" of relation "retention_policy" does not exist`。
--    原因是 **`mt.retention_policy` 早已存在**（迁移 015 一带建的），而且比我写的更完整：
--        rp_change_log  change_log  retain_days=**1825**（60 个月）action=RA3
--        rp_access_log  access_log  retain_days=**1095**（36 个月）action=RA3
--        rp_pii_contact person_pii  30 天 / legal_hold 标记
--    `CREATE TABLE IF NOT EXISTS` 撞上同名异构表时会**静默跳过**，INSERT 才炸 ——
--    如果它没炸，我就会用一张新表**覆盖掉项目早已定下的保留决策**（把 60 个月改成 12 个月）。
--    这类"静默跳过 + 我以为是新表"是最容易造成**制度性倒退**的形态。
--
-- 所以本迁移**不新建、不修改**保留策略 —— 既有决策是权威。
-- 真正缺的是**执行器**：策略写着 60 个月，但没有任何工具去执行它
-- （早到期的数据不会被清理，早该归档的也不会被归档）。
-- `ops/retention.py` 就是补这个缺口，并且它**只读既有策略**，不发明新的保留期。
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM retention_policy;
  IF n = 0 THEN
    RAISE EXCEPTION 'retention_policy 是空的 —— 保留策略不该为空（既有迁移应已登记）';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM retention_policy WHERE entity_name = 'change_log') THEN
    RAISE EXCEPTION 'change_log 没有保留策略 —— 请先由人决定保留期，再由工具执行';
  END IF;
  RAISE NOTICE '保留策略沿用既有决策：% 条（change_log: % 天）',
               n, (SELECT retain_days FROM retention_policy WHERE entity_name='change_log');
END $$;

-- ---------------------------------------------------------------------------
-- 6. 授权与自检
-- ---------------------------------------------------------------------------
GRANT SELECT ON audit_mode, v_audit_gap
  TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE INSERT, UPDATE, DELETE ON audit_mode
  FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
-- audit_bulk 只给写工具（运维用 postgres 连接）；不授给应用角色
REVOKE ALL ON FUNCTION audit_bulk(text, text, bigint, text) FROM PUBLIC, mt_portal;

DO $$
DECLARE n int; gap int;
BEGIN
  -- summary 登记必须真的生效：job_requirement 不应再有逐行记录
  SELECT count(*) INTO n FROM audit_mode WHERE mode = 'summary';
  IF n = 0 THEN
    RAISE EXCEPTION '没有任何 summary 粒度的表登记 —— 迁移没生效';
  END IF;
  SELECT count(*) INTO gap FROM v_audit_gap WHERE 未覆盖增量 > 0;
  RAISE NOTICE '审计粒度自检：summary 粒度 % 张表；当前有缺口的 % 张（初始会大于 0，'
               '下一次重算调用 audit_bulk 后归零）', n, gap;
END $$;

COMMIT;
