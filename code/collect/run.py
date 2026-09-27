# -*- coding: utf-8 -*-
"""
code/collect/run.py —— 采集 CLI（T07）

用法：
  # 从索引页发现链接并逐条采集
  python code/collect/run.py --source src_fixture_careers \
      --index http://127.0.0.1:8098/ --match "/jobs/"

  # 或给定 URL 清单（每行一个）
  python code/collect/run.py --source src_x --urls urls.txt

  # 本地 fixture 测试用：--delay 0 关闭限速（真实站点必须用默认 3 秒）
  python code/collect/run.py --source src_fixture_careers --index ... --delay 0

输出：L0 原始文件 + ingest_run 批次 + provenance 逐条血缘。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import (ComplianceError, conn, ensure_source, fetch, finish_run,  # noqa: E402
                  land_raw, record_provenance, start_run)

sys.stdout.reconfigure(encoding="utf-8")

HREF = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
TOOL_VERSION = "collector@0.1"


def discover(index_url: str, match: str, delay: float) -> list[str]:
    _, body = fetch(index_url, delay=delay, last_request=[0.0])
    text = body.decode("utf-8", "replace")
    urls, seen = [], set()
    for href in HREF.findall(text):
        if href.startswith(("mailto:", "javascript:", "#")):
            continue
        full = urllib.parse.urljoin(index_url, href)
        if match and match not in full:
            continue
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--index")
    ap.add_argument("--urls")
    ap.add_argument("--match", default="")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="请求间隔秒数；对第三方站点必须 ≥3，本地 fixture 可设 0")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--evidence-grade", default="C", choices=list("ABCD"))
    a = ap.parse_args()
    try:
        return _run(a)
    except ComplianceError as e:
        # 合规拒绝是可预期结果，不是崩溃：打印清晰信息并以专用退出码结束
        print("[合规拒绝] %s" % e)
        return 3


def _run(a):
    with conn() as c:
        src = ensure_source(c, a.source)
        print("[✓] 来源合规：%s（%s，证据分级 %s）"
              % (src["source_id"], src["name"], a.evidence_grade))

        if a.urls:
            with open(a.urls, encoding="utf-8") as fh:
                urls = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
            index_url = None
        elif a.index:
            index_url = a.index
            urls = discover(index_url, a.match, a.delay)
            print("[✓] 从索引页发现 %d 个链接（匹配 %r）" % (len(urls), a.match))
        else:
            print("[X] 需要 --index 或 --urls 之一")
            return 2
        if a.limit:
            urls = urls[:a.limit]
        if not urls:
            print("[X] 没有待采集 URL")
            return 1

        run_id = start_run(c, a.source, TOOL_VERSION,
                           {"index": index_url, "match": a.match,
                            "delay": a.delay, "n_urls": len(urls)})
        print("[✓] 批次 %s 已开" % run_id)

        n_ok = n_err = n_new = n_dup = 0
        last = [0.0]
        for i, url in enumerate(urls, 1):
            try:
                status, body = fetch(url, delay=a.delay, last_request=last)
                rel, sha, is_new = land_raw(a.source, url, body)
                record_provenance(c, run_id, a.source, url, rel, sha,
                                  evidence_grade=a.evidence_grade)
                n_ok += 1
                n_new += 1 if is_new else 0
                n_dup += 0 if is_new else 1
                if i % 25 == 0 or i == len(urls):
                    print("    %d/%d  新文件 %d  去重命中 %d"
                          % (i, len(urls), n_new, n_dup))
            except ComplianceError as e:
                n_err += 1
                print("    [合规拒绝] %s -> %s" % (url, str(e).splitlines()[0]))
            except Exception as e:  # noqa: BLE001
                n_err += 1
                print("    [失败] %s -> %s: %s" % (url, type(e).__name__, str(e)[:120]))

        finish_run(c, run_id, n_ok, n_err)
        c.commit()
        print("\n[✓] 批次完成：成功 %d（新落盘 %d / 内容去重 %d），失败 %d"
              % (n_ok, n_new, n_dup, n_err))
    return 0


if __name__ == "__main__":
    sys.exit(main())
