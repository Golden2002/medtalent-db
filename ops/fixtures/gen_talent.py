# -*- coding: utf-8 -*-
"""
ops/fixtures/gen_talent.py —— 生成 mock 人才档案（**合成数据，不是真实个人**）

为什么需要它
------------
岗位侧已经有 690 条 job_posting / 2970 条 job_requirement（见 build_job_side.py），
人才侧却是空的：v_skill_supply 全 0，"技能供给 / 缺口分析 / 匹配"这些在线分析看不到东西。
本脚本按**真实表结构**、走**正常写入路径**（触发器、语义 CHECK、属性门禁一律不绕过），
造一批结构可控的合成档案：

  · 学历配额硬约束：硕士 60% / 博士 22.5% / 本科 17.5%
  · 12 个专业方向：临床 / 基础 / 药学 / 临床药学 / 公卫 / 生统 / 生医工 / 护理 /
    口腔 / 中医 / 影像 / 麻醉（覆盖 concept 表中 ≥25 个不同概念）
  · 技能分布**按方向差异化**：临床方向偏 CON-K1-CLIN，生统方向偏 CON-K1-STAT/SQL，
    这样 v_skill_supply 与缺口分析才看得出结构
  · level_basis（LB1–LB6）与 evidence.cel_level（E0–E4）按 docs/04 的口径联动：
    证书→E1/LB3、论文→E3/LB4、履职记录→E4/LB6、自述→E0/LB1
  · 约 25% 的人的岗位族偏好**故意与其技能方向不一致**，供缺口分析出内容

数据纪律（合规）
----------------
· 所有 person_id 以 `per_mock_` 开头；从属行 id 由各自前缀 + per_mock 编号拼成，
  仅凭"关联到 per_mock_% 的人"即可识别与清除（ops/fixtures/reset_talent.py）。
· **不生成真实人名、电话、邮箱**：不写 person_pii，档案里没有任何自然人标识。
· 机构名一律带"（虚构机构/企业）"后缀；期刊/会议用"合成示例…（虚构）"。
· DOI 用 10.99999/mock-* 合成号段，URI 用 `.invalid` 保留域——**永不解析到真实来源**。
  （真实院校名只在学历字段出现：学历口径需要它，且院校本身不是隐私。）
· 幂等：ID 由（前缀, 序号）决定，所有 INSERT 走 ON CONFLICT DO NOTHING；
  同 seed 重复执行不会产生重复人，行数不变。

用法
----
  python ops/fixtures/gen_talent.py                      # 120 份，seed 42
  python ops/fixtures/gen_talent.py --count 120 --seed 42
  python ops/fixtures/gen_talent.py --reset              # 先清 mock 人才，再生成
  python ops/fixtures/reset_talent.py                    # 只清 mock 人才

注意：ID 只由序号决定，所以"换 seed 但不清库"不会改写已有的人——要换 seed 请配 `--reset`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sys

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public'")

MOCK_PREFIX = "per_mock_"
SRC_ID = "src_fixture_talent_mock"
RUN_ID = "run_fixture_talent_mock"

# 生成基准日：固定下来才能保证"同 seed 同结果"；DB 里的 fixture 时点是 2026-09
ANCHOR = dt.date(2026, 9, 30)

# ===========================================================================
# 1. 静态码表 / 领域数据（全部取自 mt.code_value 与 mt.concept 的真实取值）
# ===========================================================================

# ---- 可迁移性（CT_TRANSFERABILITY X0–X4）+ 迁移说明（docs/04 §3 的对照表口径）----
# 说明文字必须在 transferability >= 3 时给出（chk_assert_transfer_note）
TRANSFER = {
    # K1 技能
    "CON-K1-ANNOT": (2, "标注规范与质控流程可迁移至数据运营与AI训练数据管理"),
    "CON-K1-CLAIM": (2, "医学合理性判断可迁移至保险风控与核保理算，规则需再学"),
    "CON-K1-CLIN": (1, "临床诊疗依赖执业授权与科室场景，离开临床需大量再培训与再认证"),
    "CON-K1-CLINREQ": (3, "临床工作流理解可迁移至医疗产品需求定义（docs/04 §3 对照表）"),
    "CON-K1-DEV": (4, "编程与算法能力跨行业通用（科研统计→数据分析）"),
    "CON-K1-DEVICE": (2, "器械原理与注册知识可迁移至器械厂商与检测机构，需按品类再学"),
    "CON-K1-EDIT": (3, "稿件审校与规范把控可迁移至出版与内容质量岗（病历书写→结构化文档）"),
    "CON-K1-EPI": (3, "现场调查与监测方法可迁移至公共卫生与真实世界研究"),
    "CON-K1-EXP": (3, "实验设计与转化研究能力可迁移至药企研发与CRO"),
    "CON-K1-FIN": (4, "财务建模能力跨行业通用"),
    "CON-K1-GCP": (3, "GCP 流程与合规要求可迁移至 CRO 与药企临床运营（项目管理与流程合规）"),
    "CON-K1-HEOR": (3, "证据与支付方逻辑可迁移至市场准入与支付政策岗"),
    "CON-K1-INTL": (4, "外语工作能力跨场景通用"),
    "CON-K1-IP": (3, "专利撰写与检索能力可迁移至知识产权服务机构"),
    "CON-K1-LIT": (4, "循证医学/文献检索迁移为信息检索与证据分级（docs/04 §3 对照表）"),
    "CON-K1-MKT": (3, "行业与市场知识可迁移至咨询、投资与商务岗"),
    "CON-K1-OP": (4, "运营与增长方法跨行业通用"),
    "CON-K1-PROD": (4, "需求管理与产品方法跨行业通用"),
    "CON-K1-QC": (3, "质量体系与合规方法可迁移至药企与器械质量岗"),
    "CON-K1-REG": (2, "注册法规知识需按目标市场与产品线再学习"),
    "CON-K1-RWE": (3, "真实世界研究方法可迁移至药企医学事务与卫生技术评估"),
    "CON-K1-SALES": (3, "学术推广与客户沟通能力可迁移至商务岗"),
    "CON-K1-SQL": (4, "SQL 与数据工具跨行业通用"),
    "CON-K1-STAT": (4, "科研统计/R/SPSS 迁移为数据分析与统计建模（docs/04 §3 对照表）"),
    "CON-K1-TEACH": (4, "带教实习生迁移为培训与知识传递（docs/04 §3 对照表）"),
    "CON-K1-UNDW": (2, "核保规则与风险判断需按险种与条款再学习"),
    "CON-K1-WRITE": (4, "专业写作能力跨行业通用"),
    # K3 能力
    "CON-K3-ACOM": (4, "论文汇报与教学查房迁移为公众表达与演示（docs/04 §3 对照表）"),
    "CON-K3-BIZ": (4, "商业敏感度跨行业通用"),
    "CON-K3-COLLAB": (3, "科室协作/会诊迁移为跨职能协作与多方协调（docs/04 §3 对照表）"),
    "CON-K3-COMM": (3, "医患/家属沟通迁移为客户沟通与冲突处理（docs/04 §3 对照表）"),
    "CON-K3-EMPATHY": (4, "共情与用户理解跨场景通用"),
    "CON-K3-LEAD": (4, "MDT 病例讨论迁移为跨部门会议主持与共识推进（docs/04 §3 对照表）"),
    "CON-K3-LEARN": (4, "快速学习能力跨行业通用"),
    "CON-K3-NEGO": (4, "谈判与影响能力跨行业通用"),
    "CON-K3-OWNER": (4, "主人翁意识跨行业通用"),
    "CON-K3-PRIOR": (4, "优先级管理跨行业通用"),
    "CON-K3-RIGOR": (4, "严谨与细致迁移为数据口径与规范执行（docs/04 §3 对照表）"),
    "CON-K3-SOLVE": (4, "问题解决能力跨行业通用"),
    "CON-K3-STRESS": (3, "夜班/值班迁移为抗压与轮班执行（docs/04 §3 对照表）"),
}

# 每个方向都会带一点通用能力，避免"某方向的人完全没有软技能"
GLOBAL_K3 = {
    "CON-K3-LEARN": 2, "CON-K3-SOLVE": 2, "CON-K3-PRIOR": 2,
    "CON-K3-OWNER": 1, "CON-K3-RIGOR": 2, "CON-K3-COLLAB": 2,
}

# ---- 证据类型 → CEL 等级 → 可采信度（docs/04 §4 口径）----
EV_KIND = {
    "self":     dict(evidence_type="EV7", cel="E0", verifiability=0, access_tier="T2"),
    "cert":     dict(evidence_type="EV1", cel="E1", verifiability=5, access_tier="T0"),
    "assess":   dict(evidence_type="EV6", cel="E2", verifiability=3, access_tier="T2"),
    "paper":    dict(evidence_type="EV2", cel="E3", verifiability=5, access_tier="T0"),
    "deliver":  dict(evidence_type="EV3", cel="E3", verifiability=3, access_tier="T2"),
    "project":  dict(evidence_type="EV4", cel="E4", verifiability=2, access_tier="T2"),
    "clinical": dict(evidence_type="EV8", cel="E4", verifiability=2, access_tier="T2"),
    "peer":     dict(evidence_type="EV5", cel="E2", verifiability=4, access_tier="T2"),
}

# level_basis 对应的置信度上限（docs/04 §2 置信度规则表，取各项最小值）
CONF = {"LB3": 0.90, "LB4_DOI": 0.85, "LB4": 0.75, "LB6": 0.70, "LB2": 0.80,
        "LB5": 0.80, "LB1": 0.45}

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉", "西安",
          "苏州", "天津", "重庆", "郑州", "长沙", "青岛", "合肥", "沈阳", "济南"]

SCHOOL_TOP = ["北京大学医学部", "复旦大学上海医学院", "上海交通大学医学院", "浙江大学医学院",
              "中山大学中山医学院", "四川大学华西医学中心", "华中科技大学同济医学院",
              "中南大学湘雅医学院", "首都医科大学", "南京医科大学"]
SCHOOL_MID = ["中国医科大学", "哈尔滨医科大学", "山东大学齐鲁医学院", "武汉大学医学部",
              "西安交通大学医学部", "吉林大学白求恩医学部", "天津医科大学", "南方医科大学",
              "重庆医科大学", "郑州大学医学院", "同济大学医学院", "厦门大学医学院"]
SCHOOL_LOW = ["河北医科大学", "山西医科大学", "温州医科大学", "安徽医科大学", "福建医科大学",
              "昆明医科大学", "贵州医科大学", "徐州医科大学", "大连医科大学", "广西医科大学",
              "宁夏医科大学", "新疆医科大学"]

DEPARTMENTS = {
    "clin": ["心血管内科", "呼吸与危重症医学科", "消化内科", "神经内科", "普通外科",
             "骨科", "儿科", "急诊科"],
    "nurse": ["内科病区", "外科病区", "重症监护室", "急诊科", "手术室", "儿科病区"],
    "pharm": ["药剂科", "临床药理研究室", "药物分析实验室", "制剂室"],
    "clinpharm": ["临床药学室", "药剂科", "药物咨询门诊", "I期临床试验病房"],
    "prev": ["传染病防制科", "慢性病防制科", "免疫规划科", "卫生监测科"],
    "biostat": ["统计教研室", "临床研究数据中心", "生物统计部"],
    "basmed": ["病理学系", "生理学系", "免疫学实验室", "分子生物学实验室", "细胞生物学实验室"],
    "bme": ["医学工程科", "影像工程实验室", "生物材料实验室", "医疗器械研发部"],
    "tcm": ["中医内科", "针灸科", "推拿科", "中西医结合科"],
    "oral": ["口腔内科", "口腔颌面外科", "口腔修复科", "正畸科"],
    "imag": ["放射科", "超声医学科", "核医学科", "介入放射科"],
    "anes": ["麻醉科", "疼痛科", "重症医学科", "手术室"],
}

CLIN_SKILLS = {
    "clin": ["病史采集", "体格检查", "病历书写", "常见病诊疗", "危急值处理", "医患沟通"],
    "nurse": ["静脉输液", "生命体征监测", "护理评估", "导管护理", "健康宣教"],
    "pharm": ["处方审核", "药物浓度监测", "制剂配制", "药物信息检索"],
    "clinpharm": ["用药方案评估", "治疗药物监测", "用药宣教", "药学查房"],
    "prev": ["现场流行病学调查", "传染病报告", "免疫规划实施", "卫生统计报表"],
    "biostat": ["临床试验统计", "数据清洗", "生存分析", "统计报表"],
    "basmed": ["细胞培养", "Western blot", "动物实验", "流式细胞术", "PCR"],
    "bme": ["医疗器械临床评价", "设备质控", "生物力学测试", "信号处理"],
    "tcm": ["脉诊", "针灸操作", "方剂辨证", "推拿手法"],
    "oral": ["口腔检查", "牙体预备", "根管治疗", "拔牙术"],
    "imag": ["影像阅片", "超声扫查", "造影操作", "影像报告书写"],
    "anes": ["气管插管", "椎管内麻醉", "术中生命体征管理", "疼痛评估"],
}

# ---- 12 个专业方向：配额按 120 份设计，其它 count 按比例折算 ----
# skills = 该方向的能力分布（权重），families = 现实中最可能的目标岗位族
DIRECTIONS = [
    dict(key="clin", label="临床医学", major_code="M10001", major_raw="临床医学",
         five_year=True, clinical=True, share=26, phd=0.35, bach=0.30,
         families=["F01", "F04", "F10"],
         sig=["CON-K1-CLIN", "CON-K1-LIT"],
         skills={"CON-K1-CLIN": 12, "CON-K1-LIT": 5, "CON-K1-TEACH": 3, "CON-K1-WRITE": 3,
                 "CON-K1-GCP": 2, "CON-K3-COMM": 4, "CON-K3-STRESS": 4, "CON-K3-RIGOR": 3,
                 "CON-K3-EMPATHY": 3, "CON-K3-ACOM": 2, "CON-K1-REG": 1,
                 "CON-K3-LEAD": 2, "CON-K1-EDIT": 1},
         job=dict(etype="E01", occs=["OCC-F01-01-01", "OCC-F01-01-02", "OCC-F01-01-04"],
                  titles=["住院医师", "内科住院医师", "主治医师"],
                  employer="合成示例·三级甲等综合医院%02d（虚构机构）",
                  duties=["在上级医师指导下完成本专业常见病诊疗",
                          "规范书写病历并参与科室查房",
                          "参与急危重症患者的抢救与转运",
                          "完成规培要求的轮转与考核"])),
    dict(key="nurse", label="护理学", major_code="M600", major_raw="护理学",
         five_year=False, clinical=True, share=12, phd=0.08, bach=0.70,
         families=["F01", "F15", "F04"],
         sig=["CON-K1-CLIN", "CON-K3-EMPATHY"],
         skills={"CON-K1-CLIN": 10, "CON-K3-COMM": 5, "CON-K3-EMPATHY": 5,
                 "CON-K3-RIGOR": 4, "CON-K3-STRESS": 4, "CON-K1-GCP": 3,
                 "CON-K1-TEACH": 2, "CON-K1-WRITE": 2, "CON-K3-PRIOR": 2},
         job=dict(etype="E01", occs=["OCC-F15-02-05", "OCC-F01-02"],
                  titles=["临床护士", "病区护士", "临床研究护士"],
                  employer="合成示例·三级甲等综合医院%02d（虚构机构）",
                  duties=["执行医嘱并完成各项护理操作",
                          "观察患者病情变化并及时上报",
                          "开展患者与家属健康宣教",
                          "规范书写护理记录"])),
    dict(key="pharm", label="药学", major_code="M30001", major_raw="药学",
         five_year=False, clinical=False, share=12, phd=0.45, bach=0.20,
         families=["F02", "F05", "F14"],
         sig=["CON-K1-EXP", "CON-K1-QC", "CON-K1-REG"],
         skills={"CON-K1-EXP": 8, "CON-K1-QC": 6, "CON-K1-REG": 6, "CON-K1-LIT": 4,
                 "CON-K1-GCP": 4, "CON-K1-WRITE": 3, "CON-K3-RIGOR": 4, "CON-K1-RWE": 3,
                 "CON-K1-STAT": 2, "CON-K1-HEOR": 2, "CON-K1-PROD": 1, "CON-K1-INTL": 2,
                 "CON-K1-MKT": 2, "CON-K1-SALES": 2, "CON-K3-BIZ": 2, "CON-K3-NEGO": 1},
         job=dict(etype="E05", occs=["OCC-F02-03-01", "OCC-F02-01-01", "OCC-F02-02-01"],
                  titles=["药品注册专员", "制剂研究员", "医学科学联络官"],
                  employer="合成示例·制药企业%02d（虚构企业）",
                  duties=["整理并撰写注册申报资料",
                          "跟踪审评进度并回复补充资料要求",
                          "参与工艺与质量研究",
                          "维护注册法规数据库"])),
    dict(key="clinpharm", label="临床药学", major_code="M30002", major_raw="临床药学",
         five_year=True, clinical=True, share=6, phd=0.25, bach=0.30,
         families=["F02", "F01", "F04"],
         sig=["CON-K1-CLIN", "CON-K1-REG"],
         skills={"CON-K1-CLIN": 7, "CON-K1-REG": 5, "CON-K1-GCP": 5, "CON-K1-RWE": 4,
                 "CON-K1-QC": 4, "CON-K1-LIT": 4, "CON-K3-COMM": 3, "CON-K3-RIGOR": 3,
                 "CON-K1-WRITE": 2},
         job=dict(etype="E01", occs=["OCC-F01-02", "OCC-F02-02-01", "OCC-F15-02-06"],
                  titles=["临床药师", "临床监查员", "药事服务药师"],
                  employer="合成示例·三级甲等综合医院%02d（虚构机构）",
                  duties=["参与药学查房并给出用药建议",
                          "开展治疗药物监测与剂量调整",
                          "审核处方与医嘱合理性",
                          "开展患者用药教育"])),
    dict(key="prev", label="预防医学", major_code="M40001", major_raw="预防医学",
         five_year=True, clinical=False, share=10, phd=0.55, bach=0.25,
         families=["F09", "F04", "F02"],
         sig=["CON-K1-EPI", "CON-K1-STAT"],
         skills={"CON-K1-EPI": 10, "CON-K1-STAT": 6, "CON-K1-SQL": 4, "CON-K1-WRITE": 4,
                 "CON-K3-COMM": 3, "CON-K3-COLLAB": 3, "CON-K1-TEACH": 3,
                 "CON-K3-RIGOR": 3, "CON-K1-OP": 1, "CON-K1-EDIT": 1, "CON-K3-LEAD": 1},
         job=dict(etype="E12", occs=["OCC-F09-01-02", "OCC-F09-02-01", "OCC-F04-01-01"],
                  titles=["疾控中心技术岗", "公共卫生医师", "临床监查员"],
                  employer="合成示例·市疾病预防控制中心%02d（虚构机构）",
                  duties=["开展传染病监测与流行病学调查",
                          "实施免疫规划与健康干预",
                          "撰写疫情分析与风险评估报告",
                          "参与突发公共卫生事件处置"])),
    dict(key="biostat", label="卫生统计与流行病学", major_code="M40002",
         major_raw="卫生统计与流行病学", five_year=False, clinical=False,
         share=8, phd=0.80, bach=0.10,
         families=["F04", "F08", "F16"],
         sig=["CON-K1-STAT", "CON-K1-SQL"],
         skills={"CON-K1-STAT": 12, "CON-K1-SQL": 8, "CON-K1-EPI": 7, "CON-K1-RWE": 5,
                 "CON-K1-DEV": 4, "CON-K1-WRITE": 4, "CON-K1-LIT": 3, "CON-K3-RIGOR": 3,
                 "CON-K1-PROD": 2, "CON-K1-MKT": 1, "CON-K1-INTL": 2, "CON-K1-EDIT": 1},
         job=dict(etype="E07", occs=["OCC-F02-02-04", "OCC-F04-02-02", "OCC-F16-01-02"],
                  titles=["生物统计师", "临床数据管理员", "数据分析师"],
                  employer="合成示例·临床研究服务公司%02d（虚构企业）",
                  duties=["撰写统计分析计划并执行分析",
                          "清洗与核查临床试验数据",
                          "产出统计报表与图表",
                          "支持真实世界研究的数据方案设计"])),
    dict(key="basmed", label="基础医学", major_code="M20001", major_raw="基础医学",
         five_year=False, clinical=False, share=12, phd=0.90, bach=0.05,
         families=["F05", "F10", "F02"],
         sig=["CON-K1-EXP", "CON-K1-LIT"],
         skills={"CON-K1-EXP": 12, "CON-K1-LIT": 6, "CON-K1-WRITE": 5, "CON-K1-STAT": 4,
                 "CON-K3-RIGOR": 4, "CON-K1-DEV": 2, "CON-K1-IP": 2, "CON-K1-TEACH": 2,
                 "CON-K1-INTL": 2, "CON-K1-EDIT": 2, "CON-K3-LEAD": 1},
         job=dict(etype="E13", occs=["OCC-F05-01-01", "OCC-F05-01-02", "OCC-F10-01-03"],
                  titles=["研发科学家", "转化医学研究员", "科研助理"],
                  employer="合成示例·高校医学院课题组%02d（虚构机构）",
                  duties=["开展靶点验证与生物标志物研究",
                          "设计并执行体内外药效实验",
                          "分析组学数据并撰写研究报告",
                          "参与课题组平台建设与研究生指导"])),
    dict(key="bme", label="生物医学工程", major_code="M20002", major_raw="生物医学工程",
         five_year=False, clinical=False, share=8, phd=0.50, bach=0.15,
         families=["F03", "F08", "F05"],
         sig=["CON-K1-DEVICE", "CON-K1-DEV"],
         skills={"CON-K1-DEVICE": 10, "CON-K1-DEV": 7, "CON-K1-CLINREQ": 6,
                 "CON-K1-STAT": 4, "CON-K1-PROD": 4, "CON-K1-LIT": 3, "CON-K1-IP": 2,
                 "CON-K1-QC": 2, "CON-K3-SOLVE": 3, "CON-K1-REG": 2,
                 "CON-K1-INTL": 2, "CON-K3-BIZ": 1},
         job=dict(etype="E06", occs=["OCC-F03-01-01", "OCC-F03-02-02", "OCC-F08-02-01"],
                  titles=["临床评价工程师", "研发工程师", "医疗算法工程师"],
                  employer="合成示例·医疗器械企业%02d（虚构企业）",
                  duties=["撰写医疗器械临床评价报告",
                          "检索并评价同品种临床数据",
                          "支持注册申报中的临床部分",
                          "参与产品需求定义与验证方案设计"])),
    dict(key="tcm", label="中医学", major_code="M500", major_raw="中医学",
         five_year=True, clinical=True, share=8, phd=0.25, bach=0.30,
         families=["F01", "F15", "F13"],
         sig=["CON-K1-CLIN", "CON-K1-WRITE"],
         skills={"CON-K1-CLIN": 11, "CON-K3-COMM": 4, "CON-K1-WRITE": 3,
                 "CON-K1-TEACH": 3, "CON-K1-LIT": 3, "CON-K3-EMPATHY": 3,
                 "CON-K3-RIGOR": 3, "CON-K3-LEARN": 3},
         job=dict(etype="E01", occs=["OCC-F01-01-01", "OCC-F01-03-03", "OCC-F15-01-03"],
                  titles=["中医内科医师", "中医科医师", "康复治疗师"],
                  employer="合成示例·三级甲等中医医院%02d（虚构机构）",
                  duties=["运用中医辨证方法诊治常见病",
                          "开展针灸推拿等中医特色治疗",
                          "书写中医病历与门诊记录",
                          "参与中医健康宣教"])),
    dict(key="oral", label="口腔医学", major_code="M800", major_raw="口腔医学",
         five_year=True, clinical=True, share=7, phd=0.20, bach=0.35,
         families=["F01", "F03", "F15"],
         sig=["CON-K1-CLIN", "CON-K1-DEVICE"],
         skills={"CON-K1-CLIN": 11, "CON-K1-DEVICE": 5, "CON-K3-RIGOR": 4,
                 "CON-K3-COMM": 3, "CON-K1-WRITE": 2, "CON-K1-TEACH": 2,
                 "CON-K3-EMPATHY": 2, "CON-K3-SOLVE": 2},
         job=dict(etype="E01", occs=["OCC-F01-01-01", "OCC-F01-03-01", "OCC-F01-03-03"],
                  titles=["口腔科医师", "口腔内科医师", "口腔修复医师"],
                  employer="合成示例·三级甲等口腔医院%02d（虚构机构）",
                  duties=["完成口腔常见病的检查与诊断",
                          "独立完成牙体牙髓与修复治疗",
                          "规范书写口腔病历与影像记录",
                          "开展口腔健康宣教"])),
    dict(key="imag", label="医学影像学", major_code="M10003", major_raw="医学影像学",
         five_year=True, clinical=True, share=6, phd=0.30, bach=0.45,
         families=["F01", "F08", "F03"],
         sig=["CON-K1-ANNOT", "CON-K1-DEVICE"],
         skills={"CON-K1-CLIN": 8, "CON-K1-DEVICE": 6, "CON-K1-ANNOT": 5,
                 "CON-K1-DEV": 3, "CON-K3-RIGOR": 3, "CON-K1-LIT": 3,
                 "CON-K3-SOLVE": 2, "CON-K1-CLINREQ": 2},
         job=dict(etype="E01", occs=["OCC-F01-02-01", "OCC-F01-02-04", "OCC-F08-01-03"],
                  titles=["影像科医师", "医学影像技师", "医学标注与质控专员"],
                  employer="合成示例·三级甲等综合医院%02d（虚构机构）",
                  duties=["独立完成常见部位影像阅片与报告书写",
                          "参与影像质控与疑难病例讨论",
                          "配合介入操作与造影检查",
                          "参与影像标注规范制定"])),
    dict(key="anes", label="麻醉学", major_code="M10002", major_raw="麻醉学",
         five_year=True, clinical=True, share=5, phd=0.20, bach=0.40,
         families=["F01", "F03", "F04"],
         sig=["CON-K1-CLIN", "CON-K3-STRESS"],
         skills={"CON-K1-CLIN": 10, "CON-K3-STRESS": 5, "CON-K3-RIGOR": 4,
                 "CON-K3-COLLAB": 3, "CON-K3-SOLVE": 3, "CON-K3-COMM": 2,
                 "CON-K3-PRIOR": 2},
         job=dict(etype="E01", occs=["OCC-F01-01-05", "OCC-F01-01-04", "OCC-F03-01-03"],
                  titles=["麻醉科医师", "麻醉住院医师", "临床应用专员"],
                  employer="合成示例·三级甲等综合医院%02d（虚构机构）",
                  duties=["完成术前评估与麻醉方案制定",
                          "独立实施椎管内麻醉与全身麻醉",
                          "术中监测生命体征并处理异常",
                          "参与术后镇痛与疼痛门诊"])),
]

DIR_BY_KEY = {d["key"]: d for d in DIRECTIONS}

# 目标岗位族偏好与技能方向"不一致"时，从这些族里挑（跨行族，缺口分析才有内容）
FAR_FAMILIES = ["F06", "F07", "F11", "F12", "F13", "F14", "F16", "F17"]

# 想跨到某族的人，通常会先自学/自述该族的入门能力——这类主张一律 LB1 自述、低等级
FAR_FAMILY_CONCEPTS = {
    "F06": ["CON-K1-UNDW", "CON-K1-CLAIM"],
    "F07": ["CON-K1-FIN", "CON-K1-MKT", "CON-K1-STAT"],
    "F11": ["CON-K1-EDIT", "CON-K1-WRITE"],
    "F12": ["CON-K1-IP", "CON-K1-REG"],
    "F13": ["CON-K1-TEACH", "CON-K3-ACOM"],
    "F14": ["CON-K1-SALES", "CON-K3-NEGO"],
    "F16": ["CON-K1-SQL", "CON-K1-DEV", "CON-K1-PROD"],
    "F17": ["CON-K1-OP", "CON-K3-OWNER"],
}

# 可以用"论文/产出"支撑的 K1 概念（LB4 只对这些概念成立才讲得通）
RESEARCH_CONCEPTS = {
    "CON-K1-EXP", "CON-K1-LIT", "CON-K1-WRITE", "CON-K1-STAT", "CON-K1-DEV",
    "CON-K1-EPI", "CON-K1-RWE", "CON-K1-HEOR", "CON-K1-DEVICE", "CON-K1-PROD",
    "CON-K1-IP", "CON-K1-QC", "CON-K1-REG", "CON-K1-MKT", "CON-K1-FIN",
}

FAMILY_LABEL = {
    "F01": "临床医疗", "F02": "药企医学事务与临床研发", "F03": "医疗器械与IVD",
    "F04": "CRO与临床研究服务", "F05": "生物技术与创新药研发", "F06": "健康险与医疗支付",
    "F07": "咨询与投资", "F08": "医疗AI与数字健康", "F09": "政府与公共部门",
    "F10": "高校与科研", "F11": "出版与传播", "F12": "法务与知识产权",
    "F13": "教育培训", "F14": "销售与商务", "F15": "交叉与新兴", "F16": "完全跨行",
    "F17": "自主创业与自由职业",
}

PROJECT_TYPES = ["科研课题", "临床试验项目", "横向合作项目", "产品研发项目", "公共卫生项目"]

# ===========================================================================
# 2. 工具函数
# ===========================================================================


def rng(tag, seed, i):
    """由 (标签, seed, 序号) 派生独立随机流——保证"第 i 个人"的数据不依赖生成顺序。"""
    return random.Random("mt|%s|%s|%d" % (tag, seed, i))


def pid_of(i):
    return MOCK_PREFIX + "%04d" % i


def sha1(text, n=24):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def weighted_sample(r, weights, k):
    """按权重不重复抽 k 个键。"""
    pool = [(key, float(w)) for key, w in weights.items() if w > 0]
    picks = []
    while pool and len(picks) < k:
        total = sum(w for _, w in pool)
        x = r.uniform(0, total)
        acc = 0.0
        for idx, (key, w) in enumerate(pool):
            acc += w
            if x <= acc:
                picks.append(key)
                pool.pop(idx)
                break
        else:  # 浮点兜底
            picks.append(pool.pop()[0][0])
    return picks


def apportion(count, shares):
    """按份额把 count 分配到各方向（最大余数法），保证总数精确等于 count。"""
    total = float(sum(shares))
    exact = [count * s / total for s in shares]
    base = [int(x) for x in exact]
    rest = count - sum(base)
    order = sorted(range(len(shares)), key=lambda k: (-(exact[k] - base[k]), k))
    for k in order[:rest]:
        base[k] += 1
    return base


def d(year, month, day):
    return dt.date(year, month, day)


# ===========================================================================
# 3. 配额：方向序列 + 学历配额（硬约束）
# ===========================================================================


def plan(seed, count):
    """返回 [ (i, direction_key, degree) ]，i 从 1 开始。"""
    shares = [x["share"] for x in DIRECTIONS]
    quota = apportion(count, shares)
    seq = []
    for direc, n in zip(DIRECTIONS, quota):
        seq += [direc["key"]] * n
    random.Random("dir|%s|%d" % (seed, count)).shuffle(seq)

    # 学历配额：博士 22.5%、本科 17.5%、其余硕士（对齐 docs/04 §7 抽样配额）
    n_doc = int(round(count * 0.225))
    n_bach = int(round(count * 0.175))
    scored = []
    for i in range(1, count + 1):
        direc = DIR_BY_KEY[seq[i - 1]]
        s = direc["phd"] + rng("deg", seed, i).random()
        scored.append((s, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    doc = {i for _, i in scored[:n_doc]}
    rest = [(s, i) for s, i in scored if i not in doc]
    rest2 = sorted(((DIR_BY_KEY[seq[i - 1]]["bach"] + rng("bach", seed, i).random(), i)
                    for _, i in rest), key=lambda x: (-x[0], x[1]))
    bach = {i for _, i in rest2[:n_bach]}

    out = []
    for i in range(1, count + 1):
        deg = "D4" if i in doc else ("D2" if i in bach else "D3")
        out.append((i, seq[i - 1], deg))
    return out


# ===========================================================================
# 4. 单份档案的生成
# ===========================================================================


def edu_timeline(r, direc, degree):
    """倒推学历时间线，返回 [(degree_level, degree_name, start, end)]，从低到高。"""
    bach_years = 5 if direc["five_year"] else 4
    if degree == "D4":
        direct = r.random() < 0.30          # 直博：无独立硕士段
        segs = [("D4", "博士", 5 if direct else r.choice([3, 3, 4]))]
        if not direct:
            segs.append(("D3", "硕士", 3))
        segs.append(("D2", "本科", bach_years))
    elif degree == "D3":
        segs = [("D3", "硕士", 3), ("D2", "本科", bach_years)]
    else:
        segs = [("D2", "本科", bach_years)]

    in_school = r.random() < (0.30 if degree in ("D3", "D4") else 0.08)
    if in_school:
        end_year = ANCHOR.year + 1 + (1 if degree == "D4" and r.random() < 0.4 else 0)
    else:
        end_year = ANCHOR.year - r.choice([0, 0, 1, 1, 2, 2, 3, 4, 5])

    records = []
    cur_end = end_year
    for lvl, name, years in segs:
        start = cur_end - years
        records.append((lvl, name, d(start, 9, 1), d(cur_end, 6, 30)))
        cur_end = start
    records.reverse()
    return records


def build_rows(seed, i, dkey, degree):
    """生成第 i 份档案的全部行，返回 {表名: [行字典]}。"""
    direc = DIR_BY_KEY[dkey]
    r = rng("p", seed, i)
    pid = pid_of(i)
    buf = {}

    def add(table, row):
        buf.setdefault(table, []).append(row)

    # ---------------- 学历 ----------------
    timeline = edu_timeline(r, direc, degree)
    bach_start = timeline[0][2]
    # 出生年 = 本科入学年 - 18~19；person_demographics.birth_year 有 CHECK(1930..2020)
    birth_year = max(1990, min(2003, bach_start.year - 18 - r.choice([0, 0, 1])))
    graduated = timeline[-1][3] <= ANCHOR

    for idx, (lvl, dname, start, end) in enumerate(timeline, 1):
        if lvl == "D4":
            school = r.choice(SCHOOL_TOP)
            tags = [t for t in (r.choice(["双一流", "985"]), r.choice(["211", "医学强校"])) if t]
        elif lvl == "D3":
            school = r.choice(SCHOOL_TOP + SCHOOL_MID)
            tags = [t for t in (r.choice(["双一流", "211", "省重点"]),) if t]
        else:
            school = r.choice(SCHOOL_MID + SCHOOL_LOW)
            tags = ["省重点"]
        add("education_record", dict(
            education_id="edu_%s_%d" % (pid, idx),
            person_id=pid, degree_level=lvl,
            degree_name=("医学博士" if lvl == "D4" else
                         "医学硕士" if lvl == "D3" else "医学学士"),
            school_name=school, school_id=None, school_tags=tags,
            major_raw=direc["major_raw"], major_code=direc["major_code"],
            is_clinical=direc["clinical"], start_date=start, end_date=end,
            is_graduated=(end <= ANCHOR),
            gpa=round(r.uniform(2.9, 3.95), 2), gpa_scale=4.0,
            rank_percentile=round(r.uniform(5, 80), 1),
            supervisor_note=None, joint_program=None,
            overseas=(r.random() < 0.05),
            confidence=0.8,
            verify_status="V2" if r.random() < 0.5 else "V1",
            quality_flags=[], source_id=SRC_ID))

    # ---------------- 人口学 ----------------
    if direc["key"] == "nurse":
        sex = "S2" if r.random() < 0.85 else "S1"
    else:
        sex = r.choice(["S1", "S1", "S2", "S2", "S9"])
    add("person_demographics", dict(
        person_id=pid, birth_year=birth_year, sex=sex,
        hukou_province=r.choice(CITIES), hukou_type=r.choice(["H1", "H1", "H2"]),
        nationality="中国", ethnicity=r.choice(["汉族", "汉族", "汉族", "其他"]),
        political_status=r.choice(["P1", "P3", "P3", "P5", "P2"]),
        marital_status=r.choice(["未婚", "未婚", "已婚"]),
        health_limits=[]))

    # ---------------- 主体 ----------------
    channel = r.choices(["EC1", "EC2", "EC3", "EC4"], weights=[4, 1, 3, 2])[0]
    add("person", dict(
        person_id=pid, subject_code="MT-MOCK-%04d" % i, schema_version="1.0.0",
        status="active", enroll_channel=channel, access_tier="T1",
        source_id=SRC_ID, ingest_run_id=RUN_ID,
        confidence={"EC1": 0.75, "EC2": 0.70, "EC3": 0.80, "EC4": 0.70}[channel],
        verify_status="V1", quality_flags=["synthetic_fixture"],
        valid_from=bach_start, valid_to=None))

    if channel == "EC3":
        add("consent_record", dict(
            consent_id="cns_%s" % pid, person_id=pid, purpose="CP2",
            scope="mock fixture：研究入组，仅用于演示分析",
            granted_at=dt.datetime(ANCHOR.year, 2, 1, 9, 0, 0),
            revoked_at=None, channel="fixture", evidence_uri=None))

    # ---------------- 证据池（先建证据，能力主张再引用） ----------------
    evidence = {}          # tag -> evidence_id

    def ev(tag, kind, title, uri=None, verifier=None, verified_at=None):
        spec = EV_KIND[kind]
        eid = "evd_%s_%s" % (pid, tag)
        evidence[tag] = eid
        add("evidence", dict(
            evidence_id=eid, person_id=pid, evidence_type=spec["evidence_type"],
            source_party="合成示例数据源（fixture）", title=title, uri=uri,
            verifiability=spec["verifiability"], cel_level=spec["cel"],
            verifier=verifier, verified_at=verified_at,
            access_tier=spec["access_tier"]))
        return eid

    ev("self", "self", "合成示例·能力自述清单（候选人填报，无第三方佐证）")

    # ---------------- 证书 ----------------
    # (证型, 名称, 发证机构, 支撑的概念, 是否必需于岗位族)
    certs = []
    if direc["clinical"] and direc["key"] != "nurse":
        certs.append(("C01", "执业医师资格证", "国家卫生健康委员会",
                      ["CON-K1-CLIN"], ["F01"], True))
    if direc["key"] == "nurse":
        certs.append(("C05", "护士执业资格证", "国家卫生健康委员会",
                      ["CON-K1-CLIN"], ["F01"], True))
    if direc["key"] in ("pharm", "clinpharm") and r.random() < 0.6:
        certs.append(("C04", "执业药师", "国家药品监督管理局",
                      ["CON-K1-REG"], ["F02"], True))
    if direc["key"] in ("clin", "clinpharm", "nurse") and graduated and r.random() < 0.7:
        certs.append(("C02", "住院医师规范化培训合格证", "省级卫生健康委员会",
                      ["CON-K1-CLIN", "CON-K1-GCP"], ["F01"], True))
    if r.random() < 0.8:
        certs.append(("C09", "大学英语六级证书（CET-6）", "教育部考试中心",
                      ["CON-K1-INTL", "CON-K1-LIT"], [], False))
    if direc["key"] in ("biostat", "bme", "prev") and r.random() < 0.6:
        certs.append(("C10", "数据分析职业技能等级证书", "人力资源和社会保障部",
                      ["CON-K1-STAT", "CON-K1-SQL"], ["F16"], False))
    if direc["key"] == "prev" and r.random() < 0.4:
        certs.append(("C11", "健康管理师", "人力资源和社会保障部",
                      ["CON-K1-EPI"], ["F15"], False))
    if r.random() < 0.35:
        certs.append(("C06", "GCP 培训证书", "药物临床试验质量管理规范培训基地",
                      ["CON-K1-GCP"], ["F04", "F02"], False))

    cert_concepts = {}
    for k, (ctype, cname, issuer, concepts, req_for, hard) in enumerate(certs, 1):
        obtained = d(ANCHOR.year - r.choice([1, 2, 2, 3, 4]), r.choice([3, 6, 9, 12]), 15)
        eid = ev("cert%d" % k, "cert",
                 "合成示例证书：%s（编号为哈希，不指向真实证书）" % cname,
                 uri="https://example.invalid/mock/credential/%s" % sha1(pid + ctype, 12),
                 verifier="发证机构官网核验（合成）",
                 verified_at=dt.datetime(ANCHOR.year, 3, 15, 10, 0, 0))
        add("credential", dict(
            credential_id="crd_%s_%d" % (pid, k), person_id=pid, credential_type=ctype,
            name=cname, issuing_body=issuer,
            credential_no_hash=sha1("mock|%s|%s" % (pid, ctype), 32),
            obtained_date=obtained, expires_date=None,
            status=("CS1" if graduated else "CS3"),
            is_required_for=req_for, confidence=0.9, verify_status="V3",
            evidence_id=eid, source_id=SRC_ID))
        for cid in concepts:
            cert_concepts.setdefault(cid, eid)

    # ---------------- 测评（第三方常模，evidence E2 / basis LB2） ----------------
    assess_evd = None
    if r.random() < 0.45:
        assess_evd = ev("assess1", "assess",
                        "合成示例·职业能力测评报告（含常模组与百分位，虚构工具）",
                        uri="https://example.invalid/mock/assessment/%s" % sha1(pid, 10),
                        verifier="合成示例测评机构",
                        verified_at=dt.datetime(ANCHOR.year, 5, 20, 14, 0, 0))
        for k, dim in enumerate(r.sample(["逻辑推理", "数据分析", "沟通表达",
                                          "抗压与情绪稳定", "团队协作", "学习敏捷度"], 3), 1):
            add("assessment", dict(
                assessment_id="asm_%s_%d" % (pid, k), person_id=pid,
                instrument="合成示例·通用职业能力测评（虚构工具）", dimension=dim,
                score=round(r.uniform(45, 92), 1),
                norm_group="医学类硕博常模（合成示例）",
                percentile=round(r.uniform(25, 96), 1),
                taken_at=d(ANCHOR.year - r.choice([0, 1]), r.choice([3, 5, 9, 11]), 12),
                confidence=0.8, source_id=SRC_ID))

    # ---------------- 上级/同行评价（evidence E2 / basis LB5，须有实名核验人与核验时间） ----------------
    peer_evd = None
    if r.random() < 0.30:
        peer_evd = ev("peer1", "peer",
                      "合成示例·带教/上级评价（评价人用虚构编号，非真实自然人）",
                      uri=None,
                      verifier="合成示例带教医师编号 T-%04d" % (i % 999 + 1),
                      verified_at=dt.datetime(ANCHOR.year, 4, 10, 11, 0, 0))

    # ---------------- 实习/规培/轮转 ----------------
    clinical_evds = []
    if direc["clinical"]:
        n_exp = r.randint(1, 3)
        for k in range(1, n_exp + 1):
            dep = r.choice(DEPARTMENTS[direc["key"]])
            months = r.choice([3, 6, 6, 12, 12, 24])
            cases = r.randint(30, 400)
            eid = ev("clin%d" % k, "clinical",
                     "合成示例·出科考核与轮转记录：%s（%d 个月）" % (dep, months),
                     uri="https://example.invalid/mock/clinical/%s_%d" % (sha1(pid, 10), k),
                     verifier="带教医师（合成示例）",
                     verified_at=dt.datetime(ANCHOR.year, 6, 30, 9, 0, 0))
            clinical_evds.append((eid, cases, months))
            add("clinical_exposure", dict(
                exposure_id="clx_%s_%d" % (pid, k), person_id=pid, department=dep,
                department_code=None, procedure_count=r.randint(10, 300),
                case_volume=cases, skills=r.sample(CLIN_SKILLS[direc["key"]],
                                                   min(4, len(CLIN_SKILLS[direc["key"]]))),
                duration_months=float(months), confidence=0.8,
                verify_status="V2", source_id=SRC_ID))

    # ---------------- 科研产出 ----------------
    paper_evds = []
    research_dirs = {"basmed", "biostat", "pharm", "prev", "clin", "bme", "clinpharm"}
    if direc["key"] in research_dirs and r.random() < (0.85 if degree == "D4" else 0.65):
        n_out = r.randint(1, 3 if degree == "D4" else 2)
        for k in range(1, n_out + 1):
            otype = r.choices(["RO1", "RO2", "RO4", "RO5", "RO7"],
                              weights=[5, 3, 2, 1, 2])[0]
            first_author = r.random() < (0.7 if degree == "D4" else 0.45)
            is_doi = otype != "RO5"
            year = ANCHOR.year - r.choice([0, 1, 1, 2, 3])
            doi = "10.99999/mock-medtalent-%s-%04d" % (sha1(pid, 6), k) if is_doi else None
            if is_doi:
                eid = ev("out%d" % k, "paper",
                         "合成示例论文（虚构题名）：%s方向研究 %04d-%d" % (
                             direc["label"], i, k),
                         uri="https://example.invalid/mock/doi/%s" % doi,
                         verifier="DOI 反查（合成分区号段）",
                         verified_at=dt.datetime(year, 12, 1, 9, 0, 0))
            else:
                eid = ev("out%d" % k, "deliver",
                         "合成示例专利（虚构）：%s方向技术方案 %04d-%d" % (
                             direc["label"], i, k),
                         uri="https://example.invalid/mock/patent/%s-%d" % (sha1(pid, 8), k),
                         verifier=None, verified_at=None)
            paper_evds.append((eid, is_doi))
            add("research_output", dict(
                output_id="out_%s_%d" % (pid, k), person_id=pid, output_type=otype,
                title="合成示例·%s方向研究（%04d-%d，虚构题名）" % (direc["label"], i, k),
                venue="合成示例期刊/会议（虚构，非真实刊物）", doi=doi, pmid=None,
                year=year, author_position=("AP1" if first_author else
                                            r.choice(["AP2", "AP4", "AP5"])),
                is_first_author=first_author, if_value=round(r.uniform(1.5, 9.9), 1),
                jcr_quartile=r.choice(["Q1", "Q2", "Q2", "Q3", "QN"]),
                cas_quartile=r.choice(["Q1", "Q2", "Q2", "Q3", "QN"]),
                citation_count=r.randint(0, 60), funding_source="合成示例基金（虚构）",
                confidence=0.85, verify_status="V3" if is_doi else "V2",
                source_id=SRC_ID))

    # ---------------- 项目经历 ----------------
    project_evds = []
    p_rate = {"basmed": 0.9, "bme": 0.85, "biostat": 0.85, "prev": 0.75, "pharm": 0.7,
              "clinpharm": 0.6, "clin": 0.6, "imag": 0.6, "oral": 0.4, "tcm": 0.4,
              "anes": 0.4, "nurse": 0.35}[direc["key"]]
    if r.random() < p_rate:
        n_proj = r.randint(1, 3 if r.random() < 0.3 else 2)
        for k in range(1, n_proj + 1):
            ptype = r.choice(PROJECT_TYPES)
            start = d(ANCHOR.year - r.choice([1, 2, 2, 3]), r.choice([1, 3, 6, 9]), 1)
            end = d(start.year + r.choice([1, 1, 2]), 12, 31)
            if end > ANCHOR:
                end = ANCHOR
            eid = ev("proj%d" % k, "project",
                     "合成示例·项目验收记录：%s（%04d-%d）" % (ptype, i, k),
                     uri="https://example.invalid/mock/project/%s-%d" % (sha1(pid, 8), k),
                     verifier="项目负责人（合成示例）", verified_at=None)
            project_evds.append(eid)
            add("project_record", dict(
                project_id="prj_%s_%d" % (pid, k), person_id=pid, project_type=ptype,
                name="合成示例项目·%s方向%s（%04d-%d）" % (direc["label"], ptype, i, k),
                role=r.choice(["参与", "核心成员", "子任务负责人", "负责人"]),
                scale_note=r.choice(["课题经费 20 万元", "课题经费 50 万元", "多中心 3 家",
                                     "样本量 300 例", "课题经费 10 万元"]),
                outcome=r.choice(["按期结题", "形成技术报告", "产出论文一篇",
                                  "完成验收并交付", "进入中期评估"]),
                start_date=start, end_date=end, confidence=0.8, verify_status="V2",
                source_id=SRC_ID))

    # ---------------- 荣誉 ----------------
    if r.random() < 0.35:
        for k in range(1, r.randint(1, 2) + 1):
            add("award_honor", dict(
                award_id="awd_%s_%d" % (pid, k), person_id=pid,
                name="合成示例·%s" % r.choice(["优秀毕业生", "学业奖学金", "优秀住院医师",
                                               "病例汇报比赛", "创新创业大赛"]),
                level=r.choice(["校级", "校级", "省级", "国家级"]),
                year=ANCHOR.year - r.choice([1, 2, 2, 3, 4]),
                rank=r.choice(["一等奖", "二等奖", "三等奖", None]),
                confidence=0.8, verify_status="V1", source_id=SRC_ID))

    # ---------------- 就业经历 ----------------
    job = direc["job"]
    n_emp = 0
    if graduated:
        n_emp = 2 if r.random() < 0.25 else 1
    elif degree in ("D3", "D4") and r.random() < 0.5:
        n_emp = 1          # 在读但有规培/实习
    for k in range(1, n_emp + 1):
        if graduated and n_emp == 2 and k == 1:
            span = (ANCHOR.year - r.choice([4, 5]), ANCHOR.year - r.choice([2, 3]))
        else:
            span = (ANCHOR.year - r.choice([0, 1, 1, 2]), ANCHOR.year)
        start = d(span[0], 7 if span[0] < span[1] else 1, 1)
        current = (k == n_emp) and graduated
        end = None if current else d(max(span[1], span[0]), 6, 30)
        add("employment_record", dict(
            employment_id="emp_%s_%d" % (pid, k), person_id=pid,
            employer_name=job["employer"] % (i % 20 + 1), employer_id=None,
            employer_type=job["etype"], occupation_id=r.choice(job["occs"]),
            title_raw=r.choice(job["titles"]),
            title_normalized=r.choice(job["titles"]),
            level=r.choice(["初级", "初级", "中级"]),
            start_date=start, end_date=end, is_current=current,
            duties=r.sample(job["duties"], min(4, len(job["duties"]))),
            achievements=r.choice(["完成年度考核", "获科室表扬", "通过执业注册",
                                   "独立承担门诊工作", "参与质控改进"]),
            leave_reason=None if current else r.choice(["升学", "岗位调整", "合同到期"]),
            confidence=0.8, verify_status="V2" if current else "V1",
            source_id=SRC_ID))

    # ---------------- 观察窗（docs/04 §6：每人至少一条 W1） ----------------
    w1_start = bach_start
    w1_end = ANCHOR
    add("observation_window", dict(
        window_id="ow1_%s" % pid, person_id=pid, window_type="W1",
        start_date=w1_start, end_date=w1_end, source_id=SRC_ID,
        coverage_note="合成 fixture：覆盖在校至今（生成基准日 %s）" % ANCHOR.isoformat()))
    add("observation_window", dict(
        window_id="ow2_%s" % pid, person_id=pid, window_type="W2",
        start_date=ANCHOR - dt.timedelta(days=180), end_date=ANCHOR,
        source_id=SRC_ID, coverage_note="合成 fixture：最近一次信息更新窗口"))

    # ---------------- 偏好取向（先定下来，能力主张要用到） ----------------
    # docs/04：约 25% 的人岗位族偏好**故意与技能方向不一致**，缺口分析才有内容
    mismatch = r.random() < 0.25
    if mismatch:
        far = [f for f in FAR_FAMILIES if f not in direc["families"]]
        fam = r.choice(far)
    else:
        fam = r.choice(direc["families"])

    # ---------------- 能力主张 + 证据分级 ----------------
    profile = dict(direc["skills"])
    for cid, w in GLOBAL_K3.items():
        profile[cid] = profile.get(cid, 0) + w
    # 每个方向的"招牌能力"必出现（sig），其余按权重抽——技能供给才有方向差异
    sig = list(direc["sig"])
    n_total = r.randint(max(3, len(sig) + 1), 10)
    picks = sig + weighted_sample(
        r, {k: v for k, v in profile.items() if k not in sig}, n_total - len(sig))

    # 跨赛道意图：偏好与技能方向不一致的人，会额外自述 1–2 条目标族入门能力（一律 LB1）
    if mismatch:
        for cid in r.sample(FAR_FAMILY_CONCEPTS[fam],
                            min(len(FAR_FAMILY_CONCEPTS[fam]), r.randint(1, 2))):
            if cid not in picks:
                picks.append(cid)

    self_evd = evidence["self"]
    for cid in picks:
        # ① 证书直接对应能力 → LB3（上限 0.90）｜② 临床例数 → LB6（上限 L4）
        # ③ 测评 → LB2（仅 K3）｜④ 论文/项目产出 → LB4（上限 L5）｜⑤ 兜底自述 → LB1（≤L3）
        if cid == "CON-K1-CLIN" and clinical_evds and r.random() < 0.55:
            eid, cases, _months = r.choice(clinical_evds)
            basis, level = "LB6", min(4, 2 + cases // 120)
        elif cid in cert_concepts and r.random() < 0.8:
            basis, eid, level = "LB3", cert_concepts[cid], 3
        elif cid.startswith("CON-K3") and assess_evd and r.random() < 0.6:
            basis, eid, level = "LB2", assess_evd, r.randint(2, 4)
        elif cid.startswith("CON-K3") and peer_evd and r.random() < 0.4:
            basis, eid, level = "LB5", peer_evd, r.randint(2, 4)
        elif cid in RESEARCH_CONCEPTS and paper_evds and r.random() < 0.7:
            eid, is_doi = r.choice(paper_evds)
            basis, level = "LB4", r.randint(3, 5 if is_doi else 4)
        elif project_evds and r.random() < 0.5:
            eid = r.choice(project_evds)
            basis, level = "LB4", r.randint(2, 4)
        else:
            basis, eid, level = "LB1", self_evd, r.randint(1, 3)

        if basis == "LB1":
            conf, vstatus = CONF["LB1"], "V1"          # 仅自述：0.50 × V1 修正 0.9
        elif basis == "LB3":
            conf, vstatus = CONF["LB3"], "V3"
        elif basis == "LB6":
            conf, vstatus = CONF["LB6"], "V2"
        elif basis == "LB2":
            conf, vstatus = CONF["LB2"], "V2"
        elif basis == "LB5":
            conf, vstatus = CONF["LB5"], "V2"
        else:
            doi_backed = (eid, True) in paper_evds
            conf = CONF["LB4_DOI"] if doi_backed else CONF["LB4"]
            vstatus = "V3" if doi_backed else "V2"

        trans, note = TRANSFER[cid]
        add("skill_assertion", dict(
            assertion_id="skl_%s_%s" % (pid, cid.split("-")[-1].lower()),
            person_id=pid, concept_id=cid, level=level, level_basis=basis,
            claim_type="CT1" if cid.startswith("CON-K1") else
                       r.choice(["CT1", "CT1", "CT3"]),
            essentiality=None, evidence_id=eid, confidence=conf,
            transferability=trans, transfer_note=note if trans >= 3 else None,
            first_observed_at=bach_start + dt.timedelta(days=r.randint(0, 900)),
            last_verified_at=dt.datetime(ANCHOR.year, r.choice([1, 3, 6, 9]), 10, 0, 0),
            verify_status=vstatus, source_id=SRC_ID))

    # ---------------- 偏好明细 ----------------
    city = r.choice(CITIES)
    lo, hi = r.choice([(6000, 9000), (8000, 12000), (10000, 15000),
                       (12000, 18000), (15000, 22000), (20000, 30000)])
    if degree == "D4":
        lo, hi = int(lo * 1.3), int(hi * 1.3)
    accept_switch = r.random() < 0.45          # docs/04 §7：PF8"接受转行"=真 需 ≥40 人
    prefs = [
        ("PF1", city, None, 1.0, True),
        ("PF3", FAMILY_LABEL[fam], fam, 1.0, True),
        ("PF4", "%d-%d 元/月" % (lo, hi), "monthly:%d-%d" % (lo, hi), 0.8, False),
        ("PF8", "接受转行" if accept_switch else "不接受转行",
         "Y" if accept_switch else "N", 0.6, False),
    ]
    if r.random() < 0.5:
        prefs.append(("PF5", r.choice(["可接受高强度", "希望规律作息", "可接受夜班"]),
                      None, 0.5, False))
    if r.random() < 0.5:
        prefs.append(("PF7", r.choice(["重视成长空间", "重视平台稳定性", "重视收入"]),
                      None, 0.5, False))
    for ptype, vraw, vcode, w, hard in prefs:
        add("preference", dict(
            # ID 用 pref_type 而不是序号：偏好条数随 seed 变化，序号会漂移并产生孤儿行
            preference_id="prf_%s_%s" % (pid, ptype), person_id=pid, pref_type=ptype,
            value_raw=vraw, value_code=vcode, weight=w, is_hard=hard, confidence=0.7))

    meta = dict(person_id=pid, index=i, direction=dkey, direction_label=direc["label"],
                major_code=direc["major_code"], degree=degree, mismatch=mismatch,
                family=fam, n_skill=len(picks), graduated=graduated)
    return buf, meta


# ===========================================================================
# 5. 写库
# ===========================================================================


def upsert(c, buf):
    """按表写入；一律 ON CONFLICT DO NOTHING，保证可重跑。"""
    written = {}
    order = ["source_registry", "ingest_run", "person", "person_demographics",
             "education_record", "training_record", "clinical_exposure",
             "employment_record", "project_record", "research_output", "award_honor",
             "credential", "assessment", "evidence", "skill_assertion", "preference",
             "observation_window", "consent_record"]
    with c.cursor() as cur:
        for table in order:
            rows = buf.get(table) or []
            if not rows:
                continue
            cols = sorted(rows[0].keys())
            stmt = sql.SQL("INSERT INTO mt.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(x) for x in cols),
                sql.SQL(", ").join(sql.Placeholder() for _ in cols))
            cur.executemany(stmt, [tuple(row.get(x) for x in cols) for row in rows])
            written[table] = len(rows)
    c.commit()
    return written


def count_rows(c, table, where="person_id LIKE %s", arg=MOCK_PREFIX + "%"):
    with c.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) AS n FROM mt.{} WHERE " + where)
                    .format(sql.Identifier(table)), (arg,))
        return cur.fetchone()["n"]


# ===========================================================================
# 6. 分布摘要
# ===========================================================================


def report(c, metas):
    with c.cursor() as cur:
        def run(sql_text, args=None):
            cur.execute(sql_text, args or ())
            return cur.fetchall()

        print("\n" + "=" * 78)
        print("▶ 写入行数（仅 per_mock_% 人才）")
        print("=" * 78)
        tables = ["person", "person_demographics", "education_record", "employment_record",
                  "clinical_exposure", "project_record", "research_output", "award_honor",
                  "credential", "assessment", "skill_assertion", "evidence", "preference",
                  "observation_window", "consent_record"]
        for t in tables:
            print("    %-22s %5d" % (t, count_rows(c, t)))

        print("\n▶ 学历分布（每人最高学历；目标：硕士 55–65% / 博士 20–25% / 本科 10–20%）")
        rows = run("""
            SELECT degree_level, count(*) AS n,
                   round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
            FROM (SELECT DISTINCT ON (person_id) person_id, degree_level
                  FROM mt.education_record WHERE person_id LIKE %s
                  ORDER BY person_id, degree_level DESC) t
            GROUP BY degree_level ORDER BY degree_level""", (MOCK_PREFIX + "%",))
        for r in rows:
            print("    %-4s %5d  %5s%%" % (r["degree_level"], r["n"], r["pct"]))

        print("\n▶ 专业方向分布（Top 10）")
        rows = run("""
            SELECT major_code, major_raw, count(DISTINCT person_id) AS n
            FROM mt.education_record WHERE person_id LIKE %s
            GROUP BY major_code, major_raw ORDER BY n DESC, major_code LIMIT 10""",
                   (MOCK_PREFIX + "%",))
        for r in rows:
            print("    %-8s %-22s %5d" % (r["major_code"], r["major_raw"], r["n"]))

        print("\n▶ 技能供给 Top 10（不同人数）")
        rows = run("""
            SELECT sa.concept_id, co.preferred_label,
                   count(DISTINCT sa.person_id) AS persons, round(avg(sa.level), 2) AS lvl
            FROM mt.skill_assertion sa JOIN mt.concept co USING (concept_id)
            WHERE sa.person_id LIKE %s
            GROUP BY 1, 2 ORDER BY persons DESC, 1 LIMIT 10""", (MOCK_PREFIX + "%",))
        for r in rows:
            print("    %-16s %-18s %5d 人  平均 L%s" % (
                r["concept_id"], r["preferred_label"], r["persons"], r["lvl"]))

        print("\n▶ 证据分级 cel_level 分布（E0 自述 … E4 履职记录）")
        rows = run("""
            SELECT cel_level, count(*) AS n FROM mt.evidence
            WHERE person_id LIKE %s GROUP BY 1 ORDER BY 1""", (MOCK_PREFIX + "%",))
        for r in rows:
            print("    %-4s %5d" % (r["cel_level"], r["n"]))

        print("\n▶ level_basis 分布（LB1 自述 … LB6 临床例数）")
        rows = run("""
            SELECT level_basis, count(*) AS n, round(avg(confidence), 3) AS conf
            FROM mt.skill_assertion WHERE person_id LIKE %s
            GROUP BY 1 ORDER BY 1""", (MOCK_PREFIX + "%",))
        for r in rows:
            print("    %-4s %5d  平均置信度 %s" % (r["level_basis"], r["n"], r["conf"]))

    n_skill = sum(m["n_skill"] for m in metas)
    n_mis = sum(1 for m in metas if m["mismatch"])
    print("\n    覆盖方向 %d 个；能力主张 %d 条（人均 %.1f）；岗位族偏好与技能方向不一致 %d 人（%.0f%%）"
          % (len({m["direction"] for m in metas}), n_skill, n_skill / max(1, len(metas)),
             n_mis, 100.0 * n_mis / max(1, len(metas))))


def fingerprint(buf):
    """对将要写入的全部行做内容指纹——用来判断"库里这批 mock 人才是不是本次生成器产出的"。

    为什么需要它：ID 由 (前缀, 序号) 决定，如果生成器改了逻辑而库里还有旧数据，
    重复执行只会在旧数据上"补"出新行，形成半新半旧的混合样本（行数看起来正常，
    分布却是错的）。有了内容指纹，这种情况会被拦住并要求 --reset。
    """
    h = hashlib.sha1()
    for table in sorted(buf):
        for row in buf[table]:
            h.update(table.encode("utf-8"))
            h.update(b"|")
            h.update(json.dumps(row, sort_keys=True, default=str,
                                ensure_ascii=False).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def stored_fingerprint(c):
    with c.cursor() as cur:
        cur.execute("SELECT params ->> 'fingerprint' AS fp, params -> 'count' AS n "
                    "FROM mt.ingest_run WHERE ingest_run_id = %s", (RUN_ID,))
        row = cur.fetchone()
        return row["fp"] if row else None


def ensure_registry(c, fp, n_person, seed, count):
    """登记 mock 数据来源与采集批次（person.source_id / ingest_run_id 的外键目标）。"""
    params = json.dumps({"generator": "ops/fixtures/gen_talent.py",
                         "note": "synthetic fixture, not real people",
                         "seed": seed, "count": count, "fingerprint": fp,
                         "synthetic": True}, ensure_ascii=False)
    with c.cursor() as cur:
        cur.execute("""
            INSERT INTO mt.source_registry (source_id, name, source_type, license_note,
                credibility, evidence_grade, update_freq, access_tier, status)
            VALUES (%s, %s, 'SRC7', %s, 0.50, 'D', 'once', 'T1', 'active')
            ON CONFLICT (source_id) DO NOTHING""", (
            SRC_ID, "本地合成人才档案 fixture（非真实数据）",
            "合成数据，仅供演示与在线分析；不含任何自然人信息"))
        cur.execute("""
            INSERT INTO mt.ingest_run (ingest_run_id, source_id, started_at, finished_at,
                record_count, error_count, status, tool_version, params)
            VALUES (%s, %s, now(), now(), %s, 0, 'ok', %s, %s::jsonb)
            ON CONFLICT (ingest_run_id) DO UPDATE
              SET params = EXCLUDED.params, record_count = EXCLUDED.record_count,
                  tool_version = EXCLUDED.tool_version, finished_at = now(),
                  status = 'ok'""", (
            RUN_ID, SRC_ID, n_person, "gen_talent@1.0", params))
    c.commit()


def main():
    ap = argparse.ArgumentParser(description="生成 mock 人才档案（合成数据）")
    ap.add_argument("--count", type=int, default=120, help="生成多少份（默认 120）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    ap.add_argument("--reset", action="store_true",
                    help="先清除 per_mock_%% 人才及其从属行，再重新生成")
    a = ap.parse_args()

    import reset_talent  # 同目录

    with psycopg.connect(DSN, row_factory=dict_row) as c:
        non_mock_before = count_non_mock(c)

        # 先纯内存生成，再决定要不要写库
        plan_rows = plan(a.seed, a.count)
        buf, metas = {}, []
        for i, dkey, degree in plan_rows:
            rows, meta = build_rows(a.seed, i, dkey, degree)
            for t, rs in rows.items():
                buf.setdefault(t, []).extend(rs)
            metas.append(meta)
        fp = fingerprint(buf)
        existing = count_rows(c, "person")

        if not a.reset and existing and stored_fingerprint(c) != fp:
            print("[X] 库里的 mock 人才不是本次生成器的产物（内容指纹不一致）。")
            print("    继续执行会在旧样本上补出新行，得到半新半旧的混合数据。")
            print("    请改用：python ops\\fixtures\\gen_talent.py --count %d --seed %d --reset"
                  % (a.count, a.seed))
            return 3

        if a.reset:
            print("▶ --reset：清除旧 mock 人才")
            reset_talent.reset(c, quiet=False)

        ensure_registry(c, fp, len(plan_rows), a.seed, a.count)

        before = {t: count_rows(c, t) for t in
                  ("person", "person_demographics", "education_record", "employment_record",
                   "clinical_exposure", "project_record", "research_output", "award_honor",
                   "credential", "assessment", "skill_assertion", "evidence", "preference",
                   "observation_window", "consent_record")}
        upsert(c, buf)
        after = {t: count_rows(c, t) for t in before}

        print("[✓] 生成完成：请求 %d 份，seed=%d" % (a.count, a.seed))
        added = ["%s +%d" % (t, after[t] - before[t]) for t in before if after[t] != before[t]]
        print("    新增行：" + ("、".join(added) if added else "无（内容与库中完全一致）"))
        if after["person"] == before["person"] and not added:
            print("    （幂等：全部 ON CONFLICT DO NOTHING，未产生任何重复行）")

        report(c, metas)
        non_mock_after = count_non_mock(c)

    print("\n    非 mock 人才行数：%d → %d（必须不变）" % (non_mock_before, non_mock_after))
    if non_mock_before != non_mock_after:
        print("[X] 非 mock 数据被改动了！")
        return 2
    print("    下一步核对：python ops\\pg.py sql ops\\fixtures\\check_talent.sql")
    return 0


def count_non_mock(c):
    with c.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM mt.person WHERE person_id NOT LIKE %s",
                    (MOCK_PREFIX + "%",))
        return cur.fetchone()["n"]


if __name__ == "__main__":
    sys.exit(main())
