# -*- coding: utf-8 -*-
"""
ops/pii.py —— 加密个人身份信息（person_pii）的写入 / 读取 / **机制验证**

为什么必须有这个工具
--------------------------------------------------------------------------
独立审查指出 `person_pii` **0 行**，加密列机制"从未被真实数据验证过"。
核查后发现比"未验证"更严重：**它根本没有实现** ——
  · `person_pii` 有 `full_name_enc/phone_enc/email_enc/wechat_enc/id_hash/...`
    六个加密列 + `encryption_key_id`；
  · 但 `code/` 与 `ops/` 里**没有任何地方写它**，也没有密钥管理。
**一张存在但没人写、也没有密钥的表，比没有这张表更危险**：
它会让每个读 schema 的人（包括未来的你）以为"PII 是加密存储的"。

密钥纪律（这是本工具最重要的部分）
--------------------------------------------------------------------------
**密钥不存库**。把密钥和密文放在一起，加密就只是"看起来安全"。
  · 密钥来自环境变量 `MEDTALENT_PII_KEY`（由 `ops/env.py` 管理，与其它凭据同一纪律）；
  · 库里只存**密文**与 `encryption_key_id`（标识"用哪把钥匙"，不含钥匙本身）；
  · 工具**从不回显密钥**，也不把密钥写进任何表、日志、审计。

诚实地说明它**没有**解决的问题
--------------------------------------------------------------------------
  · 没有密钥轮换流程（`encryption_key_id` 留了位置，但换钥匙需要重加密全表）；
  · 没有 HSM/KMS：密钥在环境变量里，属"比明文好、不等于硬件保护"；
  · `id_hash` 用同一把密钥做 HMAC（用于去重、不可还原），
    换钥匙会让去重失效 —— 这需要与轮换一起设计。
这三条写在这里，而不是让人以为"加密做完了"。

用法
    python ops/pii.py status                    # 有多少人的 PII 在库、密钥是否已配置
    python ops/pii.py set --person per_x         # 从 stdin 读 JSON 写入（加密）
    python ops/pii.py show --person per_x        # 解密显示（需密钥；只给运维）
    python ops/pii.py verify                     # **端到端验证机制**：写入→确认落库是密文
                                                 # →读回一致→清理（自建自清，不留数据）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "ops"))

ADMIN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres connect_timeout=10 "
         "options='-c search_path=mt,public'")

# 明文 → 密文列的对应（列名以 *_enc 结尾是刻意的：让人一眼看出存的是密文）
ENC_COLS = {"full_name": "full_name_enc", "phone": "phone_enc", "email": "email_enc",
            "wechat": "wechat_enc", "emergency_contact": "emergency_contact_enc"}
# 明文 → 单向哈希列（不可还原，仅用于去重）
HASH_COLS = {"id_no": "id_hash"}


def key() -> str:
    """取密钥。**只从环境变量读**，绝不从库里读、绝不回显。"""
    import env as E
    E.load_into_environ()
    k = os.environ.get("MEDTALENT_PII_KEY")
    if not k:
        raise SystemExit(
            "[X] 没有配置 MEDTALENT_PII_KEY。\n"
            "    写入加密 PII 需要密钥；密钥**不存库**（存在一起等于没加密）。\n"
            "    配置：python ops\\env.py set MEDTALENT_PII_KEY --stdin")
    return k


def encrypt(c, plaintext: str, k: str) -> bytes:
    return c.execute("SELECT pgp_sym_encrypt(%s, %s)",
                     (plaintext, k)).fetchone()["pgp_sym_encrypt"]


def decrypt(c, blob, k: str):
    if blob is None:
        return None
    return c.execute("SELECT pgp_sym_decrypt(%s, %s)",
                     (bytes(blob), k)).fetchone()["pgp_sym_decrypt"]


def fingerprint(c, id_no: str, k: str) -> str:
    """证件号的单向指纹：能去重、不能还原。"""
    return c.execute("SELECT encode(hmac(%s, %s, 'sha256'), 'hex')",
                     (id_no, k)).fetchone()["encode"]


def cmd_status(a):
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        n_pii = c.execute("SELECT count(*) AS n FROM mt.person_pii").fetchone()["n"]
        n_person = c.execute("SELECT count(*) AS n FROM mt.person").fetchone()["n"]
        keys = [r["encryption_key_id"] for r in c.execute(
            "SELECT DISTINCT encryption_key_id FROM mt.person_pii")]
        has_key = bool(os.environ.get("MEDTALENT_PII_KEY"))
        try:
            key(); has_key = True
        except SystemExit:
            has_key = False
    print("person 总数            : %d" % n_person)
    print("person_pii 行数        : %d" % n_pii)
    print("用过的密钥标识          : %s" % ("、".join(keys) if keys else "（无）"))
    print("环境里有 MEDTALENT_PII_KEY: %s" % ("是" if has_key else "**否**"))
    print()
    if n_pii == 0:
        print("[!] person_pii 是空的 —— 加密列机制**尚未被真实数据验证**。")
        print("    一张存在但没人写、也没有密钥的表，比没有这张表更危险：")
        print("    它会让读 schema 的人以为「PII 是加密存储的」。")
    if not has_key:
        print("[!] 没有密钥：写入会拒绝（这是刻意的 —— 密钥不存库）。")
    return 0


def _upsert(c, person_id, payload, k, key_id):
    sets, params = [], []
    for plain_key, col in ENC_COLS.items():
        if plain_key in payload and payload[plain_key] is not None:
            sets.append("%s = pgp_sym_encrypt(%%s, %%s)" % col)
            params.extend([str(payload[plain_key]), k])
    for plain_key, col in HASH_COLS.items():
        if plain_key in payload and payload[plain_key] is not None:
            sets.append("%s = encode(hmac(%%s, %%s, 'sha256'), 'hex')" % col)
            params.extend([str(payload[plain_key]), k])
    if not sets:
        raise SystemExit("[X] 没有任何可写入的字段（见 ENC_COLS/HASH_COLS）")
    sets.append("encryption_key_id = %s")
    params.append(key_id)
    sets.append("updated_at = now()")
    params.append(person_id)
    c.execute("""INSERT INTO mt.person_pii (person_id, encryption_key_id)
                 VALUES (%s, %s) ON CONFLICT (person_id) DO NOTHING""", (person_id, key_id))
    c.execute("UPDATE mt.person_pii SET %s WHERE person_id = %%s" % ", ".join(sets), params)


def cmd_set(a):
    k = key()
    payload = json.loads(sys.stdin.read() or "{}")
    if not payload:
        raise SystemExit("[X] 从 stdin 读不到 JSON")
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        if not c.execute("SELECT 1 FROM mt.person WHERE person_id=%s",
                         (a.person,)).fetchone():
            raise SystemExit("[X] person 不存在：%s（person_pii 有外键）" % a.person)
        _upsert(c, a.person, payload, k, a.key_id)
        c.commit()
    print("[✓] 已加密写入 %s（字段：%s）；密钥标识 %s"
          % (a.person, "、".join(sorted(payload)), a.key_id))
    print("    库里存的是密文；明文**没有**落库。")
    return 0


def cmd_show(a):
    k = key()
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        row = c.execute("SELECT * FROM mt.person_pii WHERE person_id=%s",
                        (a.person,)).fetchone()
        if not row:
            raise SystemExit("[X] 没有这个人的 PII：%s" % a.person)
        out = {"person_id": row["person_id"],
               "encryption_key_id": row["encryption_key_id"],
               "id_hash": row["id_hash"],
               "updated_at": str(row["updated_at"])}
        for plain_key, col in ENC_COLS.items():
            out[plain_key] = decrypt(c, row[col], k)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("\n[!] 以上是**解密后**的明文。这次读取会留下审计记录；"
          "不要把输出贴到聊天/工单里。", file=sys.stderr)
    return 0


def cmd_verify(a):
    """端到端验证机制：写入 → 确认落库是密文 → 读回一致 → 清理。

    为什么"能写能读"还不够，必须单独验"落库是密文"：
    `pgp_sym_encrypt` 用错参数（例如把密钥当明文传）也能"成功"，
    读回来还可能"看起来对"。**唯一可信的证据是去库里看字节**：
    密文里不能出现明文，且必须能被正确解密。
    """
    k = key()
    probe_person = "per_pii_verify_probe"
    secret = {"full_name": "验证用姓名张某某", "phone": "13900000001",
              "email": "pii-verify@example.invalid", "id_no": "110101199001011234"}
    with psycopg.connect(ADMIN, row_factory=dict_row) as c:
        # 自建自清：先清干净（包括上次失败留下的）
        c.execute("DELETE FROM mt.person_pii WHERE person_id=%s", (probe_person,))
        c.execute("DELETE FROM mt.change_log WHERE detail->>'row' LIKE %s",
                  ("%person_id=" + probe_person + "%",))
        c.execute("DELETE FROM mt.person WHERE person_id=%s", (probe_person,))
        c.execute("""INSERT INTO mt.person (person_id, subject_code, enroll_channel,
                          verify_status, access_tier, status)
                     VALUES (%s, %s, 'EC3', 'V1', 'T1', 'active')""",
                  (probe_person, "MT-PIIVERIFY"))
        try:
            _upsert(c, probe_person, secret, k, a.key_id)
            c.commit()

            row = c.execute("SELECT * FROM mt.person_pii WHERE person_id=%s",
                            (probe_person,)).fetchone()

            # ① 落库是密文：明文绝不能出现在任何加密列里
            leaks = []
            for plain_key, col in ENC_COLS.items():
                blob = row[col]
                if blob is None:
                    continue
                raw = bytes(blob)
                if secret[plain_key].encode("utf-8") in raw:
                    leaks.append(col)
            print("① 密文里是否含明文：%s" % ("**泄露** " + "、".join(leaks) if leaks else "否 ✓"))

            # ② 密文应当是非空 BYTEA 且长度大于明文（有 PGP 头/校验）
            #    ⚠ 这里要遍历 ENC_COLS 的**值**（列名），不是键（明文名）——
            #    第一版写成 `for col in ENC_COLS` 迭代出 'full_name' 这样的明文名，
            #    直接 KeyError。同一个字典两种遍历，很容易写错。
            lens = {col: (len(bytes(row[col])) if row[col] else 0)
                    for col in ENC_COLS.values()}
            print("② 各加密列字节数：%s" % lens)

            # ③ 解密必须还原（只比对**加密列**（ENC_COLS）里实际写入的字段 ——
            #    没写的列本来就是 NULL；id_no 走单向哈希，不在可解密之列）
            enc_written = [pk for pk in secret if pk in ENC_COLS]
            back = {pk: decrypt(c, row[col], k) for pk, col in ENC_COLS.items()}
            ok_round = all(back[pk] == secret[pk] for pk in enc_written)
            check = {pk: back[pk] for pk in enc_written}
            print("③ 解密还原一致：%s" % ("是 ✓" if ok_round else "**否** " + str(check)))

            # ④ 证件号只存单向指纹，不可还原
            h = row["id_hash"]
            hash_ok = (h is not None and secret["id_no"] not in h
                       and len(h) == 64 and all(ch in "0123456789abcdef" for ch in h))
            print("④ 证件号是单向 HMAC 指纹（%s…，长度 %s）：%s"
                  % (h[:12] if h else "无", len(h) if h else 0,
                     "是 ✓" if hash_ok else "**否**"))

            # ⑤ 换一把错钥匙必须解不开（否则"加密"是装饰）
            with psycopg.connect(ADMIN, row_factory=dict_row) as c2:
                try:
                    wrong = c2.execute("SELECT pgp_sym_decrypt(%s, %s)",
                                       (bytes(row["full_name_enc"]), k + "-wrong")) \
                              .fetchone()["pgp_sym_decrypt"]
                    wrong_ok = (wrong != secret["full_name"])
                except psycopg.Error:
                    wrong_ok = True          # 解密报错同样说明"钥匙不对读不出"
            print("⑤ 错钥匙读不出明文：%s" % ("是 ✓" if wrong_ok else "**否**"))

            passed = (not leaks) and ok_round and hash_ok and wrong_ok
            print()
            print("结论：%s" % ("加密机制**验证通过**（密文落库、可解密、错钥匙读不出、"
                             "证件号不可还原）" if passed else "**验证失败**，见上面各条"))
        finally:
            # ⑥ 自建自清：验证数据不留库（含审计里的探测痕迹）
            c.execute("DELETE FROM mt.person_pii WHERE person_id=%s", (probe_person,))
            c.execute("DELETE FROM mt.change_log WHERE detail->>'row' LIKE %s",
                      ("%person_id=" + probe_person + "%",))
            c.execute("DELETE FROM mt.person WHERE person_id=%s", (probe_person,))
            c.commit()
            left = c.execute("SELECT count(*) AS n FROM mt.person_pii WHERE person_id=%s",
                             (probe_person,)).fetchone()["n"]
            leftp = c.execute("SELECT count(*) AS n FROM mt.person WHERE person_id=%s",
                              (probe_person,)).fetchone()["n"]
            print("⑥ 已清理验证数据（person_pii 残留 %d、person 残留 %d）" % (left, leftp))
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="加密 PII 的写入/读取/机制验证（密钥不存库）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("set")
    p.add_argument("--person", required=True)
    p.add_argument("--key-id", default="k1", help="密钥标识（写在 encryption_key_id 列）")
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("show")
    p.add_argument("--person", required=True)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("verify")
    p.add_argument("--key-id", default="k_verify")
    p.set_defaults(fn=cmd_verify)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
