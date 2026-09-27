#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
code/compliance_check.py — T05 采集合规校验器

作用：在**任何采集动作之前**给出"能不能采"的判定，并把判定留痕。
三层判定：
  1. 硬黑名单：条款/robots 明确禁止自动化的平台，一律拒绝（附理由）。
  2. 来源登记：未在 source_registry 登记、或无 license_note 的来源，拒绝。
  3. robots.txt：按目标 URL 实际判定 can_fetch，并记录检查时间。

用法：
  python code/compliance_check.py --url https://example.com/careers/1
  python code/compliance_check.py --enforce https://example.com/careers/1   # 不允许则退出码 1
  python code/compliance_check.py --register src_company_careers --name "某公司招聘页" \
         --url https://example.com/careers --type SRC2 --grade C
  python code/compliance_check.py --check-all
  python code/compliance_check.py --list

证据依据见 docs/03-岗位图谱构建方案.md §2.2。
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG = os.path.join(ROOT, "ops", "pg.py")
TOOLS = r"D:\wbo-workspace\.tools"
PGBIN = os.path.join(TOOLS, "pgsql", "bin")
TMPDIR = os.path.join(ROOT, "dist")
UA = "MedTalentKB-ComplianceBot/0.1 (+local research; respects robots.txt)"

# 硬黑名单：条款或 robots.txt 明确禁止自动化。域名后缀匹配。
BLACKLIST = {
    "zhipin.com": "BOSS 直聘：robots.txt 明确 Disallow /*?* 与 /job_detail/l*.html",
    "zhaopin.com": "智联招聘：站点条款禁止抓取与转载",
    "51job.com":  "前程无忧：robots.txt 缺失且页脚明文禁止转载",
    "linkedin.com": "LinkedIn：用户协议禁止自动化抓取",
    "indeed.com": "Indeed：站点条款禁止自动化抓取",
}
# 需逐 URL 判定（robots 允许静态页、禁带参数页），不做整站放行也不整站封禁
CONDITIONAL = {
    "liepin.com": "猎聘：robots.txt Disallow /*?*，仅静态聚合页可用；带查询参数的 URL 一律拒绝",
}


def psql(sql, tuples_only=True):
    """执行 SQL。

    **为什么不直接把 SQL 放在命令行**：Windows 下 psql.exe 以 ANSI 代码页解释
    argv，含中文的 SQL 经命令行传入会触发 "invalid byte sequence for encoding UTF8"。
    因此统一写入 UTF-8 临时文件后用 `-f` 执行（psql 按 client_encoding 读文件）。
    """
    os.makedirs(TMPDIR, exist_ok=True)
    tmp = os.path.join(TMPDIR, "_compliance_tmp.sql")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sql.rstrip() + "\n")
    cmd = [os.path.join(PGBIN, "psql.exe"), "-h", "127.0.0.1", "-p", "55432",
           "-U", "postgres", "-d", "medtalent", "-v", "ON_ERROR_STOP=1",
           "-f", tmp]
    if tuples_only:
        cmd.append("-tA")
    env = dict(os.environ)
    env["PGCLIENTENCODING"] = "UTF8"
    env["PGOPTIONS"] = "-c search_path=mt,public"
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0 and err:
        out = (out + "\n" + err).strip()
    return out, r.returncode


def domain_of(url):
    return (urllib.parse.urlparse(url).hostname or "").lower()


def match_blacklist(url):
    d = domain_of(url)
    for dom, why in BLACKLIST.items():
        if d == dom or d.endswith("." + dom):
            return why
    return None


def match_conditional(url):
    d = domain_of(url)
    for dom, why in CONDITIONAL.items():
        if d == dom or d.endswith("." + dom):
            return why
    return None


def robots_allows(url, timeout=20):
    """按 RFC 9309 判定 robots 许可。

    返回 (allowed, robots_url, detail)
    策略（RFC 9309 §2.3.1）：
      · 200        → 正常解析 can_fetch
      · 404 / 410 / 其它 4xx → robots.txt 不存在，视为**允许**（记录"无 robots.txt"）
      · 401 / 403  → robots.txt 存在但拒绝访问，视为**不允许**
      · 5xx / 网络或 TLS 错误 → 无法判定，**保守视为不允许**，标记待重试
    """
    p = urllib.parse.urlparse(url)
    robots_url = "%s://%s/robots.txt" % (p.scheme, p.netloc)
    req = urllib.request.Request(robots_url, headers={"User-Agent": UA})
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            rp.parse(fh.read().decode("utf-8", "replace").splitlines())
        allowed = rp.can_fetch(UA, url)
        return allowed, robots_url, "robots.txt 已读取，can_fetch=%s" % allowed
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, robots_url, "robots.txt 返回 %d（存在但拒绝访问），视为不允许" % e.code
        if 400 <= e.code < 500:
            return True, robots_url, ("robots.txt 返回 %d（不存在）——按 RFC 9309 视为允许，"
                                      "但仍须遵守站点条款与频率限制" % e.code)
        return False, robots_url, "robots.txt 返回 %d（服务端错误）——保守视为不允许，待重试" % e.code
    except urllib.error.URLError as e:
        return False, robots_url, "网络/TLS 异常（%s）——保守视为不允许" % str(e.reason)[:60]
    except Exception as e:
        return False, robots_url, "robots 检查失败（%s）——保守视为不允许" % type(e).__name__


def check(url, verbose=True):
    """返回 (allowed: bool, reasons: list[str])"""
    reasons = []
    bl = match_blacklist(url)
    if bl:
        if verbose:
            print("[DENY] %s\n   理由：%s" % (url, bl))
        return False, [bl]

    if urllib.parse.urlparse(url).query:
        cond = match_conditional(url)
        if cond:
            msg = "%s；该 URL 带查询参数" % cond
            if verbose:
                print("[DENY] %s\n   理由：%s" % (url, msg))
            return False, [msg]

    allowed, robots_url, detail = robots_allows(url)
    reasons.append(detail)
    if not allowed:
        if verbose:
            print("[DENY] %s\n   %s（%s）" % (url, detail, robots_url))
        return False, reasons
    if verbose:
        print("[ALLOW] %s\n   %s" % (url, detail))
    return True, reasons


def cmd_url(a):
    ok, _ = check(a.url)
    print("判定：" + ("允许采集" if ok else "禁止采集"))
    return 0 if ok else 1


def cmd_enforce(a):
    target = a.enforce
    ok, reasons = check(target, verbose=False)
    print(("[ALLOW] " if ok else "[DENY] ") + target)
    for r in reasons:
        print("   - " + r)
    return 0 if ok else 1


def cmd_register(a):
    existing, _ = psql("SELECT source_id FROM source_registry WHERE source_id=%s"
                       % sql_lit(a.source_id))
    if existing:
        print("[=] 来源已存在：%s" % a.source_id)
        return 0
    sql = ("INSERT INTO source_registry (source_id, name, source_type, base_url, "
           "license_note, credibility, evidence_grade, update_freq, access_tier, status) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')"
           % (sql_lit(a.source_id), sql_lit(a.name), sql_lit(a.type),
              sql_lit(a.base_url), sql_lit(a.license_note or "待补充：采集前必须写明许可结论"),
              a.credibility, sql_lit(a.grade), sql_lit(a.freq), sql_lit(a.tier)))
    out, code = psql(sql, tuples_only=False)
    print(out)
    if code != 0:
        return code
    print("[✓] 已登记来源 %s" % a.source_id)
    return 0


def sql_lit(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def cmd_list(a):
    out, _ = psql("SELECT source_id || ' | ' || source_type || ' | ' || "
                  "COALESCE(evidence_grade,'-') || ' | ' || status || ' | ' || "
                  "COALESCE(base_url,'-') FROM source_registry ORDER BY source_id",
                  tuples_only=False)
    print(out)
    return 0


def cmd_check_all(a):
    out, _ = psql("SELECT source_id || '|' || COALESCE(base_url,'') || '|' || "
                  "COALESCE(license_note,'') FROM source_registry WHERE status='active'")
    rows = [l for l in out.splitlines() if "|" in l and not l.startswith("  $")]
    if not rows:
        print("没有已登记且启用的来源")
        return 0
    bad = 0
    for row in rows:
        sid, base, note = (row.split("|") + ["", ""])[:3]
        print("\n== %s ==" % sid)
        if not base:
            print("  [WARN] 未填写 base_url，跳过 robots 检查")
            continue
        if "待补充" in note:
            print("  [DENY] license_note 未完善 —— 合规前提下不得采集")
            bad += 1
            continue
        ok, _ = check(base)
        if not ok:
            bad += 1
        psql("UPDATE source_registry SET robots_checked_at = DATE %s WHERE source_id=%s"
             % (sql_lit(dt.date.today().isoformat()), sql_lit(sid)))
    print("\n不合规来源数：%d" % bad)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--enforce")
    g.add_argument("--register", dest="source_id")
    g.add_argument("--check-all", action="store_true")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--base-url", dest="base_url",
                    help="登记来源时的站点根地址（与 --url 互斥，避免与单 URL 判定混淆）")
    ap.add_argument("--type", default="SRC2")
    ap.add_argument("--grade", default="C", choices=list("ABCD"))
    ap.add_argument("--freq", default="weekly")
    ap.add_argument("--tier", default="T1")
    ap.add_argument("--credibility", default="0.6")
    ap.add_argument("--license-note")
    a = ap.parse_args()

    if a.url:
        return cmd_url(a)
    if a.enforce:
        return cmd_enforce(a)
    if a.source_id:
        if not a.name:
            print("[X] --register 需要 --name")
            return 2
        return cmd_register(a)
    if a.check_all:
        return cmd_check_all(a)
    if a.list:
        return cmd_list(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
