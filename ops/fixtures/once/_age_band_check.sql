-- 年龄段分布：公开视图 vs 精确出生年（两者的分级差异）
SELECT band, band_label, n_persons FROM mt.v_age_band_public;

SELECT 'age_band 覆盖率' AS k,
       count(age_band)::text || ' / ' || count(*)::text AS v,
       '' AS note
  FROM mt.person_demographics
UNION ALL
SELECT '基准年', base_year::text, note FROM mt.age_band_asis
UNION ALL
SELECT 'birth_year 有值数', count(birth_year)::text, 'T2 起可读' FROM mt.person_demographics;

-- 权限层面实测：匿名能读什么、读不到什么
SELECT 'age_band 匿名可读' AS 检查,
       has_column_privilege('mt_t0','mt.person_demographics','age_band','SELECT')::text AS 结果
UNION ALL
SELECT 'birth_year 匿名可读（应为 false）',
       has_column_privilege('mt_t0','mt.person_demographics','birth_year','SELECT')::text
UNION ALL
SELECT 'birth_year T2 可读（应为 true）',
       has_column_privilege('mt_t2','mt.person_demographics','birth_year','SELECT')::text
UNION ALL
SELECT '年龄分布视图 匿名可读（应为 true）',
       has_table_privilege('mt_t0','mt.v_age_band_public','SELECT')::text;
