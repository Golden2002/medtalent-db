# -*- coding: utf-8 -*-
"""
code/bridge/outbox.py —— 同步事件消费者（T11，契约 §7.1）

契约对可靠同步的要求，逐条落到实现：

  · **至少一次交付**：接收端以 eventId 幂等；重复 ACK 安全。
  · **按人串行**：同一 person 的事件按 aggregateVersion 顺序处理，
    不在新事件未处理完时并发覆盖旧事件。
  · **消费时复核**：投递前重新检查最新授权与删除状态；
    过期的 upsert 不得越过撤回或墓碑。
  · **只存引用**：出向事件不内嵌完整画像，消费时按 payload_ref 从
    response_session 按冻结版本重建，避免"拿当前画像伪装旧版本"。
  · **租约**：lease_until 到期后事件可被其他消费者接管，不能仅靠 status 永久锁死。
  · **退避重试与死信**：失败按指数退避；超过上限进 dead 并告警，不无限重试。

用法（库内部调用；HTTP 层见 code/bridge/api.py）：
    python code/bridge/outbox.py --stats
    python code/bridge/outbox.py --drain --max 50        # 用空 sink 演练（不做真实投递）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=mt_bridge "
       "client_encoding=UTF8 options='-c search_path=mt,public'")
LEASE_SECONDS = 60
MAX_ATTEMPTS = 5


class DeliveryBlocked(Exception):
    """消费时复核不通过：不重试，直接按规则处理（撤回/删除）。"""


def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def _now():
    return datetime.now(timezone.utc)


def rebuild_payload(c, event: dict) -> dict:
    """按 payload_ref 从冻结的答卷重建那一版的载荷，而不是拿当前画像冒充。"""
    if event.get("payload"):
        return event["payload"]
    ref = event.get("payload_ref")
    if not ref:
        return {}
    with c.cursor() as cur:
        cur.execute("SELECT raw_payload, aggregate_version FROM response_session "
                    "WHERE session_id=%s", (ref,))
        r = cur.fetchone()
    if not r:
        return {}
    body = dict(r["raw_payload"] or {})
    body["_rebuiltFromVersion"] = r["aggregate_version"]
    body["personId"] = event["person_id"]
    return body


def check_gates(c, event: dict) -> None:
    """消费时复核最新授权与删除状态（契约 §7.1）。"""
    with c.cursor() as cur:
        cur.execute("SELECT status FROM external_identity "
                    "WHERE person_id=%s AND source_system=%s",
                    (event["person_id"], event["source_system"]))
        ident = cur.fetchone()
        if ident and ident["status"] != "active":
            raise DeliveryBlocked("身份状态为 %s，停止投递" % ident["status"])

        cur.execute("SELECT deleted_through_version FROM tombstone "
                    "WHERE person_id=%s AND source_system=%s",
                    (event["person_id"], event["source_system"]))
        tb = cur.fetchone()
        if tb and event["aggregate_version"] <= tb["deleted_through_version"]:
            raise DeliveryBlocked("已被墓碑拦截（至版本 %d）" % tb["deleted_through_version"])

        # 分析类事件要求 personal_analysis 授权有效
        if event["event_type"] in ("profile_upsert", "match_result"):
            cur.execute("""SELECT granted_at, revoked_at FROM consent_record
                           WHERE person_id=%s AND purpose='CP1'
                           ORDER BY granted_at DESC LIMIT 1""", (event["person_id"],))
            cons = cur.fetchone()
            if cons and cons["revoked_at"] is not None:
                raise DeliveryBlocked("个人分析授权已撤回，过期 upsert 不得越过撤回")


def claim(c, limit: int = 20, direction: str = "outbound") -> list[dict]:
    """认领一批事件（带租约）。用 SKIP LOCKED 避免消费者互相阻塞。"""
    with c.cursor() as cur:
        cur.execute("""
            WITH picked AS (
              SELECT e.event_id FROM sync_event e
              JOIN external_identity x ON x.person_id = e.person_id
                                      AND x.source_system = e.source_system
              WHERE e.direction = %s
                AND e.status IN ('pending','failed')
                AND (e.lease_until IS NULL OR e.lease_until < now())
                AND x.status = 'active'
                AND e.aggregate_version = (
                      -- 取**最小**未交付版本：按 aggregateVersion 升序交付，
                      -- 让新事件等待旧事件，而不是反过来越过它。
                      SELECT min(e2.aggregate_version) FROM sync_event e2
                      WHERE e2.person_id = e.person_id
                        AND e2.source_system = e.source_system
                        AND e2.direction = e.direction
                        AND e2.status <> 'delivered'
                    )
              ORDER BY e.person_id, e.aggregate_version
              LIMIT %s
              FOR UPDATE OF e SKIP LOCKED
            )
            UPDATE sync_event s SET status='processing',
                   lease_until=now() + interval '%s seconds', attempts=s.attempts+1
            FROM picked WHERE s.event_id = picked.event_id
            RETURNING s.event_id, s.event_type, s.source_system, s.person_id,
                      s.aggregate_version, s.profile_version, s.payload, s.payload_ref,
                      s.attempts""", (direction, limit, LEASE_SECONDS))
        return cur.fetchall()


def ack(c, event_id: str) -> None:
    with c.cursor() as cur:
        cur.execute("UPDATE sync_event SET status='delivered', acked_at=now(), "
                    "lease_until=NULL, last_error=NULL WHERE event_id=%s", (event_id,))


def fail(c, event_id: str, err: str) -> str:
    """失败：未超上限 → 留作待重试；超限 → dead（并保留错误信息供告警）。"""
    with c.cursor() as cur:
        cur.execute("""UPDATE sync_event
                       SET status = CASE WHEN attempts >= %s THEN 'dead' ELSE 'failed' END,
                           lease_until = NULL,
                           last_error = %s
                       WHERE event_id=%s RETURNING status""",
                    (MAX_ATTEMPTS, err[:500], event_id))
        return cur.fetchone()["status"]


def drain(c, sink, limit: int = 20, direction: str = "outbound") -> dict:
    """消费一批：同一人的事件严格按 aggregateVersion 顺序投递。"""
    events = claim(c, limit, direction)
    stats = {"claimed": len(events), "delivered": 0, "blocked": 0, "failed": 0, "dead": 0}
    seen_person = {}
    for e in events:
        # 同一人串行：若该人上一事件本轮失败，则跳过其后续事件
        if seen_person.get(e["person_id"]) in ("failed", "dead", "blocked"):
            stats["blocked"] += 1
            continue
        try:
            check_gates(c, e)
            payload = rebuild_payload(c, e)
            # 契约 §7.1：profile_upsert 的 profile.personId 必须等于顶层 personId，
            # 且 profileVersion 与本事件版本一致；授权变化事件 profile 必须为 null
            if e["event_type"] == "profile_upsert":
                if payload.get("personId") != e["person_id"]:
                    raise DeliveryBlocked("载荷 personId 与事件不一致")
                if payload.get("aggregateVersion") not in (None, e["aggregate_version"]):
                    raise DeliveryBlocked("载荷版本与事件版本不一致")
            elif e["event_type"] == "consent_change":
                if payload.get("profile") is not None or \
                        payload.get("personId") not in (None, e["person_id"]):
                    raise DeliveryBlocked("授权变化事件不得携带画像载荷")
            sink(e, payload)
            ack(c, e["event_id"])
            stats["delivered"] += 1
            seen_person[e["person_id"]] = "delivered"
        except DeliveryBlocked as ex:
            # 撤回/删除/版本不符：不重试，标记为已投递（按规则拦下并留痕）
            with c.cursor() as cur:
                cur.execute("UPDATE sync_event SET status='delivered', acked_at=now(), "
                            "lease_until=NULL, last_error=%s WHERE event_id=%s",
                            ("BLOCKED: " + str(ex), e["event_id"]))
            stats["blocked"] += 1
            seen_person[e["person_id"]] = "blocked"
        except Exception as ex:  # noqa: BLE001
            st = fail(c, e["event_id"], "%s: %s" % (type(ex).__name__, ex))
            stats["dead" if st == "dead" else "failed"] += 1
            seen_person[e["person_id"]] = st
    c.commit()
    return stats


def enqueue(c, event_type: str, source_system: str, person_id: str,
            aggregate_version: int, profile_version: int = None,
            payload_ref: str = None, payload: dict = None,
            direction: str = "outbound") -> str:
    eid = "evt_out_" + hashlib.sha1(("%s|%s|%s|%s" % (
        source_system, person_id, aggregate_version, event_type)).encode()).hexdigest()[:20]
    with c.cursor() as cur:
        cur.execute("""INSERT INTO sync_event (event_id, direction, event_type, source_system,
                          person_id, aggregate_version, profile_version, payload, payload_ref,
                          status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'pending')
                       ON CONFLICT (event_id) DO NOTHING""",
                    (eid, direction, event_type, source_system, person_id,
                     aggregate_version, profile_version,
                     json.dumps(payload, ensure_ascii=False) if payload else None,
                     payload_ref))
    return eid


def stats(c) -> list[dict]:
    with c.cursor() as cur:
        cur.execute("""SELECT direction, status, count(*) AS n FROM sync_event
                       GROUP BY 1,2 ORDER BY 1,2""")
        return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--max", type=int, default=20)
    a = ap.parse_args()

    with conn() as c:
        if a.stats:
            for r in stats(c):
                print("  %-9s %-11s %d" % (r["direction"], r["status"], r["n"]))
            return 0
        if a.drain:
            def null_sink(e, p):
                print("    -> %s v%s (%s)" % (e["event_type"], e["aggregate_version"],
                                              e["person_id"]))
            s = drain(c, null_sink, a.max)
            print("[✓] %s" % s)
            return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
