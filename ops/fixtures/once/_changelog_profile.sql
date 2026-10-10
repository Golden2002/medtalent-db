-- change_log 的构成与体量：它到底是"真实审计"还是"测试噪声"？
SELECT count(*) AS 总行数,
       pg_size_pretty(pg_total_relation_size('mt.change_log')) AS 总大小,
       min(at)::date AS 最早,
       max(at)::date AS 最晚
  FROM mt.change_log;

-- 按天分布（最近 12 天）
SELECT at::date AS 日期, count(*) AS 行数
  FROM mt.change_log
 WHERE at > now() - interval '12 days'
 GROUP BY 1 ORDER BY 1 DESC LIMIT 12;

-- 按 actor 分布：测试进程留下的痕迹 vs 真实用户
SELECT coalesce(actor, '(空)') AS actor, count(*) AS 行数
  FROM mt.change_log
 GROUP BY 1 ORDER BY 2 DESC LIMIT 12;

-- 按对象类型分布
SELECT object_type, count(*) AS 行数
  FROM mt.change_log
 GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
