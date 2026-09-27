# -*- coding: utf-8 -*-
"""
ops/screenshots.py —— 可重放的展示截图采集器

为什么需要它：
    README 里的界面截图必须是**可复现的产物**，而不是某次手工截完就再也对不上的图片。
    页面一改（例如画像向量卡片挪到第二张卡），旧截图就变成错误证据。
    所以截图的定义写进代码：一个页面一条记录，一条命令重截全部。

与 static_site.py 的关系：
    static_site.py 把离线单页站点导出成 HTML+PDF；本脚本只负责"给正在跑的门户拍屏"，
    两者都用系统已装的 Chrome/Edge 无头模式，不引入任何依赖。

用法：
    python ops/screenshots.py                      # 截全部展示页到 docs/screenshots/
    python ops/screenshots.py --only talent-detail  # 只重截一页
    python ops/screenshots.py --base http://127.0.0.1:8082
    python ops/screenshots.py --list                # 只列清单不截图

前置条件：门户已在跑（python code/demo/serve_all.py）且数据库已启动。
截图前会先 HTTP 探活：状态码不是 200 就报错退出，绝不产出一张"连接被重置"的空白图。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUTDIR_DEFAULT = os.path.join(ROOT, "docs", "screenshots")

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "connect_timeout=5")

# ---------------------------------------------------------------------------
# 展示页清单：(文件名, 路径, 视口宽, 视口高, 说明)
#   视口高度按页面实际长度给：给太矮会截掉下半页，给太大会留一大片空白。
#   高度是"看第一屏"用的，不是整页长图 —— 展示图要看得清，不要一条细长条。
# ---------------------------------------------------------------------------
PAGES = [
    ("overview",        "/",                 1680, 1250, "首页：库体量、域分布、质量门禁、最近变更"),
    ("talent-list",     "/talent",           1680, 1400, "人才库：35 列字段注册表 + 预设 + 列选择器 + 表内分析按钮"),
    ("talent-detail",   "/talent/{pid}",     1680, 1500, "人才详情：画像向量卡片（50 维）+ 分维度取值与登记状态"),
    ("occupations",     "/occupations",      1680, 1300, "职业库：职业节点、能力权重、需求侧统计"),
    ("tree",            "/tree",             1680, 1300, "职业树：父子边 + 演化变更加速度视图"),
    ("match",           "/match",            1680, 1400, "能力-职业匹配：三态 met/gap/unknown + 逐维度分解"),
    ("viz",             "/viz",              1680, 1500, "可视化：16 个在线图表（内联 SVG，零 CDN）"),
    ("viz-build",       "/viz/build",        1680, 1200, "图表构建器：选表→选维度→出图→导出 CSV"),
    ("schema",          "/schema",           1680, 1300, "库结构：80 表 / 17 视图，主外键与约束定义"),
    ("quality",         "/quality",          1680, 1300, "质量门禁与合规：口径唯一的指标 + 审计与保留策略"),
    ("extend",          "/extend",           1680, 1300, "扩展机制：三种受控加列/加行方式的入口"),
    ("dev-mode",        "/dev",              1680, 1250, "开发者模式：在线 SQL / 建模 / DDL 计量 / 审计"),
]


def pick_browser() -> str | None:
    return next((b for b in BROWSERS if os.path.exists(b)), None)


def probe(url: str, timeout: float = 6.0) -> int:
    """HTTP 探活：返回状态码，连不上抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "medtalent-shots"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(2048)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def showcase_person(need: bool = True) -> str | None:
    """
    选一个"信息最全"的样本做人才详情展示图。

    口径：画像维度有取值的项数最多、且挂在偏好/证书/教育记录上的行数最多。
    不要用固定 id —— 样本库重生成后固定 id 可能变空壳。
    """
    try:
        import psycopg
    except ImportError:
        if need:
            print("[!] 未安装 psycopg，无法自动选样本；改用兜底 id")
        return None
    sql = """
        SELECT p.person_id
        FROM mt.person p
        LEFT JOIN mt.preference pr ON pr.person_id = p.person_id
        LEFT JOIN mt.credential c  ON c.person_id  = p.person_id
        LEFT JOIN mt.education_record e ON e.person_id = p.person_id
        GROUP BY p.person_id
        ORDER BY count(pr.preference_id) + count(c.credential_id)
                 + count(e.education_id) DESC, p.person_id
        LIMIT 1
    """
    try:
        with psycopg.connect(DSN) as c, c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001 —— 探活失败不该让截图脚本崩，退化为兜底
        print("[!] 查样本失败（%s），改用兜底 id" % type(e).__name__)
        return None


def shoot(exe: str, url: str, out: str, w: int, h: int) -> int:
    """无头截一张图，返回文件字节数（0 表示失败）。"""
    if os.path.exists(out):
        os.remove(out)
    cmd = [
        exe, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--force-device-scale-factor=1",
        "--virtual-time-budget=4000",          # 等内联 SVG 画完
        "--window-size=%d,%d" % (w, h),
        "--screenshot=" + out,
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    if not os.path.exists(out):
        print("    stderr: %s" % (r.stderr or "")[:300])
        return 0
    return os.path.getsize(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8082",
                    help="门户地址（默认 http://127.0.0.1:8082）")
    ap.add_argument("--out", default=OUTDIR_DEFAULT)
    ap.add_argument("--only", default=None,
                    help="只截清单里名字包含该子串的页")
    ap.add_argument("--list", action="store_true", help="只列清单")
    ap.add_argument("--min-bytes", type=int, default=8000,
                    help="小于该字节数视为空白页并报错（默认 8000）")
    a = ap.parse_args()

    pages = PAGES
    if a.only:
        pages = [p for p in PAGES if a.only in p[0]]
        if not pages:
            print("[!] --only %r 没有匹配项；清单：%s"
                  % (a.only, ", ".join(p[0] for p in PAGES)))
            return 2

    if a.list:
        for name, path, w, h, note in pages:
            print("%-14s %-22s %dx%d  %s" % (name, path, w, h, note))
        return 0

    exe = pick_browser()
    if not exe:
        print("[!] 未找到 Chrome/Edge，无法截图")
        return 2
    print("浏览器：%s" % exe)

    pid = showcase_person()
    if pid is None:
        pid = "per_mock_0001"
        print("[!] 兜底样本 id = %s（可能不是最全的一条）" % pid)
    else:
        print("展示样本：%s" % pid)

    os.makedirs(a.out, exist_ok=True)

    # ---- 先探活：宁可整体失败，也不要产出一堆空白图 ----
    bad = []
    for name, path, _w, _h, _n in pages:
        url = a.base + path.replace("{pid}", str(pid))
        try:
            code = probe(url)
        except Exception as e:  # noqa: BLE001
            print("[!] %s 连不上：%s" % (url, e))
            return 2
        if code != 200:
            bad.append((name, url, code))
    if bad:
        for name, url, code in bad:
            print("[!] %s → HTTP %s（%s）" % (name, code, url))
        print("[!] 有页面非 200，中止截图（不产出错误证据）")
        return 1

    print("-" * 78)
    manifest = []
    fails = 0
    for name, path, w, h, note in pages:
        url = a.base + path.replace("{pid}", str(pid))
        out = os.path.join(a.out, "%s.png" % name)
        size = shoot(exe, url, out, w, h)
        ok = size >= a.min_bytes
        if not ok:
            fails += 1
        print("%s %-14s %7.0f KB  %s" % ("[OK  ]" if ok else "[FAIL]",
                                         name, size / 1024.0, note))
        manifest.append({"name": name, "path": path, "url": url,
                         "file": os.path.relpath(out, ROOT).replace("\\", "/"),
                         "viewport": "%dx%d" % (w, h),
                         "bytes": size, "ok": ok, "note": note})

    mf = os.path.join(a.out, "manifest.json")
    with open(mf, "w", encoding="utf-8") as fh:
        json.dump({"base": a.base, "person": pid, "shots": manifest},
                  fh, ensure_ascii=False, indent=2)
    print("-" * 78)
    print("清单：%s" % mf)
    print("%d/%d 张成功" % (len(pages) - fails, len(pages)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
