-- ============================================================================
-- 医学生人才信息库 · 065 域划分进数据库（让"建表"与"归类"回到一处）v1.0.0
--
-- 【为什么】"表属于哪个域"原来在 Python（portal.DOMAIN_TABLES），而表在 SQL 迁移里建 ——
-- **两处清单必然漏**。实测漏了四次：
--   053 分析口径三张表 · 062 刻意锁登记表 · 063 算法注册表 · 064 展示策略表，
-- 每次都是"门户套件 + 门禁同时失败"，且症状是"有张表没人认领"，排查成本不低。
--
-- 【修法】把域划分搬进 `mt.domain_registry`，并让**建表的迁移顺带声明域**：
-- 同一个迁移文件里既建表又登记域，于是"建表"与"归类"重新回到一处。
-- 本迁移把现状搬过去（由 portal.DOMAIN_TABLES 生成），之后由迁移维护。
--
-- 【判据】门禁 I1.2 与门户都读这张表；Python 里的清单退化为**兜底**
-- （DB 读不到时用它，避免门户整体起不来）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS domain_registry (
  table_name  TEXT PRIMARY KEY,
  domain      TEXT NOT NULL,
  note        TEXT,
  declared_by TEXT NOT NULL DEFAULT 'migration',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE domain_registry IS
  '表 → 业务域 的**权威登记**。建表的迁移应当顺带 INSERT 一行 —— '
  '这样"建表"与"归类"在同一处，不会像以前那样两处清单各漏一次。'
  '门禁 I1.2 会拦"既不在本表、也不在 portal.DOMAIN_TABLES 兜底清单"里的表。';

INSERT INTO domain_registry (table_name, domain) VALUES
  ('assessment', '人才档案'),
  ('award_honor', '人才档案'),
  ('clinical_exposure', '人才档案'),
  ('credential', '人才档案'),
  ('education_record', '人才档案'),
  ('employment_record', '人才档案'),
  ('evidence', '人才档案'),
  ('gap_analysis', '人才档案'),
  ('match_result', '人才档案'),
  ('match_run', '人才档案'),
  ('narrative', '人才档案'),
  ('person', '人才档案'),
  ('person_constraint', '人才档案'),
  ('person_demographics', '人才档案'),
  ('person_pii', '人才档案'),
  ('preference', '人才档案'),
  ('project_record', '人才档案'),
  ('research_output', '人才档案'),
  ('salary_benchmark', '人才档案'),
  ('skill_assertion', '人才档案'),
  ('training_record', '人才档案'),
  ('trajectory_event', '人才档案'),
  ('transition_case', '人才档案'),
  ('algorithm_registry', '分析口径体系'),
  ('dimension_registry', '分析口径体系'),
  ('domain_registry', '分析口径体系'),
  ('funnel_registry', '分析口径体系'),
  ('locked_object_registry', '分析口径体系'),
  ('match_display_policy', '分析口径体系'),
  ('metric_registry', '分析口径体系'),
  ('backup_policy', '备份与发布'),
  ('backup_run', '备份与发布'),
  ('dataset_release', '备份与发布'),
  ('restore_run', '备份与发布'),
  ('attribute_definition', '字典与语义'),
  ('category_node', '字典与语义'),
  ('code_table', '字典与语义'),
  ('code_value', '字典与语义'),
  ('concept', '字典与语义'),
  ('concept_ancestor', '字典与语义'),
  ('concept_mapping', '字典与语义'),
  ('concept_relation', '字典与语义'),
  ('dimension', '字典与语义'),
  ('entity_catalog', '字典与语义'),
  ('field_catalog', '字典与语义'),
  ('metric_definition', '字典与语义'),
  ('search_document', '字典与语义'),
  ('answer', '小程序接入'),
  ('crosswalk', '小程序接入'),
  ('experience_episode', '小程序接入'),
  ('experience_task', '小程序接入'),
  ('external_identity', '小程序接入'),
  ('response_session', '小程序接入'),
  ('sync_event', '小程序接入'),
  ('employer', '岗位与市场'),
  ('ingest_run', '岗位与市场'),
  ('job_competency_weight', '岗位与市场'),
  ('job_posting', '岗位与市场'),
  ('job_requirement', '岗位与市场'),
  ('job_task', '岗位与市场'),
  ('occupation', '岗位与市场'),
  ('provenance', '岗位与市场'),
  ('source_registry', '岗位与市场'),
  ('assertion', '扩展值与观测'),
  ('derived_feature', '扩展值与观测'),
  ('field_value', '扩展值与观测'),
  ('first_occurrence', '扩展值与观测'),
  ('observation_window', '扩展值与观测'),
  ('access_log', '治理与合规'),
  ('access_policy', '治理与合规'),
  ('access_tier', '治理与合规'),
  ('age_band_asis', '治理与合规'),
  ('app_user', '治理与合规'),
  ('audit_mode', '治理与合规'),
  ('change_log', '治理与合规'),
  ('column_policy', '治理与合规'),
  ('column_profile', '治理与合规'),
  ('consent_record', '治理与合规'),
  ('consent_withdrawal_action', '治理与合规'),
  ('data_lifecycle_run', '治理与合规'),
  ('export_denied', '治理与合规'),
  ('function_grant', '治理与合规'),
  ('incident_report', '治理与合规'),
  ('login_attempt', '治理与合规'),
  ('login_policy', '治理与合规'),
  ('public_count_excluded', '治理与合规'),
  ('public_view_registry', '治理与合规'),
  ('retention_policy', '治理与合规'),
  ('schema_migration', '治理与合规'),
  ('subject_request', '治理与合规'),
  ('talent_deletion_request', '治理与合规'),
  ('tombstone', '治理与合规'),
  ('web_session', '治理与合规'),
  ('competency_drift', '演化层'),
  ('competency_run', '演化层'),
  ('concept_candidate', '演化层'),
  ('evolution_policy', '演化层'),
  ('occupation_candidate', '演化层'),
  ('occupation_change', '演化层'),
  ('occupation_migration', '演化层')
ON CONFLICT (table_name) DO UPDATE
  SET domain = EXCLUDED.domain, updated_at = now();

-- 授权：定义层，T0 起可读（它说明"库里有什么、怎么分类"，不含个人数据）
GRANT SELECT ON domain_registry TO mt_t0, mt_t1, mt_t2, mt_t3;

DO $$
DECLARE n int; n_undec int;
BEGIN
  SELECT count(*) INTO n FROM domain_registry;
  IF n < 90 THEN RAISE EXCEPTION '域登记只有 % 行 —— 种子不完整', n; END IF;
  -- 迁移 064 那张表必须被显式登记（它正是第四次漏掉的那张）
  IF NOT EXISTS (SELECT 1 FROM domain_registry WHERE table_name='match_display_policy') THEN
    RAISE EXCEPTION 'match_display_policy 没登记 —— 本次要修的就是它';
  END IF;
  -- 所有 mt 基础表都应在登记里或属于"刻意锁"
  SELECT count(*) INTO n_undec FROM pg_class c
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname='mt' AND c.relkind='r'
     AND c.relname NOT IN (SELECT table_name FROM domain_registry)
     AND c.relname NOT IN (SELECT object_name FROM locked_object_registry);
  RAISE NOTICE '域登记 % 行；仍未登记的基础表 % 张', n, n_undec;
END $$;

COMMIT;
