# -*- coding: utf-8 -*-
"""
ops/fixtures/reset_fixture.py —— 重置 fixture 语料（**仅用于 mock 数据**）

为什么需要它：mock 语料会随生成参数变化（--per 30 与 --per 8 产生的随机内容不同），
重新生成会产生一批新哈希的 L0 文件，旧的 job_posting 就成了孤儿，统计会重复计数。

真实来源**绝不能**用这个脚本：L0 只增不改是硬纪律（见 docs/03 §2.2）。
它只清理由 src_fixture_* 开头的测试来源。

用法：python ops/fixtures/reset_fixture.py
"""
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")


def main():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT source_id FROM source_registry WHERE source_id LIKE 'src_fixture%'")
        srcs = [r[0] for r in cur.fetchall()]
        if not srcs:
            print("[=] 没有 fixture 来源")
            return 0
        cur.execute("""DELETE FROM job_requirement WHERE job_id IN
                       (SELECT job_id FROM job_posting WHERE source_id = ANY(%s))""", (srcs,))
        n_req = cur.rowcount
        cur.execute("DELETE FROM job_task WHERE job_id IN "
                    "(SELECT job_id FROM job_posting WHERE source_id = ANY(%s))", (srcs,))
        n_task = cur.rowcount
        cur.execute("DELETE FROM job_posting WHERE source_id = ANY(%s)", (srcs,))
        n_job = cur.rowcount
        cur.execute("DELETE FROM provenance WHERE source_id = ANY(%s)", (srcs,))
        cur.execute("DELETE FROM ingest_run WHERE source_id = ANY(%s)", (srcs,))
        cur.execute("DELETE FROM job_competency_weight")
        c.commit()

    n_files = 0
    for s in srcs:
        d = os.path.join(BASE, "data", "raw", s)
        if os.path.isdir(d):
            n_files += sum(len(f) for _, _, f in os.walk(d))
            shutil.rmtree(d)
    d = os.path.join(BASE, "ops", "fixtures", "jd", "fixture_careers")
    if os.path.isdir(d):
        shutil.rmtree(d)

    print("[✓] fixture 已重置：job_posting %d、job_task %d、job_requirement %d、L0 文件 %d"
          % (n_job, n_task, n_req, n_files))
    print("    现在可重新运行：python ops\\fixtures\\gen_jd_corpus.py --per 30")
    return 0


if __name__ == "__main__":
    sys.exit(main())
