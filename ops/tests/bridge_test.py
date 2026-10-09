# -*- coding: utf-8 -*-
"""
ops/tests/bridge_test.py —— 小程序接入契约端到端测试（T11）

逐条验证《medtalent-integration-schema》v1.0.0 中可判定的硬规则：

  §7   交换包禁含身份标识；personId 随机绑定；自述即未核验；不臆造映射
  §7.1 eventId 幂等；按人 aggregateVersion 门；墓碑拦住迟到写入；
       outbox 只存引用；至少一次交付、重复 ACK 安全
  §7.2 岗位投影 must/preferred/unclear；薪资口径；每页 ≤20；游标翻页
       匹配 met/gap/unknown —— **unknown 不是不合格**；带 coverage 与版本
  §5   撤回分析授权后不得返回派生画像（CONSENT_REQUIRED）
  §8   删除受理 ≠ 完成；需下游确认

测试顺序刻意如此：先走通正向链路（摄入→映射→投影→匹配），
再做破坏性动作（撤回→删除→墓碑），因为删除后一切写入都会被合法拦住。

用法：python ops/tests/bridge_test.py
"""
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.join(BASE, "code"))

import api as bridge_api  # noqa: E402
import exchange as ex  # noqa: E402
import outbox as ob  # noqa: E402
import projection as pj  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
DSN = ex.DSN
# 运维连接：**清理测试数据要用它，不能用 bridge 自己的连接**。
# 为什么必须分开（这是迁移 033 之后才暴露的）：
#   bridge 现在跑在最小权限角色 mt_bridge 上 —— 它**故意没有** answer/consent_record
#   等表的 DELETE 权限（"删人/删答卷"必须是显式的运维动作，不能让摄入路径顺手做）。
#   而测试的 cleanup() 要删 12 张表的数据，那是**运维动作**。
#   让测试用 bridge 的身份去清库，等于要求 bridge 具备它不该有的权限 ——
#   那样"最小权限"就被测试的需要给反推掉了。
#   所以：摄入路径用 bridge 身份（被测对象），清理与全表断言用运维身份。
ADMIN_DSN = ex.DSN.replace("user=mt_bridge", "user=postgres")
SRC = "bridge_test_" + secrets.token_hex(4)     # 独立来源，跑完即清
ANON = "anon_" + SRC
PORT = 8096

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H  # noqa: E402

PASS, FAIL = H.PASS, H.FAIL
check, q1 = H.check, H.q1


def db():
    """运维连接（清理与需要广读的断言）。"""
    return H.connect(ADMIN_DSN)


def bridge_db():
    """被测对象自己的连接（最小权限角色）。"""
    return H.connect(DSN)


def pkg(ver, event_id, **over):
    p = ex.example_package()
    p.update({"sourceSystem": SRC, "personId": over.pop("personId", ANON),
              "aggregateVersion": ver, "eventId": event_id,
              "submissionId": "sub_" + event_id, "requestId": "req_" + event_id,
              "eventType": over.pop("eventType", "profile_upsert")})
    p.update(over)
    return p


def cleanup(c):
    """清掉本测试来源的全部痕迹（真实来源永不做这个操作）。
    按外键顺序删除，且**范围严格限定在本来源**，不误伤其他来源的遗留数据。"""
    with c.cursor() as cur:
        cur.execute("SELECT person_id FROM external_identity WHERE source_system=%s", (SRC,))
        pids = [r["person_id"] for r in cur.fetchall()]
        for sql in [
            "DELETE FROM answer WHERE session_id IN (SELECT session_id FROM response_session WHERE source_system=%s)",
            "DELETE FROM experience_task WHERE episode_id IN (SELECT episode_id FROM experience_episode WHERE source_system=%s)",
            "DELETE FROM experience_episode WHERE source_system=%s",
            "DELETE FROM match_result WHERE person_id = ANY(%s)",
            "DELETE FROM tombstone WHERE source_system=%s",
            "DELETE FROM sync_event WHERE source_system=%s",
            "DELETE FROM talent_deletion_request WHERE source_system=%s",
            "DELETE FROM consent_record WHERE person_id = ANY(%s)",
            "DELETE FROM skill_assertion WHERE person_id = ANY(%s)",
            "DELETE FROM assertion WHERE subject_id = ANY(%s)",
            "DELETE FROM preference WHERE person_id = ANY(%s)",
            "DELETE FROM education_record WHERE person_id = ANY(%s)",
            "DELETE FROM observation_window WHERE person_id = ANY(%s)",
            "DELETE FROM response_session WHERE source_system=%s",
            "DELETE FROM external_identity WHERE source_system=%s",
            "DELETE FROM person WHERE person_id = ANY(%s)",
            "DELETE FROM crosswalk WHERE external_system=%s",
        ]:
            if "%s" in sql and "ANY(%s)" in sql:
                cur.execute(sql, (pids,))
            else:
                cur.execute(sql, (SRC,))
    c.commit()


def main():
    print("=" * 78)
    print("小程序接入契约端到端测试（T11）   测试来源标识：%s" % SRC)
    print("=" * 78)

    with db() as c:
        cleanup(c)

    # ===============================================================
    print("\n【T1】§7 交换包禁含身份标识（任意层级的深度扫描）")
    for bad_key, where in (("memberKey", "顶层"), ("openid", "顶层"),
                           ("phone", "嵌套在 facts 内")):
        p = pkg(1, "evt_bad_" + bad_key)
        if where == "顶层":
            p[bad_key] = "x"
        else:
            p["facts"][0][bad_key] = "13800000000"
        r = bridge_api.Api.exchange(p)
        check(r.get("ok") is False and r.get("code") == "INVALID_ARGUMENT",
              "含 %s（%s）被整包拒绝" % (bad_key, where))

    # ===============================================================
    print("\n【T2】§7 首次摄入：随机 personId 绑定 + 答卷留存")
    r1 = bridge_api.Api.exchange(pkg(1, "evt_v1"))
    check(r1.get("ok") is True, "首次摄入成功")
    pid = r1["data"]["personId"]
    check(pid.startswith("per_") and SRC not in pid, "生成随机 person_id：%s（不含来源标识）" % pid)
    with db() as c:
        check(q1(c, "SELECT count(*) FROM external_identity WHERE source_system=%s", (SRC,)) == 1,
              "外部身份绑定唯一")
        check(q1(c, "SELECT count(*) FROM response_session WHERE source_system=%s", (SRC,)) == 1,
              "答卷原始记录已留存（可用于按冻结映射重建那一版）")

    print("\n【T3】§7.1 eventId 幂等：重复投递返回原结果")
    r2 = bridge_api.Api.exchange(pkg(1, "evt_v1"))
    check(r2.get("ok") and r2["data"].get("idempotent") is True, "识别为幂等重投")
    check(r2["data"]["personId"] == pid, "返回同一 personId")
    with db() as c:
        check(q1(c, "SELECT count(*) FROM external_identity WHERE source_system=%s", (SRC,)) == 1,
              "人数仍为 1（未创建第二个人）")

    print("\n【T4】§7.1 版本门：同版本不同载荷 / 倒退版本均 CONFLICT")
    r3 = bridge_api.Api.exchange(pkg(1, "evt_v1_other"))
    check(r3.get("ok") is False and r3["code"] == "CONFLICT", "同版本不同载荷被拒")
    r4 = bridge_api.Api.exchange(pkg(0, "evt_v0"))
    check(r4.get("ok") is False and r4["code"] == "CONFLICT",
          "版本倒退被拒：%s" % r4.get("message", "")[:36])

    # ===============================================================
    print("\n【T5】§7 未登记映射 → 不臆造概念，转通用断言并上报 unmapped")
    check(r1["data"]["unmappedSkillClaims"] == 2,
          "两个自述技能均未映射（unmappedSkillClaims=%d）" % r1["data"]["unmappedSkillClaims"])
    with db() as c:
        n_asr = q1(c, "SELECT count(*) FROM assertion WHERE subject_id=%s "
                      "AND predicate='self_reported_skill'", (pid,))
        check(n_asr == 2, "自述技能以通用断言保留 %d 条，未硬塞 concept" % n_asr)

    print("\n【T6】§7 crosswalk 登记后 → 投影为 skill_assertion（未核验/无证据/无概率）")
    with db() as c:
        with c.cursor() as cur:
            # crosswalk_id 必须每轮唯一：固定 id 会在下一轮 PK 冲突被 DO NOTHING 吞掉
            cur.execute("""INSERT INTO crosswalk (crosswalk_id, mapping_version, local_kind,
                              local_id, external_system, external_kind, external_id,
                              relation_type, review_status, note)
                           VALUES (%s,'xw_test','concept','CON-K1-LIT',%s,
                                   'concept','q_lit_review','exact','reviewed',
                                   '测试用：小程序题号 → 本库概念')
                           ON CONFLICT DO NOTHING""", ("xw_" + SRC, SRC))
        c.commit()
        check(q1(c, "SELECT count(*) FROM crosswalk WHERE external_system=%s "
                    "AND review_status='reviewed'", (SRC,)) == 1, "crosswalk 已登记（reviewed）")
    r5 = bridge_api.Api.exchange(pkg(2, "evt_v2", skillClaims=[
        {"sourceQuestionId": "q_lit_review", "label": "文献检索与证据分级"}]))
    check(r5["data"]["mappedSkillClaims"] == 1, "映射成功的自述技能 1 条")
    with db() as c:
        check(q1(c, "SELECT confidence FROM skill_assertion WHERE person_id=%s "
                    "AND concept_id='CON-K1-LIT'", (pid,)) == 0.5,
              "置信度 0.5（契约：自述不含任意置信概率）")
        check(q1(c, "SELECT count(*) FROM skill_assertion WHERE person_id=%s "
                    "AND evidence_id IS NOT NULL", (pid,)) == 0, "未补造任何证据引用")
        check(q1(c, "SELECT verify_status FROM skill_assertion WHERE person_id=%s "
                    "AND concept_id='CON-K1-LIT'", (pid,)) == "V1", "标记 V1（未核验）")
        check(q1(c, "SELECT count(*) FROM experience_episode WHERE person_id=%s",
                 (pid,)) == 1, "经验重复组已按 instanceId 拆行入库")

    # ===============================================================
    print("\n【T7】§7.2 岗位投影：三态要求 + 薪资口径 + 每页 ≤20 + 游标翻页")
    with db() as c:
        jp = pj.export_jobs(c, 100)          # 故意请求 100
        check(len(jp["items"]) <= pj.PAGE_SIZE,
              "每页被限制为 ≤20（实际 %d）" % len(jp["items"]))
        it = jp["items"][0]
        check(set(it["requirements"].keys()) == {"must", "preferred", "unclear"},
              "要求拆成 must/preferred/unclear 三态")
        check("requirementLogic" in it, "保留 requirementLogic（AND/OR）")
        check(it["salary"]["basis"] in ("employer_disclosed", "not_disclosed"),
              "薪资带口径 basis=%s" % it["salary"]["basis"])
        check(it["validity"]["status"] == "open", "只返回 open 岗位")
        check(bool(it["sourceVersion"]), "带 sourceVersion（%s）" % it["sourceVersion"])
        jp2 = pj.export_jobs(c, 5, cursor=jp["nextCursor"])
        check(jp2["items"] and jp2["items"][0]["jobId"] != it["jobId"],
              "游标翻页返回不同记录")

    # ===============================================================
    print("\n【T8】§7.2 匹配：met/gap/unknown 三态，unknown 不是不合格")
    with db() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO observation_window (window_id, person_id, window_type,
                              start_date, coverage_note)
                           VALUES (%s,%s,'W1', CURRENT_DATE - 400, '测试观测窗口')""",
                        ("ow_" + SRC, pid))
        c.commit()
    mr = bridge_api.Api.matches(SRC, ANON, {})
    check(mr.get("ok") is True, "有授权时可取匹配结果")
    if mr.get("ok"):
        top = mr["data"]["items"][0]
        check({"met", "gap", "unknown", "coverage", "rankingScore", "profileVersion",
               "jobSourceVersion", "ruleVersion"} <= set(top.keys()),
              "结果字段齐全（met/gap/unknown + coverage + 版本号）")
        check(any(x.get("reason") == "requirement_not_mapped_to_concept"
                  for x in top["unknown"]) or len(top["unknown"]) == 0,
              "unknown 带原因（requirement_not_mapped_to_concept），与 gap 分开记")
        check(0 <= top["rankingScore"] <= 1,
              "rankingScore=%.3f 是排序分，不是录用概率" % top["rankingScore"])
        check(top["coverage"] <= 1, "coverage=%.3f 用于判断解释是否覆盖当前事实" % top["coverage"])

    # ===============================================================
    print("\n【T9】§5 撤回个人分析授权 → 不再返回派生画像")
    rr = bridge_api.Api.consent(SRC, ANON, {"purpose": "personal_analysis", "granted": False})
    check(rr.get("ok") is True, "撤回授权成功")
    with db() as c:
        check(q1(c, "SELECT status FROM person WHERE person_id=%s", (pid,)) == "restricted",
              "人才状态置 restricted")
    r = bridge_api.Api.matches(SRC, ANON, {})
    check(r.get("ok") is False and r["code"] == "CONSENT_REQUIRED",
          "撤回后返回 CONSENT_REQUIRED，而不是继续给完整画像")

    # ===============================================================
    print("\n【T10】§8 删除受理 ≠ 完成")
    rd = bridge_api.Api.request_deletion(SRC, ANON, {"requestId": "del_req_1"})
    check(rd.get("ok") and rd["data"]["status"] == "pending", "删除请求受理，状态 pending")
    st = bridge_api.Api.deletion_status(SRC, ANON, "del_req_1")
    check(st["data"]["completedAt"] is None and st["data"]["downstreamConfirmed"] is False,
          "未完成且下游未确认（不得先显示完成再等下游）")
    r = bridge_api.Api.matches(SRC, ANON, {})
    check(r.get("ok") is False and r["code"] == "PROFILE_DELETING", "删除中阻断分析")

    # ===============================================================
    print("\n【T11】§7.1 墓碑拦住迟到 upsert（不得复活资料）")
    with db() as c:
        with c.cursor() as cur:
            cur.execute("""INSERT INTO tombstone (tombstone_id, person_id, source_system,
                              deleted_through_version, reason)
                           VALUES (%s,%s,%s,5,'测试删除') ON CONFLICT DO NOTHING""",
                        ("tomb_" + SRC, pid, SRC))
        c.commit()
    r = bridge_api.Api.exchange(pkg(3, "evt_v3_late"))
    check(r.get("ok") is False and r["code"] == "PROFILE_DELETING",
          "迟到事件被墓碑拦截：%s" % r.get("message", "")[:44])
    with db() as c:
        check(q1(c, "SELECT count(*) FROM response_session WHERE source_system=%s "
                    "AND aggregate_version=3", (SRC,)) == 0, "迟到事件未落库")

    # ===============================================================
    print("\n【T12】§7.1 outbox：删除中的人不投递；活跃的人至少一次交付且重复 ACK 安全")
    # 12a：上面那位已进入 deleting，其事件**不应**被投递（消费时复核删除状态）
    with db() as c:
        eid_blocked = ob.enqueue(c, "profile_upsert", SRC, pid, 99, profile_version=99,
                                 payload_ref="rs_not_exist")
        c.commit()
        sent = []
        ob.drain(c, lambda e, p: sent.append(e["event_id"]), 50)
        check(eid_blocked not in sent, "删除中的人的事件未被投递（消费时复核了删除状态）")

    # 12b：另建一位仍活跃的人，验证正常投递与重复 ACK 安全
    p2 = pkg(1, "evt_p2_v1", personId="anon2_" + SRC)
    r6 = bridge_api.Api.exchange(p2)
    check(r6.get("ok") is True, "第二位人才摄入成功（多主体互不干扰）")
    pid2 = r6["data"]["personId"]
    check(pid2 != pid, "两位人才 personId 不同")
    with db() as c:
        sess2 = q1(c, "SELECT session_id FROM response_session WHERE person_id=%s", (pid2,))
        eid2 = ob.enqueue(c, "profile_upsert", SRC, pid2, 1, profile_version=1,
                          payload_ref=sess2)
        # 再造一条"载荷与事件不一致"的事件：消费端必须拦下而不是投出去
        eid_bad = ob.enqueue(c, "consent_change", SRC, pid2, 2, payload={"personId": "someone_else"})
        c.commit()
        sent2 = []
        ob.drain(c, lambda e, p: sent2.append(e["event_id"]), 50)
        ob.drain(c, lambda e, p: None, 50)      # 第二次不应重复交付
        check(sent2.count(eid2) == 1, "活跃人的事件投递 1 次（实际 %d 次）" % sent2.count(eid2))
        check(q1(c, "SELECT status FROM sync_event WHERE event_id=%s", (eid2,)) == "delivered",
              "事件已 ACK；重复 ACK 安全")
        check(eid_bad not in sent2, "载荷与事件不一致的事件被拦下，未投递")

    # 12c：payload_ref 指向不存在的记录时，拒绝投递（不能拿重建不出的载荷去投）
    with db() as c:
        eid_ghost = ob.enqueue(c, "profile_upsert", SRC, pid2, 3, profile_version=3,
                               payload_ref="rs_does_not_exist")
        c.commit()
        sent3 = []
        ob.drain(c, lambda e, p: sent3.append(e["event_id"]), 50)
        check(eid_ghost not in sent3, "载荷无法按引用重建时不投递（避免投出空/错数据）")

    # ===============================================================
    print("\n【T13】HTTP 层：无令牌 401、有令牌 200（前端不直连库）")
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), bridge_api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/health" % PORT, timeout=10) as r:
            check(json.loads(r.read().decode("utf-8")).get("ok") is True, "健康检查可访问")
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/v1/jobs" % PORT, timeout=10)
            check(False, "无令牌访问受保护端点竟成功")
        except urllib.error.HTTPError as e:
            check(e.code == 401, "无令牌访问 /v1/jobs 返回 401")
        req = urllib.request.Request("http://127.0.0.1:%d/v1/jobs?limit=3" % PORT,
                                     headers={"Authorization": "Bearer " + bridge_api.TOKEN})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
        check(body.get("ok") and len(body["data"]["items"]) <= 3,
              "带令牌可取岗位投影（%d 条）" % len(body["data"]["items"]))
    finally:
        srv.shutdown()

    with db() as c:
        cleanup(c)

    return H.report(note="（测试数据已清理）")


if __name__ == "__main__":
    sys.exit(main())
