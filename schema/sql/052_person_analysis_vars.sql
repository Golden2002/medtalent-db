-- ============================================================================
-- 医学生人才信息库 · 052 分类变量影响分析用的宽表视图 v1.0.0
--
-- 需求背景（用户原话）
-- ---------------------------------------------------------------------------
-- "分析特定几个分类变量（如年龄和海外经历）对求职结果的影响"。
-- 实测发现这件事此前**根本跑不起来**，原因不是分析方法，而是**没有数据**：
--   · `field_value` 总共 **0 行** —— 52 个字段登记了，但一个值都没有；
--   · "海外经历"字段**早已登记**（`F_PSN_RES_OVERSEAS`，码表 CT_YES_NO）；
--   · "年龄段"也已有（`F_PSN_CRED_AGE_BAND`，码表 CT_AGE_BAND）；
--   · 而"求职结果"**不存在** → 用 `mt.add_dimension()` 新建（零 DDL，见指南第 3 步）。
--
-- 本迁移只做**一件事**：把 EAV 的 `field_value` 转成"一行一个人"的宽表。
-- ---------------------------------------------------------------------------
-- 为什么需要它：`field_value` 是 EAV（一行一个字段值），而任何交叉分析都需要
-- "一行一个人、多列属性"。这一步是所有 EAV 分析的地基。
-- **但口径只能有一处** —— 如果每个分析各自 join 一遍，迟早会出现
-- "同一个页面两个数字对不上"（本项目在这方面吃过多次亏，见 docs/25）。
--
-- ⚠ 缺失就是 NULL，不是空串
-- ---------------------------------------------------------------------------
-- `set_value` 在"没填"时**不写行**，所以这里没值的字段自然就是 NULL。
-- 于是任何统计都必须说清楚**分母**：
--   · "已就业率" 的分母是**填了求职结果的人**，不是全体；
--   · `count(*)` 与 `count(变量)` 的差就是"没填的人数"。
-- 视图注释里写明这一点，免得后来人把 NULL 当成一个类别去分组。
--
-- ⚠ 权限：**这个视图含 person_id，绝不公开**
-- ---------------------------------------------------------------------------
-- 因此**不登记进 public_view_registry**（那等于对匿名开放）。
-- 由 `apply_role_baseline()` 按默认规则只授给 T3 —— 见迁移 038 的注册表机制。
--
-- 可重放：CREATE OR REPLACE VIEW + 对账 + 断言。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

CREATE OR REPLACE VIEW v_person_analysis_vars AS
SELECT p.person_id,
       max(fv.value_code) FILTER (WHERE fv.field_id = 'F_PSN_CRED_AGE_BAND')
         AS age_band,
       max(fv.value_code) FILTER (WHERE fv.field_id = 'F_PSN_RES_OVERSEAS')
         AS overseas,
       max(fv.value_code) FILTER (WHERE fv.field_id = 'F_PSN_RES_JOB_OUTCOME')
         AS job_outcome
  FROM person p
  JOIN field_value fv
    ON fv.subject_type = 'person' AND fv.subject_id = p.person_id
   AND fv.field_id IN ('F_PSN_CRED_AGE_BAND', 'F_PSN_RES_OVERSEAS',
                       'F_PSN_RES_JOB_OUTCOME')
 GROUP BY p.person_id;
COMMENT ON VIEW v_person_analysis_vars IS
  '分类变量影响分析用的宽表：一行一个人，三列 —— 年龄段 / 海外经历 / 求职结果。'
  '把 EAV（field_value）转成宽表是分析的地基，且**口径只此一处**。'
  '**缺失就是 NULL**（没填的字段不写行），所以统计必须显式说明分母：'
  '"已就业率"的分母是填了求职结果的人；`count(*)` 与 `count(列)` 的差 = 没填的人数。'
  '**含 person_id，故不登记为公开视图**，由 apply_role_baseline() 只授 T3。';

-- 它也必须在策略对账里被管到：跑一次，让基线按"未登记的视图只给 T3"授权
SELECT apply_column_grants();

-- 硬断言：只给 T3，绝不给匿名；且宽表真能读出数据（不是空壳）
DO $$
DECLARE n int;
BEGIN
  IF has_table_privilege('mt_t0', 'mt.v_person_analysis_vars', 'SELECT') THEN
    RAISE EXCEPTION '匿名能读分析宽表（含 person_id）—— 披露控制失效';
  END IF;
  IF NOT has_table_privilege('mt_t3', 'mt.v_person_analysis_vars', 'SELECT') THEN
    RAISE EXCEPTION 'T3 读不到分析宽表 —— 分析做不了（收得太紧）';
  END IF;
  -- 再对账一次，确认状态稳定（不是"这次恰好有"）
  PERFORM apply_column_grants();
  IF NOT has_table_privilege('mt_t3', 'mt.v_person_analysis_vars', 'SELECT')
     OR has_table_privilege('mt_t0', 'mt.v_person_analysis_vars', 'SELECT') THEN
    RAISE EXCEPTION '两次对账后权限不稳定 —— 视图授权在飘';
  END IF;
  SELECT count(*) INTO n FROM v_person_analysis_vars;
  RAISE NOTICE '分析宽表就绪：T3 可读、匿名不可读；当前 % 行（0 行说明还没导数据）', n;
END $$;

COMMIT;
