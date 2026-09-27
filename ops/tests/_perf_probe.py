# -*- coding: utf-8 -*-
"""Throwaway timing probe: run each run_all step separately and time it."""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.stdout.reconfigure(encoding="utf-8")

import importlib.util

spec = importlib.util.spec_from_file_location("run_all", os.path.join(BASE, "ops", "tests", "run_all.py"))
run_all = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_all)

PY = sys.executable
STEPS = list(run_all.STEPS) + [("R" + s[0][1:], s[1], s[2], s[3]) for s in run_all.AFTER]


def main():
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    rows = []
    total = 0.0
    for n, name, kind, tgt in STEPS:
        if only and n not in only:
            continue
        t0 = time.perf_counter()
        if kind == "py":
            cmd = [PY, os.path.join(BASE, tgt.replace("/", os.sep))]
        else:
            cmd = [PY, os.path.join(BASE, "ops", "pg.py"), "sql",
                   os.path.join(BASE, tgt.replace("/", os.sep))]
        p = subprocess.run(cmd, cwd=BASE, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=3600)
        dt = time.perf_counter() - t0
        total += dt
        raw = p.stdout.decode("utf-8", "replace")
        tail = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        summ = tail[-1][:70] if tail else "(no output)"
        rows.append((dt, n, name, p.returncode, summ))
        print("  %7.2fs  [%s] %-28s rc=%d  %s" % (dt, n, name, p.returncode, summ), flush=True)
    print("\nTOTAL %.2fs" % total)
    print("\n-- ranked --")
    for dt, n, name, rc, summ in sorted(rows, reverse=True):
        print("  %7.2fs  %-4s %s" % (dt, n, name))


if __name__ == "__main__":
    main()
