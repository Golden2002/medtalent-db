# -*- coding: utf-8 -*-
"""
code/bridge/exchange.py —— 小程序 ↔ MedTalent 交换包摄入（T11）

对应契约《medtalent-integration-schema》v1.0.0 §7「与 MedTalent 数据库交换」。

本模块实现契约要求的、可被测试判定的硬规则：

  R1 **禁止身份标识**：交换包中不得出现 memberKey / openid / appid / 手机号 /
     证件号 / 密钥等。深度扫描，发现即整包拒绝（不是丢弃字段后继续）。
  R2 **personId 绑定**：用 sourceSystem + personId 唯一绑定到本库 person_id；
     本库 person_id 为随机 ID，不含任何微信标识。
  R3 **幂等**：同一 (sourceSystem, personId, aggregateVersion, eventType) 重复投递，
     载荷一致 → 返回原结果；载荷不一致 → CONFLICT。
  R4 **按人有序**：aggregateVersion 不得倒退；迟到（更小版本）的包直接拒绝，
     避免旧状态覆盖新状态。
  R5 **墓碑优先**：已删除的人的迟到事件一律拒绝，不得复活资料。
  R6 **授权门**：撤回 personal_analysis 后，任何新分析产物（facts / skillClaims）
     一律拒绝，返回 CONSENT_REQUIRED。
  R7 **自述即未核验**：skillClaims 落库为 verify_status=V1、evidence_id=NULL、
     不写任何置信概率（本库 CHECK 会强制 confidence ≤ 0.5）。
  R8 **不臆造映射**：事实一律先原样写入 answer；只有 crosswalk 里登记了映射的
     字段才投影到规范表；未映射的事实计数上报，绝不静默丢弃。

用法：
  python code/bridge/exchange.py --example > dist/exchange.example.json
  python code/bridge/exchange.py --ingest dist/exchange.example.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
SCHEMA_VERSION = "1.0.0"

# R1：禁止出现的键（任意层级、大小写不敏感）。memberKey 虽不是密码，仍是可关联身份标识。
FORBIDDEN_KEYS = {
    "memberkey", "member_key", "openid", "open_id", "unionid", "union_id",
    "appid", "app_id", "phone", "mobile", "phonenumber", "idcard", "id_card",
    "idnumber", "password", "secret", "token", "sessionkey", "session_key",
    "wechat", "weixin", "nickname", "realname", "real_name",
}


class ExchangeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _walk_keys(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, path
            yield from _walk_keys(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, "%s[%d]" % (path, i))


def assert_no_identity(pkg: dict) -> None:
    """R1：深度扫描禁止键。"""
    bad = sorted({k for k, _ in _walk_keys(pkg) if str(k).lower() in FORBIDDEN_KEYS})
    if bad:
        raise ExchangeError(
            "INVALID_ARGUMENT",
            "交换包含有被禁止的身份标识字段：%s。"
            "契约 §7 明确交换包只含随机 personId、版本、facts、skillClaims 与授权状态。"
            % "、".join(bad))


def validate(pkg: dict) -> None:
    if not isinstance(pkg, dict):
        raise ExchangeError("INVALID_ARGUMENT", "交换包必须是 JSON 对象")
    for f in ("schemaVersion", "sourceSystem", "personId", "eventId",
              "eventType", "aggregateVersion"):
        if not pkg.get(f) and pkg.get(f) != 0:
            raise ExchangeError("INVALID_ARGUMENT", "交换包缺少必填字段 %s" % f)
    if not str(pkg["personId"]).strip():
        raise ExchangeError("INVALID_ARGUMENT", "personId 不能为空")
    if not isinstance(pkg["aggregateVersion"], int) or pkg["aggregateVersion"] < 0:
        raise ExchangeError("INVALID_ARGUMENT", "aggregateVersion 必须是非负整数")
    if pkg["eventType"] not in ("profile_upsert", "consent_change", "deletion"):
        raise ExchangeError("INVALID_ARGUMENT", "不支持的 eventType：%s" % pkg["eventType"])
    if pkg["eventType"] == "consent_change" and pkg.get("facts"):
        raise ExchangeError("INVALID_ARGUMENT",
                            "consent_change 事件不得携带 facts（契约 §7.1：授权变化事件 profile 必须为 null）")
    assert_no_identity(pkg)


def canonical_hash(pkg: dict) -> str:
    """请求哈希：语义载荷的规范化哈希（排除投递时间等非语义字段）。"""
    sem = {k: v for k, v in pkg.items() if k not in ("deliveredAt", "sentAt", "transport")}
    return hashlib.sha256(json.dumps(sem, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def ingest(pkg: dict, conn=None) -> dict:
    """摄入一个交换包。返回 {ok, data} 或抛 ExchangeError。"""
    validate(pkg)
    own = conn is None
    c = conn or psycopg.connect(DSN, row_factory=dict_row)
    try:
        src = pkg["sourceSystem"]
        ext_pid = str(pkg["personId"])
        ver = int(pkg["aggregateVersion"])
        etype = pkg["eventType"]
        rhash = canonical_hash(pkg)

        with c.cursor() as cur:
            # R2：身份绑定
            cur.execute("""SELECT person_id, status, last_seen_version, identity_id
                           FROM external_identity
                           WHERE source_system=%s AND external_person_id=%s""", (src, ext_pid))
            ident = cur.fetchone()

            if ident and ident["status"] == "deleted":
                raise ExchangeError("PROFILE_DELETING",
                                    "该 personId 的资料已删除，不再接受任何写入")

            # R5：墓碑
            cur.execute("""SELECT deleted_through_version FROM tombstone
                           WHERE person_id=%s AND source_system=%s""",
                        (ident["person_id"], src) if ident else ("", src))
            tb = cur.fetchone()
            if tb and ver <= tb["deleted_through_version"]:
                raise ExchangeError("PROFILE_DELETING",
                                    "版本 %d 已被墓碑拦截（该人至版本 %d 的资料已删除）"
                                    % (ver, tb["deleted_through_version"]))

            if ident:
                person_id = ident["person_id"]
                # R3：幂等（按"来源 + 对方事件号"判定，契约 §7.1）
                # request_hash 存在 response_session 上，故需 JOIN
                ext_eid = pkg["eventId"]
                cur.execute("""SELECT e.event_id, e.status, s.request_hash
                               FROM sync_event e
                               LEFT JOIN response_session s ON s.session_id = e.payload_ref
                               WHERE e.source_system=%s AND e.external_event_id=%s
                               LIMIT 1""", (src, ext_eid))
                prev = cur.fetchone()
                if prev:
                    old_hash = prev.get("request_hash")
                    if old_hash and old_hash != rhash:
                        raise ExchangeError("CONFLICT",
                                            "相同 eventId 的载荷与已记录不一致")
                    return {"ok": True, "data": {
                        "personId": person_id, "eventId": prev["event_id"],
                        "externalEventId": ext_eid,
                        "aggregateVersion": ver, "syncStatus": "delivered",
                        "idempotent": True}}

                # R4：版本不得倒退
                if ver <= ident["last_seen_version"]:
                    raise ExchangeError("CONFLICT",
                                        "aggregateVersion %d 不大于已处理版本 %d"
                                        % (ver, ident["last_seen_version"]))
                person_id = ident["person_id"]
            else:
                # 新人：创建随机 person_id（不含任何微信标识）
                person_id = "per_" + hashlib.sha1(
                    ("%s|%s|%s" % (src, ext_pid, _now().isoformat())).encode()
                ).hexdigest()[:24]
                cur.execute("""INSERT INTO person (person_id, subject_code, enroll_channel,
                                  verify_status, access_tier, status)
                               VALUES (%s, %s, 'EC3', 'V1', 'T1', 'active')""",
                            (person_id, "MT-EXT-" + person_id[-8:].upper()))
                cur.execute("""INSERT INTO external_identity (identity_id, source_system,
                                  external_person_id, person_id, last_seen_version, status)
                               VALUES (%s, %s, %s, %s, 0, 'active')""",
                            ("eid_" + hashlib.sha1(("%s|%s" % (src, ext_pid)).encode())
                             .hexdigest()[:20], src, ext_pid, person_id))

            # R6：授权门
            has_analysis = bool(pkg.get("facts") or pkg.get("skillClaims"))
            if has_analysis:
                cur.execute("""SELECT granted_at, revoked_at FROM consent_record
                               WHERE person_id=%s AND purpose='CP1'
                               ORDER BY granted_at DESC LIMIT 1""", (person_id,))
                cons = cur.fetchone()
                if cons and cons["revoked_at"] is not None:
                    raise ExchangeError("CONSENT_REQUIRED",
                                        "个人分析授权已撤回，不得再写入分析产物")

            # 写答卷（R8：事实一律先原样留存）
            session_id = "rs_" + hashlib.sha1(
                ("%s|%s" % (person_id, pkg.get("submissionId") or pkg["eventId"])).encode()
            ).hexdigest()[:20]
            cur.execute("""INSERT INTO response_session (session_id, person_id, source_system,
                              source_submission_id, questionnaire_id, questionnaire_version,
                              catalog_id, mapping_version, aggregate_version, submitted_at,
                              request_hash, request_id, raw_payload)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s::jsonb)
                           ON CONFLICT (source_system, source_submission_id) DO NOTHING""",
                        (session_id, person_id, src,
                         pkg.get("submissionId") or pkg["eventId"],
                         pkg.get("questionnaireId", "unknown"),
                         pkg.get("questionnaireVersion", "unknown"),
                         pkg.get("catalogId", "unknown"),
                         pkg.get("mappingVersion"),
                         ver, rhash, pkg.get("requestId"),
                         json.dumps({k: v for k, v in pkg.items()
                                     if k in ("facts", "skillClaims", "preferences",
                                              "consents", "education", "experience")},
                                    ensure_ascii=False)))

            n_fact = n_mapped = 0
            for fact in (pkg.get("facts") or []):
                n_fact += 1
                cur.execute("""INSERT INTO answer (answer_id, session_id, question_id,
                                  instance_id, status, option_ids, ordinal)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (session_id, question_id, instance_id)
                               DO UPDATE SET status=EXCLUDED.status,
                                             option_ids=EXCLUDED.option_ids""",
                            ("ans_" + hashlib.sha1(("%s|%s|%s" % (
                                session_id, fact.get("fieldId"), fact.get("instanceId"))).encode()
                             ).hexdigest()[:24],
                             session_id, fact.get("fieldId"),
                             fact.get("instanceId") or "_",
                             fact.get("status", "answered"),
                             fact.get("valueCodes") or None, n_fact))
                if crosswalk_project(cur, person_id, src, pkg, fact):
                    n_mapped += 1

            # 经验重复组
            for ep in (pkg.get("experience") or []):
                _upsert_episode(cur, person_id, src, ep,
                                pkg.get("submissionId") or pkg["eventId"])

            # R7：自述技能——未核验、无证据、无概率
            # R8：对方给的是"行为题导出的自述"，概念由本库的 crosswalk 决定；
            #     映射不上时**不臆造概念**，改入通用断言表并计入 unmapped 上报。
            n_skill = n_skill_mapped = 0
            for sc in (pkg.get("skillClaims") or []):
                n_skill += 1
                concept_id = resolve_concept(cur, src, sc)
                if concept_id:
                    n_skill_mapped += 1
                    cur.execute("""INSERT INTO skill_assertion (assertion_id, person_id, concept_id,
                                      level, level_basis, claim_type, evidence_id, confidence,
                                      transferability, transfer_note, verify_status, source_id)
                                   VALUES (%s,%s,%s,%s,'LB1','CT1',NULL,0.5,NULL,%s,'V1',NULL)
                                   ON CONFLICT (assertion_id) DO NOTHING""",
                                ("skl_" + hashlib.sha1(("%s|%s|%s" % (
                                    person_id, concept_id, sc.get("sourceQuestionId"))).encode()
                                 ).hexdigest()[:24],
                                 person_id, concept_id, sc.get("level"),
                                 "小程序自述：%s" % (sc.get("label") or sc.get("sourceQuestionId"))))
                else:
                    cur.execute("""INSERT INTO assertion (assertion_id, subject_type, subject_id,
                                      predicate, object_type, object_value, confidence,
                                      source_id, status)
                                   VALUES (%s,'person',%s,'self_reported_skill','text',%s::jsonb,
                                           0.5,NULL,'provisional')
                                   ON CONFLICT (assertion_id) DO NOTHING""",
                                ("asr_" + hashlib.sha1(("%s|%s" % (
                                    person_id, sc.get("sourceQuestionId"))).encode()).hexdigest()[:24],
                                 person_id,
                                 json.dumps({"label": sc.get("label"),
                                             "questionId": sc.get("sourceQuestionId"),
                                             "valueCodes": sc.get("valueCodes")},
                                            ensure_ascii=False)))

            # 偏好
            prefs = pkg.get("preferences") or {}
            for pref_type, code in (("PF3", "targetOccupations"), ("PF2", "industries"),
                                    ("PF6", "workModes")):
                for v in (prefs.get(code) or []):
                    cur.execute("""INSERT INTO preference (preference_id, person_id, pref_type,
                                      value_code, weight, is_hard, confidence)
                                   VALUES (%s,%s,%s,%s,0.7,false,0.6)
                                   ON CONFLICT (preference_id) DO NOTHING""",
                                ("prf_" + hashlib.sha1(("%s|%s|%s" % (
                                    person_id, pref_type, v)).encode()).hexdigest()[:24],
                                 person_id, pref_type, v))

            # 授权
            cons = pkg.get("consents") or {}
            if cons:
                for purpose, key in (("CP1", "personalAnalysis"),
                                     ("CP2", "opportunityNotifications")):
                    if key in cons:
                        granted = bool(cons[key])
                        cur.execute("""INSERT INTO consent_record (consent_id, person_id, purpose,
                                          scope, granted_at, revoked_at, channel)
                                       VALUES (%s,%s,%s,%s, now(),
                                               CASE WHEN %s THEN NULL ELSE now() END, 'web_form')""",
                                    ("cns_" + hashlib.sha1(("%s|%s|%s" % (
                                        person_id, purpose, ver)).encode()).hexdigest()[:24],
                                     person_id, purpose, "miniprogram onboarding", granted))

            # 事件入账：本库生成稳定 event_id，另存对方事件号用于幂等
            event_id = "evt_in_" + hashlib.sha1(
                ("%s|%s" % (src, pkg["eventId"])).encode()).hexdigest()[:20]
            cur.execute("""INSERT INTO sync_event (event_id, external_event_id, direction,
                              event_type, source_system, person_id, aggregate_version,
                              profile_version, payload_ref, status, attempts, acked_at)
                           VALUES (%s,%s,'inbound',%s,%s,%s,%s,%s,%s,'delivered',1,now())
                           ON CONFLICT (source_system, external_event_id) DO NOTHING""",
                        (event_id, pkg["eventId"], etype, src, person_id, ver, ver, session_id))

            cur.execute("""UPDATE external_identity SET last_seen_version=%s
                           WHERE source_system=%s AND external_person_id=%s""",
                        (ver, src, ext_pid))

        if own:
            c.commit()
        return {"ok": True, "data": {
            "personId": person_id, "eventId": pkg["eventId"],
            "aggregateVersion": ver, "profileVersion": ver, "syncStatus": "delivered",
            "facts": n_fact, "mappedFacts": n_mapped,
            "unmappedFacts": n_fact - n_mapped,
            "skillClaims": n_skill, "mappedSkillClaims": n_skill_mapped,
            "unmappedSkillClaims": n_skill - n_skill_mapped}}
    except ExchangeError:
        if own:
            c.rollback()
        raise
    except Exception:
        if own:
            c.rollback()
        raise
    finally:
        if own:
            c.close()


def resolve_concept(cur, src: str, claim: dict) -> str | None:
    """R8：用 crosswalk 把对方的概念/题号解析到本库 concept_id。
    未登记映射时返回 None——**不猜测、不硬塞**。"""
    keys = [k for k in (claim.get("conceptId"), claim.get("sourceQuestionId")) if k]
    for k in keys:
        cur.execute("""SELECT local_id FROM crosswalk
                       WHERE external_system=%s AND external_kind='concept'
                         AND external_id=%s AND local_kind='concept'
                         AND review_status <> 'rejected'
                       ORDER BY review_status='reviewed' DESC LIMIT 1""", (src, k))
        m = cur.fetchone()
        if m:
            return m["local_id"]
    return None


def crosswalk_project(cur, person_id, src, pkg, fact) -> bool:
    """R8：只有 crosswalk 登记了映射的字段才投影到规范表；返回是否映射成功。"""
    cur.execute("""SELECT local_kind, local_id FROM crosswalk
                   WHERE external_system=%s AND external_kind='field'
                     AND external_id=%s AND review_status <> 'rejected'
                   ORDER BY review_status='reviewed' DESC LIMIT 1""",
                (src, fact.get("fieldId")))
    m = cur.fetchone()
    if not m:
        return False
    codes = fact.get("valueCodes") or []
    if not codes:
        return False
    if m["local_kind"] == "field" and m["local_id"] == "education.degree_level":
        cur.execute("""INSERT INTO education_record (education_id, person_id, degree_level,
                          degree_name, verify_status, confidence, source_id)
                       VALUES (%s,%s,%s,NULL,'V1',0.7,NULL)
                       ON CONFLICT (education_id) DO NOTHING""",
                    ("edu_" + hashlib.sha1(("%s|%s" % (person_id, codes[0])).encode())
                     .hexdigest()[:20], person_id, codes[0]))
        return True
    return False


def _upsert_episode(cur, person_id, src, ep, submission_id) -> None:
    eid = "exp_" + hashlib.sha1(("%s|%s|%s" % (
        person_id, src, ep.get("instanceId"))).encode()).hexdigest()[:20]
    cur.execute("""INSERT INTO experience_episode (episode_id, person_id, instance_id,
                      episode_type, organization, role_title, start_ym, end_ym,
                      is_current, source_system, source_submission_id, verify_status, confidence)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'V1',0.7)
                   ON CONFLICT (person_id, source_system, instance_id) DO UPDATE SET
                      episode_type=EXCLUDED.episode_type,
                      organization=EXCLUDED.organization,
                      role_title=EXCLUDED.role_title,
                      start_ym=EXCLUDED.start_ym, end_ym=EXCLUDED.end_ym,
                      is_current=EXCLUDED.is_current""",
                (eid, person_id, ep.get("instanceId"), ep.get("episodeType", "other"),
                 ep.get("organization"), ep.get("roleTitle"), ep.get("startYm"),
                 ep.get("endYm"), ep.get("isCurrent"), src, submission_id))
    for k, t in enumerate(ep.get("tasks") or [], 1):
        cur.execute("""INSERT INTO experience_task (task_id, episode_id, ordinal, task_text)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (task_id) DO NOTHING""",
                    ("etk_" + hashlib.sha1(("%s|%d" % (eid, k)).encode()).hexdigest()[:20],
                     eid, k, t))


def example_package() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sourceSystem": "weijie_miniprogram",
        "personId": "wx_anon_7f3a91c2e5b84d60",
        "eventId": "evt_20260927_000001",
        "eventType": "profile_upsert",
        "submissionId": "sub_20260927_000001",
        "requestId": "req_20260927_000001",
        "questionnaireId": "onboarding",
        "questionnaireVersion": "1.0.0",
        "catalogId": "catalog_1.0.0",
        "mappingVersion": "xw_1.0.0",
        "aggregateVersion": 1,
        "facts": [
            {"fieldId": "current_stage", "status": "answered", "valueCodes": ["D3"]},
            {"fieldId": "education.degree_level", "instanceId": "education/edu_1",
             "status": "answered", "valueCodes": ["D3"]},
            {"fieldId": "current_city", "status": "answered", "valueCodes": ["CN-31"]},
            {"fieldId": "target_direction", "status": "prefer_not_to_say"},
        ],
        "experience": [
            {"instanceId": "experience/exp_1", "episodeType": "research",
             "organization": "某医科大学", "roleTitle": "硕士研究生",
             "startYm": "2022-09", "endYm": "2025-06", "isCurrent": False,
             "tasks": ["开展课题实验", "撰写学位论文"]}
        ],
        "skillClaims": [
            {"sourceQuestionId": "q_lit_review", "label": "文献检索与证据分级"},
            {"sourceQuestionId": "q_teamwork", "label": "团队协作"}
        ],
        "preferences": {
            "targetOccupations": ["F02"],
            "industries": ["pharma"],
            "workModes": ["onsite"]
        },
        "consents": {
            "noticeVersion": "1.0.0",
            "personalAnalysis": True,
            "opportunityNotifications": False
        }
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--example", action="store_true")
    ap.add_argument("--example-out", help="把示例交换包写到指定路径（UTF-8，无 BOM）")
    ap.add_argument("--ingest")
    a = ap.parse_args()

    if a.example_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.example_out)), exist_ok=True)
        with open(a.example_out, "w", encoding="utf-8") as fh:
            json.dump(example_package(), fh, ensure_ascii=False, indent=2)
        print("[✓] 示例交换包已写入 %s" % a.example_out)
        return 0
    if a.example:
        print(json.dumps(example_package(), ensure_ascii=False, indent=2))
        return 0
    if a.ingest:
        # utf-8-sig：容忍 Windows 工具写入的 BOM
        with open(a.ingest, encoding="utf-8-sig") as fh:
            pkg = json.load(fh)
        try:
            r = ingest(pkg)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        except ExchangeError as e:
            print(json.dumps({"ok": False, "code": e.code, "message": e.message},
                             ensure_ascii=False, indent=2))
            return 3
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
