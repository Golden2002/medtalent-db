-- 聚合闸门 public_counts() 的真实开销：它是否对**每一张表**都做了 count(*)？
-- 如果是，那么匿名打开首页 / quality 页就会全表扫描 change_log（100MB+）。
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT * FROM mt.public_counts() WHERE table_name = 'person';
