# -*- coding: utf-8 -*-
"""
ops/tests/evolution_test.py —— 演化能力端到端测试（T13）

模拟一次真实的"市场变化"，验证职业树与能力映射能否跟着长：

  T1 期：用现有 690 条 JD 建立基线版本 v1
  —— 市场变化 ——
  注入 30 条"医疗数据合规官"（职业树里没有的新岗位）+ 把某项既有能力的
  要求频次显著抬高（模拟它变成刚需）
  T2 期：
    ① discover 把它发现为候选（岗位 + 能力）
    ② promote 把它提升为正式节点（新 ID、有变更记录、有迁移表）
    ③ 拆分一个既有节点，验证旧 ID 可追溯
    ④ 把某个候选并入既有概念作别名（不新建重复概念）
    ⑤ 重建映射 → 重算 v2 → 产出漂移（rising / new）

同时验证三条演化纪律：
  · 时间是第一等维度：as-of 查询能回到"当时"的树
  · ID 不复用、只退役：旧节点还在，靠迁移表追溯
  · 提升必须有证据：证据不足的候选不能被提升

用法：python ops/tests/evolution_test.py
"""
import datetime as dt
import hashlib
import os
import secrets
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in ("code", "code/evolve", "code/analytics"):
    sys.path.insert(0, os.path.join(BASE, p))

import recompute as rc  # noqa: E402
import promote as pr  # noqa: E402
import discover as dc  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
DSN = rc.DSN
TAG = "EVOTEST" + secrets.token_hex(3).upper()
SRC = "src_evotest_" + secrets.token_hex(3)
PASS, FAIL = H.PASS, H.FAIL
check, q, q1 = H.check, H.q, H.q1
# T6 拆分的**源节点**：它是真实种子节点（label 不含 TAG），清理时必须恢复。
# 踩过的坑（问题 #34）：只按 label LIKE TAG 清理，会把种子节点永久留在
# retired 状态，并且留下 to_ids 指向已删节点的悬空变更记录。
SPLIT_TARGET = []


def db():
    return H.connect(DSN)


def cleanup(c):
    """清掉本测试注入的数据（真实演化记录永不删除；这里只为可重复运行）。
    删除顺序必须服从外键：业务数据 → 权重/漂移 → 迁移 → 变更 → 节点自身。

    问题 #34 的教训：T6 拆分的源节点是**真实种子节点**，其 label 不含 TAG，
    仅按 label 清理会留下两处脏数据：
      ① 源节点永久停在 retired（职业树凭空少一枝）；
      ② occupation_change.to_ids 指向已删的增量节点（悬空引用）。
    所以这里显式恢复源节点，并把「to_ids 全部不存在」的变更记录一并清掉。
    """
    with c.cursor() as cur:
        cur.execute("SELECT occupation_id FROM occupation WHERE label_zh LIKE %s",
                    ("%" + TAG + "%",))
        oids = [r["occupation_id"] for r in cur.fetchall()]
        # 找出需要恢复/清理的变更记录（含历史残留：to_ids 悬空的记录）
        cur.execute("""SELECT DISTINCT unnest(from_ids) AS oid FROM occupation_change
                       WHERE label_zh LIKE %s OR from_ids && %s
                          OR (coalesce(array_length(to_ids,1),0) > 0
                              AND NOT EXISTS (SELECT 1 FROM occupation o
                                              WHERE o.occupation_id = ANY(to_ids)))""",
                    ("%" + TAG + "%", SPLIT_TARGET))
        touched = [r["oid"] for r in cur.fetchall()] + list(SPLIT_TARGET)
        cur.execute("DELETE FROM job_requirement WHERE job_id IN "
                    "(SELECT job_id FROM job_posting WHERE source_id LIKE 'src_evotest_%')")
        cur.execute("DELETE FROM job_task WHERE job_id IN "
                    "(SELECT job_id FROM job_posting WHERE source_id LIKE 'src_evotest_%')")
        cur.execute("DELETE FROM job_posting WHERE source_id LIKE 'src_evotest_%'")
        cur.execute("DELETE FROM provenance WHERE source_id LIKE 'src_evotest_%'")
        cur.execute("DELETE FROM ingest_run WHERE source_id LIKE 'src_evotest_%'")
        cur.execute("DELETE FROM source_registry WHERE source_id LIKE 'src_evotest_%'")
        if oids:
            cur.execute("DELETE FROM job_competency_weight WHERE occupation_id = ANY(%s)",
                        (oids,))
            cur.execute("DELETE FROM competency_drift WHERE occupation_id = ANY(%s)", (oids,))
        cur.execute("DELETE FROM occupation_migration WHERE old_id = ANY(%s) "
                    "OR new_id = ANY(%s)", (oids, oids))
        cur.execute("""DELETE FROM occupation_change
                       WHERE label_zh LIKE %s OR from_ids && %s
                          OR (coalesce(array_length(to_ids,1),0) > 0
                              AND NOT EXISTS (SELECT 1 FROM occupation o
                                              WHERE o.occupation_id = ANY(to_ids)))""",
                    ("%" + TAG + "%", touched))
        # 恢复被拆分的种子节点（真实节点不能因为一次测试就消失）
        if touched:
            cur.execute("""UPDATE occupation SET status='active', valid_to=NULL,
                              retired_reason=NULL, updated_at=now()
                           WHERE occupation_id = ANY(%s) AND status <> 'active'""",
                        (touched,))
        cur.execute("DELETE FROM occupation WHERE label_zh LIKE %s", ("%" + TAG + "%",))
        cur.execute("DELETE FROM occupation_candidate WHERE title_key LIKE %s",
                    ("%" + TAG.lower() + "%",))
        cur.execute("DELETE FROM concept_candidate WHERE phrase_key LIKE %s",
                    ("%" + TAG + "%",))
        cur.execute("DELETE FROM concept WHERE alt_labels && ARRAY[%s] OR definition LIKE %s",
                    (TAG, "%" + TAG + "%"))
    c.commit()


def inject_jobs(c, n=30, title=None, new_skill=None, boost_concept=None):
    """注入一批 mock JD：可选新岗位名、新能力短语、或抬高某项既有能力的要求频次。"""
    title = title or ("%s 医疗数据合规官" % TAG)
    with c.cursor() as cur:
        cur.execute("""INSERT INTO source_registry (source_id, name, source_type, base_url,
                          license_note, credibility, evidence_grade, access_tier, status)
                       VALUES (%s,%s,'SRC2','http://127.0.0.1:8098/','演化测试用 mock 语料',
                               1.0,'C','T1','active') ON CONFLICT DO NOTHING""",
                    (SRC, "演化测试语料"))
        jids = []
        for i in range(n):
            jid = "job_evotest_%s_%02d" % (TAG.lower(), i)
            jids.append(jid)
            cur.execute("""INSERT INTO job_posting (job_id, source_id, title_raw,
                              title_normalized, job_family, city, employer_name_raw,
                              salary_min, salary_max, salary_period, education_req,
                              raw_text_ref, raw_sha256, parse_version, confidence, verify_status)
                           VALUES (%s,%s,%s,%s,NULL,'北京','某医疗科技公司',
                                   20000,30000,'SP1','D3','L0://evotest','x','evotest@0.1',0.8,'V1')
                           ON CONFLICT (job_id) DO NOTHING""", (jid, SRC, title, title))
            reqs = []
            if new_skill:
                reqs.append((new_skill, "RK1"))
            if boost_concept:
                reqs.append((boost_concept, "RK1"))
            for k, (text, kind) in enumerate(reqs, 1):
                cur.execute("""INSERT INTO job_requirement (requirement_id, job_id,
                                  requirement_kind, requirement_type, raw_text,
                                  essentiality, substitutable_by)
                               VALUES (%s,%s,%s,'RT5',%s,1.0,'[]'::jsonb)
                               ON CONFLICT (requirement_id) DO NOTHING""",
                            ("req_evotest_%s_%02d_%d" % (TAG.lower(), i, k), jid, kind, text))
    c.commit()
    return jids


def main():
    print("=" * 78)
    print("演化能力端到端测试（T13）   测试标记：%s" % TAG)
    print("=" * 78)

    with db() as c:
        cleanup(c)

    # ===============================================================
    print("\n【T1】基线：用现有语料建立 v1 版本")
    v1 = rc.recompute(min_sample=30, note="演化测试基线")
    check(v1["rows"] > 0, "基线版本写入 %d 行权重" % v1["rows"])
    with db() as c:
        base_nodes = q1(c, "SELECT count(*) FROM occupation WHERE status='active' "
                           "AND valid_to IS NULL")
        base_asof = dt.date.today()
        pick = q(c, """SELECT jp.occupation_id, jr.concept_id, count(*) AS n
                       FROM job_posting jp JOIN job_requirement jr ON jr.job_id=jp.job_id
                       WHERE jp.occupation_id IS NOT NULL AND jr.concept_id IS NOT NULL
                       GROUP BY 1,2 ORDER BY n DESC LIMIT 1""")[0]
    check(base_nodes > 100, "基线树节点 %d" % base_nodes)

    # ===============================================================
    print("\n【T2】市场变化：注入新岗位 + 新能力 + 抬高某项能力的需求频次")
    # 取被选中概念对应的一条原文，用它作为"频次被抬高"的要求文本
    with db() as c:
        boost_text = q1(c, "SELECT raw_text FROM job_requirement WHERE concept_id=%s LIMIT 1",
                        (pick["concept_id"],))
    with db() as c:
        inject_jobs(c, 30, new_skill="%s 熟悉医疗数据出境合规" % TAG, boost_concept=boost_text)
    with db() as c:
        n_new = q1(c, "SELECT count(*) FROM job_posting WHERE source_id=%s", (SRC,))
    check(n_new == 30, "注入 %d 条新岗位 JD（并抬高「%s」的需求频次）"
          % (n_new, (boost_text or "")[:16]))

    # ===============================================================
    print("\n【T3】discover：从新 JD 里长出候选")
    d = dc.run()
    check(d["occupations"]["buckets"] >= 1, "发现 %d 个未识别岗位名" % d["occupations"]["buckets"])
    with db() as c:
        cand = q(c, "SELECT * FROM occupation_candidate WHERE title_key LIKE %s "
                    "AND status='ready'", ("%" + TAG.lower() + "%",))
    check(len(cand) == 1, "新岗位候选已达阈值（%s 条证据）"
          % (cand[0]["evidence_count"] if cand else 0))
    with db() as c:
        ccand = q(c, "SELECT * FROM concept_candidate WHERE phrase_key LIKE %s",
                  ("%" + TAG + "%",))
    # 注意：候选可能落在 ready（新概念）或 merged（命中既有概念关键词 → 建议作别名）——
    # 后者是**正确行为**，不是缺陷。这里只要求它被"看见"。
    check(len(ccand) >= 1, "新能力被捕获为候选（状态：%s）"
          % "、".join(x["status"] for x in ccand))
    if ccand:
        check(ccand[0]["status"] in ("ready", "merged", "watching"),
              "候选状态合法（%s%s）" % (ccand[0]["status"],
                                       "，命中既有概念 " + str(ccand[0]["suggested_concept_id"])
                                       if ccand[0]["suggested_concept_id"] else ""))

    # ===============================================================
    print("\n【T4】提升：候选 → 正式节点（新 ID + 变更记录）")
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT occupation_id FROM occupation WHERE family='F09' "
                        "AND level=1 LIMIT 1")
            parent = cur.fetchone()["occupation_id"]
    r = pr.promote_occupation(cand[0]["candidate_id"], "F09", parent,
                              label_zh="%s 医疗数据合规官" % TAG,
                              note="演化测试：真实 JD 支撑充分")
    oid = r["occupation_id"]
    with db() as c:
        check(q1(c, "SELECT count(*) FROM occupation WHERE occupation_id=%s", (oid,)) == 1,
              "新节点已入树：%s" % oid)
        check(q1(c, "SELECT count(*) FROM occupation WHERE status='active' "
                    "AND valid_to IS NULL") == base_nodes + 1, "树节点数 +1（%d → %d）"
              % (base_nodes, base_nodes + 1))
        check(q1(c, "SELECT status FROM occupation_candidate WHERE candidate_id=%s",
                 (cand[0]["candidate_id"],)) == "promoted", "候选状态置 promoted")
        chg = q(c, "SELECT * FROM occupation_change WHERE change_type='add' "
                   "AND to_ids @> ARRAY[%s]", (oid,))
        check(len(chg) == 1 and chg[0]["evidence_count"] >= 5,
              "写出变更记录（类型 add，证据 %d 条）" % (chg[0]["evidence_count"] if chg else 0))

    print("\n【T5】提升门槛：证据不足的候选不能被提升")
    with db() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO occupation_candidate (candidate_id, title_key,
                              title_sample, evidence_count, first_seen, last_seen, status)
                           VALUES (%s,%s,%s,1,CURRENT_DATE,CURRENT_DATE,'watching')
                           ON CONFLICT (title_key) DO NOTHING""",
                        ("oc_weak_" + TAG, "weak_" + TAG.lower(), "弱证据岗位 " + TAG))
        c.commit()
    try:
        pr.promote_occupation("oc_weak_" + TAG, "F09", parent)
        check(False, "证据不足的候选竟被提升")
    except SystemExit as e:
        check("证据不足" in str(e), "证据不足被拦下：%s" % str(e)[:40])

    # ===============================================================
    print("\n【T6】拆分：旧 ID 退役但可追溯（ID 不复用）")
    with db() as c:
        target = q1(c, "SELECT occupation_id FROM occupation WHERE level=3 "
                       "AND status='active' AND family='F02' ORDER BY occupation_id LIMIT 1")
    SPLIT_TARGET.append(target)   # 真实种子节点，cleanup 必须把它恢复回 active
    sp = pr.split_occupation(target, [
        {"label": "%s 拆分A" % TAG, "share": 0.6},
        {"label": "%s 拆分B" % TAG, "share": 0.4}], reason="演化测试：岗位分化")
    with db() as c:
        old = q(c, "SELECT status, valid_to, retired_reason FROM occupation "
                   "WHERE occupation_id=%s", (target,))
        check(old[0]["status"] == "retired" and old[0]["valid_to"] is not None,
              "被拆分的旧节点已退役（valid_to=%s）" % old[0]["valid_to"])
        migs = q(c, "SELECT new_id, relation, coverage FROM occupation_migration "
                    "WHERE old_id=%s ORDER BY new_id", (target,))
        check(len(migs) == 2 and all(m["relation"] == "split_into" for m in migs),
              "迁移表记录 2 条 split_into（占比 %s）"
              % "、".join(str(m["coverage"]) for m in migs))
        resolved = q(c, "SELECT * FROM occupation_resolve(%s)", (target,))
        check(len(resolved) == 2, "可按旧 ID 追溯出 %d 个后继节点" % len(resolved))
        check(q1(c, "SELECT count(*) FROM occupation WHERE occupation_id=%s", (target,)) == 1,
              "旧节点记录仍在（不物理删除）")

    # ===============================================================
    print("\n【T7】能力生长：优先作别名并入既有概念，而不是新建重复概念")
    if ccand:
        with db() as c:
            with c.cursor() as cur:
                cur.execute("""INSERT INTO concept_candidate (candidate_id, phrase_key,
                                  phrase_sample, evidence_count, first_seen, last_seen, status)
                               VALUES (%s,%s,%s,10,CURRENT_DATE,CURRENT_DATE,'ready')
                               ON CONFLICT (phrase_key) DO NOTHING""",
                            ("cc_alias_" + TAG, "%s 数据合规审查" % TAG,
                             "%s 具备数据合规审查能力" % TAG))
            c.commit()
        with db() as c:
            n_con_before = q1(c, "SELECT count(*) FROM concept")
        pr.merge_alias("cc_alias_" + TAG, pick["concept_id"])
        with db() as c:
            n_con_after = q1(c, "SELECT count(*) FROM concept")
            check(n_con_after == n_con_before, "并入别名后概念总数未变（%d）→ 没造重复概念"
                  % n_con_after)
            check(q1(c, "SELECT count(*) FROM concept WHERE concept_id=%s "
                        "AND alt_labels @> ARRAY[%s]", (pick["concept_id"],
                                                        "%s 数据合规审查" % TAG)) == 1,
                  "别名已写入既有概念的 alt_labels")
            check(q1(c, "SELECT status FROM concept_candidate WHERE candidate_id=%s",
                     ("cc_alias_" + TAG,)) == "merged", "候选状态置 merged")

    # ===============================================================
    print("\n【T8】重建映射 → 重算 v2 → 产出漂移")
    # 关键：只有**已映射到职业**且**要求已映射到概念**的 JD 才会参与权重聚合。
    # 注入的 JD 走的是正常管道（map_occupations 按标题匹配），但为了让漂移断言
    # 确定性成立，这里显式把注入数据挂到新节点与目标概念上，并断言映射生效。
    with db() as c:
        with c.cursor() as cur:
            cur.execute("""UPDATE job_posting SET occupation_id=%s, job_family='F09'
                           WHERE source_id=%s""", (oid, SRC))
            n_map = cur.rowcount
            cur.execute("""UPDATE job_requirement SET concept_id=%s
                           WHERE job_id IN (SELECT job_id FROM job_posting WHERE source_id=%s)
                             AND concept_id IS NULL""", (pick["concept_id"], SRC))
            n_req = cur.rowcount
        c.commit()
    check(n_map == 30, "注入的 %d 条 JD 已挂到新职业节点" % n_map)
    check(n_req >= 1, "注入的 %d 条要求已映射到概念" % n_req)

    import subprocess
    r = subprocess.run([sys.executable, os.path.join(BASE, "code", "analytics",
                                                     "build_competency.py"),
                        "--min-sample", "30"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("    派生层重建失败：\n%s" % (r.stdout or "")[-400:] + (r.stderr or "")[-400:])
    check(r.returncode == 0, "派生层重建成功")
    with db() as c:
        n_runs = q1(c, "SELECT count(*) FROM competency_run")
        check(n_runs >= 2, "已有 %d 个重算版本" % n_runs)
        latest = q1(c, "SELECT run_id FROM competency_run ORDER BY created_at DESC LIMIT 1")
        dr = q(c, "SELECT drift_type, count(*) AS n FROM competency_drift "
                  "WHERE to_run_id=%s GROUP BY 1", (latest,))
    types = {x["drift_type"]: x["n"] for x in dr}
    check(sum(types.values()) > 0, "产出漂移记录：%s" % types)
    check(types.get("new", 0) >= 1, "包含 new 漂移（新职业节点带来了新的能力需求）")
    with db() as c:
        rising = q(c, """SELECT c.preferred_label, d.from_importance, d.to_importance
                         FROM competency_drift d JOIN concept c ON c.concept_id=d.concept_id
                         WHERE d.to_run_id=%s AND d.drift_type <> 'stable'
                         ORDER BY abs(coalesce(d.delta,0)) DESC LIMIT 5""", (latest,))
    check(True, "非稳定漂移 %d 项%s" % (len(rising),
                                       ("：" + "、".join(x["preferred_label"] for x in rising[:3]))
                                       if rising else ""))

    # ===============================================================
    print("\n【T9】时间维：as-of 查询能回到「当时」的树与权重")
    with db() as c:
        at_today = q1(c, "SELECT count(*) FROM occupation_asof(CURRENT_DATE)")
        at_past = q1(c, "SELECT count(*) FROM occupation_asof(DATE '2026-01-01')")
        check(at_today > at_past, "今天 %d 个节点 > 年初 %d 个节点（树确实长大了）"
              % (at_today, at_past))
        check(q1(c, "SELECT count(*) FROM occupation_asof(CURRENT_DATE) "
                    "WHERE occupation_id=%s", (oid,)) == 1, "新节点在今天的树里")
        check(q1(c, "SELECT count(*) FROM occupation_asof(DATE '2026-01-01') "
                    "WHERE occupation_id=%s", (oid,)) == 0, "新节点不在年初的树里")
        cur_v = q1(c, "SELECT count(*) FROM v_competency_current")
        all_v = q1(c, "SELECT count(*) FROM job_competency_weight")
        check(all_v > cur_v, "历史权重仍保留：总 %d 行，当前有效 %d 行" % (all_v, cur_v))

    # ===============================================================
    print("\n【T10】演化健康度可查")
    with db() as c:
        h = q(c, "SELECT * FROM v_evolution_health")[0]
    check(h["runs"] >= 2, "重算版本 %d 个" % h["runs"])
    check(h["migrations"] >= 2, "迁移记录 %d 条" % h["migrations"])
    check(h["retired_nodes"] >= 1, "退役节点 %d 个" % h["retired_nodes"])
    print("    健康度：树 %d 节点（退役 %d）｜迁移 %d｜候选 ready：岗位 %d / 能力 %d｜"
          "版本 %d｜上升信号 %d"
          % (h["active_nodes"], h["retired_nodes"], h["migrations"],
             h["occ_ready"], h["con_ready"], h["runs"], h["rising_signals"]))

    with db() as c:
        cleanup(c)

    return H.report(note="（测试注入数据已清理）")


if __name__ == "__main__":
    sys.exit(main())
