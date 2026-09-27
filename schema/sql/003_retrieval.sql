-- ============================================================================
-- 医学生人才信息库 · 检索层：全文检索 v0.3.0
-- 目的：支撑 docs/05-匹配与分析引擎方案.md 的"BM25 召回"。
-- 依赖：PostgreSQL 内置全文检索（无需任何扩展）。
-- 向量部分见 004_vector.sql（需 pgvector，可选）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;

-- 中文分词说明（重要）：
-- PostgreSQL 自带分词器不支持中文，'simple' 配置只会按空格/标点切分。
-- 本项目采取"入库前先分词"的务实方案：由 Python 侧用 jieba 分词，
-- 结果以空格连接写入 body_tokens，再用 to_tsvector('simple', body_tokens) 建索引。
-- （若部署环境可安装 pg_jieba / zhparser，可改为直接对 body 建索引，本表结构不变。）

CREATE TABLE search_document (
  doc_id           TEXT PRIMARY KEY,        -- doc_<ULID>
  subject_type     TEXT NOT NULL,           -- person | job_posting | employer | concept | occupation
  subject_id       TEXT NOT NULL,
  lang             TEXT NOT NULL DEFAULT 'zh',
  title            TEXT,
  body             TEXT NOT NULL,           -- 用于展示与向量化的原文
  body_tokens      TEXT,                    -- jieba 分词后、空格连接；用于 to_tsvector
  tsv              tsvector GENERATED ALWAYS AS (
                     to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(body_tokens, body))
                   ) STORED,
  source_id        TEXT REFERENCES source_registry(source_id),
  access_tier      mt.access_tier_t NOT NULL DEFAULT 'T1',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subject_type, subject_id)
);
CREATE INDEX idx_search_doc_tsv ON search_document USING GIN (tsv);
CREATE INDEX idx_search_doc_subject ON search_document(subject_type, subject_id);

COMMIT;

-- ============================================================================
-- 登记要求（执行后必须完成，否则违反"字段必须登记"纪律）
-- ============================================================================
-- entity_catalog 登记 search_document 实体；
-- field_catalog 登记上表业务字段（tsv 的 collection_method=derived）。
