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


# ---------------------------------------------------------------------------
# 派生口径的实现（对应注册表里 `derived:` 开头的落点）
# ---------------------------------------------------------------------------
# 一条铁律：**能在数据字典里找到刻度的一律用词表刻度，找不到就不派生。**
#   · 经验档的边界写在 CT_EXPERIENCE_BAND 的标签里（1年以内 / 1-3年 / 3-5年 /
#     5-10年 / 10年以上）→ 派生规则去**读标签**，而不是把 1/3/5/10 硬编码进代码。
#     词表改了派生跟着改；代码里一旦出现第二套数字，就是第二个口径。
#   · 城市层级用 CT_CITY_TIER.external_mapping 里登记的城市清单（只登记已核实的
#     一线/新一线；二线及以下故意留空）。
#   · 找不到刻度的（如"哪些专业算临床类"）**不派生**，如实报缺什么。
#
# 另一个容易踩的地方：rank 用"按 sort_order 排序后的**序号**"（1 起），
# 而**不是** sort_order 本身。CT_EXPERIENCE_BAND 的 sort_order 是 10/20/30…，
# 直接当 rank 会让 cmp_ordinal 的"差一档给 0.5"永远不触发
# （它判的是 rank + 1 = min_rank）。
def _band_table(c, ctable):
    """把区间型词表解析成 [(code, lo, hi, rank)]。

    支持三种标签写法（实测存在的）：
      `1年以内` / `1-3年` / `5-10年` / `10年以上` / `应届/无经验` / `不限`
    解析不出来的档位（如"不限"）跳过：它不是一个区间，不该参与比较。
    """
    import re as _re
    cvals = code_values(c, ctable)
    items = sorted(cvals.items(), key=lambda kv: kv[1]["sort_order"])
    out = []
    for rank, (code, meta) in enumerate(items, 1):
        lab = meta["label_zh"] or ""
        lo = hi = None
        m = _re.search(r"(\d+)\s*[-~至]\s*(\d+)", lab)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
        elif _re.search(r"(\d+)\s*年以内", lab):
            lo, hi = 0.0, float(_re.search(r"(\d+)\s*年以内", lab).group(1))
        elif _re.search(r"(\d+)\s*年以上", lab):
            lo, hi = float(_re.search(r"(\d+)\s*年以上", lab).group(1)), None
        elif "应届" in lab or "无经验" in lab:
            lo, hi = 0.0, 0.0
        if lo is None:
            continue          # "不限"这类：不是区间，不参与比较
        out.append((code, lo, hi, rank))
    return out


def _band_of(value, bands):
    """数值 → 落在哪个档。区间是左闭右开（`1-3年` 含 1 不含 3），与标签的通常读法一致。"""
    if value is None:
        return None
    for code, lo, hi, rank in bands:
        if hi is None:
            if value >= lo:
                return code
        elif lo <= value < hi or (lo == hi == 0 and value == 0):
            return code
    # 落在最后一档之上（例如 10 年以上但词表只到 5-10 年）→ 取最高档，并让调用方知道是"截断"
    return bands[-1][0] if bands and value >= bands[-1][1 or 0] else None


def _rank_of_code(c, ctable, code):
    """码 → 该词表内按 sort_order 排序后的序号（1 起）。"""
    cvals = code_values(c, ctable)
    items = sorted(cvals.items(), key=lambda kv: kv[1]["sort_order"])
    for i, (cd, _meta) in enumerate(items, 1):
        if cd == code:
            return i
    return None


def _rank_from_text(c, ctable, raw_values):
    """原始值是**文本**（不是码）时，折算成序数 rank。两条路径，都要留痕：

      ① 码表标签匹配：`省重点` → ST5「省重点/部属」
      ② 区间文本解析：`1-3年` / `3年以上` / `应届` → EX 档
    为什么必须有这条路径：岗位侧的经验要求与偏好侧的强度要求都是**自由文本**，
    它们和码值维度一样在做 ordinal_ge 比较，但值不是码。没有这一步，
    `DIM_WORK_YEARS`（权重 1.0 的硬门槛）在岗位侧永远是 0 —— 门禁就成了摆设。
    匹配不上就返回 (None, None)，让调用方如实记"缺映射"，不猜。
    """
    texts = [str(v) for v in raw_values if v is not None]
    if not texts:
        return None, None
    # ① 标签匹配
    codes, _unmatched = text_to_codes(c, ctable, texts)
    if codes:
        rk = max(x for x in (_rank_of_code(c, ctable, x) for x in codes) if x is not None)
        return rk, "label_match"
    # ② 区间文本
    bands = _band_table(c, ctable)
    if not bands:
        return None, None
    best = None
    for s in texts:
        if "应届" in s or "无经验" in s:
            val = 0.0
        else:
            nums = re.findall(r"\d+(?:\.\d+)?", s)
            if not nums:
                continue
            val = min(float(x) for x in nums)   # 取下限："3-5年"意味着至少 3 年
        code = _band_of(val, bands)
        if code:
            r = _rank_of_code(c, ctable, code)
            best = r if best is None else max(best, r)
    return (best, "band_from_text") if best is not None else (None, None)


def _emp_months(c, pid):
    """任职总月数。end_date 为空表示至今 → 用当前日期。"""
    rows = c.execute("""SELECT start_date, end_date FROM mt.employment_record
                         WHERE person_id=%s AND start_date IS NOT NULL""", (pid,)).fetchall()
    total = 0.0
    for r in rows:
        end = r["end_date"]
        if end is None:
            end = c.execute("SELECT current_date AS d").fetchone()["d"]
        total += (end - r["start_date"]).days / 30.4375
    return total, len(rows)


# ---- 人侧派生 ----
def _p_work_years(c, pid, d):
    months, n = _emp_months(c, pid)
    if not n:
        return None, "有落点但该主体没有值（无任职记录）"
    years = months / 12.0
    bands = _band_table(c, "CT_EXPERIENCE_BAND")
    code = _band_of(years, bands)
    if not code:
        return None, R_NEEDS_MAP
    return ({"rank": _rank_of_code(c, "CT_EXPERIENCE_BAND", code)},
            "由 %d 段任职起止日期求和得 %.1f 年 → %s（档位边界取自 CT_EXPERIENCE_BAND 标签）"
            % (n, years, code))


def _p_city_tier(c, pid, d):
    """期望城市 → 城市层级。期望城市取 PF1 的码（CT_CITY），层级取 CT_CITY_TIER.external_mapping。"""
    rows = c.execute("""SELECT value_code, value_raw FROM mt.preference
                         WHERE person_id=%s AND pref_type='PF1'""", (pid,)).fetchall()
    if not rows:
        return None, "有落点但该主体没有值（未填 PF1 期望城市）"
    city = None
    cvals_city = code_values(c, "CT_CITY")
    for r in rows:
        v = (r["value_code"] or r["value_raw"] or "").strip()
        if v in cvals_city:
            city = v
            break
        for code, meta in cvals_city.items():
            if meta["label_zh"] and meta["label_zh"] == v:
                city = code
                break
        if city:
            break
    if not city:
        return None, R_NEEDS_MAP
    for tier_code, meta in code_values(c, "CT_CITY_TIER").items():
        cities = (meta["external_mapping"] or {}).get("cities") or []
        if city in cities:
            return ({"rank": _rank_of_code(c, "CT_CITY_TIER", tier_code)},
                    "期望城市 %s 属于 %s（映射来自 CT_CITY_TIER.external_mapping）"
                    % (city, tier_code))
    return None, "该城市未登记层级映射（二线及以下未登记，见 CT_CITY_TIER 的 note）"


def _p_school_tier(c, pid, d):
    """院校标签 → 院校层次码。school_tags 里存的是中文标签（如「省重点」）。"""
    rows = c.execute("""SELECT DISTINCT unnest(school_tags) AS t FROM mt.education_record
                         WHERE person_id=%s""", (pid,)).fetchall()
    tags = [r["t"] for r in rows if r["t"]]
    if not tags:
        return None, "有落点但该主体没有值（无院校标签）"
    codes, unmatched = text_to_codes(c, "CT_SCHOOL_TIER", tags)
    if not codes:
        return None, R_NEEDS_MAP
    best = max(codes, key=lambda x: -_rank_of_code(c, "CT_SCHOOL_TIER", x))
    return ({"rank": _rank_of_code(c, "CT_SCHOOL_TIER", best)},
            "院校标签 %s → %s" % ("、".join(tags[:3]), best))


def _p_research_level(c, pid, d):
    """科研层级：按**作者位次与产出类型**判，标签本身写的就是这些角色。

    RL5 专利/成果转化 > RL3 通讯或主持 > RL2 第一作者 > RL1 参与 > RL0 无科研参与。
    注意：只认学术产出（论著/综述/病例/Meta/专利），**文学与科普作品不算科研**
    —— 这条区分是必要的，否则一本畅销书会被算成"主持课题"。
    """
    rows = c.execute("""SELECT output_type, author_position, is_first_author
                          FROM mt.research_output WHERE person_id=%s""", (pid,)).fetchall()
    if not rows:
        return None, "有落点但该主体没有值（无产出记录）"
    academic = {"RO1", "RO2", "RO3", "RO4"}
    lvl = "RL0"
    for r in rows:
        if r["output_type"] == "RO5":
            lvl = "RL5"
            break
        pos = (r["author_position"] or "")
        if "通讯" in pos or "主持" in pos or "corresponding" in pos.lower():
            lvl = max(lvl, "RL3")
        elif r["is_first_author"] and lvl < "RL2":
            lvl = "RL2"
        elif r["output_type"] in academic and lvl < "RL1":
            lvl = "RL1"
    return ({"rank": _rank_of_code(c, "CT_RESEARCH_LEVEL", lvl)},
            "由 %d 条产出的作者位次判定为 %s（文学/科普作品不计入科研）" % (len(rows), lvl))


# ---- 岗位侧派生 ----
def _j_work_years(c, job_id, d, jp):
    """经验要求文本 → 经验档。取**下限**："3-5年"意味着至少 3 年，"应届"是 0。"""
    raw = (jp.get("experience_req") or "").strip()
    if not raw:
        return None, R_NO_VALUE
    import re as _re
    if "应届" in raw or "无经验" in raw:
        years = 0.0
    else:
        nums = [float(x) for x in _re.findall(r"\d+", raw)]
        if not nums:
            return None, R_NEEDS_MAP
        years = min(nums)
    bands = _band_table(c, "CT_EXPERIENCE_BAND")
    code = _band_of(years, bands)
    if not code:
        return None, R_NEEDS_MAP
    return ({"min_rank": _rank_of_code(c, "CT_EXPERIENCE_BAND", code)},
            "经验要求「%s」按下限 %.1f 年 → %s" % (raw, years, code))


def _j_city_tier(c, job_id, d, jp):
    city_raw = (jp.get("city") or "").strip()
    if not city_raw:
        return None, R_NO_VALUE
    city = None
    for code, meta in code_values(c, "CT_CITY").items():
        if not code.startswith("CTY"):
            continue
        if meta["label_zh"] == city_raw:
            city = code
            break
    if not city:
        return None, R_NEEDS_MAP
    for tier_code, meta in code_values(c, "CT_CITY_TIER").items():
        if city in ((meta["external_mapping"] or {}).get("cities") or []):
            return ({"min_rank": _rank_of_code(c, "CT_CITY_TIER", tier_code)},
                    "岗位城市 %s 属于 %s" % (city_raw, tier_code))
    return None, "该城市未登记层级映射"


# 岗位侧单位类型：语料把雇主匿名化成「某三甲医院」「某CRO公司」这类描述，
# 所以只能按关键词判。**这条规则依赖于本语料的匿名化命名约定**，不是通用方法 ——
# 换成真实 JD（有真实企业名）时应当替换为雇主主数据匹配，这一点写在这里免得被误用。
EMPLOYER_RULES = [
    ("三甲医院", "E01"), ("三级医院", "E01"), ("三甲", "E01"),
    ("二级医院", "E02"), ("社区卫生", "E02"), ("基层医疗", "E02"),
    ("民营医院", "E03"), ("民营医疗", "E03"), ("门诊", "E03"), ("诊所", "E03"),
    ("跨国制药", "E04"), ("外资药", "E04"),
    ("创新药企", "E05"), ("本土药企", "E05"), ("制药", "E05"),
    ("医疗器械", "E06"), ("IVD", "E06"), ("器械", "E06"),
    ("CRO", "E07"), ("SMO", "E07"),
    ("生物制药", "E08"), ("生物技术", "E08"), ("基因", "E08"),
    ("健康险", "E09"), ("保险", "E09"), ("TPA", "E09"),
    ("咨询", "E10"), ("管理顾问", "E10"),
    ("证券", "E11"), ("基金", "E11"), ("投资", "E11"),
    ("事业单位", "E12"), ("疾控", "E12"), ("卫健委", "E12"),
    ("高校", "E13"), ("医学院", "E13"), ("研究院", "E13"), ("科研院所", "E13"),
    ("互联网", "E14"), ("数字健康", "E14"), ("医疗人工智能", "E14"), ("健康内容", "E14"),
    ("出版", "E15"), ("媒体", "E15"), ("期刊", "E15"),
    ("律所", "E16"), ("知识产权", "E16"),
    ("教育", "E17"), ("培训", "E17"),
]


def _j_employer_type(c, job_id, d, jp):
    name = (jp.get("employer_name_raw") or "").strip()
    if not name:
        return None, R_NO_VALUE
    for kw, code in EMPLOYER_RULES:
        if kw.lower() in name.lower():
            return ({"codes": [code]}, "雇主描述「%s」按关键词规则判为 %s" % (name, code))
    return None, R_NEEDS_MAP


def _j_accept_cross(c, job_id, d, jp):
    """跨行岗位：职业族 F16 是"完全跨行"的那一族（口径来自注册表 note）。"""
    fam = (jp.get("job_family") or "").strip()
    if not fam:
        return None, R_NO_VALUE
    return ({"code": "Y" if fam == "F16" else "N"},
            "岗位族 %s %s F16（完全跨行）" % (fam, "=" if fam == "F16" else "≠"))


DERIVE_PERSON = {
    "DIM_WORK_YEARS": _p_work_years,
    "DIM_CITY_TIER": _p_city_tier,
    "DIM_SCHOOL_TIER": _p_school_tier,
    "DIM_RESEARCH_LEVEL": _p_research_level,
}
DERIVE_JOB = {
    "DIM_WORK_YEARS": _j_work_years,
    "DIM_CITY_TIER": _j_city_tier,
    "DIM_EMPLOYER_TYPE": _j_employer_type,
    "DIM_ACCEPT_CROSS_INDUSTRY": _j_accept_cross,
}

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

    两种输入都要处理：
      · 标量/文本：`'a'`、`'{a,b}'`（text[] 的文本形态）
      · 列表：每一行一个值 —— 但**列表里的元素本身也可能是 `{a,b}` 形态**
        （查询写的是 `SELECT DISTINCT col::text`，数组列出来就是 `{a,b}`）。
        第一版在列表分支里没有再拆一层，于是 `school_tags = {双一流,985}` 变成
        一个元素 `'{双一流,985}'`，标签匹配全灭 —— 这正是 DIM_SCHOOL_TIER 一直是
        0/123 的原因。**列表不等于"已经拆好了"**。
    空数组 '{}' 化成 None：因为"没有值"和"要求为空集"语义完全不同
    （后者在 cmp_set 里返回 NULL → unknown）。
    """
    if v is None:
        return None
    out = []
    items = v if isinstance(v, (list, tuple, set)) else [v]
    for item in items:
        if item is None:
            continue
        s = str(item).strip()
        if s in ("", "{}", "[]"):
            continue
        if s.startswith("{") and s.endswith("}"):
            out.extend(p.strip().strip('"') for p in s[1:-1].split(",") if p.strip())
        else:
            out.append(s)
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
            # 派生口径：实现了的走规则，没实现的如实报"未实现"。
            # 不把"没实现"和"没值"混为一谈 —— 前者要补规则，后者要补数据。
            fn = DERIVE_PERSON.get(did)
            if fn is None:
                reason = R_DERIVED
            else:
                pay, note = fn(c, pid, d)
                if pay:
                    payload[did] = pay
                    source = "derived: " + note
                else:
                    reason = note or R_DERIVED
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
                    # 值不是码（校园标签、经验要求这类自由文本）→ 走标签/区间归一化。
                    # 这一步是"权重 1.0 的门禁能不能评"的关键，见 _rank_from_text 的说明。
                    rk, how = _rank_from_text(c, d["code_table_id"], codes)
                    if rk is not None:
                        source = "%s（%s）" % (source, how)
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
                     "reason": reason if did in payload else (reason or R_NO_VALUE),
                     "n_values": len(codes or [])}
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
            fn = DERIVE_JOB.get(did)
            if fn is None:
                reason = R_DERIVED
            else:
                pay, note = fn(c, job_id, d, jp)
                if pay:
                    payload[did] = pay
                    source = "derived: " + note
                else:
                    reason = note or R_DERIVED
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
                    # 岗位侧序数要求常常是自由文本（`1-3年`），必须走同一条归一化路径，
                    # 否则 DIM_WORK_YEARS 在岗位侧永远是 0，权重 1.0 的门禁形同虚设。
                    rk, how = _rank_from_text(c, d["code_table_id"], codes)
                    if rk is not None:
                        source = "%s（%s）" % (source, how)
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
                     # 不变量：没进 payload 就必须有原因。少一个原因，报告里就会
                     # 出现"既没值又没说为什么"的洞，而那正是最容易被忽略的失真的来源。
                     "reason": reason if did in payload else (reason or R_NO_VALUE),
                     "n_values": len(codes)}
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
