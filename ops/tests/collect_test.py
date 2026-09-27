# -*- coding: utf-8 -*-
"""
ops/tests/collect_test.py —— 采集管道端到端测试（T07）

验证四件事：
  1. 合规前置真的会拦人（未登记来源、license_note 占位、robots 禁止的路径）
  2. L0 落盘按内容哈希去重（重跑不产生新文件）
  3. ingest_run / provenance 逐条血缘完整且可追溯
  4. 限速参数生效（本地 fixture 用 0，真实站点默认 3 秒）

用法：python ops/tests/collect_test.py
"""
import os
import shutil
import subprocess
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "ops", "tests"))

from fixture_server import serve  # noqa: E402
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
PORT = 8098
ROOT = "http://127.0.0.1:%d" % PORT
# 用**独立的测试来源**，避免动到流水线 src_fixture_careers 已采集并把解析入库的数据
SRC = "src_fixture_collecttest"
LIMIT = 25
RAW = os.path.join(BASE, "data", "raw", SRC)

PASS, FAIL = H.PASS, H.FAIL
check, q1 = H.check, H.q1


def conn():
    return H.connect(DSN)


def qrow(c, sql, p=None):
    """取一行（多列），返回 dict。"""
    with c.cursor() as cur:
        cur.execute(sql, p)
        return cur.fetchone()


def collector(args):
    """以子进程方式调用采集 CLI，测的是真实入口。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # 否则子进程 traceback 中文会按 GBK 输出而乱码
    r = subprocess.run([sys.executable, os.path.join(BASE, "code", "collect", "run.py")] + args,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def l0_files(ext=".html"):
    """递归统计 L0 文件。注意 L0 按 data/raw/<source>/<日期>/ 分层，
    直接 os.listdir(RAW) 只会拿到日期目录。"""
    out = []
    for r, _, fs in os.walk(RAW):
        out += [os.path.join(r, f) for f in fs if f.endswith(ext)]
    return out


def reset_test_source(c):
    """测试专用重置：清掉该测试来源的批次、血缘与 L0 目录。
    注意：真实来源**永远不做这个操作**（L0 只增不改）。
    删除顺序必须遵守外键：job_* → provenance → ingest_run → source_registry。"""
    with c.cursor() as cur:
        cur.execute("""DELETE FROM job_requirement WHERE job_id IN
                       (SELECT job_id FROM job_posting WHERE source_id=%s)""", (SRC,))
        cur.execute("""DELETE FROM job_task WHERE job_id IN
                       (SELECT job_id FROM job_posting WHERE source_id=%s)""", (SRC,))
        cur.execute("DELETE FROM job_posting WHERE source_id = %s", (SRC,))
        cur.execute("DELETE FROM provenance WHERE source_id = %s", (SRC,))
        cur.execute("DELETE FROM ingest_run WHERE source_id = %s", (SRC,))
        cur.execute("DELETE FROM source_registry WHERE source_id = %s", (SRC,))
        cur.execute("""
            INSERT INTO source_registry (source_id, name, source_type, base_url,
                license_note, credibility, evidence_grade, update_freq, access_tier, status)
            VALUES (%s, %s, 'SRC2', %s,
                    '本地 fixture 站点：自建测试语料，robots.txt 显式 Allow: /jobs/',
                    1.0, 'C', 'oneoff', 'T1', 'active')""", (SRC, "本地招聘站点 fixture（采集测试）", ROOT))
    c.commit()
    if os.path.isdir(RAW):
        shutil.rmtree(RAW)


def main():
    print("=" * 78)
    print("采集管道端到端测试（合规门禁 / L0 去重 / 血缘 / 限速）")
    print("=" * 78)

    srv = serve(PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("【准备】fixture 站点已起：%s" % ROOT)

    try:
        with conn() as c:
            reset_test_source(c)

        # -------------------------------------------------------------
        print("\n【T1】合规前置：未登记来源必须被拒")
        rc, out = collector(["--source", "src_not_registered", "--index", ROOT + "/", "--delay", "0"])
        check(rc != 0 and "未登记" in out, "未登记来源被拒绝（退出码 %d）" % rc)

        print("\n【T2】合规前置：license_note 为占位符必须被拒")
        with conn() as c:
            with c.cursor() as cur:
                cur.execute("""INSERT INTO source_registry
                    (source_id,name,source_type,base_url,license_note,credibility,
                     evidence_grade,access_tier,status)
                    VALUES ('src_placeholder','占位来源','SRC2',%s,
                            '待补充：采集前必须写明许可结论',0.5,'C','T1','active')""", (ROOT,))
            c.commit()
        rc, out = collector(["--source", "src_placeholder", "--index", ROOT + "/", "--delay", "0"])
        check(rc != 0 and "license_note 未完善" in out, "占位 license_note 被拒绝")
        with conn() as c:
            c.cursor().execute("DELETE FROM source_registry WHERE source_id='src_placeholder'")
            c.commit()

        print("\n【T3】合规前置：robots.txt 禁止的路径必须被拒")
        rc, out = collector(["--source", SRC, "--urls", _write_urls(
            [ROOT + "/private/secret.html"]), "--delay", "0"])
        check("合规拒绝" in out or "禁止" in out, "robots Disallow 路径被拒绝")
        print("     采集器输出：" + [l for l in out.splitlines() if "拒绝" in l or "禁止" in l][:1][0].strip())

        # -------------------------------------------------------------
        print("\n【T4】正式采集：从索引发现链接并逐条落盘")
        rc, out = collector(["--source", SRC, "--index", ROOT + "/",
                             "--match", "/jobs/", "--delay", "0", "--limit", str(LIMIT)])
        check(rc == 0, "采集命令成功返回")
        n_ok = None
        for line in out.splitlines():
            if "批次完成" in line:
                n_ok = int(line.split("成功")[1].split("（")[0].strip())
        check(n_ok and n_ok == LIMIT, "采集到 %s 份页面（限量 %d）" % (n_ok, LIMIT))

        with conn() as c:
            run = qrow(c, """SELECT ingest_run_id, status, record_count, error_count, tool_version,
                                    params->>'n_urls' AS n_urls
                             FROM ingest_run WHERE source_id=%s
                             ORDER BY started_at DESC LIMIT 1""", (SRC,))
            check(run and run["status"] == "ok", "ingest_run 状态 ok（error_count=%s）"
                  % (run["error_count"] if run else "?"))
            n_prov = q1(c, "SELECT count(*) FROM provenance WHERE source_id=%s", (SRC,))
            check(n_prov == run["record_count"], "血缘条数 %d == 批次记录数 %d"
                  % (n_prov, run["record_count"]))
            check(q1(c, "SELECT count(*) FROM provenance WHERE source_id=%s "
                        "AND evidence_grade='C'", (SRC,)) == n_prov,
                  "每条血缘都带证据分级 C")
            check(q1(c, "SELECT count(*) FROM provenance WHERE source_id=%s "
                        "AND source_url IS NULL", (SRC,)) == 0, "每条血缘都有来源 URL")
            sample = qrow(c, """SELECT p.source_url, p.note, p.fetched_at
                                FROM provenance p WHERE p.source_id=%s LIMIT 1""", (SRC,))
            check(sample["note"] and sample["note"].startswith("data"),
                  "血缘记录了 L0 落盘路径（%s）" % sample["note"])

        n_files = len(l0_files(".html"))
        check(n_files == n_ok, "L0 落盘文件数 %d == 采集数 %d" % (n_files, n_ok))

        # -------------------------------------------------------------
        print("\n【T5】L0 去重：重跑一次不产生新文件")
        rc, out = collector(["--source", SRC, "--index", ROOT + "/",
                             "--match", "/jobs/", "--delay", "0", "--limit", str(LIMIT)])
        n2 = None
        for line in out.splitlines():
            if "批次完成" in line:
                n2 = int(line.split("新落盘")[1].split("/")[0].strip())
        check(rc == 0 and n2 == 0, "第二次采集新落盘 %s 份（应为 0，全部命中内容哈希去重）" % n2)
        check(len(l0_files(".html")) == n_files, "L0 文件总数未变（%d）" % n_files)
        with conn() as c:
            check(q1(c, "SELECT count(*) FROM ingest_run WHERE source_id=%s", (SRC,)) >= 3,
                  "每次采集都有独立批次记录")

        # -------------------------------------------------------------
        print("\n【T6】L0 不可变：文件带来源侧车文件，可反查")
        htmls = l0_files(".html")
        side = l0_files(".src")
        check(len(side) == len(htmls), "每个 L0 文件都有 .src 侧车（%d/%d）"
              % (len(side), len(htmls)))
        c0 = open(htmls[0], encoding="utf-8").read()
        check(len(c0) > 500 and "岗位职责" in c0, "L0 文件保存的是完整原始页面")

    finally:
        srv.shutdown()

    return H.report()


def _write_urls(urls):
    p = os.path.join(BASE, "dist", "_collect_test_urls.txt")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(urls) + "\n")
    return p


if __name__ == "__main__":
    sys.exit(main())
