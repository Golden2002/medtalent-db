# -*- coding: utf-8 -*-
"""
ops/fixtures/build_job_side.py —— 一键复现整条岗位侧流水线（T06→T10）

依次执行：
  1. 重置 fixture（仅 mock 数据；真实来源绝不重置）
  2. 生成 mock 招聘语料（默认每模板 30 条，保证每族样本 ≥30）
  3. 起本地 fixture 站点（真实 HTTP + 真实 robots.txt）
  4. 采集：合规门禁 → L0 落盘（按内容哈希去重）→ ingest_run/provenance 血缘
  5. 解析入库：job_posting / job_task / job_requirement
  6. 概念映射 + 职业映射 + 能力权重矩阵
  7. 质量门（T09）逐项验收

用法：python ops/fixtures/build_job_side.py            # 默认每模板 30 条
      python ops/fixtures/build_job_side.py --per 8    # 小样本快速验证
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "ops", "tests"))

from fixture_server import serve  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
PORT = 8098
ROOT = "http://127.0.0.1:%d" % PORT
SRC = "src_fixture_careers"


def run(title, args, show_tail=3):
    print("\n" + "=" * 78)
    print("▶ %s" % title)
    print("=" * 78)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, cwd=BASE)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-show_tail:] if show_tail else out.splitlines():
        print("  " + line)
    if r.returncode != 0:
        print("  [X] 步骤失败（退出码 %d）" % r.returncode)
        sys.exit(r.returncode)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=30)
    a = ap.parse_args()

    run("步骤 1/7 重置 fixture 语料", [os.path.join(BASE, "ops", "fixtures", "reset_fixture.py")])
    run("步骤 2/7 生成 mock 招聘语料（每模板 %d 条）" % a.per,
        [os.path.join(BASE, "ops", "fixtures", "gen_jd_corpus.py"), "--per", str(a.per)])

    srv = serve(PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("\n" + "=" * 78)
    print("▶ 步骤 3/7 起本地 fixture 站点：%s" % ROOT)
    print("=" * 78)
    try:
        run("步骤 4/7 采集（合规门禁 + L0 落盘 + 血缘）",
            [os.path.join(BASE, "code", "collect", "run.py"), "--source", SRC,
             "--index", ROOT + "/", "--match", "/jobs/", "--delay", "0"], show_tail=2)
        run("步骤 5/7 解析入库（job_posting / job_task / job_requirement）",
            [os.path.join(BASE, "code", "parse", "jd_ingest.py"), "--source", SRC], show_tail=3)
    finally:
        srv.shutdown()

    run("步骤 6/7 概念映射 + 职业映射 + 能力权重矩阵",
        [os.path.join(BASE, "code", "analytics", "build_competency.py"), "--min-sample", "30"],
        show_tail=6)
    run("步骤 7/7 质量门验收",
        [os.path.join(BASE, "code", "gates", "jd_quality_gate.py"),
         "--source", SRC, "--sample", "3"], show_tail=0)
    print("\n[✓] 岗位侧流水线复现完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
