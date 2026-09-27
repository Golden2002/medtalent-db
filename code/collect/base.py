# -*- coding: utf-8 -*-
"""
code/collect/base.py —— 采集基座（T07）

三条不可绕过的纪律，全部在这里强制：

1. **合规前置**：来源必须已在 source_registry 登记，且 license_note 不是占位符；
   同时调用 code/compliance_check.py 的黑名单 + robots.txt 判定。任一不过 → 直接抛错。
2. **L0 只增不改**：原始内容按 sha256 命名落盘到 data/raw/<source>/<日期>/，
   同名即同内容，天然去重；不覆盖、不删除。
3. **逐条血缘**：每次采集写 ingest_run（批次）与 provenance（逐条），
   记录 URL、抓取时间、方法、抽取器版本与证据分级。

限速默认 3 秒/请求（对第三方站点的礼貌要求）；本地 fixture 测试可显式设为 0。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.path.insert(0, os.path.join(BASE, "code"))
from compliance_check import check as compliance_check  # noqa: E402

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
UA = "MedTalentKB-Collector/0.1 (+local research; respects robots.txt)"
RAW_ROOT = os.path.join(BASE, "data", "raw")


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


# ---------------------------------------------------------------------------
# 1. 合规前置
# ---------------------------------------------------------------------------
class ComplianceError(RuntimeError):
    pass


def ensure_source(c, source_id: str) -> dict:
    with c.cursor() as cur:
        cur.execute("""
            SELECT source_id, name, source_type, base_url, license_note,
                   evidence_grade, status
            FROM source_registry WHERE source_id = %s""", (source_id,))
        row = cur.fetchone()
    if not row:
        raise ComplianceError("来源 %s 未登记。请先运行："
                              "python code/compliance_check.py --register %s --name ... --base-url ..."
                              % (source_id, source_id))
    if row["status"] != "active":
        raise ComplianceError("来源 %s 状态为 %s，不可采集" % (source_id, row["status"]))
    note = (row["license_note"] or "").strip()
    if not note or "待补充" in note:
        raise ComplianceError("来源 %s 的 license_note 未完善，合规前提下不得采集" % source_id)
    return row


def ensure_url_allowed(url: str) -> None:
    ok, reasons = compliance_check(url, verbose=False)
    if not ok:
        raise ComplianceError("URL 未通过合规判定：%s\n  - %s" % (url, "\n  - ".join(reasons)))


# ---------------------------------------------------------------------------
# 2. 批次与 L0 落盘
# ---------------------------------------------------------------------------
def start_run(c, source_id: str, tool_version: str, params: dict) -> str:
    run_id = "run_%s_%s" % (dt.datetime.now().strftime("%Y%m%d_%H%M%S"),
                            hashlib.sha1(os.urandom(8)).hexdigest()[:6])
    with c.cursor() as cur:
        cur.execute("""
            INSERT INTO ingest_run (ingest_run_id, source_id, started_at, status,
                                    tool_version, params)
            VALUES (%s, %s, now(), 'running', %s, %s::jsonb)""",
            (run_id, source_id, tool_version, _json(params)))
    return run_id


def finish_run(c, run_id: str, n_ok: int, n_err: int, status: str = None) -> None:
    st = status or ("ok" if n_err == 0 else ("partial" if n_ok else "failed"))
    with c.cursor() as cur:
        cur.execute("""
            UPDATE ingest_run SET finished_at = now(), record_count = %s,
                   error_count = %s, status = %s WHERE ingest_run_id = %s""",
            (n_ok, n_err, st, run_id))


def land_raw(source_id: str, url: str, content: bytes, ext: str = ".html") -> tuple[str, str, bool]:
    """把原始内容落 L0。返回 (相对路径, sha256, 是否新文件)。按内容哈希命名 → 天然去重。"""
    sha = hashlib.sha256(content).hexdigest()
    day = dt.date.today().isoformat()
    d = os.path.join(RAW_ROOT, source_id, day)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sha[:16] + ext)
    if os.path.exists(path):
        return os.path.relpath(path, BASE), sha, False
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(content)
    os.replace(tmp, path)          # 原子落盘：不会留下半截文件
    with open(path + ".src", "w", encoding="utf-8") as fh:
        fh.write(url + "\n")       # 侧车文件记录来源 URL，便于反查
    return os.path.relpath(path, BASE), sha, True


def record_provenance(c, run_id: str, source_id: str, url: str, raw_path: str,
                      sha: str, method: str = "html_parse",
                      extractor_version: str = "collector@0.1",
                      evidence_grade: str = "C", record_uid: str = None) -> str:
    # provenance_id 必须包含 source_id：血缘是"某个来源产出了某条记录"，
    # 只按 (sha,url) 派生会让不同来源采到同一页面时被 ON CONFLICT 静默吞掉。
    pid = "prv_" + hashlib.sha1(("%s|%s|%s" % (source_id, sha, url)).encode()).hexdigest()[:20]
    with c.cursor() as cur:
        cur.execute("""
            INSERT INTO provenance (provenance_id, record_uid, source_id, ingest_run_id,
                                    source_url, fetched_at, extract_method,
                                    extractor_version, evidence_grade, human_verified, note)
            VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s, false, %s)
            ON CONFLICT (provenance_id) DO NOTHING""",
            (pid, record_uid or ("raw:" + sha[:16]), source_id, run_id,
             url, method, extractor_version, evidence_grade, raw_path))
    return pid


# ---------------------------------------------------------------------------
# 3. 抓取（限速 + 重试 + 合规）
# ---------------------------------------------------------------------------
def fetch(url: str, delay: float = 3.0, timeout: int = 30, retries: int = 3,
          last_request: list = None) -> tuple[int, bytes]:
    ensure_url_allowed(url)
    last = last_request if last_request is not None else [0.0]
    for attempt in range(1, retries + 1):
        wait = delay - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                last[0] = time.time()
                return r.status, body
        except urllib.error.HTTPError as e:
            last[0] = time.time()
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)          # 指数退避
                continue
            raise
        except urllib.error.URLError:
            last[0] = time.time()
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def _json(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False)
