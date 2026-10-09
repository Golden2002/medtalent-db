# -*- coding: utf-8 -*-
"""
ops/tests/mockreg_test.py —— 需求④：mock 小程序注册窗口的「真实加入 / 真实删除」

这一套与 `ops/tests/bridge_test.py` 的分工
--------------------------------------------------------------------------
bridge_test 测的是**适配器契约**（validate/幂等/版本门/墓碑/crosswalk，纯逻辑）；
本套测的是**从浏览器表单到库里那一行**的整条路 —— 包括网页层、证据面板、
三种删除档位，以及"删完之后有没有留孤儿"。

判据设计（每条都为了能在实现坏掉时失败）
--------------------------------------------------------------------------
1. **精确计数对照**：写前/写后 `count(*)` 逐表比，不看页面文案。
2. **独立连接复核**：复核用另一条连接（不是处理请求的那条）。
3. **孤儿行检测**：29 张外键子表 + 5 张**没有外键**的表
   （answer / experience_task / assertion / consent_record / field_value）——
   实测确认：直接 `DELETE FROM person` 会成功、这些行原样留下、且不报任何错。
4. **反面用例**（最重要）：
   · 不勾授权就提交 → 必须被授权门拒绝，且**一个人都不许写进去**
   · 撤回授权后再提交 → 必须被拒
   · 注销后再用同一编号提交 → 必须被墓碑拦下（否则"删除"会被迟到写入复活）
5. **幂等**：同一 eventId 重放不产生第二个人。
6. **自愈**：开场先清理本来源的历史残留，再取基线 ——
   否则上一次失败运行留下的数据会让"回到基线"永远失败（探针第一版就栽在这）。
7. **清理覆盖面 = 构造覆盖面**：清理后表计数回到基线且孤儿为 0。

用法：python ops/tests/mockreg_test.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))
sys.path.insert(0, os.path.join(BASE, "code", "demo"))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                        # noqa: E402
from psycopg.rows import dict_row                     # noqa: E402

import portal_dev as D                                # noqa: E402
import portal_mockreg as MR                           # noqa: E402
import exchange as EX                                 # noqa: E402
import _harness as H                                  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
check = H.check

PORT = 8099
ROOT = "http://127.0.0.1:%d" % PORT


def conn():
    return H.connect(MR.P.DSN)


def post(data):
    req = urllib.request.Request(ROOT + "/mockreg")
    req.data = urllib.parse.urlencode(data, doseq=True).encode()
    req.method = "POST"
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(path):
    try:
        with urllib.request.urlopen(ROOT + path, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    D.P.meta()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), D.DevHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    ext = "regmock_test_%d" % int(time.time())
    try:
        # ===========================================================
        print("\n【M1】窗口可访问，且开场自愈（清掉上次失败留下的残留）")
        st, body = get("/mockreg")
        check(st == 200 and "mock 小程序注册窗口" in body,
              "GET /mockreg 返回 200 且是注册窗口（实得 %d）" % st)
        check("bridge" in body and "不直" in body or "适配器" in body,
              "页面说明写路径走接入适配器、不直写 person")
        post({"act": "hardclean", "ext_id": ""})
        o = MR.orphan_check()
        check(not o["problems"],
              "自愈清理后无孤儿行（读了 %d 张外键子表）" % o["n_fk_children"])
        base = MR.snapshot()

        # ===========================================================
        print("\n【M2】反面用例：不勾授权就提交 → 授权门必须拦住，且一个人都不许写进去")
        st, body = post({"act": "submit", "ext_id": ext + "_no", "stage": "D3",
                         "degree": "D3", "major": "M10001", "consent_cp1": ""})
        a = MR.snapshot()
        check("接入适配器拒绝了这次写入" in body and "CONSENT_REQUIRED" in body,
              "被拒且是结果页（不是把表单页又渲染一遍）")
        check(a["person"] == base["person"],
              "**没有写入 person**（%d → %d）" % (base["person"], a["person"]))
        check(a["external_identity"] == base["external_identity"],
              "也没留下外部身份（不留半截数据）")

        # ===========================================================
        print("\n【M3】真实写入：勾上授权后提交，逐表精确计数")
        st, body = post({"act": "submit", "ext_id": ext, "stage": "D3", "degree": "D3",
                         "city": "CTY01", "major": "M10001",
                         "org": "某医科大学附属医院", "role": "住院医师",
                         "start_ym": "2024-09", "is_current": "1",
                         "skill_lit": "1", "skill_team": "1",
                         "occupation": "F02", "consent_cp1": "1"})
        a = MR.snapshot()
        check(a["person"] == base["person"] + 1,
              "person 精确 +1（%d → %d）" % (base["person"], a["person"]))
        check(a["external_identity"] == base["external_identity"] + 1, "external_identity +1")
        check(a["response_session"] >= base["response_session"] + 1, "答卷留痕 +1")
        check(a["experience_episode"] >= base["experience_episode"] + 1, "经历 +1")
        check(a["consent_record"] >= base["consent_record"] + 1, "授权记录 +1")
        check("L3 独立复核" in body and "孤儿行检测" in body,
              "页面给出 L3 独立复核与孤儿行检测（不是只说「成功」）")

        with conn() as c:
            pid = H.q1(c, """SELECT person_id FROM external_identity
                              WHERE source_system=%s AND external_person_id=%s""",
                       (MR.SRC, ext))
        check(bool(pid), "落库 person_id = %s" % pid)

        # 独立连接读回**规范化表**（bridge 写入的那一层）。
        # ⚠ 不能用 mt.profile_json：它读动态模型层（field_value），
        # 而 bridge 写的是规范化表 —— 实测对该人返回 NULL，会得出"没写进去"的假象。
        with conn() as c:
            rb = c.execute("""SELECT
                    (SELECT count(*) FROM mt.answer a JOIN mt.response_session s
                       ON s.session_id=a.session_id WHERE s.person_id=%s) AS n_ans,
                    (SELECT count(*) FROM mt.experience_episode WHERE person_id=%s) AS n_exp,
                    (SELECT count(*) FROM mt.consent_record WHERE person_id=%s
                       AND revoked_at IS NULL) AS n_cons,
                    (SELECT count(*) FROM mt.skill_assertion WHERE person_id=%s) AS n_skill""",
                (pid, pid, pid, pid)).fetchone()
        check(rb["n_ans"] > 0 and rb["n_exp"] > 0 and rb["n_cons"] == 1,
              "独立连接读回规范化表：答卷 %d / 经历 %d / 有效授权 %d / 自述技能 %d"
              % (rb["n_ans"], rb["n_exp"], rb["n_cons"], rb["n_skill"]))
        check("L2 读回" in body, "页面有 L2 读回块")

        # 专业必须是**选项码**：本库答卷只存码（answer 表没有文本列，
        # 且有 CHECK：status='answered' 必须有 option_ids）
        with conn() as c:
            codes = [r["option_ids"] for r in c.execute("""
                SELECT a.option_ids FROM mt.answer a JOIN mt.response_session s
                  ON s.session_id=a.session_id
                 WHERE s.person_id=%s AND a.question_id='education.major'""", (pid,))]
        check(codes and "M10001" in (codes[0] or []),
              "专业按选项码存（%s）—— 本库答卷只存码，不存自由文本" % (codes[0] if codes else None))

        # ===========================================================
        print("\n【M4】幂等：同一 eventId 重放不产生第二个人")
        with conn() as c:
            ev = H.q1(c, """SELECT external_event_id FROM sync_event
                             WHERE source_system=%s ORDER BY event_id DESC LIMIT 1""",
                      (MR.SRC,))
        n0 = MR.snapshot()["person"]
        pkg = EX.example_package()
        pkg.update({"sourceSystem": MR.SRC, "personId": ext, "aggregateVersion": 2,
                    "eventId": ev, "submissionId": "sub_replay", "requestId": "req_replay",
                    "consents": {"noticeVersion": "1.0.0", "personalAnalysis": True}})
        try:
            r = EX.ingest(pkg)
            idem = bool(r["data"].get("idempotent"))
        except EX.ExchangeError as e:
            idem = (e.code == "CONFLICT")
        check(idem and MR.snapshot()["person"] == n0,
              "重放同一 eventId：适配器判定幂等且人数不变（%d）" % n0)

        # ===========================================================
        print("\n【M5】撤回授权（D-A）后用同一编号再提交 → 必须被拒")
        st, body = post({"act": "revoke", "ext_id": ext})
        check("撤回" in body, "撤回动作返回撤回确认")
        with conn() as c:
            n_rev = H.q1(c, """SELECT count(*) FROM consent_record
                                WHERE person_id=%s AND purpose='CP1'
                                  AND revoked_at IS NOT NULL""", (pid,))
        check(n_rev >= 1, "库里有撤回记录（append-only：新写一条，不是改旧的）")
        st, body = post({"act": "submit", "ext_id": ext, "stage": "D4", "degree": "D4",
                         "major": "M10001", "consent_cp1": "1", "version": "9"})
        check("CONSENT_REQUIRED" in body or "已撤回" in body,
              "撤回后再提交被拒（撤回优先于包内声明）")

        # ===========================================================
        print("\n【M6】注销（D-B）：异步受理，且墓碑拦住迟到写入")
        st, body = post({"act": "deregister", "ext_id": ext})
        check("受理" in body and "完成" in body,
              "页面区分「受理」与「完成」（不把受理说成已完成）")
        with conn() as c:
            tb = c.execute("""SELECT deleted_through_version AS v FROM tombstone
                               WHERE person_id=%s AND source_system=%s""",
                           (pid, MR.SRC)).fetchone()
            dr = c.execute("""SELECT status, completed_at FROM talent_deletion_request
                               WHERE person_id=%s ORDER BY requested_at DESC LIMIT 1""",
                           (pid,)).fetchone()
        check(tb is not None, "写了墓碑（deleted_through_version=%s）" % (tb or {}).get("v"))
        check(dr and dr["status"] == "pending" and dr["completed_at"] is None,
              "注销请求 pending 且 completed_at=NULL（受理 ≠ 完成）")
        st, body = post({"act": "submit", "ext_id": ext, "stage": "D5", "degree": "D5",
                         "major": "M10001", "consent_cp1": "1", "version": "20"})
        check("PROFILE_DELETING" in body or "墓碑" in body or "已删除" in body,
              "注销后迟到写入被墓碑拦下（否则删除会被小程序重试复活）")

        # ===========================================================
        print("\n【M7】硬清理（D-C）：清理覆盖面必须等于构造覆盖面")
        st, body = post({"act": "hardclean", "ext_id": ext})
        a = MR.snapshot()
        o = MR.orphan_check()
        check("干净" in body, "清理后的页面显示孤儿检测结果")
        check(not o["problems"], "清理后**没有孤儿行**（否则就是假删）")
        for t in ("person", "external_identity", "tombstone", "talent_deletion_request",
                  "response_session", "experience_episode", "consent_record"):
            check(a[t] == base[t], "%s 回到基线（%d）" % (t, base[t]))
        with conn() as c:
            nfv = H.q1(c, """SELECT count(*) FROM field_value fv
                              WHERE fv.subject_id LIKE 'per_%%'
                                AND NOT EXISTS (SELECT 1 FROM person p
                                                 WHERE p.person_id = fv.subject_id)""")
        check(nfv == 0, "field_value 没有悬空 subject_id（该列没有外键，必须单独清）")

        # 越界防护：不能清理别的来源
        st, body = post({"act": "hardclean", "ext_id": "per_mock_0001"})
        check("拒绝" in body, "硬清理拒绝非本来源前缀（防止误删别人的数据）")
        with conn() as c:
            alive = H.q1(c, "SELECT count(*) FROM person WHERE person_id='per_mock_0001'")
        check(alive == 1, "被拒之后那条真实数据仍在（拒绝是有效的，不只是文案）")

    finally:
        srv.shutdown()
        srv.server_close()
        # teardown 覆盖面 = 构造覆盖面：本条同样适用于测试自己
        try:
            post({"act": "hardclean", "ext_id": ""})
        except Exception:                                 # noqa: BLE001
            pass

    return H.report(width=74, list_fails=True)


if __name__ == "__main__":
    sys.exit(main())
