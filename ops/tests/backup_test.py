# -*- coding: utf-8 -*-
"""
ops/tests/backup_test.py —— 备份/清理/校验/恢复端到端测试（T12）

验证用户提出的三点：
  a1 定期备份 → 备份产物完整（全库 dump + 用户数据 XLSX + manifest）
  a2 清理备份、保留一定时间内的副本 → GFS 保留策略判定正确
  a3 **能通过副本恢复数据** → 真删一批数据，再从未受损的副本恢复出来核对

外加两条工程纪律：
  · 备份损坏必须能被发现（sha256 校验）
  · 默认不导出 PII；显式 --include-pii 才导出并标记 T3

用法：python ops/tests/backup_test.py
"""
import datetime as dt
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "ops", "backup"))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import backup as bk  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
DSN = bk.DSN
PASS, FAIL = H.PASS, H.FAIL
check, q1 = H.check, H.q1


def db():
    return H.connect(DSN)


def main():
    print("=" * 78)
    print("备份 / 清理 / 校验 / 恢复 端到端测试（T12）")
    print("=" * 78)

    made = []

    # ===============================================================
    print("\n【T1】备份产物完整性：dump + XLSX + manifest + 恢复指引")
    rc = bk.do_backup(note="自动化测试备份")
    check(rc == 0, "备份命令返回 0")
    with db() as c:
        b = q1(c, "SELECT backup_id FROM backup_run WHERE status='ok' "
                  "ORDER BY started_at DESC LIMIT 1")
    made.append(b)
    d = os.path.join(bk.BACKUP_ROOT, b)
    for f in ("medtalent.dump", "user_data.xlsx", "manifest.json", "RESTORE.md"):
        check(os.path.isfile(os.path.join(d, f)), "产物存在：%s" % f)
    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
        man = json.load(fh)
    check(all(f.get("sha256") for f in man["files"]), "manifest 为每个文件记录 sha256")
    check(man["accessTier"] == "T2" and man["containsPii"] is False,
          "默认不导出 PII，访问层级标记为 %s" % man["accessTier"])
    check(len(man["tableCounts"]) >= 30, "记录了 %d 张表的行数（恢复核对依据）"
          % len(man["tableCounts"]))

    print("\n【T2】XLSX 副本可读，且默认剔除 PII 列")
    from openpyxl import load_workbook
    wb = load_workbook(os.path.join(d, "user_data.xlsx"), read_only=True)
    sheets = wb.sheetnames
    check("person" in sheets and "job_posting" in sheets,
          "含用户表与业务表（共 %d 个 sheet）" % len(sheets))
    check("person_pii" not in sheets, "person_pii 整表未导出")
    if "person_demographics" in sheets:
        hdr = [c.value for c in next(wb["person_demographics"].iter_rows(max_row=1))]
        check("health_limits" not in hdr, "敏感列 health_limits 已剔除")
    if "person" in sheets:
        hdr = [c.value for c in next(wb["person"].iter_rows(max_row=1))]
        check("subject_code" in hdr, "非敏感列正常导出（subject_code）")
    wb.close()

    print("\n【T3】sha256 校验：能发现静默损坏")
    rc = bk.do_verify(b, all_=False)
    check(rc == 0, "未损坏时校验通过")
    dump = os.path.join(d, "medtalent.dump")
    with open(dump, "rb") as fh:
        data = bytearray(fh.read())
    data[200] ^= 0xFF                      # 篡改一个字节
    with open(dump, "wb") as fh:
        fh.write(data)
    rc = bk.do_verify(b, all_=False)
    check(rc != 0, "篡改一个字节后被检出（返回非 0）")
    bk.do_backup(note="恢复被篡改的测试备份")     # 重新造一份可用的
    with db() as c:
        b2 = q1(c, "SELECT backup_id FROM backup_run WHERE status='ok' "
                   "ORDER BY started_at DESC LIMIT 1")
    made.append(b2)
    check(bk.do_verify(b2, all_=False) == 0, "新备份校验通过")

    print("\n【T4】真删数据 → 从副本恢复到新库核对（核心能力）")
    with db() as c:
        before = q1(c, "SELECT count(*) FROM job_posting")
        with c.cursor() as cur:
            cur.execute("""DELETE FROM job_requirement WHERE job_id IN
                           (SELECT job_id FROM job_posting ORDER BY job_id LIMIT 10)""")
            cur.execute("""DELETE FROM job_task WHERE job_id IN
                           (SELECT job_id FROM job_posting ORDER BY job_id LIMIT 10)""")
            cur.execute("DELETE FROM job_posting WHERE job_id IN "
                        "(SELECT job_id FROM job_posting ORDER BY job_id LIMIT 10)")
        c.commit()
        after_del = q1(c, "SELECT count(*) FROM job_posting")
    check(after_del == before - 10,
          "生产库已丢失 10 条岗位（%d → %d），模拟数据损失" % (before, after_del))

    target = "medtalent_restore_test"
    rc = bk.do_restore(b2, target, keep=True)      # 保留恢复库以便直接核对
    check(rc == 0, "从未受损副本恢复到新库成功且逐表核对通过")

    dsn2 = DSN.replace("dbname=medtalent", "dbname=" + target)
    try:
        with psycopg.connect(dsn2, row_factory=dict_row) as c2:
            n = q1(c2, "SELECT count(*) FROM mt.job_posting")
            n_req = q1(c2, "SELECT count(*) FROM mt.job_requirement")
        check(n == before,
              "恢复库中岗位数 %d == 备份时 %d（**数据确实从副本回来了**）" % (n, before))
        check(n_req > 0, "关联的要求条目也一并恢复（%d 条）" % n_req)
    finally:
        bk.run([os.path.join(bk.PGBIN, "dropdb.exe"), "-h", bk.HOST, "-p", bk.PORT,
                "-U", bk.USER, "--if-exists", target])

    with db() as c:
        rr = q1(c, "SELECT status FROM restore_run ORDER BY started_at DESC LIMIT 1")
        check(rr in ("ok", "verified"), "恢复记录状态为 %s" % rr)

    # 生产库的 10 条：用 L0 重跑解析重建（派生数据可由管道确定性重建）
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(BASE, "code", "parse", "jd_ingest.py"),
                        "--source", "src_fixture_careers"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    # 关键：jd_ingest 会为每个 job 先删后插 job_requirement，
    # 因此概念映射与能力权重会被清空，**必须紧接着重建派生层**，
    # 否则整个库的匹配能力静默失效（这个坑在 docs/08 的问题 #24 记过）。
    subprocess.run([sys.executable, os.path.join(BASE, "code", "analytics",
                                                 "build_competency.py"), "--min-sample", "30"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    with db() as c:
        rest = q1(c, "SELECT count(*) FROM job_posting")
        mapped = q1(c, "SELECT count(*) FROM job_requirement WHERE concept_id IS NOT NULL")
    check(rest == before, "生产库已由 L0 重跑解析恢复到 %d 条（派生数据可确定性重建）" % rest)
    check(mapped > 0, "派生层已重建：%d 条要求重新映射到概念（否则匹配会静默失效）" % mapped)

    print("\n【T5】清理策略（GFS 日/周/月）判定正确")
    pol = {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6, "min_copies": 3}
    today = dt.date.today()

    def mk(days_ago, bid):
        return {"backup_id": bid, "started_at": dt.datetime.combine(
            today - dt.timedelta(days=days_ago), dt.time(2, 0))}

    def day_of(b):
        """从 backup_id 里取天数。用 split 而不是 `b[-2:]`：
        以前写 `int(b[-2:])`，一旦 id 变成三位数（例如 bk_d220）就会被读成第 0 天，
        把周层断言悄悄算错。"""
        return int(b["backup_id"].split("_d")[1])

    # 样本里额外放一份**确定超出月度层**的（keep_monthly×31 = 186 天）。
    sample = [mk(i, "bk_d%02d" % i) for i in range(0, 40)] + [mk(220, "bk_d220")]
    keep, drop = bk.plan_prune(pol, sample)
    keep_ids = {b["backup_id"] for b in keep}
    check("bk_d00" in keep_ids, "今天的备份保留")
    check("bk_d05" in keep_ids, "5 天前（日层）保留")
    # 周层每周只留一份：7–27 天区间内应有若干份被保留（而不是每天都留）
    weekly_kept = [b for b in keep_ids if 7 <= day_of({"backup_id": b}) < 28]
    check(len(weekly_kept) >= 2,
          "周层保留了 %d 份（7–27 天区间，每周一份）：%s"
          % (len(weekly_kept), sorted(weekly_kept)))
    check(len(weekly_kept) < 21, "周层未按天全留（否则策略失效）")
    # ⚠ 这条断言原来写的是「39 天前被清理」，**它只在部分日期成立**：
    #    月度层的判据是 `age < keep_monthly*31`（186 天），39 天落进月度分支；
    #    而"某个月的备份保不保留"取决于**该月是否已有更新的副本**。
    #    实测（2026-10-09）：39 天前 = 8 月 31 日，样本里没有别的 8 月备份，
    #    于是它作为"8 月的月度副本"被合法保留 —— 断言因此红。
    #    这是**日期依赖的测试缺陷**（不是备份逻辑的问题，也与本轮改动无关），
    #    修法是用一个确定超出月度窗口的天数，让结论不再取决于今天是几号。
    check("bk_d220" not in keep_ids,
          "220 天前（确定超出月层 6×31=186 天）被清理")
    check(len(keep) < len(sample), "确有清理（保留 %d / 共 %d）" % (len(keep), len(sample)))
    check(len(keep) >= pol["min_copies"], "至少保留 min_copies=%d 份" % pol["min_copies"])

    print("\n【T6】备份健康视图可查（可回答「到底有没有备份、还能不能用」）")
    with db() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM v_backup_health")
        h = cur.fetchone()
    check(h["ok_backups"] >= 2, "可用备份 %d 份" % h["ok_backups"])
    check(h["last_ok_at"] is not None, "最近成功时间：%s" % h["last_ok_at"])
    check(h["verified_restores"] >= 1, "已验证恢复 %d 次" % h["verified_restores"])

    # ---------------------------------------------------------------
    print("\n【清理】删除本次测试产生的备份")
    for bid in set(made):
        shutil.rmtree(os.path.join(bk.BACKUP_ROOT, bid), ignore_errors=True)
        with db() as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM restore_run WHERE backup_id=%s", (bid,))
                cur.execute("DELETE FROM backup_run WHERE backup_id=%s", (bid,))
            c.commit()
    print("    已删除测试备份：%s" % "、".join(sorted(set(made))))

    return H.report()


if __name__ == "__main__":
    sys.exit(main())
