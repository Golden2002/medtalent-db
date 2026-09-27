-- ============================================================================
-- 医学生人才信息库 · 备份与恢复元数据 v0.8.0
--
-- 设计要点：
--   · **保留策略是数据，不是脚本里的常量**。改策略 = 改一行配置，不用改代码。
--   · **每次备份留痕**：路径、大小、sha256、保留到期日都进库，
--     这样"到底有没有备份、备份还能不能用"是可查询的，而不是靠翻文件夹。
--   · **不物理删除备份记录**：超期只把文件删掉、状态置 pruned，审计仍在。
--   · **含 PII 的导出必须显式声明**：XLS 默认剔除 PII 列；
--     若要导出完整副本，必须在 policy 里把 include_pii 打开，
--     并在 manifest 里标记访问层级 T3（对应 docs/06 的敏感个人信息要求）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 备份策略（保留期即数据）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_policy (
  policy_id        TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  keep_daily       INTEGER NOT NULL DEFAULT 7,    -- 最近 N 天，每天保留一份
  keep_weekly      INTEGER NOT NULL DEFAULT 4,    -- 最近 N 周，每周保留一份
  keep_monthly     INTEGER NOT NULL DEFAULT 6,    -- 最近 N 月，每月保留一份
  min_copies       INTEGER NOT NULL DEFAULT 3,    -- 无论如何至少保留这么多份
  include_pii      BOOLEAN NOT NULL DEFAULT false,-- 是否把 PII 一起导出（默认否）
  export_xlsx      BOOLEAN NOT NULL DEFAULT true,
  enabled          BOOLEAN NOT NULL DEFAULT true,
  note             TEXT,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO backup_policy (policy_id, name, keep_daily, keep_weekly, keep_monthly,
                           min_copies, include_pii, export_xlsx, note)
VALUES ('bp_default', '默认策略：日 7 / 周 4 / 月 6，导出 XLS 但剔除 PII',
        7, 4, 6, 3, false, true,
        'PII 单独通过 --include-pii 显式导出；含 PII 的备份按 T3 管理')
ON CONFLICT (policy_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. 备份运行记录
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_run (
  backup_id        TEXT PRIMARY KEY,
  policy_id        TEXT REFERENCES backup_policy(policy_id),
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ,
  status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','pruned')),
  backup_type      TEXT NOT NULL DEFAULT 'full'
                     CHECK (backup_type IN ('full','data_only')),
  dump_path        TEXT,             -- pg_dump -Fc 产物
  xlsx_path        TEXT,             -- 用户数据副本（XLSX）
  manifest_path    TEXT,
  size_bytes       BIGINT,
  sha256           TEXT,             -- 备份包内容清单的哈希
  table_counts     JSONB,            -- 备份时各表行数（恢复后用于校验）
  contains_pii     BOOLEAN NOT NULL DEFAULT false,
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T2',
  expires_at       DATE,             -- 保留到期日（prune 依据）
  pruned_at        TIMESTAMPTZ,
  note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_run_created ON backup_run(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_run_status ON backup_run(status, expires_at);

-- ---------------------------------------------------------------------------
-- 3. 恢复记录（恢复必须留痕：从哪份、恢复到哪、结果如何）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS restore_run (
  restore_id       TEXT PRIMARY KEY,
  backup_id        TEXT NOT NULL REFERENCES backup_run(backup_id),
  target_db        TEXT NOT NULL,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at      TIMESTAMPTZ,
  status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','verified')),
  rows_restored    JSONB,
  mismatch         JSONB,            -- 与备份清单对不上的表
  operator         TEXT,
  note             TEXT
);

-- ---------------------------------------------------------------------------
-- 4. 查询视图：当前备份健康状况
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_backup_health AS
SELECT
  (SELECT count(*) FROM backup_run WHERE status='ok')                        AS ok_backups,
  (SELECT max(finished_at) FROM backup_run WHERE status='ok')                AS last_ok_at,
  (SELECT count(*) FROM backup_run WHERE status='failed')                    AS failed_backups,
  (SELECT coalesce(sum(size_bytes),0) FROM backup_run WHERE status='ok')     AS total_bytes,
  (SELECT min(expires_at) FROM backup_run WHERE status='ok')                 AS earliest_expiry,
  (SELECT count(*) FROM backup_run WHERE status='ok' AND contains_pii)       AS pii_backups,
  (SELECT count(*) FROM restore_run WHERE status IN ('ok','verified'))       AS verified_restores;

COMMIT;
