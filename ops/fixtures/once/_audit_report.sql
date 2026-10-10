-- 访问日志的真实状态：应用层审计 + 数据库层连接日志
SELECT 'access_log 总行数' AS k, count(*)::text AS v FROM mt.access_log
UNION ALL SELECT '最早一条', min(at)::text FROM mt.access_log
UNION ALL SELECT '最近一条', max(at)::text FROM mt.access_log
UNION ALL SELECT '不同访问者数', count(DISTINCT actor)::text FROM mt.access_log
UNION ALL SELECT '不同来源角色数', count(DISTINCT actor_role)::text FROM mt.access_log;

-- 最近 12 条：谁 · 以什么角色 · 对什么 · 什么动作 · 读到多少行 · 什么时候
SELECT access_id, actor, actor_role, action, target, access_tier, row_count,
       to_char(at, 'MM-DD HH24:MI:SS') AS 时间
  FROM mt.access_log ORDER BY access_id DESC LIMIT 12;

-- 按动作与访问者汇总
SELECT actor, actor_role, action, count(*) AS 次数, max(at) AS 最近
  FROM mt.access_log GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 12;

-- 被拒绝的访问（权限不足）也记下来了 —— 这是"有人试图越权"的证据
SELECT actor, actor_role, target, detail->>'need' AS 需要等级,
       detail->>'db_error' AS 数据库原话, to_char(at,'MM-DD HH24:MI:SS') AS 时间
  FROM mt.access_log WHERE action='denied' ORDER BY access_id DESC LIMIT 6;

-- 数据库层：PostgreSQL 自己的连接日志开关
SELECT name, setting, short_desc FROM pg_settings
 WHERE name IN ('log_connections','log_disconnections','log_statement',
                'log_line_prefix','log_destination','logging_collector','log_directory')
 ORDER BY name;

-- 当前连着库的会话（实时）
SELECT usename, application_name, client_addr, state,
       to_char(backend_start,'MM-DD HH24:MI:SS') AS 连接于
  FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
 ORDER BY backend_start;
