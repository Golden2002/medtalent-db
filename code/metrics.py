# -*- coding: utf-8 -*-
"""
code/metrics.py —— 度量的**单一口径定义**（Semantic Layer 的代码侧）

为什么需要这个文件：

    门禁脚本 `jd_quality_gate.py`、只读门户 `/quality` 与 `/analyze/a3`、
    可视化面板 `m_gate`、以及文档里的数字，说的都是"能力概念映射覆盖率"。
    只要其中任何一处自己写一遍 SQL，就会出现**同一个指标两个值**——
    实测发生过：质量门 90.2%，可视化页 61.7%。差别只在分母：
    质量门用 `requirement_type IN ('RT5','RT6','RT7','RT8')`（只算"应当映射到
    能力概念"的要求），而可视化页写成 `NOT IN ('RT5','RT6')`，
    把 RT1/RT3/RT4 这些**资格门槛**（学历/经验/证照）也算了进去，分母被放大。

    在 BI 体系里这叫"语义层"：Superset 的 dataset/metric、dbt 的 metric、
    Metabase 的 Model，存在的唯一理由就是**让同一个指标只有一个定义**。

所以：谁要算这些指标，就 import 这里的 SQL。`ops/tests/viz_test.py` 里有一条
跨组件一致性断言——把质量门的输出数字和门户页面上的数字对比，不一致就失败。
口径漂移必须被测试抓住，而不是等客户发现两个页面数字不一样。
"""
from __future__ import annotations

# 应当映射到能力概念的要求类型：技能 / 知识 / 语言 / 其他
CONCEPT_TYPES = ("RT5", "RT6", "RT7", "RT8")
# 资格门槛类：学历 / 经验 / 证照——由专用字段承载，不参与能力映射率
QUALIFICATION_TYPES = ("RT1", "RT3", "RT4")

#  能力概念映射覆盖率
#  ---------------------------------------------------------------
# 返回 total / denom / numer / qualification 四个计数，由调用方算比率。
# 刻意返回计数而不是比率：比率一旦四舍五入，两个组件就可能"看起来不一样"，
# 而计数是精确的，能一眼看出差在哪一部分。
CONCEPT_COVERAGE_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE r.requirement_type = ANY(%(types)s)) AS denom,
       count(*) FILTER (WHERE r.requirement_type = ANY(%(types)s)
                          AND r.concept_id IS NOT NULL) AS numer,
       count(*) FILTER (WHERE r.requirement_type = ANY(%(quals)s)) AS qualification
  FROM job_requirement r
"""

COVERAGE_TARGET = 70.0          # %
REQUIRED_COMPLETE_TARGET = 99.0  # %
TRACEABLE_TARGET = 100.0         # %
FAMILY_TARGET = 17               # 岗位族数


def coverage_params() -> dict:
    return {"types": list(CONCEPT_TYPES), "quals": list(QUALIFICATION_TYPES)}


def coverage_pct(row) -> float:
    """从 CONCEPT_COVERAGE_SQL 的一行结果算百分比（分母为 0 时返回 0.0）。"""
    denom = row["denom"] or 0
    numer = row["numer"] or 0
    return round(100.0 * numer / denom, 1) if denom else 0.0


# 岗位侧质量门的四个指标：名称 → (SQL, 目标, 单位, higher_is_better)
GATE_SQL = """
SELECT '岗位族覆盖' AS "指标",
       count(DISTINCT jp.job_family)::numeric AS "实际", %(fam)s::numeric AS "目标"
  FROM job_posting jp
UNION ALL SELECT '必填字段完整率',
       round(100.0 * count(*) FILTER (WHERE jp.title_raw IS NOT NULL
                AND jp.raw_text_ref IS NOT NULL) / greatest(count(*),1), 1), %(req)s
  FROM job_posting jp
UNION ALL SELECT '原文可回溯率',
       round(100.0 * count(*) FILTER (WHERE jp.raw_sha256 IS NOT NULL)
             / greatest(count(*),1), 1), %(tra)s
  FROM job_posting jp
UNION ALL SELECT '能力概念映射覆盖率',
       round(100.0 * (SELECT count(*) FROM job_requirement r
                       WHERE r.requirement_type = ANY(%(types)s)
                         AND r.concept_id IS NOT NULL)
             / greatest((SELECT count(*) FROM job_requirement r
                          WHERE r.requirement_type = ANY(%(types)s)), 1), 1), %(cov)s
"""


def gate_params() -> dict:
    return {"fam": FAMILY_TARGET, "req": REQUIRED_COMPLETE_TARGET,
            "tra": TRACEABLE_TARGET, "cov": COVERAGE_TARGET,
            "types": list(CONCEPT_TYPES), "quals": list(QUALIFICATION_TYPES)}
