# -*- coding: utf-8 -*-
"""
code/parse/jd_parser.py —— 招聘信息解析器 v1（T08，规则优先）

设计取舍：**规则优先，LLM 兜底**。规则能覆盖的（薪资/学历/城市/是否校招/职责与要求
分条）就先规则解决，因为规则可解释、可回归、零成本；模糊语义（"有经验者优先"该算
hard 还是 bonus）在模棱两可时**降低 parse_confidence，而不是硬判**（见 docs/03 §5）。

输入：L0 原始 HTML
输出：与 schema/examples/job_posting_example.json 同构的 dict

提取能力：
  · 岗位标题 / 雇主 / 城市 / 招聘人数 / 经验
  · 薪资：区间 / 单值 / 面议（面议置空 + quality_flag）
  · 学历：映射 CT_DEGREE_LEVEL 码
  · 职责条目（job_task）与要求条目（job_requirement），并按情态词切分 hard/soft/bonus
  · 要求文本 → 概念（concept）的关键词映射（映射不上则保留原文，不硬塞）
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from html.parser import HTMLParser

sys.stdout.reconfigure(encoding="utf-8")

PARSER_VERSION = "jd_parser@0.1"

# 学历表述 → CT_DEGREE_LEVEL 码（按"最高要求"优先匹配）
EDU_RULES = [
    (r"博士", "D4"), (r"硕士|研究生", "D3"), (r"本科|学士", "D2"), (r"大专|专科", "D1"),
]
# 薪资：形如 12000-18000元/月、300000-450000元/年、15000元/月、面议
SALARY_RANGE = re.compile(r"(\d[\d,]*)\s*[-~至]\s*(\d[\d,]*)\s*元?\s*/?\s*(月|年|天|小时)?")
SALARY_SINGLE = re.compile(r"(\d[\d,]*)\s*元\s*/?\s*(月|年|天|小时)?")
SALARY_NEGO = re.compile(r"面议|薪资面谈|待遇面议")
PERIOD = {"月": "SP1", "年": "SP2", "天": "SP3", "小时": "SP3"}
# 情态词：决定 requirement_kind
HARD_WORDS = ["必须", "需", "要求", "须", "持有", "具备", "已取得", "通过", "熟练"]
BONUS_WORDS = ["优先", "加分", "更佳", "者优先"]
SOFT_WORDS = ["能力", "沟通", "团队", "抗压", "细致", "热情", "思维", "意愿"]


class JDExtractor(HTMLParser):
    """轻量结构化抽取：按语义区块（class）归集文本与列表项。"""

    SECTION_CLASSES = {
        "job-duties": "duties",
        "job-requirements": "requirements",
        "job-extra": "extra",
        "job-meta": "meta",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.sections: dict[str, list[str]] = {"duties": [], "requirements": [], "extra": []}
        self._stack: list[tuple[str, str]] = []   # [(tag, semantc_block)] 便于正确配对出栈
        self._span: str | None = None
        self._in_title = False
        self._in_page_title = False
        self.page_title = ""
        self._buf: list[str] = []

    def _flush(self):
        txt = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        return txt

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").strip()
        # 语义区块既可能是 <section> 也可能是 <div>（真实站点两种都常见）
        if tag in ("section", "div") and cls in self.SECTION_CLASSES:
            self._stack.append((tag, self.SECTION_CLASSES[cls]))
            return
        if tag == "h1" and "job-title" in cls:
            self._in_title = True
            self._buf = []
            return
        if tag == "title":
            self._in_page_title = True
            self._buf = []
            return
        if tag == "span" and self._stack and self._stack[-1][1] == "meta":
            self._span = cls or "_"
            self._buf = []
            return
        if tag == "li" and self._stack and self._stack[-1][1] in ("duties", "requirements"):
            self._buf = []

    def handle_data(self, data):
        if self._in_title or self._in_page_title:
            self._buf.append(data)
        elif self._span:
            self._buf.append(data)
        elif self._stack and self._stack[-1][1] in ("duties", "requirements"):
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_page_title:
            self.page_title = self._flush() or self.page_title
            self._in_page_title = False
            return
        if tag == "h1" and self._in_title:
            self.title = self._flush() or self.title
            self._in_title = False
            return
        if tag == "span" and self._span:
            t = self._flush()
            if t:
                self.meta[self._span] = t
            self._span = None
            return
        if tag == "li" and self._stack and self._stack[-1][1] in ("duties", "requirements"):
            t = self._flush()
            if t:
                self.sections[self._stack[-1][1]].append(t)
            return
        # 用标签名配对出栈，避免非区块的 </div> 把语义区块弹掉
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()


def strip_text(html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def parse_salary(text: str) -> dict:
    """返回 salary_min/max/period/flags。面议 → 全空 + quality_flag。"""
    out = {"min": None, "max": None, "period": None, "flags": []}
    if not text:
        out["flags"].append("salary_missing")
        return out
    if SALARY_NEGO.search(text):
        out["flags"].append("salary_negotiable")
        return out
    m = SALARY_RANGE.search(text)
    if m:
        lo, hi, per = m.group(1), m.group(2), m.group(3) or "月"
        out["min"] = int(lo.replace(",", ""))
        out["max"] = int(hi.replace(",", ""))
        out["period"] = PERIOD.get(per, "SP1")
        return out
    m = SALARY_SINGLE.search(text)
    if m:
        out["min"] = out["max"] = int(m.group(1).replace(",", ""))
        out["period"] = PERIOD.get(m.group(2) or "月", "SP1")
        out["flags"].append("salary_single_value")
        return out
    out["flags"].append("salary_unparsed")
    return out


def parse_education(text: str) -> tuple[str | None, str | None]:
    for pat, code in EDU_RULES:
        if re.search(pat, text or ""):
            return code, None
    return None, "education_unparsed"


def classify_requirement(text: str, req_type: str = None) -> tuple[str, float]:
    """情态词 → (requirement_kind, 该判定的置信度)。
    模棱两可时给低置信度，而不是硬判 hard。

    领域规则：学历(RT1)/证照(RT4) 类要求在没有"优先/加分"字样时默认按硬性处理——
    因为招聘里"硕士及以上学历"几乎总是筛选项。这条规则让解析结果更贴近真实筛选逻辑。
    """
    hard = any(w in text for w in HARD_WORDS)
    bonus = any(w in text for w in BONUS_WORDS)
    soft = any(w in text for w in SOFT_WORDS)
    if bonus and not hard:
        return "RK3", 0.85
    if bonus and hard:
        return "RK2", 0.55          # "必须具备…，有…者优先" —— 模棱两可，降置信度
    if hard:
        return "RK1", 0.80
    if req_type in ("RT1", "RT4"):
        return "RK1", 0.75          # 学历/证照默认硬性
    if soft:
        return "RK2", 0.70
    return "RK2", 0.45              # 无任何情态词 → 默认软性但低置信度


def classify_req_type(text: str) -> str:
    if re.search(r"学历|学位|本科|硕士|博士|大专", text):
        return "RT1"
    if re.search(r"专业", text):
        return "RT2"
    if re.search(r"年经验|年以上|工作年限|工作经验|任职经验", text):
        return "RT3"
    # 培训/资历类也属"经历"而不是"能力"——否则会污染能力词表的候选池
    if re.search(r"培训|规培|规范化培训|转岗|进修|经历|资历", text):
        return "RT3"
    # 论文/专利/成果属于科研资历
    if re.search(r"论文|SCI|核心期刊|专利|发表|同行评议|期刊工作", text):
        return "RT3"
    if re.search(r"资格证|执业|证书|备案|GCP|考试|公开招聘|准入", text):
        return "RT4"
    if re.search(r"英语|英文|外语|CET|雅思|托福", text):
        return "RT7"
    if re.search(r"技能|熟练使用|掌握|能力|素养|意识", text):
        return "RT5"
    return "RT8"


def parse(html_text: str) -> dict:
    ex = JDExtractor()
    ex.feed(html_text)

    title = ex.title or ""
    # meta 值常带标签前缀（"学历：硕士及以上"），去掉前缀只留值
    meta = {k: re.sub(r"^(学历|经验|招聘|薪资|城市)[：:]\s*", "", v).strip()
            for k, v in ex.meta.items()}
    sal = parse_salary(meta.get("salary", ""))
    edu, edu_flag = parse_education(meta.get("edu", ""))

    flags = list(sal["flags"])
    if edu_flag:
        flags.append(edu_flag)
    if not ex.sections["requirements"]:
        flags.append("no_requirements_found")
    if not title:
        flags.append("no_title")

    city = meta.get("city", "") or None
    headcount = None
    m = re.search(r"(\d+)", meta.get("headcount", "") or "")
    if m:
        headcount = int(m.group(1))

    duties = [t for t in ex.sections["duties"] if len(t) >= 4]
    reqs = []
    for t in ex.sections["requirements"]:
        if len(t) < 4:
            continue
        rtype = classify_req_type(t)
        kind, conf = classify_requirement(t, rtype)
        reqs.append({
            "raw_text": t,
            "requirement_kind": kind,
            "requirement_type": rtype,
            "parse_confidence": conf,
        })

    # 整体解析置信度：结构越完整越高，缺失项扣分
    conf = 0.7
    conf -= 0.05 * len([f for f in flags if f.startswith("salary")])
    conf -= 0.10 * len([f for f in flags if f.startswith("education")])
    conf -= 0.15 if "no_requirements_found" in flags else 0
    conf -= 0.10 if "no_title" in flags else 0
    conf = max(0.3, min(0.95, round(conf, 2)))

    return {
        "title_raw": title,
        # 雇主常出现在 <title> 的 "岗位 - 雇主 - 招聘" 结构中
        "employer_name_raw": (ex.page_title.split(" - ")[1].strip()
                              if ex.page_title.count(" - ") >= 2 else None),
        "city": city,
        "salary_min": sal["min"],
        "salary_max": sal["max"],
        "salary_period": sal["period"],
        "education_req": edu,
        "experience_req": meta.get("exp") or None,
        "headcount": headcount,
        "duties": duties,
        "requirements": reqs,
        "quality_flags": flags,
        "confidence": conf,
        "plain_text": strip_text(html_text),
        "parse_version": PARSER_VERSION,
    }


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else None
    if not p:
        print("用法：python code/parse/jd_parser.py <html文件>")
        sys.exit(2)
    import json
    with open(p, encoding="utf-8") as fh:
        r = parse(fh.read())
    r.pop("plain_text", None)
    print(json.dumps(r, ensure_ascii=False, indent=2))
