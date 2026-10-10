-- ============================================================================
-- 第 4 件事：分析分类变量对「求职结果」的影响
-- 口径：把 field_value 里三个分类变量按主体取出来，做成一张宽表，再交叉分析。
--
-- 为什么用一个 CTE 先把"长表转宽表"：field_value 是 EAV（一行一个字段值），
-- 而分析需要"一行一个人、多列属性"。这一步是所有 EAV 分析的地基，
-- 写成视图或 CTE 都行，但**口径只能有一处**（否则各页面数字会对不上）。
-- ============================================================================
CREATE OR REPLACE VIEW v_person_analysis_vars AS
SELECT p.person_id,
       max(fv.value_code) FILTER (WHERE fv.field_id = 'F_PSN_CRED_AGE_BAND')   AS age_band,
       max(fv.value_code) FILTER (WHERE fv.field_id = 'F_PSN_RES_OVERSEAS')    AS overseas,
       max(fv.value_code) FILTER (WHERE fv.field_id = 'F_PSN_RES_JOB_OUTCOME') AS job_outcome
  FROM mt.person p
  JOIN mt.field_value fv
    ON fv.subject_type = 'person' AND fv.subject_id = p.person_id
   AND fv.field_id IN ('F_PSN_CRED_AGE_BAND', 'F_PSN_RES_OVERSEAS',
                       'F_PSN_RES_JOB_OUTCOME')
 GROUP BY p.person_id;
COMMENT ON VIEW v_person_analysis_vars IS
  '把 EAV 的 field_value 转成"一行一个人"的宽表，供分类变量影响分析使用。'
  '三个变量：年龄段 / 海外经历 / 求职结果。**缺失就是 NULL**（没有这一行的值），'
  '所以任何统计都要显式说明"分母是填了该变量的人"，而不是全体。';

-- ① 海外经历 × 求职结果：行数 + 行百分比
SELECT coalesce(overseas, '（未填）') AS 海外经历,
       count(*) AS 人数,
       count(*) FILTER (WHERE job_outcome = 'JO1') AS 已就业,
       count(*) FILTER (WHERE job_outcome = 'JO2') AS 待业中,
       count(*) FILTER (WHERE job_outcome = 'JO3') AS 升学深造,
       count(*) FILTER (WHERE job_outcome = 'JO4') AS 出国出境,
       round(100.0 * count(*) FILTER (WHERE job_outcome = 'JO1')
             / nullif(count(*) FILTER (WHERE job_outcome IS NOT NULL), 0), 1) AS 已就业率
  FROM mt.v_person_analysis_vars
 GROUP BY 1 ORDER BY 1;

-- ② 年龄段 × 海外经历：已就业率（**分层看**，避免把年龄和海外经历混在一起）
SELECT coalesce(age_band, '（未填）') AS 年龄段,
       count(*) FILTER (WHERE overseas = 'Y') AS "有海外_人数",
       round(100.0 * count(*) FILTER (WHERE overseas = 'Y' AND job_outcome = 'JO1')
             / nullif(count(*) FILTER (WHERE overseas = 'Y' AND job_outcome IS NOT NULL), 0), 1)
         AS "有海外_已就业率",
       count(*) FILTER (WHERE overseas = 'N') AS "无海外_人数",
       round(100.0 * count(*) FILTER (WHERE overseas = 'N' AND job_outcome = 'JO1')
             / nullif(count(*) FILTER (WHERE overseas = 'N' AND job_outcome IS NOT NULL), 0), 1)
         AS "无海外_已就业率"
  FROM mt.v_person_analysis_vars
 GROUP BY 1 ORDER BY 1;

-- ③ 关联强度：Cramér's V（0=无关，1=完全相关）—— 比"看百分比差多少"严谨
WITH t AS (
  SELECT overseas, job_outcome, count(*) AS n
    FROM mt.v_person_analysis_vars
   WHERE overseas IS NOT NULL AND job_outcome IS NOT NULL
   GROUP BY 1, 2),
tot AS (SELECT sum(n) AS n FROM t),
rt AS (SELECT overseas, sum(n) AS n FROM t GROUP BY 1),
ct AS (SELECT job_outcome, sum(n) AS n FROM t GROUP BY 1),
exp AS (
  SELECT t.n, rt.n * ct.n * 1.0 / (SELECT n FROM tot) AS e
    FROM t JOIN rt USING (overseas) JOIN ct USING (job_outcome)),
chi AS (SELECT sum(power(n - e, 2) / e) AS x2 FROM exp)
SELECT round(x2, 3) AS "卡方",
       (SELECT count(*) FROM rt) AS "海外经历类别数",
       (SELECT count(*) FROM ct) AS "求职结果类别数",
       (SELECT n FROM tot) AS "样本量",
       round(sqrt(x2 / ((SELECT n FROM tot)
                        * least((SELECT count(*) FROM rt) - 1,
                                (SELECT count(*) FROM ct) - 1))), 3) AS "Cramér_V"
  FROM chi;

-- ④ **缺失统计**：这是"字段缺失"在分析阶段的样子 —— 缺失不进分母，但必须数得出来
SELECT count(*) AS 总人数,
       count(age_band) AS 有年龄段,
       count(overseas) AS 有海外经历,
       count(job_outcome) AS 有求职结果,
       count(*) FILTER (WHERE age_band IS NOT NULL AND overseas IS NOT NULL
                          AND job_outcome IS NOT NULL) AS 三个变量齐全,
       count(*) FILTER (WHERE overseas IS NULL) AS 缺海外经历,
       count(*) FILTER (WHERE job_outcome IS NULL) AS 缺求职结果
  FROM mt.v_person_analysis_vars;
