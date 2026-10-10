-- 修好之后：日志里到底记了什么「用户信息」
SELECT access_id, actor, actor_role, action, target, access_tier, row_count,
       to_char(at, 'HH24:MI:SS') AS 时间
  FROM mt.access_log ORDER BY access_id DESC LIMIT 14;

-- 关键一问：登录成功后，后续请求记的是谁？
SELECT actor, actor_role, action, count(*) AS 次数, max(at) AS 最近
  FROM mt.access_log
 WHERE at > now() - interval '10 minutes'
 GROUP BY 1,2,3 ORDER BY 4 DESC;
