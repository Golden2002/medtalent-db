-- 一次性结清历史缺口：job_task 的改动发生在 050 之前（当时没有 summary 机制）
SELECT mt.audit_bulk('job_task', 'rebuild',
                     (SELECT count(*) FROM mt.job_task),
                     '历史遗留：050 之前的 JD 重算（当时尚无 summary 粒度），'
                     '此处一次性确认，此后由 jd_ingest 每次写汇总行') AS 确认行id;

SELECT table_name, 未覆盖增量 FROM mt.v_audit_gap ORDER BY table_name;
