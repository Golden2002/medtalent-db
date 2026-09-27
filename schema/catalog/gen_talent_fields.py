#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
schema/catalog/gen_talent_fields.py — 人才画像维度体系种子生成器（一次性工具，不改 code/）

单一来源：本文件里的 NEW_CODE_TABLES / NEW_FIELDS / DIMENSIONS 三张常量表。
产物（全部用 io.open(encoding='utf-8') 显式写出，绝不经 PowerShell 读写）：
  1. schema/catalog/code_table_seed.csv   逐行**追加**新词表与新选项，已有行按字节原样保留
  2. schema/catalog/field_catalog_seed.csv 逐行**追加**新字段，已有行按字节原样保留
  3. schema/catalog/talent_field_seed.csv  新建：维度注册表（权威来源）
  4. schema/sql/013_dimensions.sql         结构 + 上述种子的 INSERT ... ON CONFLICT

CSV 与 SQL 由同一份常量生成，因此不可能漂移。重跑幂等（先删掉自己上次追加的行）。
"""
import csv
import io
import os

def _project_root(start):
    d = start
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "schema", "catalog")):
            return d
        d = os.path.dirname(d)
    raise RuntimeError("找不到项目根目录（应含 schema/catalog）")


ROOT = _project_root(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "schema", "catalog")
SQLDIR = os.path.join(ROOT, "schema", "sql")

CODE_TABLE_CSV = os.path.join(CATALOG, "code_table_seed.csv")
FIELD_CSV = os.path.join(CATALOG, "field_catalog_seed.csv")
TALENT_CSV = os.path.join(CATALOG, "talent_field_seed.csv")
OUT_SQL = os.path.join(SQLDIR, "013_dimensions.sql")

# ---------------------------------------------------------------------------
# 1. 新词表（**不重复**用户已补的 12 张词表 / 75 个选项）
#    选项 8 元组：(code, label_zh, label_en, parent_code, level, sort_order,
#                  definition, external_mapping)
#    external_mapping 必须非空（{} 也算），否则 validate_catalog 会报警告。
# ---------------------------------------------------------------------------
NEW_CODE_TABLES = {}


def ct(table_id, name, desc, hierarchical=False, options=()):
    NEW_CODE_TABLES[table_id] = {"name": name, "desc": desc,
                                 "hierarchical": hierarchical, "options": list(options)}


ct("CT_INTEREST_DOMAIN", "兴趣方向",
   "自写补充为主的高频维度；external_mapping 直连可对应的岗位族，使两侧可匹配", False, [
    ("IN01", "临床与医疗健康", "Clinical & Healthcare", "", 1, 10, "兴趣域", '{"job_family":["F01","F02"]}'),
    ("IN02", "药物研发与注册", "Drug R&D & Regulatory", "", 1, 20, "兴趣域", '{"job_family":["F02","F05"]}'),
    ("IN03", "医疗器械与IVD", "Medical Device & IVD", "", 1, 30, "兴趣域", '{"job_family":["F03"]}'),
    ("IN04", "临床试验与CRO", "Clinical Trial & CRO", "", 1, 40, "兴趣域", '{"job_family":["F04"]}'),
    ("IN05", "生物技术与基因治疗", "Biotech & Gene Therapy", "", 1, 50, "兴趣域", '{"job_family":["F05"]}'),
    ("IN06", "医疗人工智能与算法", "Medical AI & Algorithms", "", 1, 60, "兴趣域", '{"job_family":["F08","F15"]}'),
    ("IN07", "数据分析与统计建模", "Data Analysis & Modeling", "", 1, 70, "兴趣域", '{"job_family":["F08","F07"]}'),
    ("IN08", "数字健康与互联网医疗", "Digital Health", "", 1, 80, "兴趣域", '{"job_family":["F08"]}'),
    ("IN09", "公共卫生与流行病学", "Public Health & Epidemiology", "", 1, 90, "兴趣域", '{"job_family":["F09","F10"]}'),
    ("IN10", "健康保险与支付", "Health Insurance & Payment", "", 1, 100, "兴趣域", '{"job_family":["F06"]}'),
    ("IN11", "医院管理与运营", "Hospital Management", "", 1, 110, "兴趣域", '{"job_family":["F01","F09"]}'),
    ("IN12", "咨询与战略", "Consulting & Strategy", "", 1, 120, "兴趣域", '{"job_family":["F07"]}'),
    ("IN13", "投资与证券研究", "Investment & Equity Research", "", 1, 130, "兴趣域", '{"job_family":["F07"]}'),
    ("IN14", "金融与风险管理", "Finance & Risk", "", 1, 140, "兴趣域", '{"job_family":["F07","F06"]}'),
    ("IN15", "法律与知识产权", "Legal & IP", "", 1, 150, "兴趣域", '{"job_family":["F12"]}'),
    ("IN16", "学术科研与教学", "Academia & Teaching", "", 1, 160, "兴趣域", '{"job_family":["F10","F13"]}'),
    ("IN17", "出版编辑与科普传播", "Publishing & SciComm", "", 1, 170, "兴趣域", '{"job_family":["F11"]}'),
    ("IN18", "教育培训与课程开发", "Education & Curriculum", "", 1, 180, "兴趣域", '{"job_family":["F13"]}'),
    ("IN19", "市场与品牌传播", "Marketing & Branding", "", 1, 190, "兴趣域", '{"job_family":["F14"]}'),
    ("IN20", "销售与商务拓展", "Sales & Business Development", "", 1, 200, "兴趣域", '{"job_family":["F14"]}'),
    ("IN21", "产品与用户研究", "Product & User Research", "", 1, 210, "兴趣域", '{"job_family":["F15","F08"]}'),
    ("IN22", "供应链与生产制造", "Supply Chain & Manufacturing", "", 1, 220, "兴趣域", '{"job_family":["F16"]}'),
    ("IN23", "政府与公共政策", "Government & Public Policy", "", 1, 230, "兴趣域", '{"job_family":["F09"]}'),
    ("IN24", "创业与商业模式", "Entrepreneurship", "", 1, 240, "兴趣域", '{"job_family":["F17"]}'),
    ("IN25", "人工智能通用技术", "General AI Technology", "", 1, 250, "兴趣域", '{"job_family":["F15"]}'),
    ("IN26", "软件工程与系统开发", "Software Engineering", "", 1, 260, "兴趣域", '{"job_family":["F15","F08"]}'),
    ("IN27", "能源环境与可持续发展", "Energy & Sustainability", "", 1, 270, "兴趣域", '{"job_family":["F16"]}'),
    ("IN28", "文化娱乐与内容创作", "Culture & Content Creation", "", 1, 280, "兴趣域", '{"job_family":["F16"]}'),
    ("IN99", "其他", "Other", "", 1, 990, "兜底项：不在选项内的自写取值先走候选池，不直接进词表", '{}'),
])

ct("CT_WORK_STYLE", "工作风格", "自评工作风格；岗位侧对应 JD 的软性描述", False, [
    ("WS1", "结构化流程型", "Process-driven", "", 1, 10, "", '{}'),
    ("WS2", "自主探索型", "Explorer", "", 1, 20, "", '{}'),
    ("WS3", "团队协作者", "Team Collaborator", "", 1, 30, "", '{}'),
    ("WS4", "独立攻坚型", "Independent Solver", "", 1, 40, "", '{}'),
    ("WS5", "目标导向型", "Goal-oriented", "", 1, 50, "", '{}'),
    ("WS6", "细节审慎型", "Detail-cautious", "", 1, 60, "", '{}'),
    ("WS7", "快速试错型", "Fast Iteration", "", 1, 70, "", '{}'),
    ("WS8", "长周期深耕型", "Long-horizon", "", 1, 80, "", '{}'),
    ("WS9", "跨部门协调型", "Cross-functional Coordinator", "", 1, 90, "", '{}'),
    ("WS10", "客户前台型", "Client-facing", "", 1, 100, "", '{}'),
    ("WS11", "幕后支持型", "Back-office Support", "", 1, 110, "", '{}'),
    ("WS12", "多任务并行型", "Multi-tasking", "", 1, 120, "", '{}'),
])

ct("CT_VALUE_ORIENT", "价值观取向", "与 CT_ORG_CULTURE 配对，构成组织文化契合的两侧", False, [
    ("VO1", "专业精进", "Craft Mastery", "", 1, 10, "", '{}'),
    ("VO2", "助人与利他", "Altruism", "", 1, 20, "", '{}'),
    ("VO3", "影响力与社会价值", "Social Impact", "", 1, 30, "", '{}'),
    ("VO4", "收入回报", "Compensation", "", 1, 40, "", '{}'),
    ("VO5", "稳定与安全", "Stability & Security", "", 1, 50, "", '{}'),
    ("VO6", "工作生活平衡", "Work-life Balance", "", 1, 60, "", '{}'),
    ("VO7", "自主与自由", "Autonomy", "", 1, 70, "", '{}'),
    ("VO8", "声望与认可", "Recognition", "", 1, 80, "", '{}'),
    ("VO9", "团队归属", "Belonging", "", 1, 90, "", '{}'),
    ("VO10", "创新与挑战", "Innovation & Challenge", "", 1, 100, "", '{}'),
])

ct("CT_YES_NO", "是否", "通用二值三态：是 / 否 / 不确定", False, [
    ("Y", "是", "Yes", "", 1, 10, "", '{}'),
    ("N", "否", "No", "", 1, 20, "", '{}'),
    ("U", "不确定", "Uncertain", "", 1, 30, "", '{}'),
])

ct("CT_AGE_BAND", "年龄段", "由出生年派生；对外只展示段位，不展示出生年", False, [
    ("AG1", "25岁以下", "Under 25", "", 1, 10, "", '{}'),
    ("AG2", "25-29岁", "25-29", "", 1, 20, "", '{}'),
    ("AG3", "30-34岁", "30-34", "", 1, 30, "", '{}'),
    ("AG4", "35-39岁", "35-39", "", 1, 40, "", '{}'),
    ("AG5", "40-44岁", "40-44", "", 1, 50, "", '{}'),
    ("AG6", "45岁及以上", "45+", "", 1, 60, "", '{}'),
    ("AG9", "未提供", "Not Provided", "", 1, 90, "", '{}'),
])

ct("CT_RANK_BAND", "学业排名段位", "专业排名百分位的段位化，避免直接比较原始百分位", False, [
    ("RB1", "前5%", "Top 5%", "", 1, 10, "", '{}'),
    ("RB2", "前10%", "Top 10%", "", 1, 20, "", '{}'),
    ("RB3", "前25%", "Top 25%", "", 1, 30, "", '{}'),
    ("RB4", "前50%", "Top 50%", "", 1, 40, "", '{}'),
    ("RB5", "后50%", "Bottom 50%", "", 1, 50, "", '{}'),
    ("RB9", "未提供", "Not Provided", "", 1, 90, "", '{}'),
])

ct("CT_PROJECT_TYPE", "项目类型", "替换 project_record.project_type 的中文标签原值", False, [
    ("PJ1", "科研课题（纵向）", "Funded Research Project", "", 1, 10, "", '{}'),
    ("PJ2", "横向合作项目", "Industry-sponsored Project", "", 1, 20, "", '{}'),
    ("PJ3", "临床试验项目", "Clinical Trial", "", 1, 30, "", '{}'),
    ("PJ4", "公共卫生项目", "Public Health Program", "", 1, 40, "", '{}'),
    ("PJ5", "产品研发项目", "Product Development", "", 1, 50, "", '{}'),
    ("PJ6", "质量改进项目", "Quality Improvement", "", 1, 60, "", '{}'),
    ("PJ7", "教学与课程建设", "Teaching & Curriculum", "", 1, 70, "", '{}'),
    ("PJ8", "信息化与数据项目", "IT & Data Project", "", 1, 80, "", '{}'),
    ("PJ9", "公益与志愿服务", "Volunteer Program", "", 1, 90, "", '{}'),
    ("PJ10", "其他", "Other", "", 1, 990, "", '{}'),
])

ct("CT_PROJECT_ROLE", "项目角色", "", False, [
    ("PR1", "负责人或主持", "Principal Investigator", "", 1, 10, "", '{}'),
    ("PR2", "核心成员", "Core Member", "", 1, 20, "", '{}'),
    ("PR3", "一般成员", "Member", "", 1, 30, "", '{}'),
    ("PR4", "数据与统计角色", "Data & Statistics", "", 1, 40, "", '{}'),
    ("PR5", "协调与联络角色", "Coordinator", "", 1, 50, "", '{}'),
    ("PR6", "观察与参与", "Observer", "", 1, 60, "", '{}'),
])

ct("CT_AWARD_LEVEL", "奖项层级", "替换 award_honor.level 的中文标签原值", False, [
    ("AW1", "国家级", "National", "", 1, 10, "与现有原值「国家级」对应", '{}'),
    ("AW2", "省部级", "Provincial or Ministerial", "", 1, 20, "与现有原值「省级」对应", '{}'),
    ("AW3", "市级", "Municipal", "", 1, 30, "", '{}'),
    ("AW4", "校级或院级", "University or School", "", 1, 40, "与现有原值「校级」对应", '{}'),
    ("AW5", "学会或协会", "Society or Association", "", 1, 50, "", '{}'),
    ("AW6", "企业或机构奖", "Corporate or Institutional", "", 1, 60, "", '{}'),
])

ct("CT_CLINICAL_DEPARTMENT", "临床科室", "替换 clinical_exposure.department 的中文标签原值", False, [
    ("DP01", "内科", "General Internal Medicine", "", 1, 10, "", '{}'),
    ("DP02", "外科", "General Surgery", "", 1, 20, "", '{}'),
    ("DP03", "妇产科", "Obstetrics & Gynecology", "", 1, 30, "", '{}'),
    ("DP04", "儿科", "Pediatrics", "", 1, 40, "", '{}'),
    ("DP05", "急诊科", "Emergency Medicine", "", 1, 50, "", '{}'),
    ("DP06", "重症医学科", "Critical Care", "", 1, 60, "", '{}'),
    ("DP07", "麻醉科", "Anesthesiology", "", 1, 70, "", '{}'),
    ("DP08", "骨科", "Orthopedics", "", 1, 80, "", '{}'),
    ("DP09", "神经内科", "Neurology", "", 1, 90, "", '{}'),
    ("DP10", "神经外科", "Neurosurgery", "", 1, 100, "", '{}'),
    ("DP11", "心血管内科", "Cardiology", "", 1, 110, "", '{}'),
    ("DP12", "心血管外科", "Cardiac Surgery", "", 1, 120, "", '{}'),
    ("DP13", "呼吸与危重症医学科", "Pulmonary & Critical Care", "", 1, 130, "", '{}'),
    ("DP14", "消化内科", "Gastroenterology", "", 1, 140, "", '{}'),
    ("DP15", "内分泌科", "Endocrinology", "", 1, 150, "", '{}'),
    ("DP16", "肿瘤科", "Oncology", "", 1, 160, "", '{}'),
    ("DP17", "影像科与放射科", "Radiology", "", 1, 170, "", '{}'),
    ("DP18", "超声医学科", "Ultrasound", "", 1, 180, "", '{}'),
    ("DP19", "核医学科", "Nuclear Medicine", "", 1, 190, "", '{}'),
    ("DP20", "检验科与病理科", "Laboratory & Pathology", "", 1, 200, "", '{}'),
    ("DP21", "口腔科", "Stomatology", "", 1, 210, "", '{}'),
    ("DP22", "中医科与针灸推拿", "TCM & Acupuncture", "", 1, 220, "", '{}'),
    ("DP23", "药学与临床药学", "Pharmacy", "", 1, 230, "", '{}'),
    ("DP24", "护理单元", "Nursing Unit", "", 1, 240, "", '{}'),
    ("DP25", "全科与社区卫生", "General Practice & Community", "", 1, 250, "", '{}'),
    ("DP26", "临床试验病房", "Clinical Trial Ward", "", 1, 260, "", '{}'),
    ("DP27", "手术室", "Operating Room", "", 1, 270, "现有数据里出现 6 次", '{}'),
    ("DP28", "病区（未细分）", "General Ward", "", 1, 280, "现有数据里出现 内科病区/外科病区/儿科病区", '{}'),
    ("DP29", "疼痛科", "Pain Medicine", "", 1, 290, "", '{}'),
])

ct("CT_ASSESSMENT_DIMENSION", "测评维度", "标准化测评的维度名，与 skill_assertion 的能力概念分层不同", False, [
    ("AD1", "逻辑推理", "Logical Reasoning", "", 1, 10, "", '{}'),
    ("AD2", "数据分析", "Data Analysis", "", 1, 20, "", '{}'),
    ("AD3", "沟通表达", "Communication", "", 1, 30, "", '{}'),
    ("AD4", "学习敏捷度", "Learning Agility", "", 1, 40, "", '{}'),
    ("AD5", "抗压与情绪稳定", "Stress Resilience", "", 1, 50, "", '{}'),
    ("AD6", "团队协作", "Teamwork", "", 1, 60, "", '{}'),
    ("AD7", "数字素养", "Digital Literacy", "", 1, 70, "", '{}'),
    ("AD8", "英语读写", "English Literacy", "", 1, 80, "", '{}'),
])

ct("CT_VOLUME_BAND", "操作例数区间", "临床操作或手术例数的段位化，避免直接比较原始计数", False, [
    ("VB0", "无操作经历", "None", "", 1, 10, "", '{}'),
    ("VB1", "1-49例", "1-49", "", 1, 20, "", '{}'),
    ("VB2", "50-199例", "50-199", "", 1, 30, "", '{}'),
    ("VB3", "200-499例", "200-499", "", 1, 40, "", '{}'),
    ("VB4", "500-999例", "500-999", "", 1, 50, "", '{}'),
    ("VB5", "1000例以上", "1000+", "", 1, 60, "", '{}'),
])

ct("CT_WORK_INTENSITY", "工作强度容忍", "原有 PF5 的自由文本取值段位化", False, [
    ("WI1", "规律作息优先", "Regular Schedule", "", 1, 10, "", '{}'),
    ("WI2", "可接受适度加班", "Moderate Overtime", "", 1, 20, "", '{}'),
    ("WI3", "可接受高强度", "High Intensity", "", 1, 30, "与现有 PF5 原值「可接受高强度」对应", '{}'),
    ("WI4", "可接受夜班与轮班", "Night Shift OK", "", 1, 40, "与现有 PF5 原值「可接受夜班」对应", '{}'),
    ("WI5", "可接受频繁出差", "Frequent Travel OK", "", 1, 50, "", '{}'),
])

ct("CT_GROWTH_PREF", "成长性偏好", "原有 PF7 的取值归一化", False, [
    ("GP1", "重视平台稳定性", "Platform Stability", "", 1, 10, "与现有 PF7 原值对应", '{}'),
    ("GP2", "重视学习与成长", "Learning & Growth", "", 1, 20, "", '{}'),
    ("GP3", "重视晋升速度", "Promotion Speed", "", 1, 30, "", '{}'),
    ("GP4", "重视收入增长", "Income Growth", "", 1, 40, "与现有 PF7 原值「重视收入」对应", '{}'),
    ("GP5", "重视行业前景", "Industry Outlook", "", 1, 50, "", '{}'),
])

ct("CT_STABILITY_PREF", "稳定性偏好", "对应 PF6（词表已定义，当前数据 0 行）", False, [
    ("ST1", "追求体制内稳定", "Public-sector Stability", "", 1, 10, "", '{}'),
    ("ST2", "偏好大型机构", "Large Institution", "", 1, 20, "", '{}'),
    ("ST3", "可接受成长期公司", "Growth-stage Company", "", 1, 30, "", '{}'),
    ("ST4", "偏好创业环境", "Startup Environment", "", 1, 40, "", '{}'),
    ("ST5", "无明确偏好", "No Preference", "", 1, 50, "", '{}'),
])

ct("CT_EVIDENCE_STRENGTH", "画像证据强度", "跨维度的置信折扣依据，本身不是岗位要求", False, [
    ("EG0", "仅自述", "Self-reported Only", "", 1, 10, "", '{}'),
    ("EG1", "自述加单一材料", "Self-report + 1 Material", "", 1, 20, "", '{}'),
    ("EG2", "材料可核验", "Verifiable Material", "", 1, 30, "", '{}'),
    ("EG3", "第三方验证", "Third-party Verified", "", 1, 40, "", '{}'),
    ("EG4", "履职记录或雇主确认", "Employment Record Confirmed", "", 1, 50, "", '{}'),
])

_CITY_REGIONS = [
    ("RG1", "华北", "North China", "", 1, 10, "", '{}'),
    ("RG2", "华东", "East China", "", 1, 20, "", '{}'),
    ("RG3", "华南", "South China", "", 1, 30, "", '{}'),
    ("RG4", "华中", "Central China", "", 1, 40, "", '{}'),
    ("RG5", "西南", "Southwest China", "", 1, 50, "", '{}'),
    ("RG6", "西北", "Northwest China", "", 1, 60, "", '{}'),
    ("RG7", "东北", "Northeast China", "", 1, 70, "", '{}'),
    ("RG8", "海外", "Overseas", "", 1, 80, "", '{}'),
    ("RG9", "其他", "Other", "", 1, 90, "", '{}'),
]
_CITY_LIST = [
    ("CTY01", "北京", "Beijing", "RG1"), ("CTY02", "天津", "Tianjin", "RG1"),
    ("CTY03", "石家庄", "Shijiazhuang", "RG1"), ("CTY04", "太原", "Taiyuan", "RG1"),
    ("CTY05", "上海", "Shanghai", "RG2"), ("CTY06", "南京", "Nanjing", "RG2"),
    ("CTY07", "苏州", "Suzhou", "RG2"), ("CTY08", "无锡", "Wuxi", "RG2"),
    ("CTY09", "杭州", "Hangzhou", "RG2"), ("CTY10", "宁波", "Ningbo", "RG2"),
    ("CTY11", "合肥", "Hefei", "RG2"), ("CTY12", "济南", "Jinan", "RG2"),
    ("CTY13", "青岛", "Qingdao", "RG2"), ("CTY14", "福州", "Fuzhou", "RG2"),
    ("CTY15", "厦门", "Xiamen", "RG2"), ("CTY16", "广州", "Guangzhou", "RG3"),
    ("CTY17", "深圳", "Shenzhen", "RG3"), ("CTY18", "佛山", "Foshan", "RG3"),
    ("CTY19", "东莞", "Dongguan", "RG3"), ("CTY20", "武汉", "Wuhan", "RG4"),
    ("CTY21", "长沙", "Changsha", "RG4"), ("CTY22", "郑州", "Zhengzhou", "RG4"),
    ("CTY23", "南昌", "Nanchang", "RG4"), ("CTY24", "成都", "Chengdu", "RG5"),
    ("CTY25", "重庆", "Chongqing", "RG5"), ("CTY26", "昆明", "Kunming", "RG5"),
    ("CTY27", "贵阳", "Guiyang", "RG5"), ("CTY28", "西安", "Xian", "RG6"),
    ("CTY29", "兰州", "Lanzhou", "RG6"), ("CTY30", "乌鲁木齐", "Urumqi", "RG6"),
    ("CTY31", "沈阳", "Shenyang", "RG7"), ("CTY32", "大连", "Dalian", "RG7"),
    ("CTY33", "长春", "Changchun", "RG7"), ("CTY34", "哈尔滨", "Harbin", "RG7"),
    ("CTY35", "海外城市", "Overseas City", "RG8"), ("CTY99", "其他城市", "Other City", "RG9"),
]
ct("CT_CITY", "城市", "地域维度统一码表：期望城市、户籍地、岗位城市都指向这里", True,
   _CITY_REGIONS + [(c, l, e, p, 2, 100 + i * 10, "", '{}')
                    for i, (c, l, e, p) in enumerate(_CITY_LIST, 1)])

# 追加到已有词表 CT_PREFERENCE_TYPE（只加 PF9-PF16，不动 PF1-PF8）
NEW_CODE_TABLES["CT_PREFERENCE_TYPE"] = {
    "name": "偏好类型", "desc": "仅在已有词表上追加 PF9-PF16", "hierarchical": False,
    "options": [
        ("PF9", "兴趣方向", "Interest Domain", "", 1, 90, "多选；选项表 CT_INTEREST_DOMAIN", '{"option_table":"CT_INTEREST_DOMAIN"}'),
        ("PF10", "工作风格", "Work Style", "", 1, 100, "多选；选项表 CT_WORK_STYLE", '{"option_table":"CT_WORK_STYLE"}'),
        ("PF11", "价值观取向", "Value Orientation", "", 1, 110, "多选；选项表 CT_VALUE_ORIENT", '{"option_table":"CT_VALUE_ORIENT"}'),
        ("PF12", "职业目标", "Career Goal", "", 1, 120, "多选；选项表 CT_CAREER_GOAL", '{"option_table":"CT_CAREER_GOAL"}'),
        ("PF13", "工作方式偏好", "Work Mode Preference", "", 1, 130, "多选；选项表 CT_WORK_MODE", '{"option_table":"CT_WORK_MODE"}'),
        ("PF14", "值班意愿", "Shift Willingness", "", 1, 140, "单选；选项表 CT_SHIFT_WILLING", '{"option_table":"CT_SHIFT_WILLING"}'),
        ("PF15", "地域流动意愿", "Mobility Willingness", "", 1, 150, "单选；选项表 CT_MOBILITY", '{"option_table":"CT_MOBILITY"}'),
        ("PF16", "组织文化偏好", "Org Culture Preference", "", 1, 160, "多选；选项表 CT_ORG_CULTURE", '{"option_table":"CT_ORG_CULTURE"}'),
    ],
}

# ---------------------------------------------------------------------------
# 2. 新字段（追加到 field_catalog_seed.csv，并由 013 同步建立）
# ---------------------------------------------------------------------------
NEW_FIELDS = [
    ("F_PREF_INTEREST_DOMAIN", "preference", "兴趣方向", "自写补充为主的高频维度；选项见 CT_INTEREST_DOMAIN",
     "array", "", "", "CT_INTEREST_DOMAIN", "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_WORK_STYLE", "preference", "工作风格", "多选自评", "array", "", "", "CT_WORK_STYLE",
     "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_VALUE_ORIENT", "preference", "价值观取向", "与 CT_ORG_CULTURE 配对形成文化契合两侧", "array", "", "",
     "CT_VALUE_ORIENT", "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_CAREER_GOAL", "preference", "职业目标", "", "array", "", "", "CT_CAREER_GOAL",
     "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_WORK_MODE", "preference", "工作方式偏好", "", "array", "", "", "CT_WORK_MODE",
     "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_SHIFT_WILLING", "preference", "值班意愿", "", "code", "", "", "CT_SHIFT_WILLING",
     "scalar", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_MOBILITY", "preference", "地域流动意愿", "", "code", "", "", "CT_MOBILITY",
     "scalar", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_ORG_CULTURE", "preference", "组织文化偏好", "", "array", "", "", "CT_ORG_CULTURE",
     "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PREF_EXPECT_CITY", "preference", "期望工作城市", "原有 PF1 的码化目标", "array", "", "", "CT_CITY",
     "array", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PERSON_HUKOU_CITY", "person_demographics", "户籍所在地", "现有 hukou_province 实际存的是城市名",
     "code", "", "", "CT_CITY", "scalar", "questionnaire", "", "", "", "T2", "active", "1.0.0", "gov"),
    ("F_CLIN_DEPARTMENT", "clinical_exposure", "临床科室", "临床暴露的科室归属", "code", "", "",
     "CT_CLINICAL_DEPARTMENT", "scalar", "resume_parse", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_PRJ_PROJECT_TYPE", "project_record", "项目类型", "", "code", "", "", "CT_PROJECT_TYPE",
     "scalar", "resume_parse", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_AWD_AWARD_LEVEL", "award_honor", "奖项层级", "", "code", "", "", "CT_AWARD_LEVEL",
     "scalar", "resume_parse", "", "", "", "T1", "active", "1.0.0", "data"),
    ("F_ASM_DIMENSION", "assessment", "测评维度", "", "code", "", "", "CT_ASSESSMENT_DIMENSION",
     "scalar", "questionnaire", "", "", "", "T1", "active", "1.0.0", "data"),
]

# ---------------------------------------------------------------------------
# 3. 维度注册表（50 个维度）
# ---------------------------------------------------------------------------
_CMP = {"enum": "enum_eq", "ordinal": "ordinal_ge", "set": "set_overlap",
        "range": "range_overlap", "text": "text_none"}
DIMENSIONS = []


def dim(did, gid, gtitle, title, kind, ctid, allow_custom, is_hard, role, weight,
        ploc, psrc, pfid, jloc, jsrc, jgap, note):
    DIMENSIONS.append({
        "dimension_id": did, "group_id": gid, "group_title": gtitle, "title_zh": title,
        "kind": kind, "comparator": _CMP[kind], "code_table_id": ctid,
        "allow_custom": allow_custom, "is_hard": is_hard, "role": role, "weight": weight,
        "person_locator": ploc, "person_source": psrc, "person_field_id": pfid,
        "job_locator": jloc, "job_source": jsrc, "job_gap": jgap, "note": note})


G1, H1 = "G1", "学历与专业"
G2, H2 = "G2", "资质与准入"
G3, H3 = "G3", "经历与年限"
G4, H4 = "G4", "科研与产出"
G5, H5 = "G5", "能力与知识"
G6, H6 = "G6", "兴趣与价值取向"
G7, H7 = "G7", "地域与流动"
G8, H8 = "G8", "薪酬与强度"
G9, H9 = "G9", "岗位族与综合"

dim("DIM_DEGREE_LEVEL", G1, H1, "学历层次", "ordinal", "CT_DEGREE_LEVEL", False, True, "gate", 1.0,
    "education_record.degree_level", "education_record.degree_level", "F_EDU_DEGREE_LEVEL",
    "job_posting.education_req", "job_posting.education_req", None,
    "两侧均有值；岗位侧 690/690 有码值 D1-D4")
dim("DIM_MAJOR", G1, H1, "专业匹配", "set", "CT_MAJOR", True, True, "gate", 0.9,
    "education_record.major_code", "education_record.major_code", "F_EDU_MAJOR_CODE",
    "job_requirement:RT1.raw_text", "job_requirement.raw_text(RT1)", "job_posting.major_req 列存在但 690/690 全空；专业要求只写在 RT1 自由文本里，需新增解析产出多值专业码",
    "岗位侧当前只能做文本包含，不能做集合比较")
dim("DIM_IS_CLINICAL_MAJOR", G1, H1, "是否临床医学类", "enum", "CT_YES_NO", False, False, "score", 0.4,
    "education_record.is_clinical", "education_record.is_clinical", "F_EDU_IS_CLINICAL",
    "derived:DIM_MAJOR", "由岗位要求的专业集合推导", None, "岗位侧为派生值，不是独立要求")
dim("DIM_ACADEMIC_RANK", G1, H1, "学业排名段位", "ordinal", "CT_RANK_BAND", False, False, "display", 0.0,
    "education_record.rank_percentile", "education_record.rank_percentile 派生段位", "F_EDU_RANK_PCT",
    "unavailable", "无结构化要求", "岗位侧无字段",
    "仅展示：学业排名不能作为录用排序依据")
dim("DIM_SCHOOL_TIER", G1, H1, "院校层次", "ordinal", "CT_SCHOOL_TIER", False, False, "score", 0.6,
    "education_record.school_tags", "education_record.school_tags", "F_EDU_SCHOOL_TAGS",
    "unavailable", "无结构化要求", "岗位侧无字段；只有 job_requirement.raw_text 里偶见名校偏好，需新增 job_posting.school_tier_req",
    "现存 school_tags 存的是中文标签（省重点/双一流/211/985/医学强校），含词表外取值，需先码化")

dim("DIM_CREDENTIAL", G2, H2, "资质证书持有", "set", "CT_CREDENTIAL_TYPE", True, True, "gate", 1.0,
    "credential.credential_type", "credential.credential_type", "F_CRED_TYPE",
    "job_requirement:RT4.concept_id", "job_requirement.concept_id(RT4)", "job_posting.license_req 列存在但 690/690 全空；证照要求只在 job_requirement(RT4, 240 行)里",
    "两侧有值；岗位侧 120/240 行映射到了概念")
dim("DIM_CREDENTIAL_STATUS", G2, H2, "证书状态", "ordinal", "CT_CRED_STATUS", False, True, "gate", 0.5,
    "credential.status", "credential.status", "F_CRED_STATUS",
    "unavailable", "岗位只要求持有，不区分状态", None, "持证状态是硬门槛的自检条件，岗位侧无需对应")
dim("DIM_TRAINING", G2, H2, "规培与培养经历", "set", "CT_TRAINING_TYPE", True, True, "gate", 0.9,
    "credential.credential_type", "credential(C02/C03) + training_record.training_type", "F_TRAIN_TYPE",
    "job_requirement:RT4.raw_text", "job_requirement.raw_text(RT4)", "岗位侧无规培要求字段，只在 RT4 文本里",
    "training_record 表 0 行，规培信息目前只体现在 credential 的 C02/C03")
dim("DIM_HEALTH_LIMIT", G2, H2, "体检受限项", "set", "CT_HEALTH_LIMIT", True, True, "gate", 0.3,
    "person_demographics.health_limits", "person_demographics.health_limits", "F_PERSON_HEALTH_LIMITS",
    "unavailable", "合规上不得写入岗位要求", "岗位侧不应有对应字段",
    "仅做单向过滤，禁止体检歧视")
dim("DIM_POLITICAL", G2, H2, "政治面貌", "enum", "CT_POLITICAL", False, True, "gate", 0.2,
    "person_demographics.political_status", "person_demographics.political_status", "F_PERSON_POLITICAL",
    "unavailable", "选调生与公务员岗位在 raw_text 里要求", "需新增 job_posting.political_req",
    "仅少部分公共部门岗位为硬门槛")
dim("DIM_AGE_BAND", G2, H2, "年龄段", "ordinal", "CT_AGE_BAND", False, False, "score", 0.2,
    "person_demographics.birth_year", "person_demographics.birth_year 派生段位", "F_PERSON_BIRTH_YEAR",
    "unavailable", "年龄上限只在 raw_text", "需新增 job_posting.age_max",
    "年龄不得作为主要排序依据")
dim("DIM_SEX", G2, H2, "性别", "enum", "CT_SEX", False, False, "display", 0.0,
    "person_demographics.sex", "person_demographics.sex", "F_PERSON_SEX",
    "unavailable", "不得作为岗位要求", "岗位侧不应有对应字段",
    "只做展示与聚合分层，绝不参与打分")

dim("DIM_WORK_YEARS", G3, H3, "工作年限", "ordinal", "CT_EXPERIENCE_BAND", False, True, "gate", 1.0,
    "derived:employment_record 起止日期", "employment_record.start_date/end_date 派生", None,
    "job_posting.experience_req", "job_posting.experience_req", "需把 experience_req 自由文本归一化为码值；已确认 job_posting.experience_min_years 不存在",
    "岗位侧 690/690 有值但只有 3 种自由文本（应届/1-3年/3-5年），无码表")
dim("DIM_EMPLOYER_TYPE", G3, H3, "单位类型", "set", "CT_EMPLOYER_TYPE", True, False, "score", 0.6,
    "employment_record.employer_type", "employment_record.employer_type", "F_EMP_EMPLOYER_TYPE",
    "unavailable", "三甲与基层偏好只在 raw_text", "需新增 job_posting.employer_type（该列确实不存在）",
    "岗位侧缺列，已实测确认")
dim("DIM_CLINICAL_BAND", G3, H3, "临床暴露层级", "ordinal", "CT_CLINICAL_BAND", False, False, "score", 0.7,
    "derived:clinical_exposure + credential", "clinical_exposure 例数与科室 + credential(C01/C02) 派生", None,
    "unavailable", "岗位侧无结构化要求", "需新增岗位侧临床经历要求，或从 RT8 文本解析",
    "人侧有值（clinical_exposure 143 行），岗位侧完全缺失")
dim("DIM_CLINICAL_DEPARTMENT", G3, H3, "临床科室经历", "set", "CT_CLINICAL_DEPARTMENT", True, False, "score", 0.5,
    "clinical_exposure.department", "clinical_exposure.department", "F_CLIN_DEPARTMENT",
    "unavailable", "岗位侧无结构化要求", "可从 job_posting.title_raw 与 occupation_id 反推",
    "现存 department 是中文标签，department_code 143/143 全空；30 种原值已全部覆盖进词表")
dim("DIM_CLINICAL_VOLUME", G3, H3, "操作例数区间", "ordinal", "CT_VOLUME_BAND", False, False, "score", 0.4,
    "clinical_exposure.procedure_count", "clinical_exposure.procedure_count 派生区间", "F_CLIN_PROCEDURE_COUNT",
    "unavailable", "岗位侧无结构化要求", "岗位侧通常在 raw_text 里写例数门槛",
    "区间化避免原始计数不可比")
dim("DIM_PROJECT_TYPE", G3, H3, "项目经历类型", "set", "CT_PROJECT_TYPE", True, False, "score", 0.5,
    "project_record.project_type", "project_record.project_type", "F_PRJ_PROJECT_TYPE",
    "job_requirement:RT3.concept_id", "job_requirement(RT3/RT5)", "岗位侧无项目要求字段",
    "现存 project_type 是 5 种中文标签原值，需码化")
dim("DIM_PROJECT_ROLE", G3, H3, "项目角色", "ordinal", "CT_PROJECT_ROLE", True, False, "score", 0.4,
    "project_record.role", "project_record.role", None,
    "unavailable", "岗位侧无结构化要求", "可从 RT3 文本里的负责与参与推断",
    "人侧有值，岗位侧缺失")
dim("DIM_JOB_ZONE", G3, H3, "岗位准备度", "ordinal", "CT_JOB_ZONE", False, False, "score", 0.5,
    "derived:学位与年限", "由 DIM_DEGREE_LEVEL 与 DIM_WORK_YEARS 派生", "F_JOB_ZONE",
    "job_posting.job_zone", "job_posting.job_zone", "job_posting.job_zone 列存在但 690/690 全空，需回填",
    "列在、值不在")

dim("DIM_RESEARCH_LEVEL", G4, H4, "科研层级", "ordinal", "CT_RESEARCH_LEVEL", False, False, "score", 0.7,
    "derived:research_output", "research_output.is_first_author 与 author_position 派生", None,
    "job_requirement:RT5.concept_id", "job_requirement(RT5/RT3) 里的发表要求", "岗位侧无科研等级要求字段",
    "人侧 89 行产出，岗位侧只有文本")
dim("DIM_RESEARCH_OUTPUT_TYPE", G4, H4, "科研产出类型", "set", "CT_RESEARCH_OUTPUT_TYPE", True, False, "score", 0.4,
    "research_output.output_type", "research_output.output_type", "F_RES_OUTPUT_TYPE",
    "unavailable", "岗位侧无结构化要求", "需新增岗位侧产出要求",
    "两侧无对应，人侧可枚举")
dim("DIM_AWARD_LEVEL", G4, H4, "获奖层级", "ordinal", "CT_AWARD_LEVEL", True, False, "score", 0.3,
    "award_honor.level", "award_honor.level", "F_AWD_AWARD_LEVEL",
    "unavailable", "岗位侧无结构化要求", "极少岗位在 raw_text 里提奖项",
    "现存 level 是中文标签原值，需码化")
dim("DIM_OVERSEAS", G4, H4, "海外经历", "enum", "CT_YES_NO", False, False, "score", 0.2,
    "education_record.overseas", "education_record.overseas", None,
    "unavailable", "岗位侧无结构化要求", "部分岗位 raw_text 提海外背景优先",
    "人侧布尔，岗位侧缺失")

dim("DIM_CONCEPT_SKILL", G5, H5, "技能概念（K1）", "set", None, True, False, "score", 1.0,
    "concept:K1", "skill_assertion.concept_id 且 concept_type=K1", "F_SKL_CONCEPT_ID",
    "job_requirement:RT5.concept_id", "job_requirement.concept_id(RT5)", None,
    "现 compute_matches 唯一使用的维度；540 条 RT5 要求全部映射到 24 个概念。allow_custom 走能力候选概念池而非词表候选")
dim("DIM_CONCEPT_ABILITY", G5, H5, "通用能力概念（K3）", "set", None, True, False, "score", 0.9,
    "concept:K3", "skill_assertion.concept_id 且 concept_type=K3", "F_SKL_CONCEPT_ID",
    "job_requirement:RT5.concept_id", "job_requirement.concept_id(RT5)", None,
    "与 K1 共用同一张表，靠 concept_type 区分；K3 当前只有 13 个概念。allow_custom 同上")
dim("DIM_SKILL_LEVEL", G5, H5, "能力等级", "ordinal", "CT_ABILITY_LEVEL", False, False, "score", 0.8,
    "skill_assertion.level", "skill_assertion.level(1-5)", "F_SKL_LEVEL",
    "job_requirement.min_level", "job_requirement.min_level", "job_requirement.min_level 2970/2970 全空，岗位侧没有任何等级要求",
    "概念能对上但等级只有人侧有值，因此当前只能做命中与缺口二值比较")
dim("DIM_SKILL_BASIS", G5, H5, "等级判定依据", "enum", "CT_LEVEL_BASIS", False, False, "modifier", 0.0,
    "skill_assertion.level_basis", "skill_assertion.level_basis", "F_SKL_LEVEL_BASIS",
    "unavailable", "不是岗位要求", None, "作为置信折扣使用，权重必须为 0")
dim("DIM_ASSESSMENT_DIMENSION", G5, H5, "测评维度得分", "set", "CT_ASSESSMENT_DIMENSION", True, False, "score", 0.3,
    "assessment.dimension", "assessment.dimension 与 percentile", "F_ASM_DIMENSION",
    "unavailable", "岗位侧无结构化要求", "需新增岗位侧测评要求",
    "现存 162 行测评维度是中文标签，且工具名为合成示例")
dim("DIM_LANGUAGE_LEVEL", G5, H5, "语言能力", "ordinal", "CT_LANGUAGE_LEVEL", True, False, "score", 0.6,
    "credential:C09", "credential(C09)，无独立语言等级列", None,
    "job_requirement:RT7.raw_text", "job_requirement.raw_text(RT7, 90 行)", "需新增 person_language 表或字段",
    "已确认：岗位侧有 90 行语言要求，人侧没有任何结构化语言等级字段")

dim("DIM_INTEREST_DOMAIN", G6, H6, "兴趣方向", "set", "CT_INTEREST_DOMAIN", True, False, "score", 0.5,
    "preference:PF9", "preference.value_code(pref_type=PF9)", "F_PREF_INTEREST_DOMAIN",
    "job_posting.job_family", "job_posting.job_family 经 CT_INTEREST_DOMAIN.external_mapping 反查", None,
    "用户点名的维度：29 个选项，external_mapping 直连岗位族，因此两侧可匹配")
dim("DIM_CAREER_GOAL", G6, H6, "职业目标", "set", "CT_CAREER_GOAL", True, False, "score", 0.5,
    "preference:PF12", "preference.value_code(pref_type=PF12)", "F_PREF_CAREER_GOAL",
    "job_posting.job_family", "job_posting.job_family 与 occupation.family", None,
    "复用用户已补的 CT_CAREER_GOAL（8 项）")
dim("DIM_WORK_STYLE", G6, H6, "工作风格", "set", "CT_WORK_STYLE", True, False, "score", 0.4,
    "preference:PF10", "preference.value_code(pref_type=PF10)", "F_PREF_WORK_STYLE",
    "job_requirement:RT8.raw_text", "job_requirement.raw_text(RT8)", "岗位侧需新增软性要求词表（RT8 现有 900 行自由文本）",
    "两侧原本都不存在，先建人侧词表，岗位侧走 RT8 文本匹配")
dim("DIM_VALUE_ORIENT", G6, H6, "价值观取向", "set", "CT_VALUE_ORIENT", True, False, "score", 0.3,
    "preference:PF11", "preference.value_code(pref_type=PF11)", "F_PREF_VALUE_ORIENT",
    "unavailable", "无结构化要求", "需新增岗位侧组织价值观要求",
    "两侧都没有，属诚实缺口")
dim("DIM_ORG_CULTURE_FIT", G6, H6, "组织文化契合", "set", "CT_ORG_CULTURE", True, False, "score", 0.3,
    "preference:PF16", "preference.value_code(pref_type=PF16)", "F_PREF_ORG_CULTURE",
    "unavailable", "无结构化要求", "需在 employer.attrs 或 job_posting 上新增 org_culture",
    "复用用户已补的 CT_ORG_CULTURE（6 项），岗位侧仍为空")

dim("DIM_EXPECT_CITY", G7, H7, "期望工作城市", "set", "CT_CITY", True, False, "score", 0.9,
    "preference:PF1", "preference.value_raw(pref_type=PF1)，value_code 120/120 为空", "F_PREF_EXPECT_CITY",
    "job_posting.city", "job_posting.city", None,
    "两侧有值；PF1 的 value_code 全空，需回填 CT_CITY 码；job_posting.province 690/690 为空，地域只能靠 city")
dim("DIM_CITY_TIER", G7, H7, "城市层级", "ordinal", "CT_CITY_TIER", False, False, "score", 0.5,
    "derived:DIM_EXPECT_CITY", "由期望城市映射 CT_CITY_TIER", None,
    "derived:job_posting.city", "由 job_posting.city 映射 CT_CITY_TIER", None,
    "两侧都可派生，但需要 city 到 tier 的映射，当前没有")
dim("DIM_MOBILITY", G7, H7, "地域流动意愿", "ordinal", "CT_MOBILITY", True, False, "score", 0.5,
    "preference:PF15", "preference.value_code(pref_type=PF15)", "F_PREF_MOBILITY",
    "unavailable", "岗位侧无对应字段", "岗位侧可由 job_posting.city 与户籍地距离间接反映",
    "复用用户已补的 CT_MOBILITY（4 项）")
dim("DIM_HUKOU_CITY", G7, H7, "户籍所在地", "enum", "CT_CITY", True, False, "score", 0.2,
    "person_demographics.hukou_province", "person_demographics.hukou_province", "F_PERSON_HUKOU_CITY",
    "unavailable", "不得作为岗位限制条件", "仅用于本地化运营与返乡倾向分析",
    "列名叫 province 但实际存的是城市（郑州/天津/青岛…），需重命名或明确口径")
dim("DIM_HUKOU_TYPE", G7, H7, "户籍类型", "enum", "CT_HUKOU_TYPE", False, False, "score", 0.1,
    "person_demographics.hukou_type", "person_demographics.hukou_type", "F_PERSON_HUKOU_TYPE",
    "unavailable", "个别岗位报考资格相关", "需新增 job_posting.hukou_req",
    "仅极少数岗位相关")
dim("DIM_WORK_MODE", G7, H7, "工作方式偏好", "set", "CT_WORK_MODE", True, False, "score", 0.4,
    "preference:PF13", "preference.value_code(pref_type=PF13)", "F_PREF_WORK_MODE",
    "job_posting.employment_type", "job_posting.employment_type", "job_posting.employment_type 列存在但 690/690 全空，需回填",
    "复用用户已补的 CT_WORK_MODE（6 项）；岗位侧列在值不在")

dim("DIM_SALARY_EXPECT", G8, H8, "薪资期望区间", "range", None, False, False, "score", 0.8,
    "preference:PF4", "preference.value_code(pref_type=PF4) 形如 monthly:10000-15000", None,
    "job_posting.salary_min", "job_posting.salary_min 与 salary_max", None,
    "两侧有值（岗位 598/690 披露）；按区间覆盖比例打分，不做数值相减")
dim("DIM_SALARY_BAND", G8, H8, "薪资档位", "ordinal", "CT_SALARY_BAND", False, False, "display", 0.0,
    "derived:DIM_SALARY_EXPECT", "由期望区间映射档位", None,
    "derived:job_posting.salary_min", "由岗位薪资映射档位", None,
    "只用于界面分档展示，不参与打分")
dim("DIM_WORK_INTENSITY", G8, H8, "工作强度容忍", "ordinal", "CT_WORK_INTENSITY", True, False, "score", 0.5,
    "preference:PF5", "preference.value_raw(pref_type=PF5)，value_code 全空", None,
    "job_requirement:RT8.raw_text", "job_requirement.raw_text(RT8)", "需新增岗位侧强度要求字段",
    "人侧 57 行只有中文文本，需码化")
dim("DIM_SHIFT_WILLING", G8, H8, "值班意愿", "ordinal", "CT_SHIFT_WILLING", True, False, "score", 0.5,
    "preference:PF14", "preference.value_code(pref_type=PF14)", "F_PREF_SHIFT_WILLING",
    "job_requirement:RT8.raw_text", "job_requirement.raw_text(RT8) 里的夜班要求", "需新增 job_posting.shift_required",
    "复用用户已补的 CT_SHIFT_WILLING（3 项）")
dim("DIM_GROWTH_PREF", G8, H8, "成长性偏好", "ordinal", "CT_GROWTH_PREF", True, False, "score", 0.3,
    "preference:PF7", "preference.value_raw(pref_type=PF7)，value_code 全空", None,
    "unavailable", "岗位侧无对应字段", "岗位侧可用 occupation.transition_ease 近似",
    "人侧 50 行中文文本，需码化")
dim("DIM_STABILITY_PREF", G8, H8, "稳定性偏好", "ordinal", "CT_STABILITY_PREF", True, False, "score", 0.3,
    "preference:PF6", "preference.value_code(pref_type=PF6)", None,
    "unavailable", "岗位侧无对应字段", "需新增岗位侧机构稳定性字段",
    "词表 PF6 已定义但数据 0 行")

dim("DIM_JOB_FAMILY_PREF", G9, H9, "岗位族偏好", "set", "CT_JOB_FAMILY", True, False, "score", 1.0,
    "preference:PF3", "preference.value_code(pref_type=PF3)", None,
    "job_posting.job_family", "job_posting.job_family", None,
    "两侧有值；PF3 的 value_code 已填 F01-F17")
dim("DIM_ACCEPT_CROSS_INDUSTRY", G9, H9, "是否接受转行", "enum", "CT_YES_NO", False, False, "score", 0.4,
    "preference:PF8", "preference.value_code(pref_type=PF8) 取值为 Y/N", None,
    "derived:job_posting.job_family", "job_family=F16 完全跨行 视为跨行岗位", None,
    "PF8 现存码值就是 Y/N，与 CT_YES_NO 对齐即可")
dim("DIM_EVIDENCE_STRENGTH", G9, H9, "画像证据强度", "ordinal", "CT_EVIDENCE_STRENGTH", False, False, "modifier", 0.0,
    "derived:evidence 与 verify_status", "evidence.cel_level 与各表 verify_status 派生", "F_EVD_VERIFIABILITY",
    "unavailable", "不是岗位要求", None,
    "跨维度折扣：所有维度得分乘以 0.6 加 0.1 乘强度档，权重必须为 0")

assert len(DIMENSIONS) == 50, len(DIMENSIONS)

# ---------------------------------------------------------------------------
# CSV helpers（按字节保留已有行，只做追加）
# ---------------------------------------------------------------------------
CODE_COLS = ["code_table_id", "code", "label_zh", "label_en", "parent_code", "level",
             "sort_order", "definition", "external_mapping"]
FIELD_COLS = ["field_id", "entity_id", "title", "description", "data_type", "unit",
              "value_range", "code_table_id", "cardinality", "collection_method",
              "applies_to", "related_fields", "derivation", "access_tier",
              "status", "version", "owner"]
TALENT_COLS = ["dimension_id", "group_id", "group_title", "title_zh", "kind",
               "code_table_id", "comparator", "allow_custom", "is_hard", "role",
               "weight", "person_locator", "person_source", "person_field_id",
               "job_locator", "job_source", "job_gap", "note"]


def read_raw(path):
    with io.open(path, "rb") as fh:
        return fh.read().decode("utf-8-sig")


def rows_of(raw):
    lines = raw.splitlines(keepends=True)
    return lines[0], lines[1:]


def ensure_nl(line):
    return line if line.endswith("\n") else line + "\n"


def render(rows, nl="\n"):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=nl)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def patch_csv(path, my_keys, my_rows, key_len=2):
    raw = read_raw(path)
    header, body = rows_of(raw)
    kept, dropped = [], 0
    for ln in body:
        s = ln.strip()
        if not s:
            continue
        fields = next(csv.reader([ln]))
        if len(fields) >= key_len and tuple(fields[:key_len]) in my_keys:
            dropped += 1
            continue
        kept.append(ensure_nl(ln))
    # 追加行沿用文件末尾既有行尾，避免引入第三种换行风格
    nl = "\r\n" if (kept and kept[-1].endswith("\r\n")) else "\n"
    out = ensure_nl(header) + "".join(kept) + render(my_rows, nl)
    with io.open(path, "wb") as fh:
        fh.write(out.encode("utf-8"))
    return len(kept), len(my_rows), dropped


def new_code_rows():
    out = []
    for tid in sorted(NEW_CODE_TABLES):
        for (code, lzh, len_, parent, level, sort, defi, ext) in NEW_CODE_TABLES[tid]["options"]:
            out.append([tid, code, lzh, len_, parent, str(level), str(sort), defi, ext])
    return out


def write_talent_csv():
    rows = []
    for d in DIMENSIONS:
        rows.append([d["dimension_id"], d["group_id"], d["group_title"], d["title_zh"],
                     d["kind"], d["code_table_id"] or "", d["comparator"],
                     "true" if d["allow_custom"] else "false",
                     "true" if d["is_hard"] else "false", d["role"],
                     ("%g" % d["weight"]), d["person_locator"], d["person_source"],
                     d["person_field_id"] or "", d["job_locator"], d["job_source"],
                     d["job_gap"] or "", d["note"]])
    with io.open(TALENT_CSV, "wb") as fh:
        fh.write((render([TALENT_COLS] + rows)).encode("utf-8"))
    return len(rows)


# ---------------------------------------------------------------------------
# SQL 生成
# ---------------------------------------------------------------------------
def q(v):
    if v is None:
        return "NULL"
    s = str(v)
    if s == "":
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def seed_block():
    L = []
    L.append("-- ---------------------------------------------------------------------------")
    L.append("-- A. 词表与选项（只含本次新增；用户已补的 12 张词表 75 个选项不在此重复）")
    L.append("--    与 schema/catalog/code_table_seed.csv 由同一生成器产出，不会漂移；")
    L.append("--    code_table 的 name/description 与 code/load_catalog.py 完全同口径，")
    L.append("--    这样两个入口互相 UPSERT 也不会来回覆盖。")
    L.append("--    CT_PREFERENCE_TYPE 是**追加**（PF9-PF16），已有 PF1-PF8 原样保留。")
    L.append("-- ---------------------------------------------------------------------------")
    for tid in sorted(NEW_CODE_TABLES):
        t = NEW_CODE_TABLES[tid]
        L.append("")
        L.append("INSERT INTO code_table (code_table_id, name, description, hierarchical, "
                 "external_standard, version, status, owner) VALUES (%s,%s,%s,%s,NULL,%s,%s,%s) "
                 "ON CONFLICT (code_table_id) DO UPDATE SET hierarchical=EXCLUDED.hierarchical, "
                 "version=EXCLUDED.version, status=EXCLUDED.status;"
                 % (q(tid), q(tid), q("由 code_table_seed.csv 导入"),
                    "true" if t["hierarchical"] else "false", q("1.0.0"), q("active"), q("data")))
        for (code, lzh, len_, parent, level, sort, defi, ext) in t["options"]:
            L.append("INSERT INTO code_value (code_table_id, code, label_zh, label_en, definition, "
                     "parent_code, level, sort_order, aliases, external_mapping, effective_from, "
                     "deprecated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s::jsonb,NULL,false) "
                     "ON CONFLICT (code_table_id, code) DO UPDATE SET label_zh=EXCLUDED.label_zh, "
                     "label_en=EXCLUDED.label_en, definition=EXCLUDED.definition, "
                     "parent_code=EXCLUDED.parent_code, level=EXCLUDED.level, "
                     "sort_order=EXCLUDED.sort_order, external_mapping=EXCLUDED.external_mapping;"
                     % (q(tid), q(code), q(lzh), q(len_), q(defi), q(parent), q(level),
                        q(sort), q(ext)))

    L.append("")
    L.append("-- ---------------------------------------------------------------------------")
    L.append("-- B. 新字段（与 schema/catalog/field_catalog_seed.csv 同步）")
    L.append("-- ---------------------------------------------------------------------------")
    for f in NEW_FIELDS:
        L.append("INSERT INTO field_catalog (field_id, entity_id, title, description, data_type, "
                 "unit, value_range, code_table_id, cardinality, collection_method, applies_to, "
                 "related_fields, derivation, access_tier, status, version, owner) VALUES "
                 "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                 "ON CONFLICT (field_id) DO UPDATE SET title=EXCLUDED.title, "
                 "description=EXCLUDED.description, data_type=EXCLUDED.data_type, "
                 "code_table_id=EXCLUDED.code_table_id, cardinality=EXCLUDED.cardinality, "
                 "collection_method=EXCLUDED.collection_method, "
                 "access_tier=EXCLUDED.access_tier, status=EXCLUDED.status, "
                 "version=EXCLUDED.version, owner=EXCLUDED.owner;"
                 % (q(f[0]), q(f[1]), q(f[2]), q(f[3]), q(f[4]), q(f[5]), q(f[6]),
                    q(f[7]), q(f[8]), q(f[9]), q(f[10]), q(f[11]), q(f[12]),
                    q(f[13]), q(f[14]), q(f[15]), q(f[16])))

    L.append("")
    L.append("-- ---------------------------------------------------------------------------")
    L.append("-- C. 维度注册表（权威来源 schema/catalog/talent_field_seed.csv，共 %d 行）"
             % len(DIMENSIONS))
    L.append("-- ---------------------------------------------------------------------------")
    cols = ("dimension_id, group_id, group_title, title_zh, title_en, kind, comparator, "
            "code_table_id, allow_custom, custom_hint, is_hard, role, weight, "
            "person_locator, person_source, person_field_id, job_locator, job_source, job_gap, "
            "note, sort_order, status, version")
    for i, d in enumerate(DIMENSIONS, 1):
        hint = ("可自写补充：不在选项内的取值先进入候选池（concept_candidate），"
                "经评审后由 mt.promote_option_candidate 提升为正式选项"
                if d["allow_custom"] else None)
        vals = [q(d["dimension_id"]), q(d["group_id"]), q(d["group_title"]), q(d["title_zh"]),
                "NULL", q(d["kind"]), q(d["comparator"]), q(d["code_table_id"]),
                "true" if d["allow_custom"] else "false", q(hint),
                "true" if d["is_hard"] else "false", q(d["role"]), ("%g" % d["weight"]),
                q(d["person_locator"]), q(d["person_source"]), q(d["person_field_id"]),
                q(d["job_locator"]), q(d["job_source"]), q(d["job_gap"]), q(d["note"]),
                str(i * 10), q("active"), q("1.0.0")]
        L.append("INSERT INTO dimension (%s) VALUES (%s) ON CONFLICT (dimension_id) DO UPDATE SET "
                 "group_id=EXCLUDED.group_id, group_title=EXCLUDED.group_title, "
                 "title_zh=EXCLUDED.title_zh, kind=EXCLUDED.kind, comparator=EXCLUDED.comparator, "
                 "code_table_id=EXCLUDED.code_table_id, allow_custom=EXCLUDED.allow_custom, "
                 "custom_hint=EXCLUDED.custom_hint, is_hard=EXCLUDED.is_hard, role=EXCLUDED.role, "
                 "weight=EXCLUDED.weight, person_locator=EXCLUDED.person_locator, "
                 "person_source=EXCLUDED.person_source, person_field_id=EXCLUDED.person_field_id, "
                 "job_locator=EXCLUDED.job_locator, job_source=EXCLUDED.job_source, "
                 "job_gap=EXCLUDED.job_gap, note=EXCLUDED.note, sort_order=EXCLUDED.sort_order, "
                 "status=EXCLUDED.status, updated_at=now();"
                 % (cols, ", ".join(vals)))
    return "\n".join(L)


HEAD = r"""-- ============================================================================
-- 医学生人才信息库 · 013 人才画像维度体系（dimension registry）v1.0.0
--
-- 解决的问题：
--   当前匹配算法 code/bridge/projection.py::compute_matches 只用了 1 个维度
--   （能力概念 concept_overlap，输出 met/gap/unknown 三态）。而"一个人能被表示成
--   多高维的向量、哪些维度参与匹配、硬门槛还是加分、权重多少"这些问题在代码里
--   是散落的字符串常量。本迁移把这件事**变成数据**：
--   mt.dimension 一张表说清全部 50 个维度及其比较语义。
--
-- 四条纪律（与项目既有纪律一致）：
--   【D1】维度是数据不是代码。加维度 = 插一行，不改代码、不改约束。
--   【D2】枚举存码不存标签。每个维度指向一张 code_table，界面显示 label_zh。
--   【D3】空就是空。人侧无值 → 该维度记 unknown，绝不按 0 分算
--         （unknown 不是不合格，延续 projection.py 的 met/gap/unknown 三态）。
--   【D4】不许把不同 kind 的维度拉平算余弦。kind 决定 comparator，
--         硬门槛先过滤、再加权聚合，逐维度输出可解释拆解（见第 6、8 节）。
--
-- 本文件由 schema/catalog/gen_talent_fields.py 生成（与两份 catalog CSV 同源，故不会漂移）。
-- 可重放：整体幂等，重复执行后注册表与选项的行数与内容不变
--        （change_log 是 append-only 审计表，按既有约定每次留痕一条）。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. field_catalog：为"选项 + 自写"双形态加开关
--    allow_custom=false → 只能选词表内的码（由 mt.assert_option 强制）
--    allow_custom=true  → 允许自写；自写值先落 concept_candidate 候选池，
--                         评审通过后才由 mt.promote_option_candidate 变成正式码值
-- ---------------------------------------------------------------------------
ALTER TABLE field_catalog ADD COLUMN IF NOT EXISTS allow_custom BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE field_catalog ADD COLUMN IF NOT EXISTS custom_hint TEXT;

-- ---------------------------------------------------------------------------
-- 2. dimension：维度注册表
--    一行 = 一个维度。关键列：
--      kind        enum / ordinal / set / range / text
--      comparator  与 kind 绑定的一对一比较函数名（见第 6 节）
--      role        gate（硬门槛过滤）/ score（加权打分）/
--                  modifier（置信折扣，权重必须 0）/ display（仅展示，权重必须 0）
--      is_hard     与 role='gate' 等价，独立成列便于阅读与筛选
--      code_table_id  固定选项所在词表；自写补充落候选池
--      person_locator / job_locator  机器可读取值定位符：
--        education_record.degree_level   表.列
--        preference:PF9                  preference.value_code(pref_type='PF9')
--        concept:K1                      skill_assertion 且 concept.concept_type='K1'
--        job_requirement:RT5.concept_id  job_requirement 表
--        derived:...                     由其他维度派生
--        unavailable                     该侧确实没有这个信息
--    说明：code_table_id 与 person_field_id 是**软引用**，不建外键。
--    理由：词表与字段目录由 code/load_catalog.py 这条独立管线导入，schema 迁移
--    不应因为"词表还没导"而失败（013 自身也会建立它新增的那 17 张词表）。
--    引用是否落实由视图 v_dimension_dependency_gap 兜底检查。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dimension (
  dimension_id     TEXT PRIMARY KEY,
  group_id         TEXT NOT NULL,
  group_title      TEXT NOT NULL,
  title_zh         TEXT NOT NULL,
  title_en         TEXT,
  kind             TEXT NOT NULL,
  comparator       TEXT NOT NULL,
  code_table_id    TEXT,
  allow_custom     BOOLEAN NOT NULL DEFAULT false,
  custom_hint      TEXT,
  is_hard          BOOLEAN NOT NULL DEFAULT false,
  role             TEXT NOT NULL DEFAULT 'score',
  weight           NUMERIC(4,3) NOT NULL DEFAULT 0,
  person_locator   TEXT,
  person_source    TEXT,
  person_field_id  TEXT,
  job_locator      TEXT,
  job_source       TEXT,
  job_gap          TEXT,
  note             TEXT,
  sort_order       INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'active',
  version          TEXT NOT NULL DEFAULT '1.0.0',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_kind_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_kind_check
  CHECK (kind IN ('enum','ordinal','set','range','text'));
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_comparator_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_comparator_check
  CHECK (comparator IN ('enum_eq','set_overlap','ordinal_ge','range_overlap','text_none'));
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_role_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_role_check
  CHECK (role IN ('gate','score','modifier','display'));
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_hard_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_hard_check
  CHECK (is_hard = (role = 'gate'));
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_weight_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_weight_check
  CHECK (weight >= 0 AND weight <= 1);
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_zero_weight_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_zero_weight_check
  CHECK (role NOT IN ('display','modifier') OR weight = 0);
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_status_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_status_check
  CHECK (status IN ('active','planned','retired'));
ALTER TABLE dimension DROP CONSTRAINT IF EXISTS dimension_kind_cmp_check;
ALTER TABLE dimension ADD CONSTRAINT dimension_kind_cmp_check
  CHECK (comparator = CASE kind
           WHEN 'enum'    THEN 'enum_eq'
           WHEN 'ordinal' THEN 'ordinal_ge'
           WHEN 'set'     THEN 'set_overlap'
           WHEN 'range'   THEN 'range_overlap'
           ELSE 'text_none' END);

CREATE INDEX IF NOT EXISTS idx_dimension_group ON dimension(group_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_dimension_role ON dimension(role) WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- 3. 候选池：自写值的落点
--    复用演化层既有的 concept_candidate（不新建候选表），phrase_key 用
--    'OPT|<field_id>|<归一化文本>' 编码字段归属；evolution_policy 增加
--    'field_option' 这一 candidate_kind。
-- ---------------------------------------------------------------------------
ALTER TABLE evolution_policy DROP CONSTRAINT IF EXISTS evolution_policy_candidate_kind_check;
ALTER TABLE evolution_policy ADD CONSTRAINT evolution_policy_candidate_kind_check
  CHECK (candidate_kind IN ('occupation','concept','field_option'));

INSERT INTO evolution_policy (policy_id, candidate_kind, min_evidence, min_days_seen,
                              auto_promote, note)
VALUES ('ep_field_option', 'field_option', 3, 0, false,
        '自写选项候选：同一字段被 3 个及以上不同主体自写同一取值才进入 ready，人工评审后提升为正式码值')
ON CONFLICT (policy_id) DO UPDATE SET min_evidence = EXCLUDED.min_evidence,
  min_days_seen = EXCLUDED.min_days_seen, auto_promote = EXCLUDED.auto_promote,
  note = EXCLUDED.note, updated_at = now();

-- ---------------------------------------------------------------------------
-- 4. 依赖兜底：013 需要的实体先确保登记
--    ON CONFLICT DO NOTHING：绝不覆盖 code/load_catalog.py 写入的内容。
--    为什么需要：entity_catalog 是 load_catalog.py 从 field_catalog 的 entity_id
--    派生的，而 apply 与 load_catalog 是两条独立管线、apply 先跑；
--    另外 project_record / award_honor / assessment 这三个实体在库里原本根本
--    没有登记（field_catalog_seed.csv 里没有它们的字段行）。
-- ---------------------------------------------------------------------------
INSERT INTO entity_catalog (entity_id, table_name, domain, description, id_prefix,
                            schema_version, access_tier, status, kind)
VALUES ('preference', 'preference', 'talent', '人才偏好：兴趣、风格、价值观、流动与强度等', 'prf',
        '1.0.0', 'T1', 'active', 'EK1'),
       ('project_record', 'project_record', 'talent', '项目与课题经历', 'prj',
        '1.0.0', 'T1', 'active', 'EK1'),
       ('award_honor', 'award_honor', 'talent', '奖项与荣誉', 'awd',
        '1.0.0', 'T1', 'active', 'EK1'),
       ('assessment', 'assessment', 'talent', '标准化测评结果', 'asm',
        '1.0.0', 'T1', 'active', 'EK1'),
       ('person_demographics', 'person_demographics', 'talent', '人口学与户籍', 'dem',
        '1.0.0', 'T2', 'active', 'EK1'),
       ('clinical_exposure', 'clinical_exposure', 'talent', '临床暴露与操作经历', 'cln',
        '1.0.0', 'T1', 'active', 'EK1')
ON CONFLICT (entity_id) DO NOTHING;
"""

TAIL = r"""
-- ---------------------------------------------------------------------------
-- 5b. 字段级"允许自写"开关与维度注册表对齐
--     放在维度种子之后执行，保证第一次跑与第二次跑结果完全一致（幂等）。
--     概念型维度（code_table_id 为空，如 DIM_CONCEPT_SKILL）也在这里被标成
--     allow_custom=true，但它的自写走能力候选概念池，不走词表选项池。
-- ---------------------------------------------------------------------------
UPDATE field_catalog f SET allow_custom = true,
       custom_hint = COALESCE(f.custom_hint,
         '选项内选取为主；不在选项内的取值先进入候选池，评审通过后成为正式选项')
WHERE f.field_id IN (SELECT d.person_field_id FROM dimension d
                     WHERE d.allow_custom AND d.person_field_id IS NOT NULL
                       AND d.status = 'active');

-- ---------------------------------------------------------------------------
-- 6. 比较函数：按 kind 分派，四种类型四种语义
--    纪律：任何一侧为空 → 返回 NULL（unknown），**不是 0 分**。
--    分数一律在 [0,1]：0 表示"确认不满足"，NULL 表示"没有信息"。
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS cmp_enum(TEXT, TEXT[]);
DROP FUNCTION IF EXISTS cmp_set(TEXT[], TEXT[], TEXT);
DROP FUNCTION IF EXISTS cmp_ordinal(INT, INT);
DROP FUNCTION IF EXISTS cmp_range(NUMERIC, NUMERIC, NUMERIC, NUMERIC);
DROP FUNCTION IF EXISTS cmp_dimension(TEXT, JSONB, JSONB);

-- 枚举：相等，或落在岗位可接受集合内
CREATE OR REPLACE FUNCTION cmp_enum(p_person TEXT, p_job TEXT[])
RETURNS NUMERIC LANGUAGE sql IMMUTABLE SET search_path = mt, public AS $$
  SELECT CASE
    WHEN p_person IS NULL OR p_job IS NULL OR cardinality(p_job) = 0 THEN NULL
    WHEN p_person = ANY (p_job) THEN 1.0
    ELSE 0.0
  END;
$$;

-- 集合：cover = 岗位要求被覆盖的比例；jaccard = 交并比；exact = 完全一致
CREATE OR REPLACE FUNCTION cmp_set(p_person TEXT[], p_job TEXT[], p_mode TEXT DEFAULT 'cover')
RETURNS NUMERIC LANGUAGE sql IMMUTABLE SET search_path = mt, public AS $$
  SELECT CASE
    WHEN p_person IS NULL OR p_job IS NULL
      OR cardinality(p_person) = 0 OR cardinality(p_job) = 0 THEN NULL
    WHEN p_mode = 'exact' THEN
      CASE WHEN (SELECT count(*) FROM (SELECT unnest(p_person) EXCEPT SELECT unnest(p_job)) a) = 0
            AND (SELECT count(*) FROM (SELECT unnest(p_job) EXCEPT SELECT unnest(p_person)) b) = 0
           THEN 1.0 ELSE 0.0 END
    WHEN p_mode = 'jaccard' THEN
      (SELECT count(*)::numeric FROM (SELECT unnest(p_person)
                                      INTERSECT SELECT unnest(p_job)) i)
      / NULLIF((SELECT count(*) FROM (SELECT unnest(p_person)
                                      UNION SELECT unnest(p_job)) u), 0)
    ELSE
      (SELECT count(*)::numeric FROM (SELECT unnest(p_person)
                                      INTERSECT SELECT unnest(p_job)) i)
      / cardinality(p_job)
  END;
$$;

-- 序数：岗位要求档位 <= 人侧档位即满足；差一档给 0.5（可协商区间）；差两档及以上 0
CREATE OR REPLACE FUNCTION cmp_ordinal(p_person_rank INT, p_job_min_rank INT)
RETURNS NUMERIC LANGUAGE sql IMMUTABLE SET search_path = mt, public AS $$
  SELECT CASE
    WHEN p_person_rank IS NULL OR p_job_min_rank IS NULL THEN NULL
    WHEN p_person_rank >= p_job_min_rank THEN 1.0
    WHEN p_person_rank + 1 = p_job_min_rank THEN 0.5
    ELSE 0.0
  END;
$$;

-- 区间：期望区间被岗位薪资上限"够到"的程度（单调不减）
--   岗位上限 >= 期望上限          → 1.0
--   岗位上限 <  期望下限          → 0.0
--   否则 (岗位上限 - 期望下限) / 期望宽度
CREATE OR REPLACE FUNCTION cmp_range(p_expect_lo NUMERIC, p_expect_hi NUMERIC,
                                     p_offer_lo NUMERIC, p_offer_hi NUMERIC)
RETURNS NUMERIC LANGUAGE sql IMMUTABLE SET search_path = mt, public AS $$
  SELECT CASE
    WHEN p_expect_lo IS NULL OR p_expect_hi IS NULL
      OR p_offer_lo IS NULL OR p_offer_hi IS NULL THEN NULL
    WHEN p_expect_hi <= p_expect_lo THEN
      CASE WHEN p_offer_hi >= p_expect_lo THEN 1.0 ELSE 0.0 END
    ELSE GREATEST(0::numeric,
                  LEAST(1.0, (p_offer_hi - p_expect_lo) / (p_expect_hi - p_expect_lo)))
  END;
$$;

-- 统一分派：按 dimension.comparator 选函数。载荷约定（全部由 engine 组装）：
--   enum    {"code":"D3"}                        vs {"codes":["D2","D3"]}
--   set     {"codes":["F01"],"mode":"cover"}     vs {"codes":["F01","F02"]}
--   ordinal {"rank":30}                          vs {"min_rank":20}
--   range   {"lo":10000,"hi":15000}              vs {"lo":9000,"hi":14000}
CREATE OR REPLACE FUNCTION cmp_dimension(p_dimension_id TEXT, p_person JSONB, p_job JSONB)
RETURNS NUMERIC LANGUAGE plpgsql STABLE SET search_path = mt, public AS $$
DECLARE
  v_cmp TEXT;
  v_pp  TEXT[];
  v_jj  TEXT[];
BEGIN
  SELECT x.comparator INTO v_cmp FROM dimension x
   WHERE x.dimension_id = p_dimension_id AND x.status = 'active';
  IF v_cmp IS NULL THEN
    RETURN NULL;
  END IF;

  IF v_cmp = 'enum_eq' THEN
    IF jsonb_typeof(p_job -> 'codes') = 'array' THEN
      v_jj := ARRAY(SELECT jsonb_array_elements_text(p_job -> 'codes'));
    END IF;
    IF (v_jj IS NULL OR cardinality(v_jj) = 0) AND (p_job ->> 'code') IS NOT NULL THEN
      v_jj := ARRAY[p_job ->> 'code'];
    END IF;
    IF v_jj IS NOT NULL AND cardinality(v_jj) = 0 THEN
      v_jj := NULL;
    END IF;
    -- 码值尚未回填的维度退化为原始文本比较（在 dimension.note 里记为已知债务）
    IF (p_person ->> 'code') IS NULL THEN
      RETURN cmp_enum(p_person ->> 'text', v_jj);
    END IF;
    RETURN cmp_enum(p_person ->> 'code', v_jj);

  ELSIF v_cmp = 'set_overlap' THEN
    IF jsonb_typeof(p_person -> 'codes') = 'array' THEN
      v_pp := ARRAY(SELECT jsonb_array_elements_text(p_person -> 'codes'));
    END IF;
    IF jsonb_typeof(p_job -> 'codes') = 'array' THEN
      v_jj := ARRAY(SELECT jsonb_array_elements_text(p_job -> 'codes'));
    END IF;
    IF v_pp IS NOT NULL AND cardinality(v_pp) = 0 THEN
      v_pp := NULL;
    END IF;
    IF v_jj IS NOT NULL AND cardinality(v_jj) = 0 THEN
      v_jj := NULL;
    END IF;
    RETURN cmp_set(v_pp, v_jj, COALESCE(p_person ->> 'mode', 'cover'));

  ELSIF v_cmp = 'ordinal_ge' THEN
    RETURN cmp_ordinal((p_person ->> 'rank')::int, (p_job ->> 'min_rank')::int);

  ELSIF v_cmp = 'range_overlap' THEN
    RETURN cmp_range((p_person ->> 'lo')::numeric, (p_person ->> 'hi')::numeric,
                     (p_job ->> 'lo')::numeric, (p_job ->> 'hi')::numeric);
  END IF;

  RETURN NULL;   -- text_none：文本不参与数值打分
END;
$$;

-- ---------------------------------------------------------------------------
-- 7. 自写值 → 候选池 → 评审 → 正式选项（"选项 + 自写"双形态的完整路径）
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS normalize_option_text(TEXT);
DROP FUNCTION IF EXISTS submit_option_candidate(TEXT, TEXT, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS promote_option_candidate(TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT);

-- 7.1 归一化：去首尾空白、压缩空白、统一小写、全角标点转半角
CREATE OR REPLACE FUNCTION normalize_option_text(p_text TEXT)
RETURNS TEXT LANGUAGE sql IMMUTABLE SET search_path = mt, public AS $$
  SELECT NULLIF(
           lower(btrim(regexp_replace(
             replace(replace(replace(replace(replace(replace(replace(
               replace(replace(replace(COALESCE(p_text, ''), '（', '('), '）', ')'),
               '：', ':'), '，', ','), '、', ','), '；', ';'),
               '　', ' '), '“', '"'), '”', '"'), '’', ''''),
             '\s+', ' ', 'g'))),
           '');
$$;

-- 7.2 提交一个自写值：只进候选池，绝不直接写 code_value
CREATE OR REPLACE FUNCTION submit_option_candidate(
  p_field_id     TEXT,
  p_raw_text     TEXT,
  p_subject_id   TEXT DEFAULT NULL,
  p_evidence_ref TEXT DEFAULT NULL
) RETURNS TEXT LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  v_ct    TEXT;
  v_ent   TEXT;
  v_allow BOOLEAN;
  v_norm  TEXT;
  v_key   TEXT;
  v_id    TEXT;
BEGIN
  SELECT f.code_table_id, f.entity_id, f.allow_custom
    INTO v_ct, v_ent, v_allow
    FROM field_catalog f
   WHERE f.field_id = p_field_id AND f.status = 'active'
   FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION '字段 % 未登记或已废弃，不允许写入', p_field_id;
  END IF;
  IF NOT v_allow THEN
    RAISE EXCEPTION '字段 % 不允许自写补充（allow_custom=false）', p_field_id;
  END IF;
  IF v_ct IS NULL THEN
    RAISE EXCEPTION '字段 % 没有绑定词表；概念型维度的自写请走能力候选池', p_field_id;
  END IF;

  v_norm := normalize_option_text(p_raw_text);
  IF v_norm IS NULL THEN
    RAISE EXCEPTION '自写值不能为空';
  END IF;

  -- 已在正式词表里的取值：直接返回 NULL，表示"无需新建候选"
  IF EXISTS (SELECT 1 FROM code_value cv
              WHERE cv.code_table_id = v_ct AND NOT cv.deprecated
                AND normalize_option_text(cv.label_zh) = v_norm) THEN
    RETURN NULL;
  END IF;

  v_key := 'OPT|' || p_field_id || '|' || v_norm;
  v_id  := 'con_' || substr(md5(v_key), 1, 20);

  INSERT INTO concept_candidate (candidate_id, phrase_key, phrase_sample, evidence_count,
                                 sample_requirement_ids, first_seen, last_seen, status, note)
  VALUES (v_id, v_key, btrim(p_raw_text), 1,
          CASE WHEN p_evidence_ref IS NULL THEN NULL ELSE ARRAY[p_evidence_ref] END,
          CURRENT_DATE, CURRENT_DATE, 'watching',
          jsonb_build_object('kind', 'field_option', 'field_id', p_field_id,
                             'entity', v_ent, 'subject', p_subject_id,
                             'code_table', v_ct)::text)
  ON CONFLICT (candidate_id) DO UPDATE
     SET evidence_count = concept_candidate.evidence_count + 1,
         last_seen = CURRENT_DATE,
         updated_at = now()
  RETURNING candidate_id INTO v_id;

  UPDATE concept_candidate c SET status = 'ready', updated_at = now()
   WHERE c.candidate_id = v_id AND c.status = 'watching'
     AND c.evidence_count >= (SELECT p.min_evidence FROM evolution_policy p
                               WHERE p.candidate_kind = 'field_option');

  RETURN v_id;
END $$;

-- 7.3 评审通过后提升为正式选项（唯一往 code_value 写自写取值的入口）
CREATE OR REPLACE FUNCTION promote_option_candidate(
  p_candidate_id TEXT,
  p_code         TEXT,
  p_label_zh     TEXT DEFAULT NULL,
  p_label_en     TEXT DEFAULT NULL,
  p_sort_order   INTEGER DEFAULT NULL,
  p_decided_by   TEXT DEFAULT NULL
) RETURNS TEXT LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE
  v_note JSONB;
  v_lbl  TEXT;
  v_fid  TEXT;
  v_ct   TEXT;
  v_ord  INTEGER;
BEGIN
  SELECT c.note::jsonb, c.phrase_sample INTO v_note, v_lbl
    FROM concept_candidate c WHERE c.candidate_id = p_candidate_id;
  IF v_note IS NULL OR v_note ->> 'kind' IS DISTINCT FROM 'field_option' THEN
    RAISE EXCEPTION '候选 % 不是字段选项候选，拒绝提升', p_candidate_id;
  END IF;

  v_fid := v_note ->> 'field_id';
  SELECT f.code_table_id INTO v_ct FROM field_catalog f WHERE f.field_id = v_fid;
  IF v_ct IS NULL THEN
    RAISE EXCEPTION '字段 % 没有绑定词表，无法提升选项', v_fid;
  END IF;

  SELECT COALESCE(max(cv.sort_order) + 10, 10) INTO v_ord
    FROM code_value cv WHERE cv.code_table_id = v_ct;

  INSERT INTO code_value (code_table_id, code, label_zh, label_en, definition, level,
                          sort_order, external_mapping, deprecated)
  VALUES (v_ct, p_code, COALESCE(p_label_zh, v_lbl), p_label_en,
          '由自写候选 ' || p_candidate_id || ' 评审提升', 1, COALESCE(p_sort_order, v_ord),
          jsonb_build_object('promoted_from', p_candidate_id), false)
  ON CONFLICT (code_table_id, code) DO UPDATE
     SET label_zh = EXCLUDED.label_zh, sort_order = EXCLUDED.sort_order;

  UPDATE concept_candidate c
     SET status = 'promoted', promoted_to = v_ct || ':' || p_code, updated_at = now()
   WHERE c.candidate_id = p_candidate_id;

  INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
  VALUES (COALESCE(p_decided_by, session_user), 'code_value', v_ct || ':' || p_code, 'add',
          '1.0.0', jsonb_build_object('from_candidate', p_candidate_id, 'field_id', v_fid));

  RETURN v_ct || ':' || p_code;
END $$;

-- ---------------------------------------------------------------------------
-- 8. 打分聚合：硬门槛过滤 + 加权归一 + 逐维度可解释拆解
--    入参是 {dimension_id: 载荷} 的 JSONB。逐维度状态：
--      met / partial / gap / unknown / blocked / skipped
--      · blocked  硬门槛未过：整体不通过，但仍返回全部维度便于解释
--      · unknown  任一侧无值 → 不计入分母（"空就是空"）
--      · skipped  text / display / modifier，不参与打分
--    加权归一：分母 = 所有 evaluable（非 unknown 非 skipped）维度的权重之和，
--    而不是全部维度权重之和 —— 否则"没填"会被人为算成扣分。
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS score_dimensions(JSONB, JSONB);
CREATE OR REPLACE FUNCTION score_dimensions(p_person JSONB, p_job JSONB)
RETURNS TABLE (
  dimension_id TEXT, group_id TEXT, group_title TEXT, title_zh TEXT,
  role TEXT, weight NUMERIC, comparator TEXT, status TEXT,
  score NUMERIC, contribution NUMERIC, reason TEXT
) LANGUAGE plpgsql STABLE SET search_path = mt, public AS $$
DECLARE
  d        RECORD;
  s        NUMERIC;
  v_reason TEXT;
BEGIN
  FOR d IN SELECT x.* FROM dimension x
            WHERE x.status = 'active' ORDER BY x.group_id, x.sort_order LOOP
    dimension_id := d.dimension_id;
    group_id     := d.group_id;
    group_title  := d.group_title;
    title_zh     := d.title_zh;
    role         := d.role;
    weight       := d.weight;
    comparator   := d.comparator;
    contribution := 0;
    s            := NULL;
    v_reason     := NULL;

    IF d.role IN ('display', 'modifier') OR d.comparator = 'text_none' THEN
      status := 'skipped';
      v_reason := '不参与打分（role=' || d.role || '）';
    ELSIF NOT (p_person ? d.dimension_id) OR NOT (p_job ? d.dimension_id) THEN
      status := 'unknown';
      v_reason := CASE
        WHEN NOT (p_person ? d.dimension_id) AND NOT (p_job ? d.dimension_id)
          THEN '两侧都没有值'
        WHEN NOT (p_person ? d.dimension_id) THEN '人侧没有值'
        ELSE '岗位侧没有值' END;
    ELSE
      s := cmp_dimension(d.dimension_id, p_person -> d.dimension_id, p_job -> d.dimension_id);
      IF s IS NULL THEN
        status := 'unknown';
        v_reason := '载荷不完整或类型不匹配，按未知处理';
      ELSIF s >= 1 THEN
        status := 'met'; v_reason := '满足';
      ELSIF s > 0 THEN
        status := 'partial'; v_reason := '部分满足（' || round(s, 3) || '）';
      ELSE
        status := 'gap'; v_reason := '确认不满足';
      END IF;
      IF d.role = 'gate' AND s IS NOT NULL AND s < 1 THEN
        status := 'blocked';
        v_reason := '硬门槛未过：' || v_reason;
      END IF;
      contribution := round(COALESCE(s, 0) * d.weight, 4);
    END IF;
    score  := s;
    reason := v_reason;
    RETURN NEXT;
  END LOOP;
END $$;

-- 汇总：门禁判定 + 加权归一总分 + 覆盖率（覆盖率低时分数的可解释性要打折扣）
DROP FUNCTION IF EXISTS score_summary(JSONB, JSONB);
CREATE OR REPLACE FUNCTION score_summary(p_person JSONB, p_job JSONB)
RETURNS TABLE (blocked_by TEXT[], gate_failed INTEGER, evaluable INTEGER,
               unknown_dimensions INTEGER, score_total NUMERIC, coverage NUMERIC)
LANGUAGE sql STABLE SET search_path = mt, public AS $$
  WITH s AS (SELECT * FROM score_dimensions(p_person, p_job))
  SELECT (SELECT array_agg(x.dimension_id ORDER BY x.dimension_id) FROM s x
           WHERE x.status = 'blocked'),
         (SELECT count(*)::int FROM s x WHERE x.status = 'blocked'),
         (SELECT count(*)::int FROM s x WHERE x.status IN ('met','partial','gap','blocked')),
         (SELECT count(*)::int FROM s x WHERE x.status = 'unknown'),
         (SELECT CASE WHEN sum(x.weight) FILTER (WHERE x.status IN ('met','partial','gap','blocked')) = 0
                      THEN NULL
                      ELSE round(sum(x.contribution) FILTER (
                             WHERE x.status IN ('met','partial','gap','blocked'))
                           / sum(x.weight) FILTER (
                             WHERE x.status IN ('met','partial','gap','blocked')), 4)
                 END FROM s x),
         (SELECT CASE WHEN count(*) = 0 THEN NULL
                      ELSE round(count(*) FILTER (
                             WHERE x.status IN ('met','partial','gap','blocked'))::numeric
                           / count(*), 4) END FROM s x
           WHERE x.status <> 'skipped');
$$;

-- ---------------------------------------------------------------------------
-- 9. 对外视图
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_dimension_registry AS
SELECT d.dimension_id, d.group_id, d.group_title, d.title_zh, d.kind, d.comparator,
       d.code_table_id, d.allow_custom, d.custom_hint, d.is_hard, d.role, d.weight,
       d.person_locator, d.person_source, d.person_field_id,
       d.job_locator, d.job_source, d.job_gap, d.note, d.sort_order, d.status,
       (SELECT count(*) FROM code_value v
         WHERE v.code_table_id = d.code_table_id AND NOT v.deprecated) AS option_count
FROM dimension d
ORDER BY d.group_id, d.sort_order;

-- 依赖兜底：注册表引用了但库里还不存在的词表 / 字段 / 空词表
CREATE OR REPLACE VIEW v_dimension_dependency_gap AS
SELECT 'code_table_missing' AS gap_kind, d.dimension_id, d.code_table_id AS missing_ref,
       '词表未导入：请先运行 python code/load_catalog.py' AS suggestion
  FROM dimension d
 WHERE d.code_table_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM code_table t WHERE t.code_table_id = d.code_table_id)
UNION ALL
SELECT 'options_empty', d.dimension_id, d.code_table_id, '词表存在但没有任何选项'
  FROM dimension d
 WHERE d.code_table_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM code_table t WHERE t.code_table_id = d.code_table_id)
   AND NOT EXISTS (SELECT 1 FROM code_value v
                    WHERE v.code_table_id = d.code_table_id AND NOT v.deprecated)
UNION ALL
SELECT 'field_missing', d.dimension_id, d.person_field_id,
       '字段未登记：请先运行 python code/load_catalog.py'
  FROM dimension d
 WHERE d.person_field_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM field_catalog f WHERE f.field_id = d.person_field_id);

-- 覆盖度自测：把 locator 翻译成精确 count(*)，六种形态都统计
--   <表>.<列>          → 该列非空行数 / 全表行数
--   preference:PFx     → 该偏好类型有值的行数 / 人才总数
--   concept:Kx         → 有该类型能力主张的人数 / 人才总数
--   job_requirement:RTx→ 有该类型要求行的岗位数 / 岗位总数（**文本桥接**，非结构化码值）
--   credential:Cxx     → 持有该证的人数 / 人才总数
--   derived:...        → 明确标注"派生维度不在此统计"，不假装统计过
--   其他（unavailable）→ 跳过，由 v_dimension_two_sided 反映
-- 纪律：一律 count(*)，不用 reltuples，不估算。
-- 结构化 vs 文本桥接的区分：job_locator 形如 表.列 才是结构化；job_requirement:RTx
-- 只在自由文本里有信息，必须走文本桥接（registry 的 job_gap 列已注明）。
DROP FUNCTION IF EXISTS dimension_coverage();
CREATE OR REPLACE FUNCTION dimension_coverage()
RETURNS TABLE (dimension_id TEXT, side TEXT, relation TEXT, column_name TEXT,
               non_null BIGINT, total BIGINT, note TEXT)
LANGUAGE plpgsql STABLE SET search_path = mt, public AS $$
DECLARE
  d      RECORD;
  v_loc  TEXT;
  v_rel  TEXT;
  v_col  TEXT;
  v_key  TEXT;
  v_n    BIGINT;
  v_t    BIGINT;
  v_note TEXT;
BEGIN
  FOR d IN SELECT x.* FROM dimension x
            WHERE x.status = 'active' ORDER BY x.sort_order LOOP
    FOREACH v_loc IN ARRAY ARRAY[
        CASE WHEN d.person_locator IS NOT NULL THEN 'person|' || d.person_locator END,
        CASE WHEN d.job_locator    IS NOT NULL THEN 'job|'    || d.job_locator    END]
    LOOP
      CONTINUE WHEN v_loc IS NULL;
      side        := split_part(v_loc, '|', 1);
      v_loc       := split_part(v_loc, '|', 2);
      dimension_id := d.dimension_id;
      relation    := NULL;
      column_name := NULL;
      non_null    := NULL;
      total       := NULL;
      v_note      := NULL;

      IF v_loc IN ('unavailable', '') THEN
        CONTINUE;
      ELSIF v_loc ~ '^[a-z_]+\.[a-z_]+$' THEN
        v_rel := split_part(v_loc, '.', 1);
        v_col := split_part(v_loc, '.', 2);
        relation := v_rel;
        column_name := v_col;
        IF to_regclass('mt.' || v_rel) IS NULL THEN
          v_note := '表不存在';
        ELSIF NOT EXISTS (SELECT 1 FROM information_schema.columns c
                           WHERE c.table_schema = 'mt' AND c.table_name = v_rel
                             AND c.column_name = v_col) THEN
          v_note := '列不存在';
        ELSE
          EXECUTE format('SELECT count(%I), count(*) FROM mt.%I', v_col, v_rel)
            INTO v_n, v_t;
          non_null := v_n; total := v_t;
        END IF;
      ELSIF v_loc ~ '^preference:[A-Za-z0-9]+$' THEN
        v_key := split_part(v_loc, ':', 2);
        relation := 'preference';
        column_name := 'pref_type=' || v_key;
        SELECT count(*) FILTER (WHERE p.value_code IS NOT NULL OR p.value_raw IS NOT NULL),
               (SELECT count(*) FROM mt.person)
          INTO v_n, v_t
          FROM mt.preference p WHERE p.pref_type = v_key;
        non_null := v_n; total := v_t;
      ELSIF v_loc ~ '^concept:[A-Za-z0-9]+$' THEN
        v_key := split_part(v_loc, ':', 2);
        relation := 'skill_assertion';
        column_name := 'concept_type=' || v_key;
        SELECT count(DISTINCT sa.person_id), (SELECT count(*) FROM mt.person)
          INTO v_n, v_t
          FROM mt.skill_assertion sa
          JOIN mt.concept c ON c.concept_id = sa.concept_id
         WHERE c.concept_type = v_key;
        non_null := v_n; total := v_t;
      ELSIF v_loc ~ '^job_requirement:[A-Za-z0-9]+' THEN
        -- 岗位侧"有该类型的要求行"的覆盖率；注意这类维度是**文本桥接**，
        -- 不是结构化码值（结构化与否看 job_locator 的形态：表.列 = 结构化）
        v_key := split_part(split_part(v_loc, ':', 2), '.', 1);
        relation := 'job_requirement';
        column_name := 'requirement_type=' || v_key;
        SELECT count(DISTINCT jr.job_id), (SELECT count(*) FROM mt.job_posting)
          INTO v_n, v_t
          FROM mt.job_requirement jr WHERE jr.requirement_type = v_key;
        non_null := v_n; total := v_t;
      ELSIF v_loc ~ '^credential:[A-Za-z0-9]+$' THEN
        v_key := split_part(v_loc, ':', 2);
        relation := 'credential';
        column_name := 'credential_type=' || v_key;
        SELECT count(DISTINCT c.person_id), (SELECT count(*) FROM mt.person)
          INTO v_n, v_t
          FROM mt.credential c WHERE c.credential_type = v_key;
        non_null := v_n; total := v_t;
      ELSE
        relation := split_part(v_loc, ':', 1);
        column_name := NULL;
        v_note := '派生或复合定位符，本函数不统计：' || v_loc;
      END IF;

      note := v_note;
      RETURN NEXT;
    END LOOP;
  END LOOP;
END $$;

-- 精度自检：注册表里标注为"两侧都有值"的维度必须真的能取到两侧载荷
CREATE OR REPLACE VIEW v_dimension_two_sided AS
SELECT d.dimension_id, d.title_zh, d.kind, d.role, d.weight,
       d.person_locator, d.job_locator,
       (d.person_locator IS NOT NULL AND d.person_locator <> 'unavailable') AS person_ok,
       (d.job_locator IS NOT NULL AND d.job_locator <> 'unavailable')       AS job_ok
FROM dimension d
WHERE d.status = 'active';

INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
VALUES ('013_dimensions.sql', 'catalog', 'dimension+code_table+code_value+field_catalog',
        'add', '1.0.0',
        jsonb_build_object('dimensions', (SELECT count(*) FROM dimension),
                           'self_writable', (SELECT count(*) FROM dimension WHERE allow_custom),
                           'gates', (SELECT count(*) FROM dimension WHERE role = 'gate'),
                           'modifiers', (SELECT count(*) FROM dimension WHERE role = 'modifier'),
                           'display_only', (SELECT count(*) FROM dimension WHERE role = 'display')));

COMMIT;

-- ============================================================================
-- 验收自测（可单独执行）：
--   SELECT count(*) FROM mt.dimension;                    -- 期望 50
--   SELECT * FROM mt.v_dimension_dependency_gap;          -- 期望 0 行
--   SELECT * FROM mt.dimension_coverage() ORDER BY 1, 2;  -- 两侧真实覆盖率
--   SELECT * FROM mt.score_summary('{...}'::jsonb, '{...}'::jsonb);
-- ============================================================================
"""


def build_sql():
    return (HEAD
            + "\n-- ===========================================================================\n"
            + "-- 5. 种子数据（词表 / 选项 / 字段 / 维度），全部 UPSERT，可重放\n"
            + "-- ===========================================================================\n"
            + seed_block()
            + "\n" + TAIL)


def main():
    code_rows = new_code_rows()
    code_keys = {(r[0], r[1]) for r in code_rows}
    field_keys = {(f[0],) for f in NEW_FIELDS}
    a = patch_csv(CODE_TABLE_CSV, code_keys, code_rows, key_len=2)
    b = patch_csv(FIELD_CSV, field_keys, [list(f) for f in NEW_FIELDS], key_len=1)
    n_dim = write_talent_csv()
    with io.open(OUT_SQL, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_sql())

    print("[catalog] code_table_seed.csv   保留 %d 行 / 追加 %d 行 / 清掉旧追加 %d 行" % a)
    print("[catalog] field_catalog_seed.csv 保留 %d 行 / 追加 %d 行 / 清掉旧追加 %d 行" % b)
    print("[catalog] talent_field_seed.csv  维度 %d 行" % n_dim)
    print("[catalog] 新增词表 %d 张 / 新增码值 %d 个" % (len(NEW_CODE_TABLES), len(code_rows)))
    print("[sql] %s（%.0f KB）" % (OUT_SQL, os.path.getsize(OUT_SQL) / 1024.0))


if __name__ == "__main__":
    main()
