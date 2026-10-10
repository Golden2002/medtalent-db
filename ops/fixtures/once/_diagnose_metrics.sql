-- 两个口径打架：授权覆盖率说 113 人，供给漏斗第 5 阶段只有 28 人 —— 查为什么
SELECT '有有效授权的人' AS 口径, count(DISTINCT person_id)::text AS 人数
  FROM mt.consent_record WHERE revoked_at IS NULL
UNION ALL SELECT '有技能断言的人', count(DISTINCT person_id)::text FROM mt.skill_assertion
UNION ALL SELECT '有基础档案的人', count(DISTINCT person_id)::text FROM mt.person_demographics
UNION ALL SELECT '授权 ∩ 技能', count(*)::text FROM (
  SELECT DISTINCT c.person_id FROM mt.consent_record c
   WHERE c.revoked_at IS NULL
     AND EXISTS (SELECT 1 FROM mt.skill_assertion s WHERE s.person_id=c.person_id)) t
UNION ALL SELECT '授权 ∩ 技能 ∩ 档案', count(*)::text FROM (
  SELECT DISTINCT c.person_id FROM mt.consent_record c
   WHERE c.revoked_at IS NULL
     AND EXISTS (SELECT 1 FROM mt.skill_assertion s WHERE s.person_id=c.person_id)
     AND EXISTS (SELECT 1 FROM mt.person_demographics d WHERE d.person_id=c.person_id)) t;

\echo '=== 授权的人是谁（来源/前缀分布）==='
SELECT left(person_id, 9) AS 前缀, count(DISTINCT person_id) AS 人数
  FROM mt.consent_record WHERE revoked_at IS NULL GROUP BY 1 ORDER BY 2 DESC;

\echo '=== 有技能断言的人是谁 ==='
SELECT left(person_id, 9) AS 前缀, count(DISTINCT person_id) AS 人数
  FROM mt.skill_assertion GROUP BY 1 ORDER BY 2 DESC;

\echo '=== match_result.score_total 的真实量纲 ==='
SELECT min(score_total) AS 最小, max(score_total) AS 最大, round(avg(score_total),3) AS 均值,
       count(*) AS 条数 FROM mt.match_result;
