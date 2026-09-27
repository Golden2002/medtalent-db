# -*- coding: utf-8 -*-
"""
code/analytics/vector.py —— 画像向量组装层 + 逐维度匹配打分

补的是哪个缺口（docs/14 §"已知缺口"第一条：向量组装层未产品化）
--------------------------------------------------------------------------
库里已经有两样东西：
  · `mt.dimension`        50 个维度的**匹配语义**（kind / comparator / role / weight）
                          以及每个维度在人侧、岗位侧的**落点**（person_locator / job_locator）
  · `mt.score_dimensions` 定义了怎么比（enum_eq / set_overlap / ordinal_ge / range_overlap），
                          入参是 {dimension_id: 载荷} 的 JSONB
缺的是中间那一步：**把一个人的类型化行读成一个向量**。

为什么库里的 `mt.profile_json` 顶不上：
  它只读 `field_value`（长尾扩展表，当前 0 行）。而主数据（教育/工作/偏好/能力）
  都在类型化表里 —— 于是它组装出来的是**空向量**，50 个维度全部落进 unknown，
  匹配结果不可解释。这个坑不写下来，下一个人还会去调它。

本模块做什么：
  1. 按 `dimension.person_locator` / `job_locator` 的形态分派，取出原始值；
  2. 按 `comparator` 组装成 cmp_* 需要的载荷键（code / text / codes / rank / min_rank / lo / hi）；
  3. 调用 `mt.score_dimensions` 打分，并**保留每个维度的取数来源与失败原因**。

一条纪律：**取不到值就记 unknown，不臆造。**
  · `derived:` 开头的落点是"派生口径"，规则没写进库 → 一律 unknown，并写明原因；
  · 岗位侧 `unavailable` → 该维度不出现在岗位向量里 → score_dimensions 记 unknown；
  · 文本要求归一化不上就**不放载荷**（→ unknown），绝不放一个空数组假装"要求为空"。
  这三种情况在报告里要能分开数出来，因为它们代表三种不同的缺法。

用法：
    python code/analytics/vector.py --coverage           # 每个维度在两侧各能组装出多少人的值
    python code/analytics/vector.py --match per_mock_0001 [--top 10]
    python code/analytics/vector.py --selftest
"""
from __future__ import annotations

import argparse
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "connect_timeout=5 options='-c search_path=mt,public'")

# 组装不出来的原因分三类。分开计数是刻意的：它们代表三种不同的缺法，
# 混成一个"未知"数就没法判断该补数据、补口径还是补字段。
R_DERIVED = "derived 派生口径未实现"
R_NO_LANDING = "该侧无落点（注册表标 unavailable）"
R_LOCATOR = "locator 形态无法解析"
R_NO_VALUE = "有落点但该主体没有值"
R_NO_FK = "来源表没有指向主体的外键"
R_NEEDS_MAP = "原始值到码值缺映射规则"

_fk_cache: dict = {}
_cv_cache: dict = {}


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


# ---------------------------------------------------------------------------
# 码表辅助
# ---------------------------------------------------------------------------
def code_values(c, table_id):
    """码表 → {code: {label, sort_order, external_mapping}}（进程内缓存）。"""
    if table_id in _cv_cache:
        return _cv_cache[table_id]
    out = {}
    if table_id:
        for r in c.execute("SELECT code, label_zh, sort_order, external_mapping "
                           "FROM mt.code_value WHERE code_table_id=%s", (table_id,)):
            out[r["code"]] = r
    _cv_cache[table_id] = out
    return out


def fk_to_parent(c, table, parent="person"):
    """找 table 上指向 parent 的外键列名。不假设它一定叫 person_id。

    踩过的坑：写死 `person_id` 在 person_demographics 上碰巧对，
    但在别的表上会静默查出 0 行 —— 而 0 行看起来和"这个人没填"一模一样。
    """
    key = (table, parent)
    if key in _fk_cache:
        return _fk_cache[key]
    rows = c.execute("""
        SELECT a.attname AS col, cl2.relname AS ref
        FROM pg_constraint con
        JOIN pg_class cl  ON cl.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        JOIN pg_class cl2 ON cl2.oid = con.confrelid
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
        WHERE con.contype='f' AND n.nspname='mt' AND cl.relname=%s
          AND cl2.relname=%s
    """, (table, parent)).fetchall()
    _fk_cache[key] = rows[0]["col"] if rows else None
    return _fk_cache[key]


# ---------------------------------------------------------------------------
# 载荷规范化
# ---------------------------------------------------------------------------
def _as_codes(v, ctable):
    """把原始值拆成码数组。

    text[] 的文本形态是 '{a,b}'；单值就是它自己。空数组 '{}' 要化成 None，
    因为"没有值"和"要求为空集"在语义上完全不同（后者在 cmp_set 里返回 NULL → unknown）。
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        out = [str(x) for x in v if x not in (None, "")]
    else:
        s = str(v).strip()
        if s in ("", "{}", "[]"):
            return None
        if s.startswith("{") and s.endswith("}"):
            out = [p.strip().strip('"') for p in s[1:-1].split(",") if p.strip()]
        else:
            out = [s]
    return out or None


def _rank_of(v, ctable, cvals):
    """序数维度的 rank 从哪来。三条规则，第三条是**文档化的近似**，不是真理。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in cvals:                      # ① 值本身是码 → 用码表的 sort_order 当序
        return cvals[s]["sort_order"]
    if re.fullmatch(r"-?\d+", s):       # ② 值本身就是序数（如 skill_assertion.level 1..5）
        return int(s)
    return None                          # ③ 其余不给猜（如 birth_year、procedure_count）


def text_to_codes(c, ctable, texts):
    """把岗位侧的自由文本归一化成码值（专业/证照这类要求就写在 raw_text 里）。

    做法：码表标签在文本里出现即命中。**这是一个保守的包含匹配，不是语义解析**：
      · 匹配不上就**不产生码**（→ 该维度 unknown），不猜；
      · 命中的记下来，报告里给出"归一化了多少条 / 还剩多少条没归一化"，
        因为"文本没归一化"和"要求为空"是两件事。
    返回 (codes, unmatched_texts)。
    """
    cvals = code_values(c, ctable)
    codes, unmatched = [], []
    for t in texts or []:
        s = str(t)
        hit = []
        for code, meta in cvals.items():
            lab = meta["label_zh"]
            if lab and (lab in s or (len(s) >= 2 and s in lab)):
                hit.append(code)
        if hit:
            for h in hit:
                if h not in codes:
                    codes.append(h)
        else:
            unmatched.append(s)
    return codes, unmatched


def _parse_range(v):
    """区间值 → (lo, hi)。PF4 的形状实测是 'monthly:12500-18000' / '12500-18000 元/月'。"""
    if v is None:
        return None, None
    s = str(v)
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    if len(nums) == 1:
        return float(nums[0]), float(nums[0])
    return None, None


# ---------------------------------------------------------------------------
# 人侧组装
# ---------------------------------------------------------------------------
def load_dims(c, only_active=True):
    sql = "SELECT * FROM mt.dimension"
    if only_active:
        sql += " WHERE status='active'"
    return c.execute(sql + " ORDER BY group_id, sort_order").fetchall()


def person_payloads(c, pid, dims):
    """人侧向量：{dimension_id: 载荷} + 每个维度的取数溯源。"""
    payload, prov = {}, {}
    for d in dims:
        did, cmp_ = d["dimension_id"], d["comparator"]
        loc = (d["person_locator"] or "").strip()
        vals, codes, reason, source = [], [], None, loc

        if not loc or loc == "unavailable":
            reason = R_NO_LANDING
        elif loc.startswith("derived:"):
            reason = R_DERIVED
        elif "." in loc:
            # `表.列` 形态，可选带值过滤：`credential.credential_type:C02,C03`。
            # 顺序很重要：必须先判 '.' 再判 ':'，否则 `表.列:过滤` 会被当成
            # `表.列` 是"head"的冒号形态 → 解析不出任何东西，静默变成无值。
            # 过滤后缀是**必要**的：证照与规培都落在 credential.credential_type 一列上，
            # 不区分就变成同一份数据被两个维度各计一次权重（实测 Jaccard = 1.000）。
            spec, _, flt = loc.partition(":")
            tbl, col = (x.strip() for x in spec.split(".", 1))
            keep = [x.strip() for x in flt.split(",") if x.strip()]
            fk = fk_to_parent(c, tbl)
            if not fk:
                reason = R_NO_FK
            else:
                try:
                    sql = ("SELECT DISTINCT {col}::text AS v FROM mt.{tbl} "
                           "WHERE {fk}=%s AND {col} IS NOT NULL").format(
                               col=col, tbl=tbl, fk=fk)
                    params = [pid]
                    if keep:
                        sql += " AND {col}::text = ANY(%s)".format(col=col)
                        params.append(keep)
                    rows = c.execute(sql, params).fetchall()
                except psycopg.Error:
                    rows, reason = [], R_LOCATOR
                if reason is None:
                    raw = [r["v"] for r in rows]
                    if not raw:
                        reason = R_NO_VALUE
                    else:
                        codes = _as_codes(raw, d["code_table_id"])
        elif ":" in loc:
            head, arg = loc.split(":", 1)
            if head == "preference":
                rows = c.execute(
                    "SELECT coalesce(value_code, value_raw) AS v, value_code, value_raw "
                    "FROM mt.preference WHERE person_id=%s AND pref_type=%s",
                    (pid, arg)).fetchall()
                if not rows:
                    reason = R_NO_VALUE
                elif cmp_ == "range_overlap":
                    lo, hi = _parse_range(rows[0]["value_raw"] or rows[0]["value_code"])
                    if lo is None:
                        reason = R_NEEDS_MAP
                    else:
                        payload[did] = {"lo": lo, "hi": hi}
                else:
                    codes = [r["v"] for r in rows if r["v"] not in (None, "")]
            elif head == "concept":
                # concept:K1 / K3 —— 概念 ID 自带类别前缀（CON-K1-*、CON-K3-*），
                # 这是本库的命名约定，不是巧合；用它过滤比再加一张表更诚实。
                rows = c.execute(
                    "SELECT DISTINCT concept_id AS v FROM mt.skill_assertion "
                    "WHERE person_id=%s AND concept_id LIKE %s",
                    (pid, "CON-" + arg + "-%")).fetchall()
                if not rows:
                    reason = R_NO_VALUE
                else:
                    codes = [r["v"] for r in rows]
            elif head == "credential":
                rows = c.execute(
                    "SELECT count(*) AS n FROM mt.credential "
                    "WHERE person_id=%s AND credential_type=%s", (pid, arg)).fetchone()
                if not rows or not rows["n"]:
                    reason = R_NO_VALUE
                elif cmp_ == "ordinal_ge":
                    # 需要"语言等级"这个序数，但 credential 表没有等级列（docs/14 已记）
                    reason = "有证照但无等级列（人侧缺字段）"
                else:
                    codes = [arg]
            else:
                reason = R_LOCATOR
        else:
            reason = R_LOCATOR

        if reason is None and codes:
            cvals = code_values(c, d["code_table_id"])
            if cmp_ == "set_overlap":
                payload[did] = {"codes": codes}
            elif cmp_ == "enum_eq":
                # 布尔列 ::text 是 'true'/'false'，而词表可能是 CT_YES_NO(Y/N)
                one = codes[0]
                if one in ("true", "false") and "Y" in cvals:
                    one = "Y" if one == "true" else "N"
                payload[did] = {"code": one}
            elif cmp_ == "ordinal_ge":
                rk = max([x for x in (_rank_of(v, d["code_table_id"], cvals)
                                      for v in codes) if x is not None] or [None])
                if rk is None:
                    reason = R_NEEDS_MAP
                else:
                    payload[did] = {"rank": rk}
            elif cmp_ == "range_overlap":
                lo, hi = _parse_range(codes[0])
                if lo is None:
                    reason = R_NEEDS_MAP
                else:
                    payload[did] = {"lo": lo, "hi": hi}
        prov[did] = {"side": "person", "locator": source, "status":
                     "ok" if did in payload else "missing",
                     "reason": reason, "n_values": len(codes or [])}
    return payload, prov


# ---------------------------------------------------------------------------
# 岗位侧组装
# ---------------------------------------------------------------------------
def job_payloads(c, job_id, dims):
    payload, prov = {}, {}
    jp = c.execute("SELECT * FROM job_posting WHERE job_id=%s", (job_id,)).fetchone()
    if not jp:
        return payload, prov
    for d in dims:
        did, cmp_ = d["dimension_id"], d["comparator"]
        loc = (d["job_locator"] or "").strip()
        reason, codes, source = None, [], loc

        if not loc or loc == "unavailable":
            reason = R_NO_LANDING
        elif loc.startswith("derived:"):
            reason = R_DERIVED
        elif loc.startswith("job_posting."):
            col = loc.split(".", 1)[1]
            v = jp.get(col)
            if v in (None, "", []):
                reason = R_NO_VALUE
            else:
                codes = _as_codes(v, d["code_table_id"]) or []
                # 岗位族 → 兴趣/职业目标码：注册表写明要经 CT_INTEREST_DOMAIN.external_mapping 反查
                if did in ("DIM_INTEREST_DOMAIN", "DIM_CAREER_GOAL") and d["code_table_id"]:
                    fam = codes[0] if codes else None
                    mapped = [code for code, meta in code_values(c, d["code_table_id"]).items()
                              if fam and meta["external_mapping"]
                              and fam in str(meta["external_mapping"])]
                    if mapped:
                        codes = mapped
                    else:
                        # job_family 有值但没有反查映射 → 不假装匹配
                        reason = R_NEEDS_MAP
        elif loc.startswith("job_requirement:"):
            spec = loc.split(":", 1)[1]
            rtype, _, col = spec.partition(".")
            col = col or "concept_id"
            rows = c.execute(
                "SELECT {col}::text AS v FROM mt.job_requirement "
                "WHERE job_id=%s AND requirement_type=%s AND {col} IS NOT NULL".format(
                    col=col), (job_id, rtype)).fetchall()
            raw = [r["v"] for r in rows if r["v"] not in (None, "")]
            if not raw:
                reason = R_NO_VALUE
            elif col == "raw_text" and cmp_ == "set_overlap":
                # 要求写在自由文本里 → 用码表标签做保守归一化；匹配不上就是 unknown
                codes, unmatched = text_to_codes(c, d["code_table_id"], raw)
                if not codes:
                    reason = R_NEEDS_MAP
            elif cmp_ == "ordinal_ge":
                # 岗位侧的序数要求（min_level、job_zone 这类）可能只带列名不带类型。
                # 把裸列名也算进来，否则这种 locator 会静默变成"无值也无原因"。
                vals = [int(x) for x in raw if re.fullmatch(r"-?\d+", x)]
                if vals:
                    payload[did] = {"min_rank": min(vals)}
                else:
                    reason = R_NEEDS_MAP
            else:
                codes = raw
        elif loc.startswith("job_requirement."):
            # locator 写成 `job_requirement.min_level`（没有 RTn 段）：
            # 跨全部要求类型取该列。这种写法真实存在于注册表里，
            # 漏掉它会让这个维度既不报值也不报原因 —— 自检第一条就抓到过。
            col = loc.split(".", 1)[1]
            rows = c.execute(
                "SELECT {col}::text AS v FROM mt.job_requirement "
                "WHERE job_id=%s AND {col} IS NOT NULL".format(col=col),
                (job_id,)).fetchall()
            raw = [r["v"] for r in rows if r["v"] not in (None, "")]
            if not raw:
                reason = R_NO_VALUE
            elif cmp_ == "ordinal_ge":
                vals = [int(x) for x in raw if re.fullmatch(r"-?\d+", x)]
                if vals:
                    payload[did] = {"min_rank": min(vals)}
                else:
                    reason = R_NEEDS_MAP
            else:
                codes = raw

        if reason is None and codes:
            cvals = code_values(c, d["code_table_id"])
            if cmp_ == "set_overlap":
                payload[did] = {"codes": codes}
            elif cmp_ == "enum_eq":
                one = codes[0]
                if one in ("true", "false") and "Y" in cvals:
                    one = "Y" if one == "true" else "N"
                payload[did] = {"code": one, "codes": codes}
            elif cmp_ == "ordinal_ge":
                rk = max([x for x in (_rank_of(v, d["code_table_id"], cvals)
                                      for v in codes) if x is not None] or [None])
                if rk is None:
                    reason = R_NEEDS_MAP
                else:
                    payload[did] = {"min_rank": rk}
            elif cmp_ == "range_overlap":
                lo = jp.get("salary_min")
                hi = jp.get("salary_max")
                if lo is None or hi is None:
                    reason = R_NO_VALUE
                else:
                    payload[did] = {"lo": float(lo), "hi": float(hi)}
        elif reason is None and cmp_ == "range_overlap":
            lo = jp.get("salary_min")
            hi = jp.get("salary_max")
            if lo is not None and hi is not None:
                payload[did] = {"lo": float(lo), "hi": float(hi)}
            else:
                reason = R_NO_VALUE

        prov[did] = {"side": "job", "locator": source,
                     "status": "ok" if did in payload else "missing",
                     "reason": reason, "n_values": len(codes)}
    return payload, prov


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def score(c, pid, job_id, dims=None):
    """逐维度打分。返回 (summary, rows, prov_person, prov_job)。"""
    import json
    dims = dims if dims is not None else load_dims(c)
    pv, pp = person_payloads(c, pid, dims)
    jv, jp = job_payloads(c, job_id, dims)
    rows = c.execute(
        "SELECT * FROM mt.score_dimensions(%s::jsonb, %s::jsonb)",
        (json.dumps(pv), json.dumps(jv))).fetchall()
    summ = c.execute(
        "SELECT * FROM mt.score_summary(%s::jsonb, %s::jsonb)",
        (json.dumps(pv), json.dumps(jv))).fetchone()
    return summ, rows, pp, jp


def job_vectors_all(c, dims=None):
    """一次性把全部岗位向量算好：{job_id: 载荷}。

    为什么必须先算好再逐人打分：岗位向量**与人是无关的**。
    原来每给一个人排名就重新组装 690 次岗位向量，120 个人就是 8 万次重复查询。
    实测：120 人 × 690 岗位从"跑不完"降到十几秒。
    """
    dims = dims if dims is not None else load_dims(c)
    out = {}
    for j in c.execute("SELECT job_id FROM mt.job_posting ORDER BY job_id"):
        jv, _ = job_payloads(c, j["job_id"], dims)
        out[j["job_id"]] = jv
    return out


def rank_jobs(c, pid, limit=10, dims=None, jobvecs=None, jobs=None):
    """给一个人排出最匹配的岗位。返回按总分降序的列表。"""
    import json
    dims = dims if dims is not None else load_dims(c)
    jobvecs = jobvecs if jobvecs is not None else job_vectors_all(c, dims)
    pv, _ = person_payloads(c, pid, dims)
    pjson = json.dumps(pv)
    if jobs is None:
        jobs = c.execute("SELECT job_id, title_raw, occupation_id, city, salary_min, "
                         "salary_max FROM mt.job_posting ORDER BY job_id").fetchall()
    out = []
    for j in jobs:
        jv = jobvecs.get(j["job_id"], {})
        s = c.execute("SELECT * FROM mt.score_summary(%s::jsonb, %s::jsonb)",
                      (pjson, json.dumps(jv))).fetchone()
        out.append({"job_id": j["job_id"], "title": j["title_raw"],
                    "occupation_id": j["occupation_id"], "city": j["city"],
                    "score": float(s["score_total"]) if s["score_total"] is not None else None,
                    "coverage": float(s["coverage"]) if s["coverage"] is not None else None,
                    "gate_failed": s["gate_failed"],
                    "blocked_by": s["blocked_by"],
                    "evaluable": s["evaluable"], "unknown": s["unknown_dimensions"]})
    out.sort(key=lambda x: (-(x["score"] or 0), x["job_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# 覆盖率体检
# ---------------------------------------------------------------------------
def coverage(c, dims=None, persons=None):
    """逐维度统计两侧能组装出值的比例。这是"维度是否足够"最硬的证据。"""
    dims = dims if dims is not None else load_dims(c)
    persons = persons or [r["person_id"] for r in
                          c.execute("SELECT person_id FROM mt.person ORDER BY person_id")]
    jobs = [r["job_id"] for r in
            c.execute("SELECT job_id FROM mt.job_posting ORDER BY job_id")]
    stats = {d["dimension_id"]: {"dimension_id": d["dimension_id"],
                                 "group_id": d["group_id"], "title": d["title_zh"],
                                 "kind": d["kind"], "role": d["role"],
                                 "weight": float(d["weight"]),
                                 "person_ok": 0, "job_ok": 0,
                                 "person_reason": {}, "job_reason": {}} for d in dims}
    for pid in persons:
        pv, prov = person_payloads(c, pid, dims)
        for did, info in prov.items():
            if info["status"] == "ok":
                stats[did]["person_ok"] += 1
            else:
                r = info["reason"] or "?"
                stats[did]["person_reason"][r] = stats[did]["person_reason"].get(r, 0) + 1
    for jid in jobs:
        jv, prov = job_payloads(c, jid, dims)
        for did, info in prov.items():
            if info["status"] == "ok":
                stats[did]["job_ok"] += 1
            else:
                r = info["reason"] or "?"
                stats[did]["job_reason"][r] = stats[did]["job_reason"].get(r, 0) + 1
    return {"n_person": len(persons), "n_job": len(jobs),
            "dims": sorted(stats.values(), key=lambda x: (x["group_id"], x["dimension_id"]))}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_coverage(a):
    with connect() as c:
        cov = coverage(c)
    print("人才 %d 人 / 岗位 %d 个" % (cov["n_person"], cov["n_job"]))
    print("%-26s %-4s %-4s %-6s %8s %8s  %s"
          % ("dimension_id", "组", "kind", "role", "人侧", "岗位侧", "主要缺因"))
    two = 0
    for d in cov["dims"]:
        pr = max(d["person_reason"].items(), key=lambda x: x[1])[0] if d["person_reason"] else "-"
        jr = max(d["job_reason"].items(), key=lambda x: x[1])[0] if d["job_reason"] else "-"
        if d["person_ok"] and d["job_ok"]:
            two += 1
        print("%-26s %-4s %-4s %-6s %5d/%-3d %5d/%-3d  %s | %s"
              % (d["dimension_id"], d["group_id"], d["kind"], d["role"],
                 d["person_ok"], cov["n_person"], d["job_ok"], cov["n_job"], pr, jr))
    print("-" * 110)
    print("两侧都能组装出值的维度：%d / %d" % (two, len(cov["dims"])))
    return 0


def cmd_match(a):
    with connect() as c:
        dims = load_dims(c)
        rows = rank_jobs(c, a.match, limit=a.top, dims=dims)
        print("人：%s   top %d" % (a.match, len(rows)))
        for r in rows:
            print("  #%-3d %-8s score=%-7s cov=%-6s 门槛未过=%-2s %s"
                  % (r["rank"], r["job_id"],
                     ("%.3f" % r["score"]) if r["score"] is not None else "NULL",
                     ("%.2f" % r["coverage"]) if r["coverage"] is not None else "NULL",
                     r["gate_failed"], r["title"]))
        if rows:
            s, det, pp, jp = score(c, a.match, rows[0]["job_id"], dims)
            print("\n第一名（%s）的逐维度拆解：" % rows[0]["job_id"])
            for d in det:
                print("  %-6s %-26s w=%-5s %-8s %s"
                      % (d["status"], d["dimension_id"], d["weight"],
                         "-" if d["score"] is None else "%.2f" % float(d["score"]),
                         d["reason"]))
    return 0


def cmd_selftest(a):
    """自检：组装层必须能对每个维度给出"有值"或"有原因"，不允许两者皆无。"""
    bad = []
    with connect() as c:
        dims = load_dims(c)
        pid = c.execute("SELECT person_id FROM mt.person ORDER BY person_id LIMIT 1").fetchone()
        pid = pid["person_id"] if pid else None
        jid = c.execute("SELECT job_id FROM mt.job_posting ORDER BY job_id LIMIT 1").fetchone()
        jid = jid["job_id"] if jid else None
        if not pid or not jid:
            print("[!] 库里没有人才或岗位，无法自检")
            return 2
        pv, pp = person_payloads(c, pid, dims)
        jv, jp = job_payloads(c, jid, dims)
        for d in dims:
            did = d["dimension_id"]
            for side, prov in (("person", pp), ("job", jp)):
                st = prov.get(did) or {}
                if st.get("status") == "ok" and did not in (pv if side == "person" else jv):
                    bad.append("%s/%s 标了 ok 但载荷缺失" % (side, did))
                if st.get("status") != "ok" and not st.get("reason"):
                    bad.append("%s/%s 没值也没原因" % (side, did))
        print("维度 %d 个；人侧载荷 %d，岗位侧载荷 %d" % (len(dims), len(pv), len(jv)))
        s, rows, _, _ = score(c, pid, jid, dims)
        print("样例打分：总分 %s，可评维度 %d，未知 %d，门槛未过 %d"
              % (s["score_total"], s["evaluable"], s["unknown_dimensions"], s["gate_failed"]))
        n_ok = sum(1 for r in rows if r["status"] in ("met", "partial", "gap", "blocked"))
        if n_ok != s["evaluable"]:
            bad.append("可评维度数 %d 与逐维度明细里的 %d 不一致" % (s["evaluable"], n_ok))
    for b in bad:
        print("  [FAIL] " + b)
    print("自检%s" % ("通过" if not bad else "失败 %d 项" % len(bad)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="画像向量组装与匹配打分")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--match", metavar="PERSON_ID")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return cmd_selftest(a)
    if a.match:
        return cmd_match(a)
    return cmd_coverage(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
