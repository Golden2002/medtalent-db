-- ============================================================================
-- 医学生人才信息库 · 检索层：向量检索 v0.3.0（可选模块）
-- 依赖：pgvector 扩展。若本机不可用，本文件不执行，其余功能不受影响
--       （匹配引擎退化为纯 BM25 + 结构化打分，见 docs/05 §8 分期）。
--
-- 为什么不合并进 003_retrieval.sql：pgvector 需要单独安装的扩展，
-- 把它隔离开可以让"没有向量"的环境依然拥有一套完整可用的数据库。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;

CREATE EXTENSION IF NOT EXISTS vector;

-- 模型名与版本必须记录，否则无法解释"为什么这条被召回了"
CREATE TABLE IF NOT EXISTS embedding (
  embedding_id     TEXT PRIMARY KEY,        -- emb_<ULID>
  subject_type     TEXT NOT NULL,
  subject_id       TEXT NOT NULL,
  model_name       TEXT NOT NULL,
  model_version    TEXT NOT NULL,
  dim              SMALLINT NOT NULL,
  vec              vector(768) NOT NULL,
  content_hash     TEXT NOT NULL,           -- 被向量化文本的 sha256，用于判断是否需要重算
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subject_type, subject_id, model_name, model_version)
);

CREATE INDEX IF NOT EXISTS idx_embedding_hnsw
  ON embedding USING hnsw (vec vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_embedding_subject
  ON embedding(subject_type, subject_id);

COMMIT;
