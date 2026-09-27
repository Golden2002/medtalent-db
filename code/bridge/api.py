# -*- coding: utf-8 -*-
"""
code/bridge/api.py —— MedTalent 接入 API（T11）

契约 §1 的分工：前端**不直连 PostgreSQL，也不持有数据库密码**。
链路是 小程序 → CloudBase 云函数 community → **本服务** → MedTalent 数据库。
所以本服务是**服务端到服务端**接口，用 Bearer 令牌鉴权，只监听回环地址。

统一返回外壳（与小程序侧 ok/data/code/message 保持一致）：
    成功 {"ok": true, "data": {...}}
    失败 {"ok": false, "code": "...", "message": "..."}

错误码沿用契约新增的定义：
    NOT_CONFIGURED / UNAUTHENTICATED / FORBIDDEN / INVALID_ARGUMENT / NOT_FOUND /
    CONFLICT / BACKEND_ERROR / QUESTIONNAIRE_RETIRED / CONSENT_REQUIRED /
    UPGRADE_REQUIRED / PROFILE_DELETING

端点：
    GET  /v1/health
    POST /v1/exchange                     摄入交换包（幂等）
    GET  /v1/jobs?limit=&cursor=&family=  岗位投影（每页 ≤20）
    GET  /v1/persons/{pid}/matches        匹配结果（需个人分析授权）
    POST /v1/persons/{pid}/consent        授权/撤回
    POST /v1/persons/{pid}/deletion       发起删除（受理≠完成）
    GET  /v1/persons/{pid}/deletion/{rid} 删除状态
    POST /v1/outbox/drain                 消费待投递事件（服务身份）

启动：python code/bridge/api.py --port 8097
令牌：环境变量 MEDTALENT_BRIDGE_TOKEN；未设置则随机生成并打印（仅开发）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(BASE, "code", "bridge"))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import exchange as ex  # noqa: E402
import outbox as ob  # noqa: E402
import projection as pj  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
TOKEN = os.environ.get("MEDTALENT_BRIDGE_TOKEN") or secrets.token_urlsafe(24)
SERVER_IDENTITY = "medtalent-bridge/0.1"


def db():
    return psycopg.connect(ex.DSN, row_factory=dict_row)


class Api:
    """纯逻辑层：不依赖 HTTP，便于单测直接调用。"""

    # --- 通用 ---
    @staticmethod
    def _person_of_source(c, source_system: str, person_id: str):
        with c.cursor() as cur:
            cur.execute("""SELECT person_id, status FROM external_identity
                           WHERE source_system=%s AND external_person_id=%s""",
                        (source_system, person_id))
            return cur.fetchone()

    @staticmethod
    def _has_analysis_consent(c, person_id: str) -> bool:
        with c.cursor() as cur:
            cur.execute("""SELECT granted_at, revoked_at FROM consent_record
                           WHERE person_id=%s AND purpose='CP1'
                           ORDER BY granted_at DESC LIMIT 1""", (person_id,))
            r = cur.fetchone()
        return bool(r) and r["revoked_at"] is None

    # --- 端点实现 ---
    @classmethod
    def health(cls):
        with db() as c, c.cursor() as cur:
            cur.execute("SELECT current_database() AS db, count(*) AS jobs FROM job_posting")
            r = cur.fetchone()
        return {"ok": True, "data": {"server": SERVER_IDENTITY, "database": r["db"],
                                     "jobs": r["jobs"], "schemaVersion": ex.SCHEMA_VERSION}}

    @classmethod
    def exchange(cls, pkg: dict):
        try:
            return ex.ingest(pkg)
        except ex.ExchangeError as e:
            return {"ok": False, "code": e.code, "message": e.message}

    @classmethod
    def jobs(cls, q: dict):
        limit = int(q.get("limit", [pj.PAGE_SIZE])[0])
        cursor = q.get("cursor", [None])[0]
        family = q.get("family", [None])[0]
        with db() as c:
            r = pj.export_jobs(c, limit, cursor, family)
        return {"ok": True, "data": r}

    @classmethod
    def matches(cls, source_system: str, person_id: str, q: dict):
        with db() as c:
            ident = cls._person_of_source(c, source_system, person_id)
            if not ident:
                return {"ok": False, "code": "NOT_FOUND", "message": "未找到该 personId"}
            if ident["status"] == "deleting":
                return {"ok": False, "code": "PROFILE_DELETING",
                        "message": "资料删除处理中，暂不提供分析"}
            if not cls._has_analysis_consent(c, ident["person_id"]):
                # 契约 §5：撤回分析许可不得继续返回完整派生画像
                return {"ok": False, "code": "CONSENT_REQUIRED",
                        "message": "个人分析授权无效，无法返回匹配结果"}
            m = pj.compute_matches(c, ident["person_id"])
            pj.persist_matches(c, ident["person_id"], m)
            c.commit()
        return {"ok": True, "data": m}

    @classmethod
    def consent(cls, source_system: str, person_id: str, body: dict):
        purpose = body.get("purpose", "personal_analysis")
        granted = bool(body.get("granted"))
        code = {"personal_analysis": "CP1", "opportunity_notifications": "CP2"}.get(purpose)
        if not code:
            return {"ok": False, "code": "INVALID_ARGUMENT", "message": "未知用途 %s" % purpose}
        with db() as c:
            ident = cls._person_of_source(c, source_system, person_id)
            if not ident:
                return {"ok": False, "code": "NOT_FOUND", "message": "未找到该 personId"}
            pid = ident["person_id"]
            with c.cursor() as cur:
                # 契约：停用成员可撤回，不得新授予
                if granted and ident["status"] != "active":
                    return {"ok": False, "code": "FORBIDDEN",
                            "message": "停用成员不得新授予授权"}
                cur.execute("""INSERT INTO consent_record (consent_id, person_id, purpose,
                                  scope, granted_at, revoked_at, channel)
                               VALUES (%s,%s,%s,%s, now(), CASE WHEN %s THEN NULL ELSE now() END,
                                       'server_api')""",
                            ("cns_" + hashlib.sha1(("%s|%s|%s" % (
                                pid, code, datetime.now(timezone.utc).isoformat())).encode()
                             ).hexdigest()[:24], pid, code, "bridge api", granted))
                if not granted and code == "CP1":
                    # 撤回个人分析 → 人才状态置 restricted，停止新分析
                    cur.execute("UPDATE person SET status='restricted' WHERE person_id=%s", (pid,))
                cur.execute("SELECT count(*) AS v FROM sync_event WHERE person_id=%s", (pid,))
                v = cur.fetchone()["v"]
                ob.enqueue(c, "consent_change", source_system, pid, v + 1,
                           payload=None, payload_ref=None)
            c.commit()
        return {"ok": True, "data": {"purpose": purpose, "granted": granted,
                                     "aggregateVersion": v + 1}}

    @classmethod
    def request_deletion(cls, source_system: str, person_id: str, body: dict):
        request_id = body.get("requestId") or ("del_" + secrets.token_hex(8))
        with db() as c:
            ident = cls._person_of_source(c, source_system, person_id)
            if not ident:
                return {"ok": False, "code": "NOT_FOUND", "message": "未找到该 personId"}
            pid = ident["person_id"]
            local_id = "dlr_" + hashlib.sha1(("%s|%s" % (pid, request_id)).encode()).hexdigest()[:20]
            with c.cursor() as cur:
                cur.execute("""INSERT INTO talent_deletion_request (local_request_id, request_id,
                                  person_id, source_system, status)
                               VALUES (%s,%s,%s,%s,'pending')
                               ON CONFLICT (person_id, request_id) DO NOTHING""",
                            (local_id, request_id, pid, source_system))
                # 受理 ≠ 完成：只置 deleting 并阻断新分析
                cur.execute("UPDATE external_identity SET status='deleting' "
                            "WHERE person_id=%s AND source_system=%s", (pid, source_system))
                cur.execute("UPDATE person SET status='restricted' WHERE person_id=%s", (pid,))
            c.commit()
        return {"ok": True, "data": {"requestId": request_id, "status": "pending"}}

    @classmethod
    def deletion_status(cls, source_system: str, person_id: str, request_id: str):
        with db() as c, c.cursor() as cur:
            cur.execute("""SELECT r.status, r.completed_at, r.downstream_confirmed
                           FROM talent_deletion_request r
                           JOIN external_identity x ON x.person_id = r.person_id
                           WHERE x.source_system=%s AND x.external_person_id=%s
                             AND r.request_id=%s""", (source_system, person_id, request_id))
            r = cur.fetchone()
        if not r:
            return {"ok": False, "code": "NOT_FOUND", "message": "未找到该删除请求"}
        return {"ok": True, "data": {
            "requestId": request_id, "status": r["status"],
            "completedAt": int(r["completed_at"].timestamp() * 1000) if r["completed_at"] else None,
            # 未配置下游时，必须明确"不存在待删除副本"才算完成
            "downstreamConfirmed": r["downstream_confirmed"]}}

    @classmethod
    def drain_outbox(cls, body: dict):
        limit = int(body.get("limit", 20))
        with db() as c:
            sent = []

            def sink(e, p):
                sent.append({"eventId": e["event_id"], "type": e["event_type"],
                             "aggregateVersion": e["aggregate_version"]})

            s = ob.drain(c, sink, limit)
        return {"ok": True, "data": {"stats": s, "delivered": sent}}


class Handler(BaseHTTPRequestHandler):
    server_version = "MedTalentBridge/0.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        h = self.headers.get("Authorization", "")
        return h == "Bearer " + TOKEN

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/v1/health":
            return self._send(Api.health())
        if not self._auth():
            return self._send({"ok": False, "code": "UNAUTHENTICATED",
                               "message": "缺少或错误的 Bearer 令牌"}, 401)
        try:
            if method == "POST" and parsed.path == "/v1/exchange":
                return self._send(Api.exchange(self._body()))
            if method == "GET" and parsed.path == "/v1/jobs":
                return self._send(Api.jobs(q))
            if method == "POST" and parsed.path == "/v1/outbox/drain":
                return self._send(Api.drain_outbox(self._body()))
            # /v1/persons/{personId}/...
            if len(parts) >= 4 and parts[0] == "v1" and parts[1] == "persons":
                pid = urllib.parse.unquote(parts[2])
                src = q.get("sourceSystem", ["weijie_miniprogram"])[0]
                if method == "GET" and parts[3] == "matches":
                    return self._send(Api.matches(src, pid, q))
                if method == "POST" and parts[3] == "consent":
                    return self._send(Api.consent(src, pid, self._body()))
                if method == "POST" and parts[3] == "deletion":
                    return self._send(Api.request_deletion(src, pid, self._body()))
                if method == "GET" and parts[3] == "deletion" and len(parts) >= 5:
                    return self._send(Api.deletion_status(src, pid, parts[4]))
            self._send({"ok": False, "code": "NOT_FOUND",
                        "message": "未知端点 %s" % parsed.path}, 404)
        except Exception as e:  # noqa: BLE001
            self._send({"ok": False, "code": "BACKEND_ERROR",
                        "message": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8097)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("[✓] MedTalent 接入 API： http://127.0.0.1:%d" % a.port)
    print("    令牌：%s" % TOKEN)
    print("    仅监听回环；前端不直连数据库（契约 §1）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[=] 已停止")


if __name__ == "__main__":
    main()
