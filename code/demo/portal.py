# -*- coding: utf-8 -*-
"""
code/demo/portal.py —— 数据库门户（信息展示 / 检索 / 基础分析）

**这不是业务演示**。业务演示在 `console.py`（岗位图谱、能力矩阵、匹配、备份）。
本文件回答的是另一个问题：**这个数据库本身长什么样？**

参考基因组学数据库的门户形态（gnomAD / Ensembl / UCSC / GTEx 那一类），把三件事做出来：

  ① 信息展示  `/`            库的"名片"：真实规模、每张表的真实行数、词表与字段规模
              `/schema`      全部基础表与视图，按域分组，含列数与**精确**行数
              `/t/<表>`      列 / 类型 / 默认值 / 约束 / 外键 / 索引 + **真实数据分页**
              `/e/<表>?val=` **实体页**：一行记录的全部字段 + 所有指向它的表（交叉引用）
              `/lineage`     来源登记、采集批次、逐条血缘、发布记录、L0 原始文件

  ② 检索      `/search?q=`   跨职业 / 概念（含别名）/ JD 标题与要求原文 / 码值 / 字段 / 表名列名
                            每一条命中都告诉你**来自哪张表的哪一列**

  ③ 基础分析  `/analyze`     8 个预置分析：岗位族、能力需求 Top-N、映射覆盖率、能力漂移、
                            词表健康度、字段目录、职业树与外部码、来源与证据分级
                            每个分析都**显示它执行的 SQL**，并可直接导出 CSV

  ④ 自己想看  `/sql`         只读 SQL 控制台：单语句、必须以 SELECT/WITH 开头、
                            在 `BEGIN TRANSACTION READ ONLY` 里执行（写操作由数据库拒绝）

设计原则（沿用整个项目的纪律）：
  · **全站只读**。门户自身不产生任何写操作，唯一接受用户输入 SQL 的页面被锁在只读事务里。
  · **不猜数字**。行数一律 `count(*)` 精确统计，不用 `reltuples` 估算——实测它就过时
    （`code_table` 估算 53、实际 49），而"看到数据库真实的样子"正是本门户的全部意义。
  · **不拼裸字符串**。表名/列名先与系统目录比对（白名单），再用 `psycopg.sql.Identifier` 转义。

启动：
  python code/demo/portal.py --serve           # http://127.0.0.1:8082
  python code/demo/portal.py --check           # 自检：元数据 + 每个页面渲染一遍，不开服务
  python code/demo/portal.py --tables          # 命令行列出全部表/视图与行数
"""
from __future__ import annotations

from datetime import datetime, timezone

import argparse
import csv
import html
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(BASE, "code"))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import metrics  # noqa: E402  ← 度量的单一口径定义
from _portal_shared import PortalError  # noqa: E402  ← 与 portal_viz/portal_dev 共用一个类对象

sys.stdout.reconfigure(encoding="utf-8")

DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
       "client_encoding=UTF8 options='-c search_path=mt,public' "
       "application_name=medtalent_admin")

# ---------------------------------------------------------------------------
# 门户自己的连接串：**不是超级用户**
# ---------------------------------------------------------------------------
# 这一行是"访问控制到底有没有生效"的分水岭。
# 实测（ops/fixtures/_lab_access.py）：超级用户读带 RLS 的表会**绕过策略**，
# 连 FORCE ROW LEVEL SECURITY 都拦不住。所以只要门户用 postgres 连接，
# 迁移 017–021 建的 1029 条列级授权对它就**没有任何约束力**——那是装饰。
#
# mt_portal 的特性（迁移 017 建的）：
#   · 非超级用户、不绕过 RLS、NOINHERIT
#   · **自身没有任何表权限**，必须先 `SET LOCAL ROLE mt_tN` 才能读到数据
# 于是"忘记设等级"的结果是**读不到数据**，而不是读到全部数据 —— 这个默认方向才安全。
PORTAL_DSN = ("host=127.0.0.1 port=55432 dbname=medtalent user=mt_portal "
              "client_encoding=UTF8 connect_timeout=5 "
              "options='-c search_path=mt,public' "
              # application_name 会进数据库的连接日志：这样 DB 层能看出
              # "这次连接来自只读门户"还是"来自备份/开发者模式" ——
              # 应用层审计说自己只以 T0 读过数据时，DB 日志可以对它做交叉验证。
              "application_name=portal_8082")

TIERS = ("T0", "T1", "T2", "T3")        # 白名单：等级绝不采信用户输入的原文
SESSION_COOKIE = "mt_session"
SCHEMA = "mt"
PORT = 8082
PAGE_SIZE = 50
STATEMENT_TIMEOUT_MS = 15000

# ---------------------------------------------------------------------------
# 域划分：把 79 张表放进人看得懂的抽屉里。
# 这张表必须覆盖全部基础表——`--check` 会报出任何未归类的表，测试也断言覆盖率 100%。
# ---------------------------------------------------------------------------
DOMAINS = [
    ("字典与语义", "词表、字段目录、概念本体——所有「受控」的东西都在这"),
    ("岗位与市场", "需求端：雇主、岗位、要求、职业树、能力权重、来源与血缘"),
    ("人才档案", "供给端：人、教育、经历、成果、证据、能力主张、匹配结果"),
    ("演化层", "职业树与能力随时间生长：候选、拆分合并、版本化重算与漂移"),
    ("扩展值与观测", "零 DDL 的统一值表、观测期、首次发生、派生特征"),
    ("治理与合规", "访问分级、授权、撤回、保留期、主体权利、变更流水"),
    ("小程序接入", "外部身份、应答会话、经历片段、crosswalk、同步事件、墓碑"),
    ("备份与发布", "备份/恢复台账与数据集发布登记"),
]

DOMAIN_TABLES = {
    "字典与语义": ["code_table", "code_value", "entity_catalog", "field_catalog", "concept",
                   "concept_ancestor", "concept_mapping", "concept_relation",
                   "attribute_definition", "metric_definition", "category_node",
                   "search_document",
                   # 人才画像维度注册表（schema/sql/013_dimensions.sql）。它是"受控"的
                   # 元数据——一行一个维度，说清 kind/comparator/权重——和 field_catalog、
                   # metric_definition 同类，所以归到这里，而不是归到人才档案（它不存人）。
                   "dimension"],
    "人才档案": ["person", "person_demographics", "person_pii", "person_constraint",
                 "education_record", "employment_record", "training_record", "credential",
                 "clinical_exposure", "project_record", "research_output", "award_honor",
                 "narrative", "preference", "assessment", "skill_assertion", "evidence",
                 "trajectory_event", "transition_case", "salary_benchmark", "gap_analysis",
                 "match_run", "match_result"],
    "岗位与市场": ["employer", "job_posting", "job_requirement", "job_task", "occupation",
                   "job_competency_weight", "source_registry", "ingest_run", "provenance"],
    "演化层": ["occupation_candidate", "occupation_change", "occupation_migration",
               "competency_run", "competency_drift", "evolution_policy", "concept_candidate"],
    "扩展值与观测": ["field_value", "first_occurrence", "observation_window",
                     "derived_feature", "assertion"],
    "治理与合规": ["access_log", "access_policy", "consent_record", "consent_withdrawal_action",
                   "data_lifecycle_run", "incident_report", "retention_policy",
                   "subject_request", "talent_deletion_request", "tombstone", "change_log",
                   # 迁移台账（ops/pg.py 创建）：记录每个迁移文件的 sha256 与应用时间，
                   # 使"只跑未应用的迁移"成为可能。运维元数据，归治理域比新开一组贴切。
                   "schema_migration",
                   # 访问控制层（schema/sql/017_access_control.sql 创建）：
                   #   access_tier    —— 等级阶梯（T0 公开 … T3 管理员，X 禁止）
                   #   column_policy  —— 字段级最低可读等级，由 refresh_column_policy() 推导
                   # 这两张表是"每个字段权限不同"这条需求的载体，属治理域。
                   # （ops/health.py 的 I1.2 指标就是靠这里发现它们漏登记的 —— 这正是指标的作用。）
                   "access_tier", "column_policy",
                   # 网页登录与会话（schema/sql/022_web_login.sql 创建）：
                   #   app_user     —— 登录用户及其等级（口令只存 bcrypt 哈希）
                   #   web_session  —— 服务端会话；等级由库里的行决定，不放在 cookie 里
                   # 它们是"记录访问用户"的载体，同属治理域。
                   "app_user", "web_session",
                   # 列级剖析快照（schema/sql/027_column_profile.sql 创建）：
                   # 它是**数据质量**的产物（空值率/基数/Top-K），不是业务数据，
                   # 所以归治理域而不是人才/岗位域。目录页只读它，计算由 ops/profile.py 离线做。
                   "column_profile",
                   # 导出禁令的显式落点（schema/sql/032_export_control.sql 创建）：
                   # 动作级策略（禁止导出）→ 物理列。与 column_policy 分工：
                   # 前者管"能不能带走"，后者管"能不能看"。
                   "export_denied",
                   # 年龄段口径（schema/sql/036_age_band.sql 创建）：
                   #   age_band_asis —— 年龄段计算的**基准年**（单行表）。
                   #   刻意不用 now()：派生字段必须说明时间基准，否则会被当成实时值。
                   "age_band_asis",
                   # 公开视图注册表（schema/sql/038_public_view_registry.sql 创建）：
                   # 登记"对匿名开放"的视图及其理由。视图以属主权限执行，
                   # 所以"哪些视图能公开"必须显式登记，不能靠函数里的字面清单（会漏）。
                   "public_view_registry",
                   # 登录限流（schema/sql/039_login_throttle.sql 创建）：
                   #   login_attempt —— 登录尝试流水（只存邮箱与时间，用于失败限流）
                   #   login_policy  —— 限流参数（阈值/窗口/保留期）
                   # 它们是"记录访问用户"的组成部分，同属治理域。
                   "login_attempt", "login_policy",
                   # 计数闸门的排除登记（schema/sql/041_count_gateway_exclusions.sql 创建）：
                   # 登记"行数也不公开"的表及理由（如登录流水）。策略元数据，属治理域。
                   "public_count_excluded",
                   # 函数执行权登记（schema/sql/045_function_grant_registry.sql 创建）：
                   # "谁可以执行哪个函数、为什么"。PostgreSQL 建函数时默认授 PUBLIC，
                   # 实测曾有 82 个函数对 PUBLIC 可执行 —— 故改为注册表驱动。属治理域。
                   "function_grant"],
    "小程序接入": ["external_identity", "response_session", "answer", "experience_episode",
                   "experience_task", "crosswalk", "sync_event"],
    "备份与发布": ["backup_policy", "backup_run", "restore_run", "dataset_release"],
}


def domain_of(table: str) -> str:
    for dom, tables in DOMAIN_TABLES.items():
        if table in tables:
            return dom
    return "未归类"


# ---------------------------------------------------------------------------
# 连接与查询
# ---------------------------------------------------------------------------
# 会话：等级由**数据库里的行**决定，不来自 cookie 里的声明
# ---------------------------------------------------------------------------
# 为什么不做"签名 cookie 里带等级"：cookie 是客户端可伪造的，而本项目的强制点是
# 数据库（`SET LOCAL ROLE mt_tN`）。若等级来自 cookie，伪造 cookie 就等于伪造 T3 权限 ——
# 前端一破、后端全开。所以用服务端会话表（迁移 022），cookie 里只放一个随机 256 位
# 会话 id，等级由 `mt.web_session_lookup()` 从库里查回来。
class Session:
    """一次 HTTP 请求的身份：谁（actor）、能读哪个等级（tier）、会话 id。

    匿名访客也有一个会话对象（tier=T0）—— 这样"没有身份"和"身份是 T0"在代码里
    是同一条路径，不会出现"忘了判断登录态就当成高权限"的分支。
    """

    def __init__(self, actor="anonymous", tier="T0", session_id=None,
                 user_id=None, ok=False, expired=False):
        self.actor = actor or "anonymous"
        self.tier = tier if tier in TIERS else "T0"
        self.session_id = session_id
        self.user_id = user_id
        self.ok = ok                    # 会话有效（已登录且未过期）
        self.expired = expired

    @property
    def role(self) -> str:
        return "mt_" + self.tier.lower()

    @property
    def is_anonymous(self) -> bool:
        return not self.ok

    def __repr__(self):
        return "<Session %s %s%s>" % (self.actor, self.tier,
                                      "" if self.ok else " (匿名)")


ANON = Session()

# 导出禁令的进程内缓存（迁移 032）
_EXPORT_CACHE = None
_EXPORT_LOCK = threading.Lock()     # 防并发首请求各建一遍（见 export_policy）
# 计数可见性缓存（迁移 048：判据唯一来源 mt.v_count_visibility）
_COUNT_VIS_CACHE = None

# 当前请求的会话：用线程局部存放，而不是把它穿进 20 多个 view 函数的签名。
# 为什么线程局部在这里是安全的：`ThreadingHTTPServer` 一个请求一个线程，
# 会话不会跨请求泄漏；而且它只用于**渲染**（页头显示身份），
# 真正的权限判断在数据库（`SET LOCAL ROLE`），不依赖这个变量。
_LOCAL = threading.local()

# ---------------------------------------------------------------------------
# 并发闸：把"数据库连接数"变成有上界的量，而不是跟着请求数涨
# ---------------------------------------------------------------------------
# 为什么必须有（实测的算式）：
#   · `max_connections = 50`（ops/pg.py 设的）；
#   · 门户**每个请求要开 3 条连接**：会话查询（`_cookie_session`）+ 页面查询（`db()`）
#     + 审计（`log_visit` 用独立连接，刻意不污染只读事务）；
#   · `ThreadingHTTPServer` 是"一请求一线程、**没有上限**"。
#   于是并发约 16 个请求就能把 50 条连接吃干净，第 17 个开始拿到
#   `sorry, too many clients already`。而这是**公网**地址，任何人可并发触发。
#
# 加闸之后：最多 `MAX_CONCURRENT_REQUESTS` 个请求同时进业务逻辑，
# 连接数上界 ≈ 3 × 该值（默认 10 → 约 30 条，留出备份/psql/其它工具的余量）。
# 超出的请求**排队等待**，等不到就回 503 + Retry-After（见 handle_one_request）。
# 这是"过载时优雅降级"，而不是"过载时雪崩"：后者会让 Cloudflare 那边看到一片 500，
# 而真正的原因是本地连接池被自己打满 —— 现象离原因很远，最难查。
#
# 为什么不用连接池（psycopg_pool）：本机**没有**这个包（实测 find_spec 为 None），
# 而网络受限无法安装。加闸是把同样的问题用更少依赖解决 ——
# 池解决"复用"，闸解决"上界"，而真正致命的是没有上界。
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MEDTALENT_MAX_CONCURRENCY", "10"))
_REQ_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_REQ_WAIT_SECONDS = float(os.environ.get("MEDTALENT_REQ_WAIT", "8"))
_REQ_WAITERS = threading.Semaphore(0)          # 仅用于观测：当前排队数



def set_current_session(s: Session):
    _LOCAL.session = s
    _LOCAL.csv_denied = None


def current_session() -> Session:
    return getattr(_LOCAL, "session", ANON)


def session_from_cookie(cookie_header: str) -> Session:
    """从 Cookie 头解析会话 id 并回库查等级。

    查库用的是 SECURITY DEFINER 的 `web_session_lookup()`：门户角色读不到
    `web_session` 表本身（迁移 022 已 REVOKE），只能通过这个函数问"这个 id 是谁"。
    """
    if not cookie_header:
        return ANON
    sid = None
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == SESSION_COOKIE and v:
            sid = v.strip('"')
            break
    if not sid:
        return ANON
    try:
        with psycopg.connect(PORTAL_DSN, row_factory=dict_row, autocommit=True) as c:
            r = c.execute("SELECT * FROM mt.web_session_lookup(%s)", (sid,)).fetchone()
    except psycopg.Error:
        # 会话查询失败 ⇒ 退回匿名。**默认方向必须是拒绝**，不是放行。
        return ANON
    if not r or not r["ok"]:
        return Session(session_id=sid, expired=bool(r and r["expired"]))
    return Session(actor=r["actor"], tier=r["tier"], session_id=sid,
                   user_id=r["user_id"], ok=True)


def db(session: Session = None, readonly: bool = True, as_role: bool = True):
    """以门户角色连接，并把会话等级翻译成数据库角色。

    这是**唯一的**权限注入点：所有读数据的页面都经 `with db(session) as c:`，
    所以"某个页面忘了判权限"不会导致越权 —— 它只会读到 T0 能读的列。

    `as_role=False`：**认证类操作**专用。登录/建会话这类函数只授给了 `mt_portal`
    （迁移 022/023），没有授给四个等级角色 —— 因为"用哪个等级读数据"和
    "能不能登录"是两件事。本探针第一版没区分这两者，于是登录请求先
    `SET LOCAL ROLE mt_t0`，紧接着调用 `web_login()` 就被数据库拒绝：
    **连登录都登不进去**。这个错误只有把两条路径分开才对。

    为什么 `SET LOCAL ROLE` 而不是 `SET ROLE`：LOCAL 在事务结束（含异常回滚）时
    自动失效，连接被线程复用时不会串身份。
    为什么等级用白名单校验后再拼进 SQL：`SET LOCAL ROLE` **不接受参数占位符**
    （扩展协议会把它发成 $1），只能拼字符串 —— 那就必须先证明它来自白名单。
    """
    session = session or ANON
    tier = session.tier if session.tier in TIERS else "T0"
    role = "mt_" + tier.lower()
    c = psycopg.connect(PORTAL_DSN, row_factory=dict_row, autocommit=True)
    with c.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = %s" % STATEMENT_TIMEOUT_MS)
        if readonly:
            cur.execute("BEGIN TRANSACTION READ ONLY")
        else:
            cur.execute("BEGIN")
        # 身份与等级都放进会话变量：日志函数从会话变量取"谁"，
        # 而"能读什么"由 SET LOCAL ROLE 决定 —— 两者都不可由客户端直接指定。
        cur.execute("SELECT set_config('mt.actor', %s, true)", (session.actor,))
        cur.execute("SELECT set_config('mt.tier', %s, true)", (tier,))
        if as_role:
            cur.execute("SET LOCAL ROLE " + role)
    # 把会话挂在连接对象上：这样"需要另开一个连接"的少数地方
    # （例如只读 SQL 控制台要隔离用户查询）能拿到同一个身份，
    # 而不必把 session 一路穿进 20 多个 view 函数的签名里。
    c.session = session
    c.tier = tier
    return c


def log_visit(session: Session, action: str, target: str, tier: str = None,
              rows: int = None, purpose: str = None, detail: dict = None):
    """写一条访问日志。

    **必须用独立连接**：页面的查询跑在 `READ ONLY` 事务里，而只读事务会拒绝
    审计写入 —— 实测 `cannot execute INSERT in a read-only transaction`，
    而且 SECURITY DEFINER 也救不了它（只读是事务级属性，不是权限问题）。
    所以审计走自己的短连接：页面只读这条纪律不被破坏（`change_log` 仍不增长），
    同时"谁读过什么"真的被记下来。

    ⚠ **独立连接上必须自己把身份设回去**（这里踩过一次真 bug）：
    `mt.actor` 是**会话变量**，`current_setting('role')` 也属于会话。
    只读连接上设过的那些值，在审计连接上**不存在**。第一版就是这么漏的 ——
    结果 346 条日志里 `actor` 全是 `anonymous`、`actor_role` 全是 `mt_portal`，
    看起来"日志在记"，实际**没记到是谁**。
    所以这里显式：`set_config('mt.actor', …)` + `SET LOCAL ROLE mt_tN`。
    顺带的好处是 `actor_role` 变成**数据库认定的等级角色**（`mt_t1` 等），
    而不只是登录角色 —— 这一列才有"权限审计"的意义。

    写不进去也不能让页面崩：审计失败只是少一条记录，而页面 500 是可用性事故。
    但**必须打印到 stderr**，否则"审计静默失效"会变成一个查不出来的洞。
    """
    session = session or ANON
    tier = (tier or session.tier or "T0")
    role = "mt_" + tier.lower() if tier.lower() in ("t0", "t1", "t2", "t3") else None
    try:
        with psycopg.connect(PORTAL_DSN, row_factory=dict_row, autocommit=True) as c:
            # 身份：应用声明的"谁"
            c.execute("SELECT set_config('mt.actor', %s, false)", (session.actor,))
            c.execute("SELECT set_config('mt.tier', %s, false)", (tier,))
            # 等级角色：让 actor_role 变成数据库认定的角色（不是 login 角色）
            if role:
                c.execute("SET ROLE " + role)
            c.execute("SELECT mt.log_access(%s, %s, %s, %s, %s, %s)",
                      (action, target, tier, rows, purpose,
                       json.dumps(detail or {}, ensure_ascii=False)))
            if role:
                c.execute("RESET ROLE")
    except psycopg.Error as e:
        print("[!] 审计写入失败（不影响页面）：%s" % str(e).splitlines()[0], file=sys.stderr)


def revoke_session(sid: str):
    """撤销一个会话，用独立连接。

    为什么不在 do_GET 的主连接里做：那条连接在 `READ ONLY` 事务里，
    而撤销会话是 UPDATE —— 只读事务会拒绝（实测：SECURITY DEFINER 也救不了，
    只读是事务级属性）。和审计写入同一个道理。
    """
    if not sid:
        return
    try:
        with psycopg.connect(PORTAL_DSN, row_factory=dict_row, autocommit=True) as c:
            c.execute("SELECT mt.web_session_revoke(%s)", (sid,))
    except psycopg.Error as e:
        print("[!] 会话撤销失败：%s" % str(e).splitlines()[0], file=sys.stderr)


# 路径 → 该页"通常"需要的等级。**只用于错误提示文案**，不是权限判断：
# 真正的判断在数据库（列级授权 + SET LOCAL ROLE），这里只是把拒绝翻译成人话。
# 写成映射而不是"从错误信息里猜"，是因为提示必须稳定且可核对。
_TIER_HINT = {
    "/talent": "T1", "/talent.csv": "T1", "/match": "T1", "/real": "T1",
    "/occupations": "T1", "/tree": "T1", "/analyze": "T1", "/viz": "T1",
    "/search": "T1", "/catalog": "T0",
    "/quality": "T2", "/audit": "T2", "/lineage": "T2",
    "/t/access_log": "T2", "/t/change_log": "T2",
    "/t/person_pii": "T3", "/t/app_user": "T3", "/t/web_session": "T3",
}


def _required_tier_for(path: str) -> str:
    return _TIER_HINT.get(path, "T1")


# 等级序数：与数据库里的 mt.access_rank 保持一致（T0<T1<T2<T3<X）。
# 这里在 Python 侧留一份只用于**渲染判断**（决定显示"权限不足"还是"未剖析"）；
# 真正的权限判断在数据库，不依赖这个表。
_TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "X": 99}


def _tier_rank(t: str) -> int:
    return _TIER_RANK.get(t or "", -1)


def q(c, sqltext, p=None):
    with c.cursor() as cur:
        cur.execute(sqltext, p)
        return cur.fetchall()


def q1(c, sqltext, p=None):
    r = q(c, sqltext, p)
    return list(r[0].values())[0] if r else None


def try_read(c, sqltext, p=None):
    """在 SAVEPOINT 里"试探性地读一条"：失败则把事务恢复到可用状态并返回 None。

    **为什么必须用 SAVEPOINT**（这个坑本项目踩过两次，第二次就在这里）：
    PostgreSQL 里一条语句失败会让**整个事务**进入 aborted 状态，之后任何语句都报
    `InFailedSqlTransaction` —— 也就是说"我试探着读一下，读不到就算了"这个写法，
    会把后面所有正常的查询一起打死，表现成整页 500 且错误信息与真正的权限问题无关。
    用 SAVEPOINT 把失败圈在一小段里，才能安全地做"能读就读、读不到降级"。

    用途很明确：**可降级的读**（例如目录页的剖析统计对低等级不可读）。
    真正的数据读取仍用 q()，让权限错误照常抛出、由 InsufficientPrivilege 处理器
    渲染成「权限不足」页 —— 降级只用在"少一列信息也能用"的地方。
    """
    try:
        c.execute("SAVEPOINT mt_probe")
        with c.cursor() as cur:
            cur.execute(sqltext, p)
            rows = cur.fetchall()
        c.execute("RELEASE SAVEPOINT mt_probe")
        return rows
    except psycopg.Error:
        c.execute("ROLLBACK TO SAVEPOINT mt_probe")
        return None


# ---------------------------------------------------------------------------
# 元数据层：门户的"信息展示"全部由这里驱动
# ---------------------------------------------------------------------------
def load_meta(c) -> dict:
    """一次读进全部系统目录信息：表、视图、列、约束、索引、行数。"""
    rels = q(c, """
        SELECT c.relname AS name,
               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view'
                              WHEN 'm' THEN 'matview' ELSE c.relkind::text END AS kind,
               obj_description(c.oid, 'pg_class') AS comment
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm')
         ORDER BY CASE c.relkind WHEN 'r' THEN 0 ELSE 1 END, c.relname""", (SCHEMA,))

    cols = q(c, """
        SELECT c.relname AS table_name, a.attname AS column_name, a.attnum,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull AS not_null,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr,
               col_description(a.attrelid, a.attnum) AS comment,
               t.typtype AS type_kind
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_type t ON t.oid = a.atttypid
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm')
           AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY c.relname, a.attnum""", (SCHEMA,))

    cons = q(c, """
        SELECT c.conrelid::regclass::text AS table_name, c.conname AS name,
               c.contype::text AS kind,
               pg_get_constraintdef(c.oid) AS definition,
               (SELECT string_agg(a.attname, ', ' ORDER BY k.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS columns,
               CASE WHEN c.contype = 'f' THEN c.confrelid::regclass::text END AS ref_table,
               CASE WHEN c.contype = 'f' THEN
                    (SELECT string_agg(a.attname, ', ' ORDER BY k.ord)
                       FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum)
               END AS ref_columns
          FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
         WHERE n.nspname = %s ORDER BY c.contype, c.conname""", (SCHEMA,))

    idx = q(c, """
        SELECT tablename AS table_name, indexname AS name, indexdef AS definition
          FROM pg_indexes WHERE schemaname = %s ORDER BY tablename, indexname""", (SCHEMA,))

    trg = q(c, """
        SELECT c.relname AS table_name, t.tgname AS name,
               pg_get_triggerdef(t.oid) AS definition
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND NOT t.tgisinternal
         ORDER BY c.relname, t.tgname""", (SCHEMA,))

    m = {"rels": [], "by_name": {}, "cols": {}, "cons": {}, "idx": {}, "trg": {},
         "fk_cols": {}, "pk": {}, "inbound": {}}

    # 精确行数：reltuples 会过时（实测 code_table 估 53 / 实 49），门户不显示估算值
    t0 = time.time()
    for r in rels:
        r = dict(r)
        with c.cursor() as cur:
            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(SCHEMA), sql.Identifier(r["name"])))
            r["rows"] = cur.fetchone()["n"]
        r["domain"] = domain_of(r["name"]) if r["kind"] == "table" else "视图"
        r["n_cols"] = 0
        m["rels"].append(r)
        m["by_name"][r["name"]] = r
    m["count_seconds"] = round(time.time() - t0, 2)

    for c_ in cols:
        c_ = dict(c_)
        t = c_["table_name"]
        m["cols"].setdefault(t, []).append(c_)
        m["by_name"][t]["n_cols"] = len(m["cols"][t])

    for x in cons:
        x = dict(x)
        t = x["table_name"]
        m["cons"].setdefault(t, []).append(x)
        if x["kind"] == "p" and x["columns"]:
            m["pk"][t] = x["columns"].split(", ")[0]
        if x["kind"] == "f" and x["ref_table"]:
            ref = x["ref_table"].split(".")[-1]
            for local, remote in zip((x["columns"] or "").split(", "),
                                     (x["ref_columns"] or "").split(", ")):
                m["fk_cols"].setdefault(t, {})[local] = (ref, remote)
            m["inbound"].setdefault(ref, []).append(
                {"child": t, "child_cols": x["columns"], "ref_cols": x["ref_columns"],
                 "constraint": x["name"]})

    for x in idx:
        m["idx"].setdefault(x["table_name"], []).append(dict(x))
    for x in trg:
        m["trg"].setdefault(x["table_name"], []).append(dict(x))

    # 码值 → 中文标签：这个库"存码不存标签"，所以任何面向人的页面都必须翻译，
    # 否则屏幕上全是 CT1 / D3 / V1 这种码。一次载入 305 条，全站复用。
    m["labels"] = {}
    for cv in q(c, "SELECT code_table_id, code, label_zh FROM code_value"):
        m["labels"][(cv["code_table_id"], cv["code"])] = cv["label_zh"]
    return m


def lbl(code_table_id: str, code) -> str:
    """把码翻译成中文标签；译不出来就原样返回（而不是编一个）。"""
    if code is None:
        return ""
    v = META.get("labels", {}).get((code_table_id, str(code)))
    return v if v else str(code)


META = {}          # 进程内缓存：门户是只读的，元数据在一次演示里不必反复重读
# 元数据缓存的**有效期**与互斥锁。
#
# 为什么必须有有效期（原来是无条件永久缓存）：
#   · 这是**长时间运行**的服务（已经跑了几十小时）；
#   · 而**另一个进程**可以改变结构 —— 开发者模式（:8083）能建维度/加列、
#     迁移也会加表。永久缓存意味着"结构页显示的是门户启动那一刻的样子"，
#     而目录页的整个卖点就是"看到数据库真实的样子"。
#     **缓存没有失效机制，等于给读者看一份越来越旧的地图。**
#   · 300 秒是权衡：重建一次要读全部系统目录 + 每张表 count(*)（约 0.5 秒），
#     而结构变化是低频事件。要立刻生效就用 `refresh_meta()`（或重启门户）。
META_TTL_SECONDS = int(os.environ.get("MEDTALENT_META_TTL", "300"))
META_AT = 0.0                      # 上次构建的时间戳（time.monotonic）
_META_LOCK = threading.Lock()      # 防"检查-构建"竞态：并发首请求会各自建一遍


def admin_db(readonly: bool = True):
    """特权连接：**只用于**进程级元数据缓存与构建期自检，不用于处理用户请求。

    为什么元数据允许用特权连接：`load_meta()` 读的是系统目录（pg_class/information_schema）
    与**每张表的行数**——行数是明确公开的信息（需求原文："数量…可以公开"），
    而且实测 T0 角色也算得出来（`count(*)` 不引用任何列，不受列级权限限制）。
    但它是**进程级缓存**、只建一次、不分会话，所以这里不做角色切换。
    用户请求一律走 `db(session)`；两者分开是为了让"哪条路径带权限"一眼可见。
    """
    if readonly:
        c = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
        with c.cursor() as cur:
            cur.execute("BEGIN TRANSACTION READ ONLY")
        return c
    return psycopg.connect(DSN, row_factory=dict_row)


def meta() -> dict:
    """进程级元数据缓存，**带有效期**（见 META_TTL_SECONDS 的说明）。

    用锁包住"检查-构建"：`ThreadingHTTPServer` 下一请求一线程，
    并发首请求会同时发现缓存为空、各自构建一遍（结果一样，但白做几遍重活，
    而且会让启动瞬间的数据库负载翻几倍）。
    """
    global META, META_AT
    now = time.monotonic()
    if META and (META_TTL_SECONDS <= 0 or now - META_AT < META_TTL_SECONDS):
        return META
    with _META_LOCK:
        # 双重检查：等锁期间别的线程可能已经建好了
        now = time.monotonic()
        if META and (META_TTL_SECONDS <= 0 or now - META_AT < META_TTL_SECONDS):
            return META
        with admin_db() as c:
            META = load_meta(c)
        META_AT = time.monotonic()
    return META


def refresh_meta():
    """立刻重建元数据（结构刚被改过时用；迁移脚本与测试也用它）。"""
    global META, META_AT
    with _META_LOCK:
        META = {}
        with admin_db() as c:
            META = load_meta(c)
        META_AT = time.monotonic()
    return META


def require_table(name: str) -> str:
    m = meta()
    if name not in m["by_name"]:
        raise PortalError(404, "未知的表或视图：%s" % name)
    return name


def require_column(table: str, col: str) -> str:
    m = meta()
    known = [x["column_name"] for x in m["cols"].get(table, [])]
    if col not in known:
        raise PortalError(400, "表 %s 没有列 %s" % (table, col))
    return col


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2328;background:#f6f8fa}
nav{background:#0d1117;color:#e6edf3;padding:0 18px;display:flex;align-items:center;gap:2px;flex-wrap:wrap}
nav .brand{font-weight:600;margin-right:16px;color:#fff;padding:12px 0}
nav a{color:#b6c2cf;text-decoration:none;padding:12px 11px;font-size:13px;border-bottom:2px solid transparent}
nav a:hover{color:#fff;background:#161b22}
nav a.on{color:#fff;border-bottom-color:#2f81f7}
/* 开发者模式是可写的另一个进程（8083），它在只读门户的导航里必须一眼可辨，
   否则"只读"这个前提在导航条上就自相矛盾。分隔线 + 「可写」标签就是干这个的。 */
nav .navsep{width:1px;height:18px;background:#30363d;margin:0 9px}
nav a .wtag{font-size:10px;line-height:15px;border:1px solid #9e6a03;color:#d29922;
  border-radius:9px;padding:0 5px;margin-left:5px;vertical-align:1px}
.wrap{max-width:1400px;margin:0 auto;padding:20px 18px 60px}
h1{font-size:21px;margin:0}
h2{font-size:15px;margin:0 0 10px}
h3{font-size:13px;margin:16px 0 6px;color:#57606a;text-transform:uppercase;letter-spacing:.04em}
a{color:#0969da}
/* 页头带：标题 + 副标题 + 面包屑。13 个顶级导航项说不出"你在第几层"，
   所以表详情 / 实体页 / 人才页 / 职业页必须自己声明层级（面包屑只放祖先，不放当前页）。 */
.phead{border-bottom:1px solid #d8dee4;padding-bottom:12px;margin-bottom:16px}
.phead .sub{margin:6px 0 0}
.crumbs{font-size:12px;color:#57606a;margin:0 0 6px}
.crumbs a{color:#0969da;text-decoration:none}
.crumbs a:hover{text-decoration:underline}
.crumbs .sep{color:#8c959f;margin:0 6px}
.sub{color:#57606a;margin-bottom:16px;font-size:13px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:16px;margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.metric{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px}
.metric .n{font-size:23px;font-weight:600;color:#0d1117;line-height:1.25}
.metric .t{font-size:12px;color:#57606a}
/* 灰阶文案统一到 #59636e / #6e7781：#8c959f 在 #fff 上只有 2.9:1，
   低于 WCAG AA 的 4.5:1（正文）与 3:1（大字）。11px 的说明行本来就要放大看，
   再给它最浅的灰等于不给看。 */
.metric .d{font-size:11.5px;color:#656d76;margin-top:3px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #e6e8eb;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600;color:#424a53;white-space:nowrap;position:sticky;top:0;
   box-shadow:inset 0 -1px 0 #d0d7de}
td code{word-break:break-word}
tr:hover td{background:#fbfcfd}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
/* 数据表：一行一条记录。长文本用省略号截断（完整值在 title 里），
   否则一个长 description 就把整张表的行高撑开，没法看。 */
.tscroll td{max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tscroll table{table-layout:auto}
.kv>div:nth-child(even){white-space:normal;word-break:break-word}
.tscroll{overflow:auto;max-height:620px;border:1px solid #e6e8eb;border-radius:6px}
/* `.tscroll` 也要吸左首列。原来只有 `.tablewin` 有（人才库走的正是它），
   而全库最宽的表 `job_posting`（实测 41 列）走的是 `.tscroll` ——
   横向滑动时看不见"这是哪一行"，多列就等于没有。
   同一套需求在两处容器上只实现了一处，看到的人才库能读、点进表页面就又不能读了。 */
.tscroll thead th{position:sticky;top:0;z-index:3;background:#f6f8fa}
.tscroll th:first-child,.tscroll td:first-child{position:sticky;left:0;z-index:2;
                          background:#fff;box-shadow:1px 0 0 #e6e8eb}
.tscroll thead th:first-child{z-index:4;background:#f6f8fa}
.tscroll tbody tr:hover td:first-child{background:#fbfcfd}
/* 数据窗口：横向 + 纵向都在窗口内滑动，表头吸顶、首列吸左。
   为什么首列也要吸：横向滑动时如果连"这是哪一行"都看不见，多列就等于没有。
   宽表（人才库 30 列）靠这两个 sticky 才读得下去。 */
.tablewin{overflow:auto;max-height:72vh;border:1px solid #e6e8eb;border-radius:6px;
          position:relative;background:#fff}
.tablewin table{border-collapse:separate;border-spacing:0}
.tablewin th,.tablewin td{white-space:nowrap;border-bottom:1px solid #e6e8eb;
                          border-right:1px solid #f2f4f7}
.tablewin thead th{position:sticky;top:0;z-index:3;background:#f6f8fa}
.tablewin tbody tr:hover td{background:#fbfcfd}
.tablewin th:first-child,.tablewin td:first-child{position:sticky;left:0;z-index:2;
                          background:#fff;box-shadow:1px 0 0 #e6e8eb}
.tablewin thead th:first-child{z-index:4;background:#f6f8fa}
.tablewin tbody tr:hover td:first-child{background:#fbfcfd}
/* ---- 字段选择器 ----------------------------------------------------------
   改版前的问题（实测，不是审美）：35 个复选框常驻铺开 = 2765 个字符的文字墙，
   而它排在数据表**前面**，于是首屏只看到筛选器和一墙复选框，一行数据都看不到。
   改版后：整体收进原生 <details>（零 JS），组内用边框盒子分区，勾中的项上底色，
   组头带"选中 n / m"计数和"全选本组 / 清空本组"链接——形状能看出状态，
   操作能一步到位，而不是 Ctrl 点 35 次。 */
.pickerbox{border:1px solid #d0d7de;border-radius:8px;background:#fafbfc;margin-top:10px}
.pickerbox>summary{cursor:pointer;padding:10px 12px;font-size:13px;list-style:none;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-radius:8px}
.pickerbox>summary::-webkit-details-marker{display:none}
.pickerbox>summary::before{content:"▸";color:#0969da;font-size:11px}
.pickerbox[open]>summary::before{content:"▾"}
.pickerbox[open]>summary{border-bottom:1px solid #e6e8eb}
.pickerbox>summary:hover{background:#f3f5f8}
.colgrps{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px;padding:10px}
.colgrp{border:1px solid #e6e8eb;border-radius:6px;background:#fff;padding:8px 10px}
.colgrphd{display:flex;align-items:baseline;gap:8px;font-size:12.5px;color:#24292f;
          border-bottom:1px solid #eaeef2;padding-bottom:5px;margin-bottom:6px}
.colgrphd .cgn{color:#656d76;font-size:11.5px;font-variant-numeric:tabular-nums}
.colgrphd .cgop{margin-left:auto;font-size:11.5px;white-space:nowrap}
.colpick{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:1px 8px}
.colpick label{font-weight:400;display:flex;align-items:center;gap:5px;font-size:12.5px;
               padding:2px 4px;border-radius:4px;margin:0}
.colpick label.on{background:#ddf4ff;color:#0a3069}
.colpick label.off{color:#656d76}
.colpick input{width:auto;margin:0;padding:0}
/* 链接做成按钮的样子：预设切换是**导航**（换一套字段），不是提交表单。
   原来它们是 35 个复选框同一个 form 里的 submit 按钮，浏览器会把勾选一起发出去，
   于是"点预设"和"点筛选"走了同一条路，预设永远被 cols 覆盖 —— 实测点了没反应。 */
.btnlink{display:inline-block;padding:7px 12px;border:1px solid #d0d7de;border-radius:6px;
         background:#f6f8fa;color:#24292f;font-size:13px;text-decoration:none;cursor:pointer}
.btnlink:hover{filter:brightness(0.97);text-decoration:none}
.btnlink.on{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:500}
.btnlink.sec{background:#fff}
code,.mono{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12px}
pre{background:#0d1117;color:#c9d1d9;padding:12px;border-radius:6px;overflow:auto;font-size:12px;margin:8px 0}
pre .k{color:#ff7b72}
details{margin:8px 0}
summary{cursor:pointer;color:#0969da;font-size:13px;outline:none}
.nul{color:#6e7781;font-style:italic}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;border:1px solid}
/* 身份条：等级必须常驻可见（"看不到"和"没有"在界面上长得一样） */
.who{font-size:12px;color:#57606a;margin-left:auto;display:inline-flex;align-items:center;gap:6px}
.loginlink{margin-left:10px;font-size:12px}
.p-t{background:#ddf4e4;border-color:#a2d5b3;color:#116329}
.p-v{background:#ddf4ff;border-color:#a5d6ff;color:#0a3069}
.p-e{background:#fff8c5;border-color:#eac54f;color:#7d4e00}
.p-n{background:#f6f8fa;border-color:#d0d7de;color:#57606a}
.p-red{background:#ffebe9;border-color:#ffc1bc;color:#a40e26}
.bar{height:9px;background:#2f81f7;border-radius:5px;display:inline-block;vertical-align:middle}
.barwrap{background:#eaeef2;border-radius:5px;height:9px;min-width:60px}
.muted{color:#656d76;font-size:12px}
.row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1 1 180px}
input,select,textarea,button{font:inherit;padding:7px 9px;border:1px solid #d0d7de;border-radius:6px;background:#fff;width:100%}
button{background:#1f6feb;color:#fff;border-color:#1f6feb;cursor:pointer;font-weight:500}
button.sec{background:#f6f8fa;color:#24292f;border-color:#d0d7de}
button:hover{filter:brightness(1.06)}
.note{padding:9px 12px;border-radius:6px;margin-bottom:12px;font-size:13px}
.note.ok{background:#ddf4e4;border:1px solid #a2d5b3}
.note.err{background:#ffebe9;border:1px solid #ffc1bc}
.note.info{background:#ddf4ff;border:1px solid #a5d6ff}
.pager a{margin-right:10px;font-size:13px}
/* 页面内的「分析」按钮：原生折叠控件，零 JS。默认收起，展开后是完整的分析块。 */
details.analysis{margin:14px 0 0}
details.analysis>summary{cursor:pointer;display:inline-block;padding:7px 16px;border-radius:6px;
  background:#1f6feb;color:#fff;font-size:13px;font-weight:500;border:1px solid #1f6feb;
  list-style:none;user-select:none}
details.analysis>summary::-webkit-details-marker{display:none}
details.analysis>summary::before{content:"▸ ";font-size:11px}
details.analysis[open]>summary::before{content:"▾ "}
details.analysis[open]>summary{margin-bottom:6px}
details.analysis>summary:hover{filter:brightness(1.06)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
/* 一组并排的小按钮。全局 button{width:100%} 会把它们撑成一列整宽按钮——
   实测：人才库的 7 个预设字段按钮竖着排，把那一行撑到 260px 高（见 before-talent.png）。 */
.btnrow{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;align-items:center}
.btnrow button{width:auto;padding:7px 12px}
/* 空状态：库里 0 行是事实，不是故障。虚线框说清"这里本来就该是空的"。 */
.empty{border:1px dashed #d0d7de;border-radius:8px;padding:20px 14px;text-align:center;
       color:#57606a;background:#fafbfc;font-size:13px}
/* 键盘焦点：默认那圈细描边在浅灰底上几乎看不见（WCAG 2.4.7）。 */
:focus-visible{outline:2px solid #1f6feb;outline-offset:1px;border-radius:3px}
.kv{display:grid;grid-template-columns:200px 1fr;gap:0}
/* 字段表：宽屏排成两对一行。职业详情有 25 个字段，单列要滚 25 行，双列 13 行看完——
   信息一个不少，滚动少一半。 */
@media(min-width:1180px){.kv{grid-template-columns:190px minmax(0,1fr) 190px minmax(0,1fr)}}
.kv>div{padding:5px 9px;border-bottom:1px solid #eaeef2}
.kv>div:nth-child(odd){color:#57606a;background:#fafbfc;font-size:12.5px}
"""

NAV = [("/", "总览"), ("/catalog", "数据目录"), ("/viz", "可视化"), ("/talent", "人才库"),
       ("/occupations", "职业库"),
       ("/tree", "职业树"), ("/match", "匹配"), ("/real", "真实案例"),
       ("/extend", "扩展与演化"),
       ("/schema", "表与视图"), ("/search", "检索"), ("/analyze", "分析"),
       ("/quality", "质量"), ("/audit", "访问日志"), ("/lineage", "血缘"), ("/sql", "SQL"),
       ("/dev", "开发者模式")]


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def page(title, body, msg="", kind="info", subtitle="", nav=None,
         here=None, crumbs=None, session=None) -> bytes:
    """页面外壳。

    `here`：当前所在的顶级栏目（用于高亮导航）。详情页的 h1 是记录名/表名/分析名，
    跟导航标签对不上——不显式指定，这些页面上一个导航项都不会高亮，用户就丢了位置。
    `crumbs`：[祖先链]，元素是 (href, 文本)，href 为 None 表示纯文本。只放祖先，不放当前页。
    `session`：当前身份。**必须在每个页面上显示等级** —— 用户看不到"我现在是什么等级"，
    就会把"权限不足导致的空数据"误读成"库里没有数据"。
    """
    active = here or title
    parts = []
    for u, t in (nav or NAV):
        cls = "on" if t == active else ""
        cur = ' aria-current="page"' if cls else ""
        if t == "开发者模式":
            parts.append('<span class="navsep" aria-hidden="true"></span>')
            parts.append('<a href="%s" class="%s"%s>%s<span class="wtag">可写</span></a>'
                         % (u, cls, cur, t))
        else:
            parts.append('<a href="%s" class="%s"%s>%s</a>' % (u, cls, cur, t))
    nav_html = "".join(parts)
    # 身份条：等级 + 是谁 + 登录/退出入口。
    # 这不是装饰：等级决定了这一页能读到哪些列，而"看不到"和"没有"在界面上长得一样，
    # 不把等级摆在眼前，用户会把权限限制读成数据缺失。
    s = session or current_session()
    who = ('<span class="who">%s <span class="pill %s">%s</span></span>'
           '<a class="loginlink" href="/logout">退出</a>'
           % (esc(s.actor), "p-t" if s.ok else "p-n", esc(s.tier))) if s.ok else \
          ('<span class="who">未登录 <span class="pill p-n">T0</span></span>'
           '<a class="loginlink" href="/login">登录</a>')
    m = '<div class="note %s">%s</div>' % (kind, esc(msg)) if msg else ""
    sub = '<div class="sub">%s</div>' % subtitle if subtitle else ""
    crumb_html = ""
    if crumbs:
        segs = ['<a href="%s">%s</a>' % (href, esc(text)) if href else esc(text)
                for href, text in crumbs]
        crumb_html = '<div class="crumbs">%s</div>' % '<span class="sep">›</span>'.join(segs)
    doc = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>%s · 医学生人才信息库</title><style>%s</style></head><body>
<nav aria-label="主导航"><span class="brand">医学生人才信息库 · 数据库门户</span>%s%s</nav>
<div class="wrap"><div class="phead">%s<h1>%s</h1>%s</div>%s%s</div></body></html>""" % (
        esc(title), CSS, nav_html, who, crumb_html, esc(title), sub, m, body)
    return doc.encode("utf-8")


def metric(v, t, d=""):
    return ('<div class="metric"><div class="n">%s</div><div class="t">%s</div>'
            '<div class="d">%s</div></div>' % (v, esc(t), esc(d)))


def trunc(v, n=70):
    s = "" if v is None else str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def cell_html(table, col, val, maxlen=70, link=True) -> str:
    """单元格渲染：NULL 显式化；外键值渲染成可点的实体链接（这就是"交叉引用"）。"""
    if val is None:
        return '<span class="nul">NULL</span>'
    m = meta()
    text = trunc(val, maxlen)
    if link and isinstance(val, (str, int)):
        fk = m["fk_cols"].get(table, {})
        if col in fk:
            rt, rc = fk[col]
            href = "/e/%s?val=%s" % (rt, urllib.parse.quote(str(val)))
            return '<a href="%s" title="%s.%s">%s</a>' % (href, esc(rt), esc(col), esc(text))
    full = esc(str(val))[:400]
    return '<span title="%s">%s</span>' % (full, esc(text))


def is_numeric(pg_type: str) -> bool:
    """`format_type` 会带修饰符（numeric(3,2) / character varying(64)），必须剥掉再比。"""
    return pg_type.split("(")[0].strip() in (
        "integer", "bigint", "smallint", "numeric", "real",
        "double precision", "money")


def auto_chart(cols, rows) -> str:
    """从结果集的形状自动选一张图。

    这是"分析即可视化"的关键：用户不需要先想"我该画什么图"——
    看到结果的同时就看到了形状。规则很朴素，但明确：

      · 1 个文本列 + ≥1 个数值列  → 条形图（取第一个数值列）
      · 首列是时间/版本/月份/日期 → 折线图（时间用线，不用面积）
      · 其余                       → 不画（宁可没有图，也不画一张误导的图）

    图旁边永远跟着结果表本身：**图是入口，表是依据**。
    """
    try:
        import charts as CH
    except Exception:                                     # noqa: BLE001
        return ""
    if not rows or len(cols) < 2:
        return ""
    def is_num(cn):
        for r in rows:
            v = r.get(cn)
            if v is None:
                continue
            return isinstance(v, (int, float)) or (
                not isinstance(v, (str, bytes, bool)) and _is_numberish(v))
        return False
    dim = cols[0]
    nums = [c for c in cols[1:] if is_num(c)]
    if not nums:
        return ""
    val = nums[0]
    try:
        if any(k in str(dim) for k in ("版本", "月份", "日期", "时间", "年")):
            ch = CH.line([{"name": val, "values": [r.get(val) for r in rows]}],
                         [str(r.get(dim)) for r in rows])
        else:
            ch = CH.bar_h(rows, dim, val, max_rows=20)
        return ch["svg"] if not ch["empty"] else ""
    except Exception:                                     # noqa: BLE001
        return ""


def _is_numberish(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _int_param(qs, key, default, lo=None, hi=None) -> int:
    """从 query string 取一个整数参数；非法值给 400，而不是抛 ValueError。

    为什么不能裸用 `int(qs.get(...))`：`/t/person?page=abc` 会抛 ValueError，
    而 `do_GET` 只接 `PortalError` 与 `psycopg.Error`——valueerror 会穿透到
    socketserver，浏览器看到的是"连接被重置"，连错误页都没有。
    """
    raw = (qs.get(key, [""])[0] or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise PortalError(400, "参数 %s 需要整数，收到：%s" % (key, raw))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def browse_where(table, fcol, fval):
    """「按某列筛某值」的 WHERE 子句；列名先过目录白名单，再用 Identifier 转义。

    表详情页与它的 CSV 导出走的是同一条筛选语义，原先各写一遍——两处一旦
    有一处忘了 require_column，就从"多两行重复"变成"一个注入面"。
    """
    if fcol and fval:
        require_column(table, fcol)
        return (sql.SQL(" WHERE {} = %s").format(sql.Identifier(fcol)), [fval])
    return sql.SQL(""), []


def qs_encode(qs, **over) -> str:
    """把 parse_qs 的结果重新编码成查询串，并叠加/删除若干参数。

    为什么不能写 `{k: v[0] for k, v in qs.items()}`（原来的写法）：
    **`cols` 是多值参数**。人才库的字段选择器是 35 个同名 checkbox，浏览器会为
    每一个勾选的框发一条 `cols=<key>`，于是 parse_qs 得到的是一个**列表**。
    取 `v[0]` 等于把用户勾的 35 列悄悄砍成第 1 列——实测：

        /talent?cols=pid&cols=degree&…&preset=教育   →  表头只剩 person_id

    这不是显示问题，是"点一下筛选就丢 34 列"。排序表头链接、分页链接、CSV 导出
    都走同一个编码路径，所以三处一起坏。统一收到这里，只在一个地方修。

    over 的值传 None 表示**删除**该参数（用于"切预设时必须丢掉旧 cols"）。
    """
    drop = {k for k, v in over.items() if v is None}
    pairs = []
    for k in sorted(qs):
        if k in over or k in drop:
            continue
        for v in qs[k]:
            if v != "":
                pairs.append((k, v))
    for k in sorted(over):
        v = over[k]
        if v is None:
            continue
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            pairs.append((k, str(item)))
    return urllib.parse.urlencode(pairs, doseq=True)


def pager_links(base, qs, page_no, n_pages, single_label="") -> str:
    """分页链接条。base 是路径（如 "/t/person"），qs 是当前查询参数。

    这里必须用 qs_encode 而不是自己拼：分页时翻页不该把用户的列选择弄丢。
    """
    if n_pages <= 1:
        return single_label
    out = []
    for i in range(max(1, page_no - 3), min(n_pages, page_no + 3) + 1):
        href = "%s?%s" % (base, qs_encode(qs, page=str(i)))
        out.append('<a href="%s">%d</a>' % (href, i))
    return " ".join(out)


def render_rows(table, cols, rows, maxlen=70, n_right=()) -> str:
    """结果表。表头用 thead + scope="col"：屏幕阅读器要靠它才知道"这一列是什么"，
    而 sticky 表头也需要一个明确的表头行。数字列右对齐（n_right）——位数对齐了才比得出大小。"""
    if not rows:
        return '<p class="muted">（0 行）</p>'
    head = "".join('<th scope="col" class="%s">%s</th>' % ("n" if c in n_right else "", esc(c))
                   for c in cols)
    body = []
    for r in rows:
        cells = "".join('<td class="%s">%s</td>'
                        % ("n" if c in n_right else "",
                           cell_html(table, c, r.get(c), maxlen))
                        for c in cols)
        body.append("<tr>%s</tr>" % cells)
    return ('<div class="tscroll"><table><thead><tr>%s</tr></thead>'
            '<tbody>%s</tbody></table></div>' % (head, "".join(body)))


def constraint_def_html(x) -> str:
    """约束的完整定义；外键把目标表名做成链接。

    踩过的坑：一开始外键只渲染了「链接 + 目标列」，把
    `FOREIGN KEY (...) REFERENCES ...` 这段定义丢了——而定义本身才是
    "这个库有哪些不变量"的证据。现在原样显示，只把表名变成可点的。
    """
    defn = esc(x["definition"])
    if x["kind"] == "f" and x["ref_table"]:
        ref = x["ref_table"].split(".")[-1]
        token = "REFERENCES " + esc(ref) + "("
        if token in defn:
            defn = defn.replace(
                token, 'REFERENCES <a href="/t/%s"><code>%s</code></a>(' % (ref, esc(ref)), 1)
    return '<code>%s</code>' % defn


def sql_box(sqltext, note="") -> str:
    pretty = re.sub(r"\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|GROUP BY|ORDER BY|LIMIT|"
                    r"OFFSET|UNION|WITH|AS|ON|AND|OR|COUNT|DISTINCT)\b",
                    r'<span class="k">\1</span>', esc(sqltext))
    return ('<details><summary>%s查看本页执行的 SQL</summary><pre>%s</pre></details>'
            % (esc(note or "本页数据来自："), pretty))


def export_policy() -> dict:
    """导出禁令（迁移 032 的 mt.export_denied），进程内缓存一次。

    为什么要按**列名**再存一份：`/sql` 与图表面板的输出列**不带来源表**
    （用户写什么 SQL 都可能），无法按表判断。此时按列名保守拦截 ——
    宁可多拦一个同名的无害列，也不能放过一个该拦的。
    这个取舍是刻意的，写在这里以免下一个人以为漏了什么。
    """
    global _EXPORT_CACHE
    if _EXPORT_CACHE is None:
        # 加锁（独立审查 P2-7）：`ThreadingHTTPServer` 下一请求一线程，
        # 并发首请求会同时发现缓存为空、各自建一遍（结果一样，但白做几遍查询 +
        # 建几次连接；过载时这正好发生在最不该浪费连接的时刻）。
        # 赋值本身是原子的，所以**不会**读到半个字典 —— 锁解决的是重复劳动，不是撕裂。
        with _EXPORT_LOCK:
            if _EXPORT_CACHE is None:
                by_table, by_name = {}, {}
                try:
                    with admin_db() as c:
                        for r in q(c, "SELECT table_name, column_name, because FROM export_denied"):
                            by_table[(r["table_name"], r["column_name"])] = r["because"]
                            by_name.setdefault(r["column_name"], []).append(r["table_name"])
                        _EXPORT_CACHE = {"by_table": by_table, "by_name": by_name}
                except psycopg.Error:
                    # 读不到就**不拦**？不 —— 禁令读不到时应该按"最严"处理是做不到的
                    # （不知道禁哪些列）。但也不能因为读不到就崩掉所有导出。
                    # 选择：缓存为空并**在日志里说清楚**，同时让 refresh 能重试。
                    _EXPORT_CACHE = {"by_table": {}, "by_name": {}, "error": True}
    return _EXPORT_CACHE


def export_filter(cols, table=None):
    """按导出禁令过滤列。返回 (保留的列, 被移除的列)。

    与列级权限的分工：`column_policy` 管"能不能**看**"，本禁令管"能不能**带走**"。
    所以被移除的列**仍然会出现在页面上** —— 那是设计，不是 bug。
    """
    pol = export_policy()
    keep, dropped = [], []
    for c in cols:
        hit = (table, c) in pol["by_table"] if table else False
        if not hit:
            hit = c in pol["by_name"]
        (dropped if hit else keep).append(c)
    return keep, dropped


def csv_safe(v):
    """把可能被 Excel/表格软件当成**公式**的单元格改成纯文本。

    为什么必须做（CSV 公式注入）：CSV 里以 `=` `+` `-` `@` 开头的单元格，
    Excel / LibreOffice / Google Sheets 打开时会**当公式执行**。
    本库导出的是**外部来源的原始文本**（JD 原文、雇主名称、人才自述），
    这些内容不由我们控制 —— 一条 `=HYPERLINK("http://evil","点我")`
    或者更糟的 DDE 形式，会在**打开导出文件的同事机器上**执行。
    这是"把不可信输入交给另一个程序当代码"的经典形态。

    做法：前缀一个单引号（Excel 的"这是文本"约定），而不是删掉内容 ——
    导出是给人看的，不能因为一个字符就把数据改了。
    数字类型不动（负数是正常数值，不构成公式）。
    """
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(cols, rows, table=None) -> bytes:
    """CSV 序列化的**唯一出口** —— 导出禁令与公式注入防护都在这里强制，
    而不是靠各调用点自觉。

    与 `db()`（权限注入）和 `_send`（审计）同一个道理：安全性质挂在**单点**上。
    """
    allowed, dropped = export_filter(cols, table)
    if dropped:
        # 记到线程局部，由 _send 写进响应头（见 _send 的说明）。
        # 不能塞进 CSV 正文：那会破坏表头、让所有按行解析的脚本（含本项目的
        # "CSV 行数 == 页面行数"断言）全部失效。
        _LOCAL.csv_denied = dropped
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(allowed)
    for r in rows:
        w.writerow([csv_safe(r.get(c)) for c in allowed])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM：Excel 直接打开不乱码


# ---------------------------------------------------------------------------
# ① 总览
# ---------------------------------------------------------------------------
def view_home(c, qs) -> bytes:
    m = meta()
    tables = [r for r in m["rels"] if r["kind"] == "table"]
    views = [r for r in m["rels"] if r["kind"] != "table"]
    n_cols = sum(len(v) for v in m["cols"].values())
    n_fk = sum(1 for t in m["cons"] for x in m["cons"][t] if x["kind"] == "f")
    n_chk = sum(1 for t in m["cons"] for x in m["cons"][t] if x["kind"] == "c")
    n_pk = sum(1 for t in m["cons"] for x in m["cons"][t] if x["kind"] == "p")
    n_uni = sum(1 for t in m["cons"] for x in m["cons"][t] if x["kind"] == "u")
    n_trg = sum(len(v) for v in m["trg"].values())
    n_idx = sum(len(v) for v in m["idx"].values())
    dom = q1(c, "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname=%s AND t.typtype='d'", (SCHEMA,))
    n_fun = q1(c, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                  "WHERE n.nspname=%s", (SCHEMA,))

    filled = [r for r in tables if r["rows"] > 0]
    empty = [r for r in tables if r["rows"] == 0]
    total_rows = sum(r["rows"] for r in tables)

    top = sorted(tables, key=lambda r: -r["rows"])[:12]
    mx = top[0]["rows"] if top and top[0]["rows"] else 1
    top_html = "".join(
        '<tr><td><a href="/t/%s">%s</a></td><td>%s</td><td class="n">%s</td>'
        '<td><span class="barwrap"><span class="bar" style="width:%d%%"></span></span></td></tr>'
        % (r["name"], esc(r["name"]), esc(r["domain"]), f"{r['rows']:,}",
           max(2, int(100 * r["rows"] / mx))) for r in top)

    dict_rows = [
        ("代码表 / 代码值", q1(c, "SELECT count(*) FROM code_table"),
         q1(c, "SELECT count(*) FROM code_value"), "/analyze/a5"),
        ("已登记字段", q1(c, "SELECT count(*) FROM field_catalog"),
         q1(c, "SELECT count(*) FROM entity_catalog"), "/analyze/a6"),
        ("概念本体", q1(c, "SELECT count(*) FROM concept"),
         q1(c, "SELECT count(*) FROM concept_relation"), "/analyze/a3"),
        ("职业树节点", q1(c, "SELECT count(*) FROM occupation"),
         q1(c, "SELECT count(*) FROM job_competency_weight WHERE valid_to IS NULL"), "/analyze/a7"),
        ("来源登记 / 血缘", q1(c, "SELECT count(*) FROM source_registry"),
         q1(c, "SELECT count(*) FROM provenance"), "/lineage"),
        ("结构化岗位 / 要求", q1(c, "SELECT count(*) FROM job_posting"),
         q1(c, "SELECT count(*) FROM job_requirement"), "/analyze/a1"),
    ]
    dict_html = "".join(
        '<tr><td><a href="%s">%s</a></td><td class="n">%s</td><td class="n">%s</td></tr>'
        % (h, esc(t), f"{a:,}", f"{b:,}") for t, a, b, h in dict_rows)

    domain_html = "".join(
        '<tr><td><a href="/schema#%s">%s</a></td><td class="n">%d</td><td class="n">%s</td>'
        '<td class="muted">%s</td></tr>'
        % (urllib.parse.quote(d), esc(d),
           len([r for r in tables if r["domain"] == d]),
           f"{sum(r['rows'] for r in tables if r['domain'] == d):,}", esc(desc))
        for d, desc in DOMAINS)

    body = """
<div class="sub">这一页的每个数字都是 <code>count(*)</code> 实时查出来的，不是估算值。
门户本身<b>只读</b>——它不写数据库。</div>
<div class="cards">%s</div>

<div class="card"><h2>按域看这个库</h2>
<table><tr><th>域</th><th class="n">表</th><th class="n">行</th><th>说明</th></tr>%s</table></div>

<div class="card"><h2>行数最多的表</h2><table>
<tr><th>表</th><th>域</th><th class="n">行数</th><th>占比</th></tr>%s</table>
<p class="muted">共 %s 行数据，%d 张表有数据，%d 张表为空（空表 = 结构已建、数据未到）。</p></div>

<div class="grid2">
<div class="card"><h2>数据字典</h2><table>
<tr><th>对象</th><th class="n">主</th><th class="n">从</th></tr>%s</table></div>
<div class="card"><h2>结构复杂度</h2><table>
<tr><td>主键</td><td class="n">%d</td></tr>
<tr><td>外键</td><td class="n">%d</td></tr>
<tr><td>唯一约束</td><td class="n">%d</td></tr>
<tr><td>CHECK 约束</td><td class="n">%d</td></tr>
<tr><td>索引</td><td class="n">%d</td></tr>
<tr><td>触发器</td><td class="n">%d</td></tr>
<tr><td>自定义域</td><td class="n">%d</td></tr>
<tr><td>函数 / 视图</td><td class="n">%d / %d</td></tr>
</table></div></div>

<div class="card"><h2>从哪开始看</h2>
<ol>
<li><a href="/schema">表与视图</a>：全部 %d 张基础表按域分组，点任意一张看它的列、约束和<b>真实数据</b>。</li>
<li>在任意表里点一个外键值 → 进入<a href="/e/occupation?val=OCC-F02-01-01">实体页</a>，
    看这一行记录被哪些表引用（这就是"交叉引用"）。</li>
<li><a href="/analyze">分析</a>：8 个预置分析，每个都显示它执行的 SQL，可导出 CSV。</li>
<li><a href="/sql">SQL</a>：自己写查询。只读事务，写操作会被数据库直接拒绝。</li>
</ol>
<p class="muted">读取全部 %d 个对象的精确行数耗时 %.2f 秒（元数据在进程内缓存）。</p></div>
""" % ("".join([
        metric(f"{len(tables)}", "基础表", "另有 %d 个视图" % len(views)),
        metric(f"{n_cols:,}", "列", "合计所有表"),
        metric(f"{total_rows:,}", "数据行", "合计所有表"),
        metric(f"{n_fk}", "外键", "真实的关系约束"),
        metric(f"{n_idx}", "索引", "%d 个触发器" % n_trg),
        metric(f"{q1(c, 'SELECT count(*) FROM code_value'):,}", "代码值", "受控词表"),
        metric(f"{q1(c, 'SELECT count(*) FROM field_catalog'):,}", "已登记字段", "数据字典"),
        metric(f"{len(filled)}/{len(tables)}", "非空表", "结构已建且已有数据"),
    ]), domain_html, top_html, f"{total_rows:,}", len(filled), len(empty), dict_html,
        n_pk, n_fk, n_uni, n_chk, n_idx, n_trg, dom, n_fun, len(views),
        len(tables), len(m["rels"]), m["count_seconds"])
    return page("总览", body, subtitle="一个只读的数据库门户：信息展示 · 检索 · 基础分析")


# ---------------------------------------------------------------------------
# ② 表与视图清单
# ---------------------------------------------------------------------------
def _profile_map(c):
    """一次读入列级剖析快照（能读就返回，读不到返回空 + 原因）。

    为什么吞掉权限错误而不是让页面 403：目录页的**主体是表清单与数量**
    （那部分是公开的），剖析统计只是它的一列信息。
    让"少一列统计"升级成"整页打不开"，是把可降级的东西做成了硬依赖。
    但**必须把原因带到界面上** —— 否则用户会把"没有统计"读成"这列没数据"。
    """
    rows = try_read(c, """SELECT table_name, column_name, n_rows, n_not_null, n_distinct,
                                 null_frac, is_enum_like, tier, computed_at, elapsed_ms,
                                 min_value, max_value, avg_len, top_values, top_note
                            FROM column_profile""")
    if rows is None:
        return {}, "读取 column_profile 被拒绝（该表含列级 Top-K 取值，只对 T3 开放）"
    return {(r["table_name"], r["column_name"]): r for r in rows}, ""


def _tier_map(c):
    """表 → 各等级列数分布。权限分布要摆在目录里：
    用户必须一眼看出"这张表里有多少字段是受限的"，否则他会以为看到的是全部。"""
    rows = try_read(c, """SELECT table_name,
                                 count(*) FILTER (WHERE min_tier='T0') AS t0,
                                 count(*) FILTER (WHERE min_tier='T1') AS t1,
                                 count(*) FILTER (WHERE min_tier='T2') AS t2,
                                 count(*) FILTER (WHERE min_tier='T3') AS t3,
                                 count(*) FILTER (WHERE min_tier='X')  AS denied,
                                 count(*) AS total
                            FROM column_policy GROUP BY table_name""")
    return {r["table_name"]: r for r in (rows or [])}


def _coverage_map(c):
    rows = try_read(c, "SELECT * FROM v_column_profile_coverage")
    return {r["table_name"]: r for r in (rows or [])}


TIER_PILL = {"T0": "p-t", "T1": "p-n", "T2": "p-v", "T3": "p-x", "X": "p-x"}


def _tier_bar(r) -> str:
    """权限分布：用文字 + 颜色，不只靠颜色（可访问性）。"""
    if not r:
        return '<span class="muted">—</span>'
    parts = []
    for key, name in (("t0", "T0"), ("t1", "T1"), ("t2", "T2"), ("t3", "T3")):
        if r[key]:
            parts.append('<span class="pill %s" title="%s：%d 列">%s %d</span>'
                         % (TIER_PILL[name], name, r[key], name, r[key]))
    if r["denied"]:
        parts.append('<span class="pill p-x">X %d</span>' % r["denied"])
    return " ".join(parts)


def view_catalog(c, qs) -> bytes:
    """数据资产目录。用户原话「最重要的…看到字段名，数量，能够分类统计，能够检索」。

    这一页要同时回答五件事，且**每件都标出它的可靠性**：
      1. 有哪些表、每张表多少行、多少列（行数是精确 count(*)）
      2. 每张表的**权限分布**（多少字段公开、多少受限）
      3. 每张表的**可分类列数**（基数 2–200：能直接 group by 的列）
      4. 哪些列**整列为空**（结构建了但数据从没到过 —— 只给行数会掩盖这件事）
      5. 剖析的**覆盖率与时间**（没算过就说没算过，绝不当成 0）
    """
    m = meta()
    prof, prof_err = _profile_map(c)
    tiers = _tier_map(c)
    cov = _coverage_map(c)

    qtext = (qs.get("q", [""])[0] or "").strip()
    dom_filter = (qs.get("dom", [""])[0] or "").strip()
    tier_filter = (qs.get("tier", [""])[0] or "").strip().upper()
    sort = (qs.get("sort", ["rows"])[0] or "rows")
    dirn = (qs.get("dir", ["desc"])[0] or "desc")

    tables = [r for r in m["rels"] if r["kind"] == "table"]

    # 元数据检索：表名 / 列名 / 表说明 / 字段字典标题
    if qtext:
        needle = qtext.lower()
        hit_tables = set()
        for r in tables:
            if needle in r["name"].lower():
                hit_tables.add(r["name"])
            if needle in (r.get("comment") or "").lower():
                hit_tables.add(r["name"])
            for col in m["cols"].get(r["name"], []):
                if needle in col["column_name"].lower():
                    hit_tables.add(r["name"])
                    break
        # 字段字典（中文标题）—— 这是"用中文找字段"的唯一入口
        try:
            for r in q(c, """SELECT e.table_name, f.title, f.field_id
                               FROM field_catalog f
                               JOIN entity_catalog e ON e.entity_id = f.entity_id
                              WHERE lower(f.title) LIKE %s OR lower(f.field_id) LIKE %s""",
                       ("%" + needle + "%", "%" + needle + "%")):
                hit_tables.add(r["table_name"])
        except psycopg.Error:
            pass
        tables = [r for r in tables if r["name"] in hit_tables]

    if dom_filter:
        tables = [r for r in tables if r["domain"] == dom_filter]
    if tier_filter in ("T0", "T1", "T2", "T3"):
        key = tier_filter.lower()
        tables = [r for r in tables if tiers.get(r["name"], {}).get(key)]

    # 组装一行需要的全部数字
    recs = []
    for r in tables:
        n = r["name"]
        cv = cov.get(n) or {}
        recs.append({
            "name": n, "domain": r["domain"], "rows": r["rows"], "n_cols": r["n_cols"],
            "profiled": cv.get("n_profiled") or 0,
            "enum_like": cv.get("n_enum_like") or 0,
            "all_null": cv.get("n_all_null") or 0,
            "last": cv.get("last_computed"),
            "tiers": tiers.get(n),
            "n_cons": len(m["cons"].get(n, [])), "n_idx": len(m["idx"].get(n, [])),
            "n_fk_out": len(m["fk_out"].get(n, [])) if "fk_out" in m else None,
        })

    keys = {"name": lambda x: x["name"], "rows": lambda x: x["rows"],
            "cols": lambda x: x["n_cols"],
            "profiled": lambda x: x["profiled"] / max(x["n_cols"], 1),
            "enum": lambda x: x["enum_like"], "empty": lambda x: x["all_null"]}
    recs.sort(key=keys.get(sort, keys["rows"]), reverse=(dirn != "asc"))

    def sorthdr(label, key, cls="n"):
        cur = " ↓" if (sort == key and dirn == "desc") else (" ↑" if sort == key else "")
        nd = "asc" if (sort == key and dirn == "desc") else "desc"
        return ('<th class="%s"><a href="%s">%s%s</a></th>'
                % (cls, esc(qs_encode(qs, sort=key, dir=nd)), esc(label), cur))

    rows_html = "".join(
        '<tr><td><a href="/t/%s"><code>%s</code></a></td>'
        '<td>%s</td><td class="n">%s</td><td class="n">%d</td>'
        '<td class="n">%d<small class="muted">/%d</small></td>'
        '<td class="n">%d</td><td class="n">%s</td><td>%s</td>'
        '<td class="muted">%s</td></tr>'
        % (x["name"], esc(x["name"]), esc(x["domain"]),
           f"{x['rows']:,}" if count_visible(x["name"]) else
           '<span class="muted" title="该表的行数属活动信息，不对匿名公开">不公开</span>',
           x["n_cols"],
           x["profiled"], x["n_cols"], x["enum_like"],
           ('<span class="warnpill">%d</span>' % x["all_null"]) if x["all_null"] else "0",
           _tier_bar(x["tiers"]),
           esc(str(x["last"])[:16]) if x["last"] else "未剖析")
        for x in recs)

    head = ('<tr><th>表</th><th>域</th>' + sorthdr("行数", "rows") + sorthdr("列", "cols")
            + sorthdr("已剖析", "profiled") + sorthdr("可分类列", "enum")
            + sorthdr("全空列", "empty") + '<th>权限分布</th><th>剖析时间</th></tr>')

    # KPI 带
    total_cols = sum(x["n_cols"] for x in recs)
    total_prof = sum(x["profiled"] for x in recs)
    total_enum = sum(x["enum_like"] for x in recs)
    total_null = sum(x["all_null"] for x in recs)
    empty_tables = [x["name"] for x in recs if x["rows"] == 0]
    kpi = "".join([
        metric(f"{len(recs):,}", "表", "当前筛选下"),
        metric(f"{sum(x['rows'] for x in recs):,}", "行（精确 count(*)）", "所有列都可统计"),
        metric(f"{total_cols:,}", "列", "字段名即来自这里"),
        metric(f"{total_prof:,} / {total_cols:,}", "已剖析列", "空值率·基数·Top-K"),
        metric(f"{total_enum:,}", "可分类列", "基数 2–200，可直接分组"),
        metric(f"{total_null:,}", "整列为空", "结构建了但数据没到过"),
    ])

    chips = "".join(
        '<a class="chip%s" href="%s">%s</a>'
        % (" on" if d == dom_filter else "", esc(qs_encode(qs, dom=d, page=None)) if d else
           esc(qs_encode(qs, dom=None, page=None)), esc(d or "全部"))
        for d in [""] + [d for d, _ in DOMAINS])

    warn = ('<div class="note warn">本页的剖析统计当前读不到：<code>%s</code>。'
            '「已剖析 / 可分类 / 整列为空」三列因此不可信 —— 显示为 0 是**读不到**，'
            '不是**真的是 0**。要么用 T3 登录，要么运行 <code>python ops\\profile.py</code>。'
            '</div>' % esc(prof_err)) if prof_err else ""

    empty_note = ('<div class="note info">有 <b>%d</b> 张表是 0 行：%s。'
                  '它们仍留在表里（结构已建、数据未到），而不是被隐藏 —— '
                  '隐藏零行表会让人以为"这些领域不存在"。</div>'
                  % (len(empty_tables), esc("、".join(empty_tables[:12])))) if empty_tables else ""

    body = """
<div class="sub">数据资产目录：<b>字段名</b>来自 <code>information_schema</code>，
<b>数量</b>是精确 <code>count(*)</code>／<code>count(列)</code>／<code>count(DISTINCT 列)</code> 三个不同含义，
<b>分类统计</b>的前提是「这列基数够小」（可分类列 = 基数 2–200）。
剖析是<b>快照</b>，不是实时值，每张表都标出了计算时间。</div>
%s%s%s
<div class="cards">%s</div>
<div class="card"><h2>检索与筛选</h2>
<form method="get" action="/catalog"><div class="row">
  <div style="flex:3 1 320px"><label>按表名 / 列名 / 中文标题检索</label>
    <input name="q" value="%s" placeholder="例如：学历、degree、健康"></div>
  <div style="flex:0 0 120px"><button type="submit">检索</button></div>
  <div style="flex:0 0 120px"><a class="btnlink" href="/catalog">清空</a></div>
</div></form>
<div class="chips">%s</div>
<p class="muted">这是<b>元数据检索</b>（表名、列名、字段字典中文标题），不是值检索 ——
两者的代价差几个数量级。<a href="/search">按值检索在这里</a>。</p></div>
<div class="card"><h2>表清单 <span class="muted">· 点表名进入结构 + 真实数据</span></h2>
<div class="tablewin"><table>%s<tbody>%s</tbody></table></div>
<p class="muted">「已剖析」分母是该表的列数；未剖析的表显示"未剖析"而不是 0。
「可分类列」= 去重值个数在 2–200 之间的列，它们可以直接 <code>GROUP BY</code>；
基数为 1 的常量列没有分组意义，基数过大的自由文本需要先区间化或码表化。</p></div>
""" % (warn, empty_note, "", kpi, esc(qtext), chips, head, rows_html)
    body += page_analysis(c, "/catalog")
    return page("数据目录", body, subtitle="字段名 · 数量 · 分类统计 · 检索",
                session=current_session())


def count_visibility():
    """哪些表的**行数对匿名公开**。判据唯一来源：数据库的 `mt.v_count_visibility`
    （由迁移 048 的 `public_count_excluded` 驱动）。

    **为什么必须有这一个函数**（独立审查 P1-1）：041 给聚合闸门 `public_counts()`
    加了排除，但公开的 `/schema` 页用 `meta()` 照样显示所有表的精确行数 ——
    同一个问题**两套实现、两个答案**，而且两边看起来都对，最难发现。
    现在闸门与页面**都读这一份判据**：一个口径一份实现。
    """
    global _COUNT_VIS_CACHE
    if _COUNT_VIS_CACHE is None:
        with _EXPORT_LOCK:
            if _COUNT_VIS_CACHE is None:
                try:
                    with admin_db() as c:
                        _COUNT_VIS_CACHE = {r["table_name"]: r["count_is_public"]
                                            for r in q(c, "SELECT * FROM v_count_visibility")}
                except psycopg.Error:
                    # 读不到判据时**按保守处理**：不显示行数（宁可少显示，不要多显示）
                    _COUNT_VIS_CACHE = {}
    return _COUNT_VIS_CACHE


def count_visible(table: str) -> bool:
    """该表的行数是否可以展示给**当前会话**。

    公开口径（048）：数据表行数公开（需求原文"数量可以公开"）；
    身份/会话/登录/审计表属**活动信息**，只对 T2（员工）及以上显示。
    """
    vis = count_visibility()
    public = vis.get(table, True)          # 判据里没有的表按公开（数据表默认公开）
    if public:
        return True
    return _tier_rank(current_session().tier) >= _tier_rank("T2")


def view_field(c, table, col, qs) -> bytes:
    """字段剖析页：一列的全部可核事实。

    为什么值得单独一页：用户"分类统计"时撞的墙几乎都在这四件事上 ——
    这列有多少值、有没有空、能不能分组、最快的取值是什么。
    把它们摊在一页上，他就不用猜。
    """
    table = require_table(table)
    if not require_column(table, col):
        raise PortalError(404, "表 %s 没有列 %s" % (table, col))
    m = meta()
    rows = q(c, """SELECT * FROM column_profile
                    WHERE table_name=%s AND column_name=%s""", (table, col)) if \
        _can_read_profile(c) else []
    if not rows:
        # 可能是 T0–T2（读不到整表剖析），退回"公开列统计"视图
        rows = try_read(c, """SELECT * FROM v_column_profile_public
                               WHERE table_name=%s AND column_name=%s""", (table, col)) or []
    p = rows[0] if rows else None
    if not p:
        # 两种"没有"必须分开：**这列真的没剖析过** vs **剖析存在但你的等级看不到**。
        # 混为一谈会让用户以为"这列没数据"——本项目一直在防的误读，
        # 而第一版这里就写错了：T0 打开 T1 列（person.subject_code）得到的是
        # "还没有剖析结果"，其实剖析结果好好地躺在表里，只是对他不可见。
        pol = q(c, """SELECT min_tier, source, note FROM column_policy
                       WHERE table_name=%s AND column_name=%s""", (table, col))
        need = pol[0]["min_tier"] if pol else "T1"
        if _tier_rank(need) > _tier_rank(current_session().tier):
            # 抛 NeedsTier 而不是 return：让 do_GET 用 **403** 发出（见该异常类的说明）
            raise NeedsTier(
                view_no_permission(c, qs, current_session(), need=need,
                                   target="%s.%s 的列级统计" % (table, col),
                                   db_error="该列的访问等级 %s 高于当前会话等级 %s"
                                            % (need, current_session().tier)),
                need)
        raise PortalError(404, "列 %s.%s 还没有剖析结果（该列确实没算过，不是权限问题）。"
                               "在命令行跑：python ops\\profile.py --tables %s"
                          % (table, col, table))

    policy = q(c, """SELECT min_tier, source, note FROM column_policy
                      WHERE table_name=%s AND column_name=%s""", (table, col))
    tier = policy[0]["min_tier"] if policy else "?"
    fld = q(c, """SELECT f.field_id, f.title, f.description, f.value_type, f.code_table_id,
                         f.is_required, f.is_private
                    FROM field_catalog f JOIN entity_catalog e ON e.entity_id=f.entity_id
                   WHERE e.table_name=%s AND lower(regexp_replace(f.field_id,
                         '^F_[A-Za-z0-9]+_','')) = lower(%s)""", (table, col))

    # Top-K：把码翻译成中文标签（本库纪律是存码不存标签，直接显示码对外行没有意义）
    top_html = '<p class="muted">（无 Top-K：%s）</p>' % esc(p.get("top_note") or "未计算")
    if p.get("top_values"):
        tv = p["top_values"]
        if isinstance(tv, str):
            tv = json.loads(tv)
        labels = {}
        if fld and fld[0].get("code_table_id"):
            try:
                labels = {r["code"]: r["label"] for r in q(
                    c, "SELECT code, label FROM code_value WHERE code_table_id=%s",
                    (fld[0]["code_table_id"],))}
            except psycopg.Error:
                labels = {}
        total = p["n_not_null"] or 1
        cum = 0
        trs = []
        for i, t in enumerate(tv, 1):
            cum += t["n"]
            trs.append('<tr><td class="n">%d</td><td><code>%s</code></td><td>%s</td>'
                       '<td class="n">%s</td><td class="n">%.1f%%</td><td class="n">%.1f%%</td></tr>'
                       % (i, esc(str(t["v"])), esc(labels.get(str(t["v"]), "—")),
                          f"{t['n']:,}", 100.0 * t["n"] / total, 100.0 * cum / total))
        top_html = ('<table><thead><tr><th class="n">#</th><th>值</th><th>中文标签</th>'
                    '<th class="n">计数</th><th class="n">占比</th><th class="n">累计</th></tr>'
                    '</thead><tbody>%s</tbody></table>'
                    '<p class="muted">%s。中文标签来自该列对应的码表 —— '
                    '本库纪律是「存码不存标签」，不翻译的话外行看到的是 <code>T1</code> '
                    '这种码而不是"注册用户"。</p>' % ("".join(trs), esc(p["top_note"] or "")))

    nullpct = float(p["null_frac"] or 0) * 100
    body = """
<div class="sub"><code>%s.%s</code> · %s · 访问等级 <span class="pill %s">%s</span>
%s</div>
%s
<div class="cards">
%s%s%s%s%s%s
</div>
<div class="card"><h2>Top-K 取值</h2>%s</div>
<div class="card"><h2>这一列的登记信息</h2>%s</div>
<div class="card"><h2>本次剖析怎么算的（可核）</h2>
<pre class="sqlbox">SELECT count(*) AS 行数,
       count(%s) AS 非空数,
       count(DISTINCT %s) AS 去重值个数
  FROM mt.%s;</pre>
<p class="muted">剖析时间是 <b>%s</b>，本列耗时 <b>%s ms</b>。
这是<b>快照</b>：数据变了它不会自动更新，需要重跑 <code>python ops\\profile.py --tables %s</code>。</p></div>
""" % (
        esc(table), esc(col), esc(p.get("data_type") or ""), TIER_PILL.get(tier, "p-n"),
        esc(tier), esc(("· 字典：" + fld[0]["title"]) if fld else "· 该列未登记字段字典"),
        '必要提示' if True else "",
        metric(f"{p['n_rows']:,}" if p.get("n_rows") is not None else "—", "行数", "表的总行数"),
        metric(f"{p['n_not_null']:,}" if p.get("n_not_null") is not None else "—",
               "非空数", "count(列)"),
        metric(f"{p['n_distinct']:,}" if p.get("n_distinct") is not None else "未算",
               "去重值个数", "能否分组的关键"),
        metric("%.1f%%" % nullpct, "空值率", "精确值，不是采样"),
        metric("是" if p.get("is_enum_like") else "否", "可分类",
               "基数 2–200 才可直接 GROUP BY"),
        metric(esc(str(p.get("min_value"))[:18] if p.get("min_value") is not None else "—"),
               "最小值 / 最大值",
               esc(str(p.get("max_value"))[:18] if p.get("max_value") is not None else "—")),
        top_html,
        ('<table><tbody>'
         '<tr><th>访问等级</th><td><span class="pill %s">%s</span> —— %s</td></tr>'
         '<tr><th>等级依据</th><td><code>%s</code>：%s</td></tr>%s</tbody></table>'
         % (TIER_PILL.get(tier, "p-n"), esc(tier), esc((policy[0]["note"] if policy else "")),
            esc(policy[0]["source"] if policy else "?"),
            esc((policy[0]["note"] if policy else "")),
            "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in
                    (("字段 ID", fld[0]["field_id"]), ("中文标题", fld[0]["title"]),
                     ("值类型", fld[0]["value_type"]), ("码表", fld[0]["code_table_id"]),
                     ("是否必填", "是" if fld[0]["is_required"] else "否"),
                     ("是否私有", "是" if fld[0]["is_private"] else "否")) if v)
            if fld else "<tr><th>字段字典</th><td>未登记</td></tr>")
         if True else ""),
        esc(col), esc(col), esc(table), esc(str(p.get("computed_at"))[:19]),
        f"{p.get('elapsed_ms') or 0:,}", esc(table))
    return page("字段 %s.%s" % (table, col), body,
                subtitle="一列的全部可核事实", here="数据目录",
                crumbs=[("/catalog", "数据目录"), ("/t/" + table, table)],
                session=current_session())


def _can_read_profile(c) -> bool:
    return try_read(c, "SELECT 1 FROM column_profile LIMIT 1") is not None


def view_schema(c, qs) -> bytes:
    m = meta()
    groups = []
    for dom, desc in DOMAINS:
        rs = [r for r in m["rels"] if r["kind"] == "table" and r["domain"] == dom]
        if rs:
            groups.append((dom, desc, rs))
    unclassified = [r for r in m["rels"] if r["kind"] == "table" and r["domain"] == "未归类"]
    if unclassified:
        groups.append(("未归类", "未在 DOMAIN_TABLES 中登记（应在 portal.py 里补齐）",
                       unclassified))
    views = [r for r in m["rels"] if r["kind"] != "table"]

    def count_cell(name, n):
        """行数单元格：与 `/catalog`、聚合闸门**同一判据**（迁移 048）。

        为什么这里也要改：041 只给 `public_counts()` 加了排除，而 `/schema`
        用 `meta()` 照样把 `change_log` 的 871,163 行显示给匿名 ——
        于是同一个问题有两套实现、两个答案（`/catalog` 藏了、`/schema` 没藏）。
        """
        if count_visible(name):
            return f"{n:,}"
        return '<span class="muted" title="该表的行数属活动信息，不对匿名公开">不公开</span>'

    def table_block(rs, show_domain=False):
        return "".join(
            '<tr><td><a href="/t/%s"><code>%s</code></a></td><td class="n">%d</td>'
            '<td class="n">%s</td><td class="n">%d</td><td class="n">%d</td><td class="n">%d</td></tr>'
            % (r["name"], esc(r["name"]), r["n_cols"], count_cell(r["name"], r["rows"]),
               len(m["cons"].get(r["name"], [])), len(m["idx"].get(r["name"], [])),
               len(m["trg"].get(r["name"], [])))
            for r in rs)

    head = ('<tr><th>表</th><th class="n">列</th><th class="n">行数</th>'
            '<th class="n">约束</th><th class="n">索引</th><th class="n">触发器</th></tr>')
    blocks = "".join(
        '<div class="card" id="%s"><h2>%s <span class="muted">· %d 张表 · %s 行</span></h2>'
        '<div class="sub">%s</div><table>%s%s</table></div>'
        % (urllib.parse.quote(d), esc(d), len(rs),
           f"{sum(r['rows'] for r in rs if count_visible(r['name'])):,}"
           + ("（部分表的行数不公开）" if any(not count_visible(r["name"]) for r in rs) else ""),
           esc(desc), head, table_block(rs))
        for d, desc, rs in groups)

    view_html = ('<div class="card"><h2>视图 <span class="muted">· %d 个 · 预置口径，不可写</span></h2>'
                 '<table>%s%s</table></div>' % (len(views), head, table_block(views)))

    body = ('<div class="sub">%d 张基础表 + %d 个视图，行数为 <code>count(*)</code> 精确值。'
            '点表名进入结构 + 真实数据。</div>%s%s'
            % (len([r for r in m["rels"] if r["kind"] == "table"]), len(views), blocks, view_html))
    body += page_analysis(c, "/schema")
    return page("表与视图", body)


# ---------------------------------------------------------------------------
# ③ 表详情：结构 + 真实数据
# ---------------------------------------------------------------------------
def view_table(c, name, qs) -> bytes:
    name = require_table(name)
    m = meta()
    rel = m["by_name"][name]
    cols = m["cols"].get(name, [])
    colnames = [x["column_name"] for x in cols]

    # --- 浏览参数（全部与系统目录比对后再用） ---
    page_no = _int_param(qs, "page", 1, lo=1)
    size = _int_param(qs, "size", PAGE_SIZE, lo=5, hi=500)
    sort = qs.get("sort", [""])[0]
    direction = "DESC" if qs.get("dir", ["asc"])[0] == "desc" else "ASC"
    fcol = qs.get("col", [""])[0]
    fval = qs.get("val", [""])[0]

    where, params = browse_where(name, fcol, fval)

    order = sql.SQL("")
    if sort:
        require_column(name, sort)
        order = sql.SQL(" ORDER BY {} {}").format(sql.Identifier(sort),
                                                  sql.SQL(direction))
    elif name in m["pk"]:
        order = sql.SQL(" ORDER BY {} ASC").format(sql.Identifier(m["pk"][name]))

    total = q1(c, sql.SQL("SELECT count(*) AS n FROM {}.{}{}").format(
        sql.Identifier(SCHEMA), sql.Identifier(name), where), params)
    select = sql.SQL("SELECT * FROM {}.{}{}{} LIMIT %s OFFSET %s").format(
        sql.Identifier(SCHEMA), sql.Identifier(name), where, order)
    t0 = time.time()
    rows = q(c, select, params + [size, (page_no - 1) * size])
    ms = int((time.time() - t0) * 1000)
    try:
        shown_sql = select.as_string(c)
    except Exception:                                     # noqa: BLE001
        shown_sql = "SELECT * FROM %s.%s  -- 参数：%r" % (SCHEMA, name, params)

    # --- 列定义 ---
    col_rows = "".join(
        '<tr><td><code>%s</code></td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>'
        % (esc(x["column_name"]), esc(x["data_type"]),
           "否" if x["not_null"] else '<span class="muted">可空</span>',
           '<code>%s</code>' % esc(trunc(x["default_expr"], 40)) if x["default_expr"] else "",
           esc(x["comment"] or ""))
        for x in cols)

    # --- 约束（外键链接到目标表） ---
    ckind = {"p": "主键", "f": "外键", "u": "唯一", "c": "CHECK", "x": "排他"}
    cons_rows = "".join(
        '<tr><td><span class="pill %s">%s</span></td><td><code>%s</code></td><td>%s</td><td>%s</td></tr>'
        % ("p-v" if x["kind"] in ("p", "u") else ("p-e" if x["kind"] == "f" else "p-n"),
           esc(ckind.get(x["kind"], x["kind"])), esc(x["name"]), esc(x["columns"] or ""),
           constraint_def_html(x))
        for x in m["cons"].get(name, []))

    idx_rows = "".join('<tr><td><code>%s</code></td><td><code>%s</code></td></tr>'
                       % (esc(x["name"]), esc(x["definition"])) for x in m["idx"].get(name, []))
    trg_rows = "".join('<tr><td><code>%s</code></td><td><code>%s</code></td></tr>'
                       % (esc(x["name"]), esc(x["definition"])) for x in m["trg"].get(name, []))

    # --- 所有指向本表的外键（交叉引用反查） ---
    inb = m["inbound"].get(name, [])
    inb_html = "".join(
        '<tr><td><a href="/t/%s"><code>%s</code></a></td><td><code>%s</code></td>'
        '<td class="n">%s</td><td class="muted">%s</td></tr>'
        % (b["child"], esc(b["child"]), esc(b["child_cols"]),
           f"{q1(c, sql.SQL('SELECT count(*) AS n FROM {}.{}').format(sql.Identifier(SCHEMA), sql.Identifier(b['child']))):,}",
           esc(b["constraint"])) for b in inb) or '<tr><td colspan="4" class="muted">没有表引用它</td></tr>'

    # --- 分页与排序链接 ---
    n_pages = max(1, (total + size - 1) // size)

    def link(**kw):
        p = dict(qs)
        for k, v in kw.items():
            p[k] = [str(v)]
        return "/t/%s?%s" % (name, urllib.parse.urlencode(
            {k: v[0] for k, v in p.items() if v and v[0]}))

    pager = pager_links("/t/%s" % name, qs, page_no, n_pages,
                        single_label='<span class="muted">共 1 页</span>')

    sort_links = " · ".join(
        '<a href="%s">按 %s %s</a>' % (link(sort=x["column_name"],
                                            dir="desc" if (sort == x["column_name"]
                                                           and direction == "ASC") else "asc"),
                                       esc(x["column_name"]),
                                       "↓" if (sort == x["column_name"] and direction == "DESC")
                                       else "↑") for x in cols[:12])

    csv_href = "/t/%s.csv?%s" % (name, urllib.parse.urlencode(
        {k: v[0] for k, v in qs.items() if v and v[0]}))
    filt = ('<div class="note info">已筛选 <code>%s = %s</code>，'
            '<a href="/t/%s">清除</a></div>' % (esc(fcol), esc(fval), name)) if fcol and fval else ""

    body = """
%s
<div class="cards">%s</div>
%s

<div class="card"><h2>数据 <span class="muted">· 第 %d / %d 页，本页 %d 行，%d ms</span>
<span style="float:right"><a href="%s">下载 CSV</a></span></h2>
%s
<div class="pager" style="margin-top:10px">%s</div>
<div class="muted" style="margin-top:6px">排序：%s</div>
<p class="muted">灰色斜体 <span class="nul">NULL</span> 表示该字段为空——这与"值为 0"或"值为空串"是
不同的事实，门户不做任何填充。蓝色带下划线的值是外键，点进去看被引用的那行记录。</p></div>

<div class="grid2">
<div class="card"><h2>列定义（%d）</h2><div class="tscroll"><table>
<tr><th>列</th><th>类型</th><th>非空</th><th>默认值</th><th>注释</th></tr>%s</table></div></div>
<div class="card"><h2>被谁引用（交叉引用）</h2><table>
<tr><th>子表</th><th>列</th><th class="n">行数</th><th>约束</th></tr>%s</table>
<p class="muted">这些表通过外键指向 <code>%s</code>。删除一行之前，必须先看这里。</p></div>
</div>

<div class="card"><h2>约束（%d）</h2><table>
<tr><th>类型</th><th>名称</th><th>列</th><th>定义</th></tr>%s</table></div>
<div class="grid2">
<div class="card"><h2>索引（%d）</h2><table><tr><th>名称</th><th>定义</th></tr>%s</table></div>
<div class="card"><h2>触发器（%d）</h2><table><tr><th>名称</th><th>定义</th></tr>%s</table>
<p class="muted">触发器是这个库"不变量"的执行者：<code>updated_at</code> 自动维护、
append-only 变更流水、扩展属性门禁都挂在触发器上。</p></div>
</div>

%s
""" % (filt,
       "".join([metric(f"{total:,}", "行数", "精确 count(*)"),
                metric(len(cols), "列"),
                metric(len(m["cons"].get(name, [])), "约束"),
                metric(len(m["idx"].get(name, [])), "索引"),
                metric(len(m["trg"].get(name, [])), "触发器"),
                metric(len(inb), "被引用", "有外键指向它")]),
       '<div class="note info">%s · <b>%s</b>%s</div>' % (
           esc(rel["kind"] == "table" and "基础表" or "视图"), esc(name),
           "（%s）" % esc(rel["domain"]) if rel["kind"] == "table" else "（只读视图）"),
       page_no, n_pages, len(rows), ms, csv_href,
       render_rows(name, colnames, rows,
                   n_right=tuple(x["column_name"] for x in cols if is_numeric(x["data_type"]))),
       pager, sort_links,
       len(cols), col_rows, inb_html, esc(name),
       len(m["cons"].get(name, [])), cons_rows,
       len(m["idx"].get(name, [])), idx_rows,
       len(m["trg"].get(name, [])), trg_rows,
       sql_box(shown_sql, "本页执行的 SQL（表名与列名均已与系统目录比对后转义）"))
    # 分析按钮放在**页面顶部**（像工具栏），不是页面底部：
    # 表页面很长，滚到底才看见按钮等于没有。分析是"对这一页的动作"。
    body = analysis_panel(table_profile_blocks(c, name), "分析这张表",
                          "分析的对象就是这张表本身，所以它长在这一页上，"
                          "而不是要你先跳到另一个页面再回想刚才看的是哪张表。") + body
    return page(name, body, subtitle="表结构 + 真实数据", here="表与视图",
                crumbs=[("/schema", "表与视图")])


# ---------------------------------------------------------------------------
# ④ 实体页：一行记录 + 所有指向它的表
# ---------------------------------------------------------------------------
def view_entity(c, name, qs) -> bytes:
    name = require_table(name)
    m = meta()
    val = qs.get("val", [""])[0]
    pk = m["pk"].get(name)
    if not pk:
        raise PortalError(400, "%s 没有主键，无法定位单行记录" % name)
    if not val:
        raise PortalError(400, "缺少 val 参数（%s 的 %s）" % (name, pk))

    rows = q(c, sql.SQL("SELECT * FROM {}.{} WHERE {}::text = %s LIMIT 1").format(
        sql.Identifier(SCHEMA), sql.Identifier(name), sql.Identifier(pk)), (val,))
    if not rows:
        raise PortalError(404, "%s 中不存在 %s = %s" % (name, pk, val))
    row = rows[0]

    kv = "".join('<div>%s</div><div>%s</div>' % (esc(k), cell_html(name, k, v, 400))
                 for k, v in row.items())

    # 反查：每张指向本表的子表，实际有多少行指向这一行
    blocks = []
    for b in m["inbound"].get(name, []):
        child, ccol = b["child"], (b["child_cols"] or "").split(", ")[0]
        if not ccol:
            continue
        try:
            n = q1(c, sql.SQL("SELECT count(*) AS n FROM {}.{} WHERE {}::text = %s").format(
                sql.Identifier(SCHEMA), sql.Identifier(child), sql.Identifier(ccol)), (val,))
        except psycopg.Error:
            n = None
        if not n:
            continue
        sample = q(c, sql.SQL("SELECT * FROM {}.{} WHERE {}::text = %s LIMIT 5").format(
            sql.Identifier(SCHEMA), sql.Identifier(child), sql.Identifier(ccol)), (val,))
        ccols = [x["column_name"] for x in m["cols"].get(child, [])][:9]
        blocks.append(
            '<div class="card"><h2><a href="/t/%s"><code>%s</code></a> '
            '<span class="muted">· %s 行指向这条记录（via %s）</span>'
            '<span style="float:right"><a href="/t/%s?col=%s&val=%s">全部 →</a></span></h2>%s</div>'
            % (child, esc(child), f"{n:,}", esc(b["constraint"]), child, esc(ccol),
               urllib.parse.quote(val), render_rows(child, ccols, sample, maxlen=48)))

    title = "%s = %s" % (name, val)
    body = ('<div class="sub">一行记录的完整字段，以及<b>所有指向它</b>的表。'
            '这是基因组学数据库那种"每个实体一个稳定 ID 页面"的形态：'
            '任何数字都能顺着外键走回去看原始记录。</div>'
            '<div class="card"><h2>字段</h2><div class="kv">%s</div></div>%s%s'
            % (kv, "".join(blocks) or '<div class="card"><div class="empty">'
                       '没有任何行通过外键指向这条记录。</div></div>',
               sql_box("SELECT * FROM %s.%s WHERE %s = '%s'" % (SCHEMA, name, pk, val))))
    body += page_analysis(c, "/entity", (name, val))
    return page(title, body, subtitle="实体页 · %s" % esc(name), here="表与视图",
                crumbs=[("/schema", "表与视图"), ("/t/%s" % name, name)])


# ---------------------------------------------------------------------------
# ⑤ 检索
# ---------------------------------------------------------------------------
SEARCH_SOURCES = [
    ("职业树", "occupation", "label_zh", "occupation_id", "label_zh",
     "SELECT occupation_id AS id, label_zh AS 名称, family AS 岗位族, level AS 层级, "
     "status AS 状态 FROM occupation WHERE label_zh ILIKE %s OR coalesce(label_en,'') ILIKE %s "
     "OR coalesce(description,'') ILIKE %s ORDER BY level, occupation_id LIMIT 40"),
    ("能力概念（含别名）", "concept", "preferred_label", "concept_id", "preferred_label",
     "SELECT concept_id AS id, preferred_label AS 能力, concept_type AS 类型, "
     "array_to_string(alt_labels, '｜') AS 别名 FROM concept "
     "WHERE preferred_label ILIKE %s OR array_to_string(alt_labels,'|') ILIKE %s "
     "OR coalesce(definition,'') ILIKE %s ORDER BY concept_id LIMIT 40"),
    ("岗位（标题）", "job_posting", "title_raw", "job_id", "title_raw",
     "SELECT job_id AS id, title_raw AS 标题, employer_name_raw AS 雇主, city AS 城市, "
     "job_family AS 岗位族, occupation_id AS 职业 FROM job_posting "
     "WHERE title_raw ILIKE %s OR coalesce(employer_name_raw,'') ILIKE %s OR city ILIKE %s "
     "ORDER BY job_id LIMIT 40"),
    ("岗位要求（原文）", "job_requirement", "raw_text", "requirement_id", "raw_text",
     "SELECT requirement_id AS id, job_id AS 岗位, left(raw_text, 90) AS 要求原文, "
     "requirement_kind AS 硬性, concept_id AS 概念 FROM job_requirement "
     "WHERE raw_text ILIKE %s OR coalesce(concept_id,'') ILIKE %s OR '' ILIKE %s "
     "ORDER BY requirement_id LIMIT 40"),
    ("代码值（受控词表）", "code_value", "label_zh", "code", "label_zh",
     "SELECT code AS id, code_table_id AS 词表, code AS 码, label_zh AS 名称, "
     "array_to_string(aliases,'｜') AS 别名 FROM code_value "
     "WHERE label_zh ILIKE %s OR code ILIKE %s OR array_to_string(aliases,'|') ILIKE %s "
     "ORDER BY code_table_id, sort_order LIMIT 40"),
    ("字段目录", "field_catalog", "title", "field_id", "title",
     "SELECT field_id AS id, title AS 字段, entity_id AS 实体, data_type AS 类型, "
     "cardinality AS 基数 FROM field_catalog "
     "WHERE title ILIKE %s OR field_id ILIKE %s OR coalesce(description,'') ILIKE %s "
     "ORDER BY entity_id, field_id LIMIT 40"),
    ("来源登记", "source_registry", "name", "source_id", "name",
     "SELECT source_id AS id, name AS 来源, source_type AS 类型, evidence_grade AS 证据级, "
     "status AS 状态 FROM source_registry "
     "WHERE name ILIKE %s OR source_id ILIKE %s OR coalesce(base_url,'') ILIKE %s LIMIT 40"),
]


def view_search(c, qs) -> bytes:
    term = (qs.get("q", [""])[0] or "").strip()
    m = meta()
    if not term:
        body = ('<div class="card"><form method="get" action="/search" class="row">'
                '<div><label>检索词（支持 <code>%%</code> 通配；大小写不敏感）</label>'
                '<input name="q" placeholder="例如：医学科学联络官 / 临床 / MSL / 法规 / OCC-F02"></div>'
                '<div style="flex:0 0 110px"><button type="submit">检索</button></div></form></div>'
                '<div class="card"><h2>能检索什么</h2><table>'
                '<tr><th>数据源</th><th>表</th><th>可搜字段</th></tr>%s</table>'
                '<p class="muted">每一条命中都会给出它<b>来自哪张表、哪个 ID</b>，'
                '然后可以点进实体页看完整记录。</p></div>'
                % "".join('<tr><td>%s</td><td><a href="/t/%s"><code>%s</code></a></td>'
                          '<td><code>%s</code></td></tr>' % (esc(t), tb, esc(tb), esc(f))
                          for t, tb, f, _, _, _ in SEARCH_SOURCES))
        return page("检索", body, subtitle="跨 7 个数据源的关键词检索")

    like = "%" + term + "%"
    # 表名 / 列名的元数据检索：搜"哪张表里有这个列"
    t_hits = [r for r in m["rels"] if term.lower() in r["name"].lower()]
    c_hits = [(t, x["column_name"], x["data_type"])
              for t, xs in m["cols"].items() for x in xs
              if term.lower() in x["column_name"].lower()][:60]

    blocks = []
    total = 0
    for title, table, field, pk, disp, sqltext in SEARCH_SOURCES:
        try:
            rows = q(c, sqltext, (like, like, like))
        except psycopg.Error as e:
            blocks.append('<div class="card"><h2>%s</h2><div class="note err">%s</div></div>'
                          % (esc(title), esc(str(e).splitlines()[0])))
            continue
        if not rows:
            continue
        total += len(rows)
        cols = list(rows[0].keys())
        html_rows = []
        for r in rows:
            rid = r["id"]
            cells = []
            for cc in cols:
                if cc == "id":
                    cells.append('<td><a href="/e/%s?val=%s"><code>%s</code></a></td>'
                                 % (table, urllib.parse.quote(str(rid)), esc(trunc(rid, 40))))
                elif cc == "岗位":
                    cells.append('<td><a href="/e/job_posting?val=%s"><code>%s</code></a></td>'
                                 % (urllib.parse.quote(str(r[cc])), esc(r[cc])))
                else:
                    cells.append('<td>%s</td>' % esc(trunc(r[cc], 90)))
            html_rows.append("<tr>%s</tr>" % "".join(cells))
        blocks.append(
            '<div class="card"><h2>%s <span class="muted">· %d 条命中 · 表 '
            '<a href="/t/%s"><code>%s</code></a></span></h2><div class="tscroll"><table>'
            '<tr>%s</tr>%s</table></div></div>'
            % (esc(title), len(rows), table, esc(table),
               "".join("<th>%s</th>" % esc(x) for x in cols), "".join(html_rows)))

    meta_block = ""
    if t_hits or c_hits:
        meta_block = ('<div class="card"><h2>元数据命中 '
                      '<span class="muted">· 表名 %d / 列名 %d</span></h2>%s%s</div>'
                      % (len(t_hits), len(c_hits),
                         "".join('<div class="note info">表：<a href="/t/%s"><code>%s</code></a>'
                                 '（%s）</div>' % (r["name"], esc(r["name"]), esc(r["domain"]))
                                 for r in t_hits[:20]),
                         "".join('<div class="note info"><a href="/t/%s"><code>%s</code></a>'
                                 '.<code>%s</code> <span class="muted">%s</span></div>'
                                 % (t, esc(t), esc(cn), esc(dt))
                                 for t, cn, dt in c_hits[:20])))

    head = ('<div class="card"><form method="get" action="/search" class="row">'
            '<div><label>检索词</label><input name="q" value="%s"></div>'
            '<div style="flex:0 0 110px"><button type="submit">检索</button></div></form></div>'
            % esc(term))
    if not blocks and not meta_block:
        return page("检索", head + '<div class="note err">没有命中：<b>%s</b>。'
                    '换个词，或到<a href="/schema">表与视图</a>里逐张翻。</div>' % esc(term),
                    subtitle="检索：%s" % esc(term))
    return page("检索", head + meta_block + "".join(blocks),
                msg="命中 %d 条业务记录（按数据源分组）" % total, kind="info",
                subtitle="检索：%s" % esc(term))


# ---------------------------------------------------------------------------
# ⑥ 基础分析
# ---------------------------------------------------------------------------
ANALYSES = [
    ("a1", "岗位族总览", "17 个岗位族各有多少 JD、覆盖多少职业节点、薪资中位区间",
     """
     SELECT jp.job_family AS "岗位族",
            count(*) AS "JD 数",
            count(DISTINCT jp.occupation_id) AS "覆盖职业",
            count(DISTINCT jp.employer_name_raw) AS "雇主数",
            min(jp.salary_min) AS "薪资下限",
            max(jp.salary_max) AS "薪资上限"
       FROM job_posting jp
      WHERE jp.verify_status <> 'V4'
      GROUP BY 1 ORDER BY 2 DESC
     """),
    ("a2", "能力需求 Top-N", "全库最被要求的 25 项能力，含必需率与样本量",
     """
     SELECT c.preferred_label AS "能力",
            c.concept_type AS "类型",
            count(DISTINCT w.occupation_id) AS "涉及职业数",
            count(*) AS "权重行数",
            round(avg(w.importance), 3) AS "平均重要性",
            sum(CASE WHEN w.essentiality = 'essential' THEN 1 ELSE 0 END) AS "必需",
            max(w.sample_size) AS "最大样本"
       FROM job_competency_weight w
       JOIN concept c ON c.concept_id = w.concept_id
      WHERE w.valid_to IS NULL
      GROUP BY 1, 2 ORDER BY 4 DESC, 5 DESC LIMIT 25
     """),
    ("a3", "概念映射覆盖率", "要求文本 → 能力概念的映射率，并显式剔除资格门槛类要求",
     """
     SELECT count(*) AS "要求总数",
            count(*) FILTER (WHERE requirement_type = ANY(ARRAY['RT5','RT6','RT7','RT8']))
              AS "计入分母",
            count(*) FILTER (WHERE requirement_type = ANY(ARRAY['RT5','RT6','RT7','RT8'])
                               AND concept_id IS NOT NULL) AS "计入分子",
            count(*) FILTER (WHERE requirement_type = ANY(ARRAY['RT1','RT3','RT4']))
              AS "资格门槛类（不计入）",
            round(100.0 * count(*) FILTER (WHERE requirement_type = ANY(ARRAY['RT5','RT6','RT7','RT8'])
                                             AND concept_id IS NOT NULL)
                  / nullif(count(*) FILTER (WHERE requirement_type = ANY(ARRAY['RT5','RT6','RT7','RT8'])), 0), 1)
              AS "覆盖率%"
       FROM job_requirement
      -- 口径单一定义在 code/metrics.py::CONCEPT_COVERAGE_SQL，此处与之保持一致
     """),
    ("a4", "能力漂移（最近两版）", "最近两次重算之间，哪些能力需求上升、下降、新出现或消失",
     """
     WITH r AS (SELECT run_id, row_number() OVER (ORDER BY created_at DESC) AS rn
                  FROM competency_run)
     SELECT d.drift_type AS "漂移类型",
            o.label_zh AS "职业",
            c.preferred_label AS "能力",
            d.from_importance AS "上版重要性",
            d.to_importance AS "本版重要性",
            d.delta AS "变化",
            d.sample_size AS "样本"
       FROM competency_drift d
       JOIN occupation o ON o.occupation_id = d.occupation_id
       JOIN concept c ON c.concept_id = d.concept_id
      WHERE d.to_run_id = (SELECT run_id FROM r WHERE rn = 1)
        AND d.drift_type <> 'stable'
      ORDER BY abs(coalesce(d.delta, 0)) DESC LIMIT 60
     """),
    ("a5", "词表健康度", "每张代码表有多少取值、被多少字段引用——空表和无人引用的表是腐化信号",
     """
     SELECT t.code_table_id AS "代码表",
            t.name AS "名称",
            count(v.code) AS "取值数",
            sum(CASE WHEN v.deprecated THEN 1 ELSE 0 END) AS "已废弃",
            (SELECT count(*) FROM field_catalog f
              WHERE f.code_table_id = t.code_table_id) AS "被字段引用",
            t.status AS "状态"
       FROM code_table t
       LEFT JOIN code_value v ON v.code_table_id = t.code_table_id
      GROUP BY 1, 2, 6
      ORDER BY 4 DESC, 1
     """),
    ("a6", "字段目录结构", "每个实体登记了多少字段、多少必填、多少可重复（arrayed/instanced）",
     """
     SELECT f.entity_id AS "实体",
            count(*) AS "字段数",
            sum(CASE WHEN f.is_required THEN 1 ELSE 0 END) AS "必填",
            sum(CASE WHEN f.is_arrayed THEN 1 ELSE 0 END) AS "可重复",
            sum(CASE WHEN f.instanced > 0 THEN 1 ELSE 0 END) AS "分次测量",
            count(DISTINCT f.code_table_id) AS "引用词表",
            count(DISTINCT f.main_category_id) AS "分类节点"
       FROM field_catalog f
      GROUP BY 1 ORDER BY 2 DESC
     """),
    ("a7", "职业树与外部码", "树的分层规模，以及外部职业码的核验进度（V 已核验 / E 估算 / N 无对应）",
     """
     SELECT o.level AS "层级",
            count(*) AS "节点数",
            sum(CASE WHEN o.status = 'active' THEN 1 ELSE 0 END) AS "在用",
            sum(CASE WHEN o.code_status = 'V' THEN 1 ELSE 0 END) AS "码已核验",
            sum(CASE WHEN o.code_status = 'E' THEN 1 ELSE 0 END) AS "码待核验",
            sum(CASE WHEN o.code_status = 'N' THEN 1 ELSE 0 END) AS "无对应码",
            count(DISTINCT o.family) AS "覆盖岗位族"
       FROM occupation o
      GROUP BY 1 ORDER BY 1
     """),
    ("a8", "来源与证据分级", "每个来源登记了多少次采集、多少条血缘，以及证据等级分布",
     """
     SELECT s.source_id AS "来源",
            s.name AS "名称",
            s.source_type AS "类型",
            s.evidence_grade AS "证据级",
            s.credibility AS "可信度",
            (SELECT count(*) FROM ingest_run i WHERE i.source_id = s.source_id) AS "采集批次",
            (SELECT count(*) FROM provenance p WHERE p.source_id = s.source_id) AS "血缘条数",
            s.status AS "状态"
       FROM source_registry s ORDER BY 7 DESC, 1
     """),
]


EMPTY_NOTES = {
    "a4": "最近两版能力矩阵完全一致，因此没有漂移记录。这本身是有信息量的结论："
          "说明这两次重算之间市场侧没有发生变化。注入新岗位或抬高某项能力的需求频次后"
          "再重算，这里就会出现 new / rising / falling / vanished。",
    "a8": "还没有登记任何来源。按设计，来源必须先登记（含 robots 检查与授权说明）才能采集。",
}


def run_analysis(c, aid):
    for a in ANALYSES:
        if a[0] == aid:
            return a
    raise PortalError(404, "未知分析：%s" % aid)


def analysis_rows(c, sqltext):
    return q(c, sqltext + (" LIMIT 500" if "LIMIT" not in sqltext.upper() else ""))


def view_analyze(c, qs, aid=None, want_csv=False):
    if aid is None:
        cards = "".join(
            '<div class="card"><h2><a href="/analyze/%s">%s</a></h2>'
            '<div class="sub">%s</div><table><tr><td>%s</td></tr></table>'
            '<p class="muted"><a href="/analyze/%s">运行 →</a></p></div>'
            % (a[0], esc(a[1]), esc(a[2]),
               esc(re.sub(r"\s+", " ", a[3].strip())[:150]), a[0]) for a in ANALYSES)
        return page("分析", '<div class="sub">'
                    '<b>分析不是另一个页面，而是每个页面上的一颗按钮。</b><br>'
                    '人才库、职业库、职业树、匹配、扩展与演化、质量、每一张表页面、'
                    '可视化面板——各自都带一个「分析」折叠块，点开就在<b>当前对象旁边</b>'
                    '执行该对象的分析，并把执行的 SQL 一起给你核对。'
                    '这一页只是<b>索引</b>：列出口径清单，方便单独运行或对照。'
                    '零 JS 的折叠控件，所以它不需要任何脚本。</div>' + cards,
                    subtitle="分析索引 · 实际入口在各页面上")

    a = run_analysis(c, aid)
    try:
        rows = analysis_rows(c, a[3])
    except psycopg.Error as e:
        return page(a[1], '<div class="note err">分析执行失败：%s</div>%s'
                    % (esc(str(e).splitlines()[0]),
                       sql_box(a[3])), subtitle=a[2])

    if want_csv:
        cols = list(rows[0].keys()) if rows else []
        return to_csv(cols, rows)

    cols = list(rows[0].keys()) if rows else []
    # 右对齐判据用**值本身的类型**，不要猜中文列名。
    # 原写法 `any(k in cn for k in ("数","率","量",…))` 会猜错两个方向：
    # 列名带"较上年""分类"的非数值列被误判成数值列；
    # 真正的数值列只要名字里没这些字就右对齐不了。
    # 分析结果集来自任意 SQL，拿不到 information_schema 的类型，所以看返回值 ——
    # 列名是给人看的，类型是数据库给的。
    n_right = tuple(cn for cn in cols
                    if any(is_numeric_value(r.get(cn)) for r in rows[:20]))
    nav = " · ".join('<a href="/analyze/%s">%s</a>' % (x[0], esc(x[1]))
                     for x in ANALYSES if x[0] != aid)
    chart = auto_chart(cols, rows)
    chart_block = ('<div class="card"><h2>这张表长什么样 <span class="muted">'
                   '· 依据首列维度与第一个数值列自动选型</span></h2>%s'
                   '<p class="muted">图是入口，下面的表是依据——'
                   '<b>分析结果与可视化是同一件事</b>，不是两个功能。</p></div>'
                   % chart) if chart else ""
    # 空结果也要解释清楚：0 行往往是结论本身，不是"页面坏了"
    empty = ""
    if not rows:
        empty = '<div class="note info">%s</div>' % esc(EMPTY_NOTES.get(
            aid, "本次查询返回 0 行。空结果也是一种结论——说明当前数据里"
                 "没有符合该口径的记录，而不是查询失败。"))
    return page(a[1], '<div class="sub">%s</div>'
                '<div class="card"><h2>结果 <span class="muted">· %d 行</span>'
                '<span style="float:right"><a href="/analyze/%s.csv">下载 CSV</a></span></h2>%s%s</div>'
                '%s%s<div class="card"><h3>其他分析</h3>%s</div>'
                % (esc(a[2]), len(rows), aid, empty,
                   render_rows(None, cols, rows, maxlen=60, n_right=n_right),
                   chart_block,
                   sql_box(a[3], "本分析执行的 SQL："), nav),
                subtitle="分析 · %s" % esc(a[1]), here="分析",
                crumbs=[("/analyze", "分析")])


# ---------------------------------------------------------------------------
# ⑦ 只读 SQL 控制台
# ---------------------------------------------------------------------------
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|"
    r"reindex|comment\s+on|merge|refresh\s+materialized|call|do|lock|prepare|execute|"
    r"declare|listen|notify|discard|security\s+label)\b", re.I)


def validate_sql(text: str) -> str:
    """只允许单条 SELECT/WITH。真正的执行者是 READ ONLY 事务——这里只是提前给好错误信息。"""
    s = (text or "").strip()
    while s.endswith(";"):
        s = s[:-1].strip()
    if not s:
        raise PortalError(400, "SQL 为空")
    if ";" in s:
        raise PortalError(400, "只允许单条语句（不允许出现分号）")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise PortalError(400, "只允许 SELECT 或 WITH 查询")
    hit = FORBIDDEN.search(s)
    if hit:
        raise PortalError(400, "检测到写操作关键字「%s」——本门户只读" % hit.group(1))
    return s


def view_sql(c, qs, want_csv=False):
    text = (qs.get("q", [""])[0] or "").strip()
    err = ""
    rows, cols, elapsed = [], [], 0.0
    if text:
        try:
            stmt = validate_sql(text)
            # 用**同一个会话身份**另开连接：SQL 控制台也必须受列级权限约束，
            # 否则它就成了绕过访问控制的后门（"我有一条 SQL 通路"就等于"我有全部列"）。
            rc = db(getattr(c, "session", None), readonly=True)
            try:
                with rc.cursor() as cur:
                    # SET 不接受参数占位符（扩展协议会把它发成 $1 → 语法错误）。
                    # 这里是代码里的整数常量，用 sql.Literal 安全内联。
                    cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(
                        sql.Literal(STATEMENT_TIMEOUT_MS)))
                    t0 = time.time()
                    cur.execute(stmt)
                    rows = cur.fetchall() if cur.description else []
                    cols = [d.name for d in (cur.description or [])]
                    elapsed = time.time() - t0
            finally:
                with rc.cursor() as cur:
                    cur.execute("ROLLBACK")
                rc.close()
        except PortalError as e:
            err = e.message
        except psycopg.Error as e:
            err = str(e).splitlines()[0]

    if want_csv and cols:
        # 任意 SQL 的输出列**不带来源表**，所以按列名保守拦截（见 export_policy 的说明）
        return to_csv(cols, rows)

    result = ""
    if err:
        result = '<div class="note err">%s</div>' % esc(err)
    elif text:
        n_right = tuple(cn for cn in cols if any(
            k in cn.lower() for k in ("count", "n", "num", "id", "score", "rate")))
        result = ('<div class="card"><h2>结果 <span class="muted">· %d 行 · %.0f ms</span>'
                  '<span style="float:right"><a href="/sql.csv?q=%s">下载 CSV</a></span></h2>%s</div>'
                  % (len(rows), elapsed * 1000, urllib.parse.quote(text),
                     render_rows(None, cols, rows, maxlen=60, n_right=n_right)))

    examples = [
        ("看看谁在库里", "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10"),
        ("职业树前三层", "SELECT level, count(*) FROM occupation GROUP BY 1 ORDER BY 1"),
        ("最近一版能力权重", "SELECT occupation_id, concept_id, importance, essentiality FROM v_competency_current ORDER BY importance DESC LIMIT 20"),
        ("未被任何字段引用的词表", "SELECT code_table_id FROM code_table WHERE code_table_id NOT IN (SELECT code_table_id FROM field_catalog WHERE code_table_id IS NOT NULL)"),
        ("触发器和它的表", "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE NOT t.tgisinternal ORDER BY 1 LIMIT 20"),
        ("每个域的数据量", "SELECT table_schema, count(*) FROM information_schema.tables WHERE table_schema='mt' GROUP BY 1"),
    ]
    ex = "".join('<div class="note info"><a href="/sql?q=%s">%s</a> '
                 '<code class="muted">%s</code></div>'
                 % (urllib.parse.quote(s), esc(t), esc(trunc(s, 90)))
                 for t, s in examples)

    body = """
<div class="note info"><b>本门户只读。</b>查询在 <code>BEGIN TRANSACTION READ ONLY</code> 里执行，
任何写操作都会被 PostgreSQL 直接拒绝；只允许单条 <code>SELECT</code>/<code>WITH</code>，
并带 %d 秒超时。想改数据请走正常的迁移或 API。</div>
<div class="card"><form method="get" action="/sql">
<label>SQL（单条 SELECT）</label>
<textarea name="q" rows="4" spellcheck="false">%s</textarea>
<div style="margin-top:8px"><button type="submit">执行</button></div></form></div>
%s<div class="card"><h2>随手可试</h2>%s</div>
""" % (STATEMENT_TIMEOUT_MS // 1000, esc(text), result, ex)
    return page("SQL", body, subtitle="只读 SQL 控制台 · 想怎么看就怎么看")


# ---------------------------------------------------------------------------
# ⑧ 血缘与来源
# ---------------------------------------------------------------------------
def view_lineage(c, qs) -> bytes:
    srcs = q(c, """
        SELECT s.*,
               (SELECT count(*) FROM ingest_run i WHERE i.source_id = s.source_id) AS batches,
               (SELECT count(*) FROM provenance p WHERE p.source_id = s.source_id) AS provs
          FROM source_registry s ORDER BY provs DESC, s.source_id""")

    # L0 原始层：真的去数磁盘上的文件（"原文永远留着"是可核的）
    raw_dir = os.path.join(BASE, "data", "raw")
    l0 = []
    if os.path.isdir(raw_dir):
        for d in sorted(os.listdir(raw_dir)):
            p = os.path.join(raw_dir, d)
            if not os.path.isdir(p):
                continue
            n_html = n_src = size = 0
            for root, _dirs, files in os.walk(p):
                for f in files:
                    fp = os.path.join(root, f)
                    size += os.path.getsize(fp)
                    if f.endswith(".src"):
                        n_src += 1
                    else:
                        n_html += 1
            l0.append((d, n_html, n_src, size))
    l0_html = "".join(
        '<tr><td><code>%s</code></td><td class="n">%d</td><td class="n">%d</td>'
        '<td class="n">%.1f KB</td></tr>' % (esc(d), h, s, sz / 1024.0)
        for d, h, s, sz in l0)

    batches = q(c, """
        SELECT i.ingest_run_id, i.source_id, i.started_at, i.status,
               (SELECT count(*) FROM provenance p WHERE p.ingest_run_id = i.ingest_run_id) AS n
          FROM ingest_run i ORDER BY i.started_at DESC LIMIT 20""")
    b_html = "".join('<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td>'
                     '<td class="n">%d</td></tr>'
                     % (esc(x["ingest_run_id"]), esc(x["source_id"]),
                        esc(x["started_at"]), esc(x["status"]), x["n"]) for x in batches)

    grades = q(c, """SELECT evidence_grade, count(*) AS n FROM provenance
                     GROUP BY 1 ORDER BY 2 DESC""")
    g_html = "".join('<tr><td><span class="pill p-v">%s</span></td><td class="n">%d</td>'
                     '<td class="muted">%s</td></tr>'
                     % (esc(x["evidence_grade"] or "—"), x["n"],
                        {"A": "权威原始来源", "B": "可信二手", "C": "线索级，不入结论",
                         "D": "未证实"}.get(x["evidence_grade"], ""))
                     for x in grades)

    rels = q(c, "SELECT * FROM dataset_release ORDER BY created_at DESC LIMIT 10")
    rel_html = "".join(
        '<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td>'
        '<td>%s</td><td>%s</td></tr>'
        % (esc(x.get("release_id")), esc(x.get("name")), esc(x.get("version")),
           esc(x.get("status")),
           "有" if x.get("doc_codebook_uri") else '<span class="pill p-red">无</span>',
           esc(trunc(x.get("checksum_sha256"), 16)))
        for x in rels) or ('<tr><td colspan="6" class="muted">还没有发布记录。'
                           '按设计：<b>没有 codebook 就不发布</b>——'
                           '这条规则由 <code>dataset_release</code> 上的 CHECK 约束执行。</td></tr>')

    body = """
<div class="sub">这个库里每一条业务记录都能回答"它从哪来"：来源登记 → 采集批次 → 逐条血缘 →
原始页面文件。这一页把那条链路的四个环节都摊开。</div>

<div class="card"><h2>来源登记 <span class="muted">· %d 个</span></h2>
<table><tr><th>来源</th><th>名称</th><th>类型</th><th>证据级</th><th>可信度</th>
<th class="n">采集批次</th><th class="n">血缘条数</th><th>状态</th><th>授权说明</th></tr>%s</table></div>

<div class="card"><h2>L0 原始层 <span class="muted">· 磁盘上的原文，只增不改</span></h2>
<table><tr><th>来源目录</th><th class="n">原始文件</th><th class="n">.src 侧车</th>
<th class="n">占用</th></tr>%s</table>
<p class="muted">路径：<code>%s</code>。原始页面按内容 sha256 命名，重复采集不会重复落盘；
每个文件配一个 <code>.src</code> 侧车记录它的来源 URL 与采集时间。</p></div>

<div class="grid2">
<div class="card"><h2>采集批次（最近 20）</h2><table>
<tr><th>批次</th><th>来源</th><th>开始</th><th>状态</th><th class="n">血缘</th></tr>%s</table></div>
<div class="card"><h2>证据分级分布</h2><table>
<tr><th>等级</th><th class="n">条数</th><th>含义</th></tr>%s</table>
<p class="muted">C 级是"线索，不能直接写进结论"——分级不是装饰，它参与匹配折扣。</p></div>
</div>

<div class="card"><h2>发布登记 <span class="muted">· 无 codebook 不发布</span></h2>
<table><tr><th>发布 ID</th><th>名称</th><th>版本</th><th>状态</th><th>codebook</th>
<th>校验和</th></tr>%s</table></div>

%s
""" % (len(srcs),
       "".join('<tr><td><a href="/e/source_registry?val=%s"><code>%s</code></a></td>'
               '<td>%s</td><td>%s</td><td><span class="pill p-v">%s</span></td>'
               '<td class="n">%s</td><td class="n">%d</td><td class="n">%d</td><td>%s</td>'
               '<td class="muted">%s</td></tr>'
               % (urllib.parse.quote(x["source_id"]), esc(x["source_id"]), esc(x["name"]),
                  esc(x["source_type"]), esc(x["evidence_grade"] or "—"),
                  esc(x["credibility"]), x["batches"], x["provs"], esc(x["status"]),
                  esc(trunc(x["license_note"], 60))) for x in srcs),
       l0_html or '<tr><td colspan="4" class="muted">data/raw 下还没有落盘文件</td></tr>',
       esc(raw_dir), b_html, g_html, rel_html,
       sql_box("""SELECT * FROM source_registry;  -- 来源登记
SELECT * FROM ingest_run ORDER BY started_at DESC;  -- 采集批次
SELECT * FROM provenance WHERE source_id = '...';   -- 逐条血缘"""))
    return page("血缘与来源", body, subtitle="来源 → 采集 → 血缘 → 原始文件")


# ---------------------------------------------------------------------------
# ⑨ 内嵌小分析：各枢纽页复用它，避免每个页面各写一套渲染
# ---------------------------------------------------------------------------
def analysis_panel(blocks, title="分析", hint="") -> str:
    """把若干分析块收进一个原生折叠控件 —— 页面内的「分析」按钮。

    为什么不做成独立页面：分析的对象就在这一页上（这张表、这个职业、这个人）。
    要人先跳到另一个页面、再回想"我刚才看的是哪张表"，等于把工具和对象拆开了。
    成熟 BI 的 drill-down 也是这个形态：**分析长在数据旁边**。

    零 JS：`<details>/<summary>` 是浏览器原生折叠控件，不需要任何脚本。
    项数写进 summary，所以折叠着也知道有 8 项分析——不是藏起来。
    """
    blocks = [b for b in blocks if b]
    if not blocks:
        return ""
    return ('<details class="analysis"><summary>%s（%d 项）</summary>%s%s</details>'
            % (esc(title), len(blocks),
               '<div class="sub" style="margin:8px 0 10px">%s</div>' % hint if hint else "",
               "".join(blocks)))


def table_profile_blocks(c, name) -> list:
    """任意一张表的通用画像分析：列空值数排行。

    这个分析对**每一张有数据的表**都成立，所以它是"分析按钮能出现在所有表页面上"的原因。
    行数为 0 的表返回空列表——不制造没有对象的分析。
    """
    m = meta()
    n = m["by_name"].get(name, {}).get("rows", 0)
    cols = [x["column_name"] for x in m["cols"].get(name, [])]
    if not n or not cols:
        return []
    exprs = ", ".join('sum(CASE WHEN "%s" IS NULL THEN 1 ELSE 0 END) AS "%s"' % (x, x)
                      for x in cols)
    sqltext = 'SELECT %s FROM mt."%s"' % (exprs, name)
    try:
        row = q(c, sqltext)[0]
    except psycopg.Error:
        return []
    pairs = sorted(((k, v or 0) for k, v in row.items() if (v or 0) > 0),
                   key=lambda x: -x[1])[:15]
    if not pairs:
        return []
    body = "".join('<tr><td><code>%s</code></td><td class="n">%s</td>'
                   '<td class="n">%.1f%%</td></tr>'
                   % (esc(k), f"{v:,}", 100.0 * v / n) for k, v in pairs)
    return ['<div class="card"><h2>列空值数 Top 15 '
            '<span class="muted">· 表内共 %s 行</span></h2>'
            '<table><tr><th>列</th><th class="n">空值行数</th><th class="n">空值率</th></tr>'
            '%s</table>'
            '<p class="muted">空值率高说明「结构建了、数据没到」。'
            '本库纪律：<b>空就是空</b>，不用默认值或推算值填充。</p>%s</div>'
            % (f"{n:,}", body, sql_box(sqltext, "这张表的列空值口径："))]


def page_analysis(c, page, ctx=None) -> str:
    """按页面返回该页的「分析」按钮内容。

    统一在这里分派，是为了让"给某页加分析"变成一句 `body += page_analysis(c, "…")`，
    而不是每页各自去改 HTML 模板的 %s 参数——那样最容易把占位符数改错
    （本项目已经因此报过两次 "not all arguments converted"）。
    """
    b = []
    if page == "/match":
        b.append(mini_analysis(c, "三态总体分布（全库）", """
            SELECT count(*) AS "匹配条数",
                   sum((score_breakdown->>'met')::numeric) AS "命中合计",
                   sum((score_breakdown->>'gap')::numeric) AS "缺口合计",
                   sum((score_breakdown->>'unknown')::numeric) AS "未知合计",
                   round(avg(score_total), 2) AS "平均得分"
              FROM match_result"""))
        b.append(mini_analysis(c, "最常出现的能力缺口", """
            SELECT c.preferred_label AS "缺口能力", count(*) AS "出现在多少条匹配里"
              FROM match_result m, unnest(m.gap_concepts) AS g(cid)
              JOIN concept c ON c.concept_id = g.cid
             GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""))
        b.append(mini_analysis(c, "算法版本与批次", """
            SELECT algo_version AS "算法版本", status AS "状态", count(*) AS "批次数",
                   sum(person_count) AS "累计人数", sum(job_count) AS "累计岗位"
              FROM match_run GROUP BY 1, 2 ORDER BY 3 DESC"""))
    elif page == "/tree":
        b.append(mini_analysis(c, "各层的节点与岗位数", """
            SELECT level AS "层级", count(*) AS "节点数",
                   count(*) FILTER (WHERE status='active') AS "在用",
                   count(DISTINCT family) AS "覆盖岗位族"
              FROM occupation GROUP BY 1 ORDER BY 1"""))
        b.append(mini_analysis(c, "候选池：等评审的新枝", """
            SELECT '岗位候选' AS "类型", status AS "状态", count(*) AS "数量",
                   sum(evidence_count) AS "证据条数"
              FROM occupation_candidate GROUP BY 1, 2
            UNION ALL
            SELECT '能力候选', status, count(*), sum(evidence_count)
              FROM concept_candidate GROUP BY 1, 2 ORDER BY 1, 3 DESC"""))
    elif page == "/schema":
        b.append(mini_analysis(c, "结构最复杂的表（列 / 约束 / 触发器）", """
            SELECT c.relname AS "表",
                   (SELECT count(*) FROM pg_attribute a
                     WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped) AS "列数",
                   (SELECT count(*) FROM pg_constraint x WHERE x.conrelid=c.oid) AS "约束数",
                   (SELECT count(*) FROM pg_trigger t
                     WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) AS "触发器"
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='mt' AND c.relkind='r'
             ORDER BY 2 DESC, 3 DESC LIMIT 15"""))
    elif page == "/entity":
        tbl, _val = (ctx or ("", ""))
        if tbl:
            # 复用与表页面同一个画像分析：exact count，不用 reltuples 估算
            return analysis_panel(table_profile_blocks(c, tbl),
                                  "分析这条记录所在的表",
                                  "同一个分析也出现在表页面上——同一份定义，不分两处。")
    return analysis_panel(b, "分析", "分析的对象就是这一页展示的东西，所以它长在这一页上。")


def is_numeric_value(v) -> bool:
    """值本身是不是数字。比猜列名可靠：列名是给人看的，类型是数据库给的。

    `mini_analysis` 跑的是**任意 SQL**，拿不到 information_schema 的 data_type，
    所以只能看返回值。这也比"列名里有没有『数』『分』"这种猜测准 ——
    原来的写法会把「较上年」「分类」误判成数值列，也会漏掉名字里没这些字的数值列。
    """
    if v is None or isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return type(v).__name__ == "Decimal"


def mini_analysis(c, title, sqltext, maxlen=60, show_sql=True, params=None) -> str:
    """跑一段分析 SQL 并渲染成卡片；卡片底部给出这段 SQL 本身。

    `params` 是**绑定参数**（独立审查 P2-5）：原来只接受裸 SQL 字符串，于是
    调用点只能自己把值 format 进去 —— `view_talent_one` 就写了
    `... WHERE s.person_id = '{pid}' ...`.format(pid=pid)。
    当前 `pid` 必须先存在于 person 表才走得到那里（前面参数化查过），所以不可直接注入；
    但那是"靠前一个查询兜底"的**隐式安全**，任何一次重排代码就会破。
    现在支持绑定参数，调用点不再有理由拼字符串。
    """
    try:
        rows = q(c, sqltext, params)
    except psycopg.Error as e:
        return ('<div class="card"><h2>%s</h2><div class="note err">%s</div>%s</div>'
                % (esc(title), esc(str(e).splitlines()[0]), sql_box(sqltext)))
    cols = list(rows[0].keys()) if rows else []
    n_right = tuple(cn for cn in cols
                    if any(is_numeric_value(r.get(cn)) for r in rows[:20]))
    body = (render_rows(None, cols, rows, maxlen=maxlen, n_right=n_right) if rows
            else '<p class="muted">（0 行）——当前库里没有符合这个口径的数据。</p>')
    return ('<div class="card"><h2>%s <span class="muted">· %d 行</span></h2>%s%s</div>'
            % (esc(title), len(rows), body,
               sql_box(sqltext, "本分析的 SQL：") if show_sql else ""))


def bar_table(rows, label_key, value_key, href=None, unit="") -> str:
    """带横条的排行表——比纯数字更容易看出分布。"""
    mx = max([r[value_key] or 0 for r in rows], default=0) or 1
    out = []
    for r in rows:
        v = r[value_key] or 0
        lab = esc(r[label_key])
        if href:
            lab = '<a href="%s">%s</a>' % (href(r), lab)
        out.append('<tr><td>%s</td><td class="n">%s%s</td>'
                   '<td><span class="barwrap"><span class="bar" style="width:%d%%"></span>'
                   '</span></td></tr>'
                   % (lab, f"{v:,}", unit, max(2, int(100 * v / mx))))
    return ('<table><tr><th>项目</th><th class="n">数量</th><th>占比</th></tr>%s</table>'
            % "".join(out))


# ---------------------------------------------------------------------------
# ⑩ 人才库：浏览 + 在线分析
# ---------------------------------------------------------------------------
TALENT_ANALYSES = [
    ("学历层次分布", """
     SELECT e.degree_level AS "学历码", count(DISTINCT e.person_id) AS "人数",
            count(*) AS "教育记录数"
       FROM education_record e GROUP BY 1 ORDER BY 2 DESC"""),
    ("专业方向分布", """
     SELECT e.major_raw AS "专业方向", count(DISTINCT e.person_id) AS "人数",
            sum(CASE WHEN e.is_clinical THEN 1 ELSE 0 END) AS "临床类记录"
       FROM education_record e GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""),
    ("能力供给 Top 15", """
     SELECT preferred_label AS "能力", concept_type AS "类型",
            person_count AS "具备人数", round(avg_level, 2) AS "平均熟练度",
            round(avg_transferability, 2) AS "平均可迁移性"
       FROM v_skill_supply ORDER BY person_count DESC, preferred_label LIMIT 15"""),
    ("证据等级分布", """
     SELECT cel_level AS "证据等级", evidence_type AS "证据类型", count(*) AS "条数",
            round(avg(verifiability), 2) AS "平均可验证性"
       FROM evidence GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15"""),
    ("供需对照（人才侧 vs 岗位侧）", """
     SELECT coalesce(s.preferred_label, d.preferred_label) AS "能力",
            s.person_count AS "具备人数", d.posting_count AS "要求岗位数",
            (coalesce(s.person_count,0) - coalesce(d.posting_count,0)) AS "净差"
       FROM v_skill_supply s FULL JOIN v_skill_demand d USING (concept_id)
      ORDER BY abs(coalesce(s.person_count,0) - coalesce(d.posting_count,0)) DESC LIMIT 15"""),
    ("有观测窗口的人才（未记录≠不具备）", """
     SELECT w.window_type AS "窗口类型", count(DISTINCT w.person_id) AS "覆盖人数",
            min(w.start_date) AS "最早开始", max(w.end_date) AS "最晚结束"
       FROM observation_window w GROUP BY 1 ORDER BY 2 DESC"""),
]


def talent_where(qs):
    cond, P = [], {}
    if qs.get("degree", [""])[0]:
        cond.append("EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id "
                    "AND e.degree_level = %(deg)s)")
        P["deg"] = qs["degree"][0]
    if qs.get("major", [""])[0]:
        cond.append("EXISTS (SELECT 1 FROM education_record e WHERE e.person_id=p.person_id "
                    "AND e.major_raw ILIKE %(maj)s)")
        P["maj"] = "%" + qs["major"][0] + "%"
    if qs.get("province", [""])[0]:
        cond.append("EXISTS (SELECT 1 FROM person_demographics d WHERE d.person_id=p.person_id "
                    "AND d.hukou_province = %(pv)s)")
        P["pv"] = qs["province"][0]
    if qs.get("skill", [""])[0]:
        cond.append("EXISTS (SELECT 1 FROM skill_assertion s JOIN concept cn ON "
                    "cn.concept_id = s.concept_id WHERE s.person_id = p.person_id "
                    "AND (cn.preferred_label ILIKE %(sk)s OR cn.concept_id = %(sk0)s))")
        P["sk"] = "%" + qs["skill"][0] + "%"
        P["sk0"] = qs["skill"][0]
    return ((" WHERE " + " AND ".join(cond)) if cond else ""), P


# ---------------------------------------------------------------------------
# 人才库列注册表（列清单的**单一定义**）
# ---------------------------------------------------------------------------
# 为什么做成注册表而不是把 SQL 写死：人才库要看的字段会随业务增长（这正是本项目
# "字段必须登记、列可以长"的一贯立场），而"页面上能看什么""CSV 导出什么""按什么排序"
# 必须来自同一份定义，否则又会出现同一个指标两个口径的问题。
#
# 分组参考成熟人才档案产品（Oracle Enhanced Talent Profiles / Taleo / Dynamics 365 HR）
# 的字段组织：基本信息 / 教育 / 经历 / 资质与成果 / 能力与证据 / 匹配与质量。
# 其中"数据质量"一列是刻意的——合成数据必须能在列表里被一眼认出。
TALENT_COLUMNS = [
    # key, 标题, 分组, 默认显示, SQL 表达式（别名 p = person）, 对齐/格式
    dict(key="pid", title="person_id", group="基本信息", default=True,
         sel="p.person_id", fmt="link"),
    dict(key="code", title="档案号", group="基本信息", default=False,
         sel="p.subject_code", fmt="text"),
    dict(key="status", title="状态", group="基本信息", default=False,
         sel="p.status", fmt="text"),
    dict(key="channel", title="入组渠道", group="基本信息", default=False,
         sel="p.enroll_channel", fmt="code:CT_ENROLL_CHANNEL"),
    dict(key="sex", title="性别", group="基本信息", default=False,
         sel="(SELECT d.sex FROM person_demographics d WHERE d.person_id=p.person_id)",
         fmt="code:CT_SEX"),
    dict(key="age", title="年龄", group="基本信息", default=False,
         sel="(SELECT (EXTRACT(YEAR FROM current_date)::int - d.birth_year) "
             "FROM person_demographics d WHERE d.person_id=p.person_id)", fmt="num"),
    dict(key="province", title="户籍城市", group="基本信息", default=True,
         sel="(SELECT d.hukou_province FROM person_demographics d "
             "WHERE d.person_id=p.person_id)", fmt="text",
         hint="列名是历史遗留：person_demographics.hukou_province 里实际存的是城市名"
              "（实测 18 个去重值全是城市）。按城市比对，不要当省用。"),

    dict(key="degree", title="最高学历", group="教育", default=True,
         sel="(SELECT e.degree_level FROM education_record e WHERE e.person_id=p.person_id "
             "ORDER BY e.degree_level DESC NULLS LAST LIMIT 1)",
         fmt="code:CT_DEGREE_LEVEL"),
    dict(key="degree_name", title="学位", group="教育", default=False,
         sel="(SELECT e.degree_name FROM education_record e WHERE e.person_id=p.person_id "
             "ORDER BY e.degree_level DESC NULLS LAST LIMIT 1)", fmt="text"),
    dict(key="major", title="专业", group="教育", default=True,
         sel="(SELECT e.major_raw FROM education_record e WHERE e.person_id=p.person_id "
             "ORDER BY e.degree_level DESC NULLS LAST LIMIT 1)", fmt="text"),
    dict(key="school", title="院校", group="教育", default=True,
         sel="(SELECT e.school_name FROM education_record e WHERE e.person_id=p.person_id "
             "ORDER BY e.degree_level DESC NULLS LAST LIMIT 1)", fmt="text"),
    dict(key="clinical", title="临床类专业", group="教育", default=False,
         sel="(SELECT bool_or(e.is_clinical) FROM education_record e "
             "WHERE e.person_id=p.person_id)", fmt="bool"),
    dict(key="overseas", title="海外经历", group="教育", default=False,
         sel="(SELECT bool_or(e.overseas) FROM education_record e "
             "WHERE e.person_id=p.person_id)", fmt="bool"),
    dict(key="edu_n", title="教育记录数", group="教育", default=False,
         sel="(SELECT count(*) FROM education_record e WHERE e.person_id=p.person_id)",
         fmt="num"),

    dict(key="emp_years", title="工作年限", group="经历", default=True,
         sel="(SELECT round(sum((coalesce(w.end_date, current_date) - w.start_date) / 365.25), 1) "
             "FROM employment_record w WHERE w.person_id=p.person_id)", fmt="num"),
    dict(key="emp_type", title="当前单位类型", group="经历", default=False,
         sel="(SELECT w.employer_type FROM employment_record w WHERE w.person_id=p.person_id "
             "ORDER BY w.is_current DESC NULLS LAST, w.start_date DESC NULLS LAST LIMIT 1)",
         fmt="code:CT_EMPLOYER_TYPE"),
    dict(key="emp_title", title="当前岗位", group="经历", default=True,
         sel="(SELECT w.title_raw FROM employment_record w WHERE w.person_id=p.person_id "
             "ORDER BY w.is_current DESC NULLS LAST, w.start_date DESC NULLS LAST LIMIT 1)",
         fmt="text"),
    dict(key="clin_months", title="临床/规培(月)", group="经历", default=False,
         sel="(SELECT round(sum(c.duration_months), 1) FROM clinical_exposure c "
             "WHERE c.person_id=p.person_id)", fmt="num"),
    dict(key="proj_n", title="项目数", group="经历", default=False,
         sel="(SELECT count(*) FROM project_record x WHERE x.person_id=p.person_id)", fmt="num"),

    dict(key="cred_n", title="证书数", group="资质与成果", default=False,
         sel="(SELECT count(*) FROM credential x WHERE x.person_id=p.person_id)", fmt="num"),
    dict(key="paper_n", title="论文/产出数", group="资质与成果", default=False,
         sel="(SELECT count(*) FROM research_output x WHERE x.person_id=p.person_id)", fmt="num"),
    dict(key="paper_fa", title="其中一作", group="资质与成果", default=False,
         sel="(SELECT count(*) FROM research_output x WHERE x.person_id=p.person_id "
             "AND x.is_first_author)", fmt="num"),
    dict(key="if_max", title="最高影响因子", group="资质与成果", default=False,
         sel="(SELECT max(x.if_value) FROM research_output x WHERE x.person_id=p.person_id)",
         fmt="num"),
    dict(key="award_n", title="获奖数", group="资质与成果", default=False,
         sel="(SELECT count(*) FROM award_honor x WHERE x.person_id=p.person_id)", fmt="num"),

    dict(key="skills", title="能力主张", group="能力与证据", default=True,
         sel="(SELECT count(*) FROM skill_assertion x WHERE x.person_id=p.person_id)", fmt="num"),
    dict(key="skill_top", title="代表能力", group="能力与证据", default=False,
         sel="(SELECT c.preferred_label FROM skill_assertion s JOIN concept c "
             "ON c.concept_id=s.concept_id WHERE s.person_id=p.person_id "
             "ORDER BY s.transferability DESC NULLS LAST LIMIT 1)", fmt="text"),
    dict(key="evs", title="证据", group="能力与证据", default=True,
         sel="(SELECT count(*) FROM evidence x WHERE x.person_id=p.person_id)", fmt="num"),
    dict(key="cel_max", title="最高证据等级", group="能力与证据", default=True,
         sel="(SELECT max(x.cel_level) FROM evidence x WHERE x.person_id=p.person_id)",
         fmt="code:CT_CEL_LEVEL", show_code=True,
         hint="E0–E4 有序：E4 履职记录 > E3 作品 > E2 第三方评估 > E1 证书 > E0 自述。"
              "词表的 label 是**类别名**不是等级名，所以这里连码一起显示。"),
    dict(key="transfer", title="平均可迁移性", group="能力与证据", default=False,
         sel="(SELECT round(avg(s.transferability), 2) FROM skill_assertion s "
             "WHERE s.person_id=p.person_id)", fmt="num"),
    dict(key="verified", title="已核验主张", group="能力与证据", default=False,
         sel="(SELECT count(*) FROM skill_assertion s WHERE s.person_id=p.person_id "
             "AND s.verify_status IN ('V2','V3','V4'))", fmt="num"),
    dict(key="windows", title="观测窗口", group="能力与证据", default=False,
         sel="(SELECT count(*) FROM observation_window x WHERE x.person_id=p.person_id)",
         fmt="num"),

    dict(key="best_score", title="最高匹配分", group="匹配与质量", default=True,
         sel="(SELECT max(m.score_total) FROM match_result m WHERE m.person_id=p.person_id)",
         fmt="num"),
    dict(key="best_occ", title="最佳匹配职业", group="匹配与质量", default=False,
         sel="(SELECT o.label_zh FROM match_result m JOIN job_posting jp ON jp.job_id=m.target_id "
             "JOIN occupation o ON o.occupation_id=jp.occupation_id "
             "WHERE m.person_id=p.person_id ORDER BY m.score_total DESC NULLS LAST LIMIT 1)",
         fmt="text"),
    dict(key="match_n", title="匹配条数", group="匹配与质量", default=False,
         sel="(SELECT count(*) FROM match_result m WHERE m.person_id=p.person_id)", fmt="num"),
    dict(key="source", title="数据来源", group="匹配与质量", default=True,
         sel="CASE WHEN p.quality_flags && ARRAY['synthetic_fixture'] THEN '合成' "
             "WHEN p.quality_flags && ARRAY['real_public_case'] THEN '真实公开案例' "
             "ELSE '演示/无标记' END", fmt="text",
         hint="合成 / 真实公开案例 / 演示无标记。这一列是刻意的："
              "成熟的数据集必须能一句话筛出哪些行可以对外。"),
]

TALENT_PRESETS = {
    "精简": ["pid", "degree", "major", "school", "province", "skills", "best_score"],
    "推荐": None,                       # None = 用 default 标记
    "教育": ["pid", "degree", "degree_name", "major", "school", "clinical", "overseas", "edu_n"],
    "能力": ["pid", "skills", "skill_top", "transfer", "verified", "evs", "cel_max", "windows"],
    "成果": ["pid", "cred_n", "paper_n", "paper_fa", "if_max", "award_n", "proj_n"],
    "匹配": ["pid", "best_score", "best_occ", "match_n", "source"],
    "全部": None,                       # None + 语义由调用处区分
}


def talent_default_cols() -> list:
    return [x["key"] for x in TALENT_COLUMNS if x["default"]]


def talent_all_cols() -> list:
    return [x["key"] for x in TALENT_COLUMNS]


def talent_preset_cols(name) -> list:
    """预设名 → 列 key 列表。`None` 的语义收口在这里，别让调用处各判一次。"""
    if name == "全部":
        return talent_all_cols()
    keys = TALENT_PRESETS.get(name)
    return list(keys) if keys else talent_default_cols()


def talent_col_keys(qs) -> list:
    """解析 cols 参数：**所有**同名值都算，值里的逗号也当分隔符。

    为什么要容忍两种形状：
      · 浏览器提交表单 → 每个勾选框一条 `cols=pid&cols=degree&…`（多值）
      · 页面自己生成的链接 → `cols=pid,degree,…`（单值逗号串）
    两者必须等价，否则"点表头排序"和"点筛选按钮"会得到不同的列。
    """
    out = []
    for v in qs.get("cols", []) or []:
        for k in (v or "").split(","):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


def talent_pick_columns(qs) -> list:
    """决定这次显示哪些列。优先级：显式 cols > preset > 默认。

    三条语义是实测钉出来的，改任何一条都会让界面骗人：
      1. **cols 是多值参数，必须全量解析**。原实现 `qs.get("cols", [""])[0]`
         只取第一条，于是正常的表单提交（35 个 checkbox）会把表格砍成只剩
         person_id 一列 —— 用户点一下"筛选 / 应用"就丢掉 34 列。
      2. **"点了选择器但一个都没勾" ≠ "没给 cols"**。前者是用户明确要清空，
         后者是首次访问。靠隐藏域 `pick=1` 区分；否则用户取消全部勾选后，
         页面会自作主张把默认列又摆回来，看起来像"没生效"。
      3. person_id 永远在第一位：它是行的身份，也是外键联查的钥匙，不允许隐藏。
    """
    known = [x["key"] for x in TALENT_COLUMNS]
    want = [k for k in talent_col_keys(qs) if k in known]
    if want:
        return ["pid"] + [k for k in want if k != "pid"]
    if (qs.get("pick", [""])[0] or "") == "1":
        return ["pid"]
    pre = (qs.get("preset", [""])[0] or "").strip()
    if pre == "全部" or pre in TALENT_PRESETS:
        return ["pid"] + [k for k in talent_preset_cols(pre) if k != "pid"]
    return talent_default_cols()


def talent_view_name(qs) -> str:
    """当前这套列是哪个预设，还是用户自己拼的。界面要回显，否则用户不知道自己在哪。"""
    cur = set(talent_pick_columns(qs)) | {"pid"}
    for name in TALENT_PRESETS:
        if (set(talent_preset_cols(name)) | {"pid"}) == cur:
            return name
    if cur == {"pid"}:
        return "仅 ID"
    return "自定义"


def talent_order(qs, colmap) -> str:
    """排序：只允许按**当前可见**的列排，且方向只有 asc/desc。"""
    sort = (qs.get("sort", [""])[0] or "").strip()
    if sort in colmap:
        return "%s %s NULLS LAST" % (colmap[sort]["sel"],
                                     "DESC" if qs.get("dir", ["asc"])[0] == "desc" else "ASC")
    return "p.person_id ASC"


def talent_cell(col, row) -> str:
    """按 fmt 渲染单元格。空值显式显示为 NULL——不用空白假装有值。"""
    k = col["key"]
    if k == "pid":
        return '<a href="/talent/%s"><code>%s</code></a>' % (
            urllib.parse.quote(str(row[k])), esc(row[k]))
    v = row.get(k)
    if v is None:
        return '<span class="nul">NULL</span>'
    fmt = col["fmt"]
    if fmt == "num":
        try:
            f = float(v)
            return '<span title="%s">%s</span>' % (esc(v), f"{f:,.1f}".rstrip("0").rstrip("."))
        except (TypeError, ValueError):
            return esc(v)
    if fmt == "bool":
        return '<span class="pill p-t">是</span>' if v else '<span class="pill p-n">否</span>'
    if fmt.startswith("code:"):
        table = fmt.split(":", 1)[1]
        lab = lbl(table, v)
        # 有些词表的 label 是"类别名"（CT_CEL_LEVEL 的 E4 = 履职记录），单看标签
        # 读不出等级高低。这类列把码一起显示：E4 · 履职记录。
        shown = ("%s · %s" % (v, lab)) if col.get("show_code") else lab
        return '<span title="%s">%s</span>' % (esc(v), esc(shown))
    return '<span title="%s">%s</span>' % (esc(v), esc(trunc(v, 34)))


def talent_csv(c, qs) -> bytes:
    """导出**当前选择的列**（不是固定 8 列）。导出的口径与页面完全一致。"""
    where, P = talent_where(qs)
    cols = talent_pick_columns(qs)
    colmap = {x["key"]: x for x in TALENT_COLUMNS}
    picked = [colmap[k] for k in cols if k in colmap]
    sel_list = ", ".join('%s AS "%s"' % (x["sel"], x["key"]) for x in picked)
    order = talent_order(qs, colmap)
    rows = q(c, "SELECT %s FROM person p%s ORDER BY %s" % (sel_list, where, order), P)
    # 码值在 CSV 里也翻译成中文，否则导出物与页面看到的不一致
    for r in rows:
        for x in picked:
            if x["fmt"].startswith("code:") and r.get(x["key"]) is not None:
                r[x["key"]] = lbl(x["fmt"].split(":", 1)[1], r[x["key"]])
            elif x["fmt"] == "bool":
                r[x["key"]] = "是" if r.get(x["key"]) else "否"
    return to_csv([x["title"] for x in picked],
                  [{x["title"]: r.get(x["key"]) for x in picked} for r in rows])


def _derived_person_payload(c, pid, d):
    """调匹配引擎的派生规则，拿到载荷与规则自述。

    为什么走 import 而不是在 portal 里再写一遍：**同一个口径只能有一个实现**。
    这个项目已经因为"同一指标两处各算一遍"出过事故（质量门 90.2% vs 可视化页 61.7%），
    派生的年限/层级比指标更容易漂移——两边各改一次就再也对不上了。
    取数失败不抛异常：页面要能显示"这里没有值"，而不是整页 500。
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "analytics"))
        import vector as _v
        fn = _v.DERIVE_PERSON.get(d["dimension_id"])
        if fn is None:
            return None, "派生口径未实现（%s）" % (d["person_locator"] or "")[5:40]
        return fn(c, pid, d)
    except Exception:  # noqa: BLE001 —— 派生失败不该让页面挂掉
        return None, "派生失败（详见 code/analytics/vector.py）"


def _payload_to_codes(c, d, pay) -> list:
    """把匹配载荷翻译成**显示用的码**。

    载荷是给比较器看的（rank / min_rank / lo / hi），码表里没有"rank=3"这个词；
    要显示就得反查：rank 是词表内按 sort_order 排序的序号（见 vector.py 的说明）。
    反查不到就返回空 —— 宁可显示"无记录"，也不要显示一个看不懂的内部字段。
    """
    ct = d["code_table_id"]
    if not ct:
        return []
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "analytics"))
        import vector as _v
        vals = q(c, "SELECT code FROM code_value WHERE code_table_id=%s "
                    "ORDER BY sort_order", (ct,))
        order = [x["code"] for x in vals]
    except psycopg.Error:
        return []
    if "codes" in pay and pay["codes"]:
        return [str(x) for x in pay["codes"]]
    if "code" in pay and pay["code"] is not None:
        return [str(pay["code"])]
    rk = pay.get("rank", pay.get("min_rank"))
    if rk is not None:
        try:
            i = int(rk) - 1
            if 0 <= i < len(order):
                return [order[i]]
        except (TypeError, ValueError):
            return []
    return []


def person_dimensions(c, pid) -> list:
    """按 `mt.dimension` 注册表取一个人的**全部维度取值**。

    这是"看见对每个样本的建模"的核心：不再把维度写死在页面里，而是**读注册表**，
    于是新增一个维度只要往 `dimension` 插一行，这里自动就显示它。

    解析 `person_locator` 的四种形态（注册表里实际出现的）：
      `表.列`            → 直接取该表该列（表/列先与系统目录比对，防注入）
      `preference:PFx`   → 取 preference 表里这个人的该类型偏好
      `concept:Kx`       → 取能力主张里属于该概念族的
      `derived:`/`unavailable` → 不解析，如实说明"由其它维度派生"或"未登记来源"
    """
    m = meta()
    dims = q(c, "SELECT * FROM dimension WHERE status='active' "
                "ORDER BY group_id, sort_order")
    # 字段登记状态：维度必须同时是 field_catalog 里的一个字段，才算"字段结构化存储"。
    # 这是最初设计的形态（docs/01 §239 + docs/09 §16）：主数据留类型化表，
    # 长尾维度登记成字段、值落 field_value。注册表只负责**匹配语义**，不充当存储。
    #
    # 为什么用 `dim_field_id(group_id, dimension_id)` 反查、而不是直接读
    # `dimension.person_field_id`：
    #   `person_field_id` 指的是**物理落点**字段（如 education_record.degree_level
    #   对应的 F_EDU_DEGREE_LEVEL），它登记在**别的实体**名下（education_record），
    #   所以按 entity_id='person' 查永远查不到 → 页面上 50 个维度全显示"未登记"。
    #   014 迁移为每个维度登记了 person 侧的 F_PSN_* 字段，并留下这个函数做确定性反查。
    #   这里是**同一个事实的两种问法**，必须问对那一种，否则页面会否定自己已完成的工作。
    reg = {r["field_id"]: r for r in q(c, """
        SELECT f.field_id, f.title, f.data_type, f.cardinality, f.code_table_id,
               f.allow_custom, f.status, f.related_fields,
               (SELECT count(*) FROM field_value v
                 WHERE v.field_id = f.field_id AND v.subject_id = %s) AS n_values
          FROM field_catalog f WHERE f.entity_id = 'person'""", (pid,))}
    out = []
    for d in dims:
        loc = (d["person_locator"] or "").strip()
        fid = (d["person_field_id"] or "").strip()
        # person 侧的登记字段：优先用约定函数反查，查不到再退回物理落点字段名
        # （q1 返回的是标量，不是行字典 —— 这里踩过一次 'str' has no attribute 'get'）
        try:
            pfid = q1(c, "SELECT dim_field_id(%s, %s)",
                      (d["group_id"], d["dimension_id"])) or fid
        except psycopg.Error:
            pfid = fid
        fld = reg.get(pfid) if pfid else None
        val, note, codes = None, "", []
        try:
            if loc.startswith("preference:"):
                pt = loc.split(":", 1)[1].strip()
                rows = q(c, """SELECT value_code, value_raw, weight FROM preference
                                WHERE person_id=%s AND pref_type=%s
                                ORDER BY weight DESC NULLS LAST LIMIT 3""", (pid, pt))
                if rows:
                    codes = [r["value_code"] or r["value_raw"] for r in rows]
            elif loc.startswith("concept:"):
                kx = loc.split(":", 1)[1].strip()
                rows = q(c, """SELECT c.preferred_label FROM skill_assertion s
                                 JOIN concept c ON c.concept_id = s.concept_id
                                WHERE s.person_id=%s AND c.concept_id LIKE %s
                                ORDER BY s.level DESC NULLS LAST LIMIT 3""",
                         (pid, kx + "%"))
                if rows:
                    val = "、".join(r["preferred_label"] for r in rows)
            elif loc.startswith("derived:"):
                # 派生口径**必须与匹配引擎共用同一套实现**。
                # 这里曾经只写一句"由其它维度派生（…）"就完事，于是出现
                # "匹配引擎已经用上了工作年限、页面上却写着无记录"的两套口径 ——
                # 页面是给人看建模的地方，它说没有，人就以为没有。
                # 所以直接调 code/analytics/vector.py 的 DERIVE_PERSON，
                # 取数说明也用那条规则自己给的文字（含实际算出的年数）。
                pay, note = _derived_person_payload(c, pid, d)
                if pay:
                    codes = _payload_to_codes(c, d, pay)
                    note = note or "由派生规则算出"
            elif not loc or loc == "unavailable":
                # 没有类型化落点 → 按设计应当落在统一值表 field_value（零 DDL）
                if fld and fld["n_values"]:
                    rows = q(c, """SELECT coalesce(value_code, value_codes::text, value_text,
                                                 value_num::text) AS v
                                     FROM field_value WHERE field_id=%s AND subject_id=%s
                                    ORDER BY array_index NULLS FIRST LIMIT 5""", (pfid, pid))
                    codes = [r["v"] for r in rows if r["v"] not in (None, "")]
                    # 取数说明必须跟着**这个主体**的真实取值走。
                    # fld["n_values"] 是**全库**该字段的行数（任何人的值都算），
                    # 拿它当"本人有值"的证据会写出"未覆盖 但来自统一值表"这种自相矛盾的说明
                    # —— 真实案例（三个真人）一上页就露出来了。
                    note = ("来自统一值表 field_value" if codes
                            else "已登记为字段，但本人没有值")
                elif fld:
                    note = "已登记为字段，但还没有值"
                else:
                    note = "未登记为字段（不在字典体系中）"
            elif "." in loc:
                tbl, col = (x.strip() for x in loc.split(".", 1))
                if tbl not in m["by_name"]:
                    note = "来源表不在系统目录中"
                elif col not in [x["column_name"] for x in m["cols"].get(tbl, [])]:
                    note = "来源列不在系统目录中"
                else:
                    # 找该表指向 person 的外键列（不假设一定叫 person_id）
                    fk = m["fk_cols"].get(tbl, {})
                    key = next((k for k, v in fk.items() if v[0] == "person"), None)
                    if not key:
                        note = "该表没有指向 person 的外键"
                    else:
                        rows = q(c, sql.SQL("SELECT {}::text AS v FROM mt.{} WHERE {} = %s LIMIT 3")
                                 .format(sql.Identifier(col), sql.Identifier(tbl),
                                         sql.Identifier(key)), (pid,))
                        vals = [r["v"] for r in rows if r["v"] not in (None, "")]
                        if vals:
                            codes = vals
            else:
                note = "无法解析的 locator"
        except psycopg.Error as e:
            note = "读取失败：" + str(e).splitlines()[0][:48]

        # ---- 取值规范化：这几种脏形态都是实测踩出来的 ----
        #  · text[] 的文本形态是 '{a,b}'，要拆开；'{}' 是空数组，等于"无记录"
        #  · boolean 要翻成 是/否，否则页面上写着 true
        #  · 多行取值要去重（同一张表里这个人可能有多条记录）
        if codes:
            flat = []
            for x in codes:
                if x is None:
                    continue
                s = str(x).strip()
                if s in ("", "{}", "[]", "NULL"):
                    continue
                if s.startswith("{") and s.endswith("}"):
                    flat.extend([p.strip().strip('"') for p in s[1:-1].split(",") if p.strip()])
                else:
                    flat.append(s)
            seen, uniq = set(), []
            for x in flat:
                if x not in seen:
                    seen.add(x)
                    uniq.append(x)
            codes = uniq
        if not codes:
            codes = []
        # 码表翻译；某个值不在词表里就原样显示，不假装翻译成功
        if codes:
            ct = d["code_table_id"]
            shown = []
            for x in codes:
                # 布尔列 ::text 出来是 'true'/'false'。像 DIM_IS_CLINICAL_MAJOR 这种
                # 码表是 CT_YES_NO（码是 Y/N），拿 'true' 去查必然查不到 —— 所以
                # 布尔先翻译成中文，不要指望码表能覆盖它。
                if x in ("true", "false"):
                    shown.append("是" if x == "true" else "否")
                else:
                    shown.append(lbl(ct, x) if ct else x)
            val = "、".join(shown)
        # ordinal/range 维度但取到的是**原始数值**（如 birth_year、例数、等级）：
        # 注册表只写了"来源列"，没写"怎么把原值变成档位"。不臆造映射，如实标注。
        if val and d["kind"] in ("ordinal", "range") and d["code_table_id"]:
            if not any(lbl(d["code_table_id"], x) != x for x in codes):
                note = (note + " " if note else "") + \
                    "取到的是原始值，档位映射在注册表中未定义（见 docs/14 向量组装层）"
        out.append({"group": d["group_title"], "title": d["title_zh"],
                    "kind": d["kind"], "role": d["role"],
                    "code_table": d["code_table_id"],
                    "value": val, "note": note,
                    "raw": "、".join(codes) if codes else None,
                    "allow_custom": d["allow_custom"],
                    "field_id": pfid,          # person 侧登记字段（014 起的 F_PSN_*）
                    "landing_field": fid,      # 物理落点字段（可能登记在别的实体下）
                    "registered": bool(fld),
                    "field_values": (fld or {}).get("n_values", 0),
                    "job": (d["job_locator"] or "")[:40]})
    return out


def view_talent(c, qs) -> bytes:
    where, P = talent_where(qs)
    pg = _int_param(qs, "page", 1, lo=1)
    size = 25
    total = q1(c, "SELECT count(*) AS n FROM person p" + where, P)

    # ---- 列选择：注册表决定能看什么，URL 决定这次看什么 ----
    cols = talent_pick_columns(qs)
    colmap = {x["key"]: x for x in TALENT_COLUMNS}
    picked = [colmap[k] for k in cols if k in colmap]
    # 所有列都起 AS "key" 别名，这样注册表的 key 就是行字典的键，两边不会错位
    sel_list = ", ".join('%s AS "%s"' % (x["sel"], x["key"]) for x in picked)
    order = talent_order(qs, colmap)
    P2 = dict(P, lim=size, off=(pg - 1) * size)
    rows = q(c, "SELECT %s FROM person p%s ORDER BY %s LIMIT %%(lim)s OFFSET %%(off)s"
             % (sel_list, where, order), P2)

    n_pages = max(1, (total + size - 1) // size)

    def lqs(**kw):
        """本地链接编码：等价于 qs_encode + HTML 转义（& 要写成 &amp; 才是合法 HTML）。"""
        return html.escape(qs_encode(qs, **kw), quote=True)

    # 表头：每一列都可点排序；当前排序列显示方向
    cur_sort = (qs.get("sort", [""])[0] or "")
    cur_dir = qs.get("dir", ["asc"])[0]
    head = "".join(
        '<th class="%s"><a href="/talent?%s" style="color:inherit">%s%s</a></th>'
        % ("n" if x["fmt"] == "num" else "",
           lqs(sort=x["key"],
               dir=("asc" if (cur_sort == x["key"] and cur_dir == "desc") else "desc")),
           esc(x["title"]),
           (" ↓" if cur_sort == x["key"] and cur_dir == "desc"
            else (" ↑" if cur_sort == x["key"] else "")))
        for x in picked)
    body_rows = "".join(
        "<tr>%s</tr>" % "".join(
            '<td class="%s">%s</td>' % ("n" if x["fmt"] == "num" else "",
                                        talent_cell(x, r))
            for x in picked)
        for r in rows)
    if not rows:
        body_rows = ('<tr><td colspan="%d" class="muted" style="padding:14px">'
                     '没有符合条件的人才。</td></tr>' % max(len(picked), 1))

    # 列选择器：按分组排布复选框。纯 GET 表单 —— 零 JS，也不需要写权限。
    # ---- 字段选择器 ----
    # 用原生 <details> 收起：35 个复选框常驻铺开会把人才清单挤出首屏（见 CSS 注释）。
    # 已经有显式 cols / pick 时默认展开——用户正在管字段，不该每次重新点开。
    picker_open = bool(talent_col_keys(qs)) or ((qs.get("pick", [""])[0] or "") == "1")
    view_name = talent_view_name(qs)
    picked_set = set(cols)
    group_names = list(dict.fromkeys(x["group"] for x in TALENT_COLUMNS))
    boxes = []
    for g in group_names:
        gk = [x["key"] for x in TALENT_COLUMNS if x["group"] == g]
        on_g = [k for k in gk if k in picked_set]
        labels = []
        for x in TALENT_COLUMNS:
            if x["group"] != g:
                continue
            is_on = x["key"] in picked_set
            dis = ' disabled title="行的身份，不能隐藏"' if x["key"] == "pid" else ""
            tip = (' title="%s"' % esc(x["hint"])) if x.get("hint") else ""
            labels.append(
                '<label class="%s"%s><input type="checkbox" name="cols" value="%s"%s%s>%s</label>'
                % ("on" if is_on else "off", tip, esc(x["key"]),
                   " checked" if is_on else "", dis, esc(x["title"])))
        # 组级操作做成**链接**而不是 checkbox：一个链接就能表达"整组替换"，
        # 不需要 JS，也不会和提交按钮的语义打架。顺序按注册表排，保持稳定。
        keep = [k for k in cols if k not in gk]
        ops = ('<a href="/talent?%s">全选本组</a>'
               % lqs(cols=keep + gk, pick="1", preset=None))
        if on_g and len(on_g) < len(gk):
            ops += ' · <a href="/talent?%s">清空本组</a>' % lqs(cols=keep, pick="1", preset=None)
        boxes.append(
            '<div class="colgrp"><div class="colgrphd"><b>%s</b>'
            '<span class="cgn">%d / %d</span><span class="cgop">%s</span></div>'
            '<div class="colpick">%s</div></div>'
            % (esc(g), len(on_g), len(gk), ops, "".join(labels)))
    # 切预设必须**丢掉 cols**：预设的定义就是"换一套字段"，
    # 带着旧 cols 去点预设，等于让视图参数被旧值覆盖（这正是改版前的死法）。
    preset_btns = "".join(
        '<a class="btnlink%s" href="/talent?%s">%s</a>'
        % (" on" if n == view_name else "", lqs(preset=n, cols=None, pick=None), esc(n))
        for n in ("精简", "推荐", "教育", "能力", "成果", "匹配", "全部"))
    preset_btns += ('<a class="btnlink%s" href="/talent?%s">仅 ID</a>'
                    % (" on" if view_name == "仅 ID" else "",
                       lqs(cols="pid", pick="1", preset=None)))
    # 筛选表单要带上排序状态：不带的话，按某列排序后再改筛选条件，排序就悄悄丢了。
    keep_hidden = "".join(
        '<input type="hidden" name="%s" value="%s">' % (esc(k), esc((qs.get(k, [""])[0] or "")))
        for k in ("sort", "dir") if (qs.get(k, [""])[0] or ""))
    # 字段名的白名单是安全属性，不是体验问题：能显示什么由**列注册表**决定，
    # 谁也不能靠改 URL 让页面去查一个没登记的表达式。被挡掉的要说出来，不能静默吞掉。
    known_keys = {x["key"] for x in TALENT_COLUMNS}
    unknown_cols = [k for k in talent_col_keys(qs) if k not in known_keys]
    col_note = ""
    if unknown_cols:
        col_note = ('<div class="note err"><b>有 %d 个字段名不在列注册表里，已忽略：</b>'
                    '<code>%s</code>。页面能显示哪些列由注册表决定，不由 URL 决定。</div>'
                    % (len(unknown_cols), esc("、".join(unknown_cols))))
    elif picker_open and cols == ["pid"]:
        col_note = ('<div class="note info">一个字段都没勾选，已只保留 <code>person_id</code>'
                    '——它是行的身份，不允许被隐藏。</div>')

    degs = q(c, "SELECT degree_level AS v, count(DISTINCT person_id) AS n "
                "FROM education_record GROUP BY 1 ORDER BY 2 DESC")
    majors = q(c, "SELECT major_raw AS v, count(DISTINCT person_id) AS n "
                  "FROM education_record WHERE major_raw IS NOT NULL "
                  "GROUP BY 1 ORDER BY 2 DESC LIMIT 20")
    provs = q(c, "SELECT hukou_province AS v, count(*) AS n FROM person_demographics "
                 "WHERE hukou_province IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 20")

    def sel(name, cur, opts, any_label):
        o = ['<option value="">%s</option>' % any_label] + [
            '<option value="%s" %s>%s（%d）</option>'
            % (esc(x["v"]), "selected" if x["v"] == cur else "",
               esc(lbl("CT_DEGREE_LEVEL", x["v"]) if name == "degree" else x["v"]), x["n"])
            for x in opts]
        return "".join(o)

    kpi = [
        metric(f"{q1(c, 'SELECT count(*) FROM person'):,}", "人才档案"),
        metric(f"{q1(c, 'SELECT count(DISTINCT person_id) FROM education_record'):,}", "有教育记录"),
        metric(f"{q1(c, 'SELECT count(DISTINCT person_id) FROM employment_record'):,}", "有工作经历"),
        metric(f"{q1(c, 'SELECT count(*) FROM skill_assertion'):,}", "能力主张"),
        metric(f"{q1(c, 'SELECT count(*) FROM evidence'):,}", "证据"),
        metric(f"{q1(c, 'SELECT count(DISTINCT person_id) FROM match_result'):,}", "有匹配结果"),
    ]
    n_syn = q1(c, "SELECT count(*) FROM person "
                  "WHERE quality_flags && ARRAY['synthetic_fixture']")
    n_all = q1(c, "SELECT count(*) FROM person")
    syn_note = ""
    if n_syn:
        syn_note = ('<div class="note info"><b>数据来源声明：</b>%d 份档案带 '
                    '<code>quality_flags = {synthetic_fixture}</code> 标记，是 '
                    '<b>合成数据</b>（<code>per_mock_%%</code> 前缀、'
                    '<code>subject_code</code> 形如 <code>MT-MOCK-####</code>、'
                    '证据 URI 用 <code>.invalid</code> 保留域、DOI 用合成号段、'
                    '机构名带"虚构"后缀），<b>不对应任何真实个人</b>。'
                    '其余 %d 份是早期演示档案。<br>'
                    '这些标记不是装饰：一句话就能筛出哪些行可以对外、哪些不行——'
                    '成熟的数据集必须能自己说清"哪些是真数据"。</div>'
                    % (n_syn, n_all - n_syn))
    body = """
<div class="sub">人才库（供给端）。列表可筛选，下面 6 个分析全部实时查库，
每个都给出它执行的 SQL——口径可核，不是截图。</div>
%(syn_note)s
<div class="cards">%(kpi)s</div>

<div class="card"><form method="get" action="/talent">
  <input type="hidden" name="pick" value="1">%(hidden)s
  <div class="row">
    <div style="flex:2 1 180px"><label>学历层次</label><select name="degree">%(deg)s</select></div>
    <div style="flex:2 1 180px"><label>专业方向（选了就按它模糊匹配）</label><select name="major">%(maj)s</select></div>
    <div style="flex:2 1 180px"><label>户籍城市</label><select name="province">%(pv)s</select></div>
    <div style="flex:2 1 200px"><label>能力（概念名或 ID）</label>
      <input name="skill" value="%(skill)s" placeholder="如 临床诊疗 / CON-K1-CLIN"></div>
  </div>

  <details class="pickerbox"%(open)s>
    <summary>显示字段 <b>%(npick)d</b> / %(nall)d · %(ngrp)d 组
      · 当前视图 <span class="pill p-v">%(view)s</span>
      <span class="muted">展开勾选，然后点「筛选 / 应用」</span></summary>
    <div class="colgrps">%(boxes)s</div>
    <p class="muted" style="padding:0 12px 10px;margin:0">
      预设按钮直接换一套字段（换预设会丢掉手改的勾选，这是刻意的——预设的定义就是"一套字段"）。
      <b>全选本组 / 清空本组</b>是链接，点一下立即生效。
      <b>表格窗口可左右滑动</b>，表头吸顶、首列吸左——宽表靠这两个 sticky 才读得下去。
      勾选状态跟着 URL 走（零 JS）。</p>
  </details>

  <div class="row" style="margin-top:10px">
    <div style="flex:0 0 120px"><button type="submit">筛选 / 应用</button></div>
    <div style="flex:0 0 100px"><a class="btnlink sec" href="/talent">重置</a></div>
    <div class="btnrow" style="flex:1 1 auto">%(presets)s</div>
  </div>
</form></div>

<div class="card"><h2>人才清单 <span class="muted">· 命中 %(total)s 人，第 %(pg)d / %(npages)d 页 · %(ncol)d 列</span>
<span style="float:right"><a href="/talent.csv?%(csvqs)s">下载 CSV（当前 %(ncol)d 列）</a></span></h2>
%(col_note)s<div class="tablewin"><table>
<thead><tr>%(head)s</tr></thead>
<tbody>%(body)s</tbody></table></div>
<div class="pager" style="margin-top:10px">%(pager)s</div>
<p class="muted">灰色斜体 <span class="nul">NULL</span> 表示该字段为空——这是"没有记录"，
不是"值为 0"。<b>数据来源</b>列标出哪些行是合成数据；合成数据不对应任何真实个人。<br>
<b>两处已知口径债</b>（不藏起来，因为字段筛选会把它直接摆到你面前）：
① <code>person_demographics.hukou_province</code> 里实际存的是<b>城市名</b>（实测 18 个去重值
全是城市），本页按实际内容标为「户籍城市」，列名迁移在路线图里；
② <code>CT_CEL_LEVEL</code> 的标签是<b>类别名</b>（E4 = 履职记录），读不出等级高低，
所以「最高证据等级」连码一起显示。两处都在鼠标悬停时有说明。</p></div>

%(analyses)s
%(tail)s
""" % dict(syn_note=syn_note, kpi="".join(kpi),
           hidden=keep_hidden,
           deg=sel("degree", qs.get("degree", [""])[0], degs, "全部学历"),
           maj=sel("major", qs.get("major", [""])[0], majors, "全部专业"),
           pv=sel("province", qs.get("province", [""])[0], provs, "全部省份"),
           skill=esc(qs.get("skill", [""])[0]),
           open=" open" if picker_open else "",
           npick=len(picked), nall=len(TALENT_COLUMNS), ngrp=len(group_names),
           view=esc(view_name), boxes="".join(boxes), presets=preset_btns,
           total=total, pg=pg, npages=n_pages, ncol=len(picked),
           csvqs=lqs(cols=",".join(cols), pick=None),
           col_note=col_note,
           head=head, body=body_rows,
           pager=pager_links("/talent", qs, pg, n_pages),
           analyses=analysis_panel([mini_analysis(c, t, s) for t, s in TALENT_ANALYSES],
                                   "分析这个人才库",
                                   "6 个分析实时查库，每个都给出它执行的 SQL。"
                                   "它们说的是**整库**口径，不受上面的筛选与字段选择影响——"
                                   "筛选改变的是「你看哪些行」，分析回答的是「这批人整体什么样」。"),
           tail="")
    return page("人才库", body, subtitle="供给端 · 浏览 + 在线分析")


TALENT_TABLES = [
    ("基本信息", "person", "person_id"),
    ("人口学", "person_demographics", "person_id"),
    ("教育经历", "education_record", "person_id"),
    ("工作经历", "employment_record", "person_id"),
    ("临床经历", "clinical_exposure", "person_id"),
    ("资质证书", "credential", "person_id"),
    ("项目经历", "project_record", "person_id"),
    ("科研产出", "research_output", "person_id"),
    ("获奖荣誉", "award_honor", "person_id"),
    ("偏好", "preference", "person_id"),
    ("观测窗口", "observation_window", "person_id"),
]


def view_talent_one(c, pid, qs) -> bytes:
    who = q(c, "SELECT * FROM person WHERE person_id=%s", (pid,))
    if not who:
        raise PortalError(404, "人才档案不存在：%s" % pid)
    w = who[0]
    kv = "".join('<div>%s</div><div>%s</div>' % (esc(k), cell_html("person", k, v, 300))
                 for k, v in w.items())

    blocks = []
    for title, tbl, col in TALENT_TABLES[1:]:
        rows = q(c, sql.SQL("SELECT * FROM {}.{} WHERE {} = %s LIMIT 20").format(
            sql.Identifier(SCHEMA), sql.Identifier(tbl), sql.Identifier(col)), (pid,))
        n = q1(c, sql.SQL("SELECT count(*) AS n FROM {}.{} WHERE {} = %s").format(
            sql.Identifier(SCHEMA), sql.Identifier(tbl), sql.Identifier(col)), (pid,))
        if not n:
            continue
        cols = [x["column_name"] for x in meta()["cols"].get(tbl, [])][:12]
        blocks.append(
            '<div class="card"><h2>%s <span class="muted">· %d 条</span>'
            '<span style="float:right"><a href="/t/%s?col=%s&val=%s">全部 →</a></span></h2>%s</div>'
            % (esc(title), n, tbl, col, urllib.parse.quote(pid),
               render_rows(tbl, cols, rows, maxlen=46)))

    # 能力主张：这是"供给"的核心，必须带证据与可迁移性
    sk = q(c, """
        SELECT s.assertion_id, c.preferred_label AS 能力, c.concept_type AS 类型,
               s.level AS 熟练度, s.claim_type AS 主张类型, s.transferability AS 可迁移性,
               s.transfer_note AS 迁移说明, s.confidence AS 置信度,
               s.verify_status AS 核验, e.cel_level AS 证据等级, e.uri AS 证据链接
          FROM skill_assertion s LEFT JOIN concept c ON c.concept_id = s.concept_id
          LEFT JOIN evidence e ON e.evidence_id = s.evidence_id
         WHERE s.person_id = %s ORDER BY s.transferability DESC NULLS LAST LIMIT 60""", (pid,))
    for r in sk:
        r["主张类型"] = lbl("CT_CLAIM_TYPE", r["主张类型"])
        r["核验"] = lbl("CT_VERIFY_STATUS", r["核验"])
    sk_html = render_rows("skill_assertion", list(sk[0].keys()), sk, maxlen=44) if sk \
        else '<p class="muted">这个人还没有能力主张记录。</p>'

    # 匹配结果：met / gap / unknown 三态
    mm = q(c, """
        SELECT m.rank AS 排名, jp.title_raw AS 岗位, jp.job_family AS 岗位族,
               m.score_total AS 得分,
               (m.score_breakdown->>'met') AS 命中,
               (m.score_breakdown->>'gap') AS 缺口,
               (m.score_breakdown->>'unknown') AS 未知,
               m.explanation AS 解释
          FROM match_result m LEFT JOIN job_posting jp ON jp.job_id = m.target_id
         WHERE m.person_id = %s ORDER BY m.rank LIMIT 20""", (pid,))
    mm_html = render_rows(None, list(mm[0].keys()), mm, maxlen=40) if mm \
        else '<p class="muted">还没有匹配结果。可在开发者模式运行匹配，或到 <a href="/match">匹配</a> 页看已有结果。</p>'

    wins = q(c, """SELECT window_type AS 窗口类型, start_date AS 开始, end_date AS 结束,
                          coverage_note AS 覆盖说明
                     FROM observation_window WHERE person_id=%s""", (pid,))
    win_html = render_rows(None, list(wins[0].keys()), wins, maxlen=60) if wins else \
        ('<p class="muted">没有登记观测窗口。按本库纪律：<b>窗口外的"无记录"不等于"不具备"</b>，'
         '匹配引擎在判定缺口前必须先查窗口。</p>')

    body = """
<div class="sub">一个人的完整档案 + 能力主张（带证据与可迁移性）+ 匹配结果。
这是"供给端"的实体页。</div>
<div class="card"><h2>基本信息</h2><div class="kv">%s</div></div>
<div class="card"><h2>能力主张 <span class="muted">· %d 条</span></h2>%s
<p class="muted">可迁移性 ≥3 的行必须写迁移说明（CHECK 约束强制）；
置信度 0.50 的是<a href="/t/evidence">无证据自述</a>，封顶值。</p></div>
<div class="card"><h2>匹配结果 <span class="muted">· %d 条</span></h2>%s
<p class="muted"><b>未知（unknown）不是不合格</b>：它表示"没有观测到"，</p></div>
<div class="card"><h2>观测窗口</h2>%s</div>
%s
""" % (kv, len(sk), sk_html, len(mm), mm_html, win_html,
       "".join(blocks))
    # ---- 画像向量：按维度注册表逐条列出这个人的建模 ----
    dv = person_dimensions(c, pid)
    n_have = len([x for x in dv if x["value"]])
    n_gate = len([x for x in dv if x["role"] == "gate"])
    vec_rows, cur_group = [], None
    n_reg = len([x for x in dv if x["registered"]])
    for x in dv:
        if x["group"] != cur_group:
            cur_group = x["group"]
            vec_rows.append('<tr><td colspan="5" style="background:#f6f8fa;'
                            'font-weight:600;color:#424a53">%s</td></tr>' % esc(cur_group))
        if x["value"]:
            cell = '<b>%s</b>' % esc(trunc(x["value"], 42))
            if x["raw"] and x["raw"] != x["value"]:
                cell += ' <span class="muted">（%s）</span>' % esc(trunc(x["raw"], 26))
        else:
            cell = '<span class="nul">无记录</span>'
        role_txt = {"gate": '<span class="pill p-red">硬门槛</span>',
                    "score": '<span class="pill p-v">参与打分</span>',
                    "modifier": '<span class="pill p-e">修正项</span>',
                    "display": '<span class="pill p-n">仅展示</span>'}.get(x["role"], x["role"])
        fld_txt = ('<code>%s</code>' % esc(x["field_id"])) if x["registered"] else \
            '<span class="pill p-red">未登记</span>'
        vec_rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                        '<td class="muted">%s</td></tr>'
                        % (esc(x["title"]), cell, role_txt, fld_txt,
                           esc(x["note"] or ("可自写补充" if x["allow_custom"] else ""))))
    vec_block = ('<div class="card"><h2>画像向量 '
                 '<span class="muted">· %d 个维度：已有取值 %d 个（%.0f%%）· '
                 '已登记为字段 %d 个 · 硬门槛 %d 个</span></h2>'
                 '<div class="sub">按 <code>mt.dimension</code> 读维度清单、按 '
                 '<code>field_catalog</code> 判断该维度是否已<b>登记为字段</b>。'
                 '设计上：主数据维度留类型化表（要约束与索引），长尾维度登记成字段、'
                 '值落统一值表 <code>field_value</code>（零 DDL）——'
                 '<b>注册表只负责匹配语义，不充当存储</b>。'
                 '新增一个维度只要往注册表插一行 + 往字段目录插一行，这里自动出现。</div>'
                 '<div class="tscroll"><table>'
                 '<thead><tr><th>维度</th><th>取值</th><th>匹配角色</th>'
                 '<th>字段登记</th><th>说明</th></tr></thead>'
                 '<tbody>%s</tbody></table></div>'
                 '<p class="muted">「无记录」<b>不等于</b>「不具备」——这是本库的三态纪律：'
                 '空值只说明没有观测到，不参与扣分。</p></div>'
                 % (len(dv), n_have, 100.0 * n_have / max(len(dv), 1), n_reg, n_gate,
                    "".join(vec_rows)))

    # 「画像向量」放在**基本信息之后、明细之前**：它是这一页的主角。
    # 放到页面底部等于没做——用户要看的正是"这个样本被建成了什么样"。
    body = body.replace('<div class="card"><h2>能力主张',
                        vec_block + '<div class="card"><h2>能力主张', 1)
    body += analysis_panel([
        # 用**绑定参数**而不是把 pid 拼进 SQL（原来写的是 .format(pid=pid)）。
        # 页面下方会把 SQL 原样展示给用户复核，所以显示成 %s 反而更诚实：
        # 读者看到的是真正执行的参数化语句，而不是一个已经填好值的字符串。
        mini_analysis(c, "这个人的能力分布 vs 全库", """
            SELECT c.preferred_label AS "能力", s.level AS "熟练度",
                   s.transferability AS "可迁移性",
                   (SELECT count(*) FROM skill_assertion x
                     WHERE x.concept_id = s.concept_id) AS "全库具备人数"
              FROM skill_assertion s JOIN concept c ON c.concept_id = s.concept_id
             WHERE s.person_id = %s ORDER BY 4 DESC LIMIT 15""", params=(pid,)),
    ], "分析这份档案",
        "看这个人的能力里哪些是「稀缺」的——全库具备人数越少越稀缺。"
        "这比单纯列出能力更有用：能立刻看出他靠什么区别于其他人。")
    return page(pid, body, subtitle="人才档案 · 供给端实体页", here="人才库",
                crumbs=[("/talent", "人才库")])


# ---------------------------------------------------------------------------
# ⑪ 职业库：浏览 + 在线分析
# ---------------------------------------------------------------------------
def view_occupations(c, qs) -> bytes:
    fam = qs.get("family", [""])[0]
    lvl = qs.get("level", [""])[0]
    cond, P = ["o.status = 'active'"], {}
    if fam:
        cond.append("o.family = %(fam)s")
        P["fam"] = fam
    if lvl:
        cond.append("o.level = %(lvl)s")
        P["lvl"] = _int_param(qs, "level", 0)
    where = " WHERE " + " AND ".join(cond)
    rows = q(c, """
        SELECT o.occupation_id, o.label_zh, o.level, o.family,
               o.medical_reliance AS med, o.transition_ease AS ease,
               o.code_status, o.degree_typical,
               (SELECT count(*) FROM job_posting j WHERE j.occupation_id = o.occupation_id) AS jds,
               (SELECT count(*) FROM job_competency_weight w
                 WHERE w.occupation_id = o.occupation_id AND w.valid_to IS NULL) AS comps
          FROM occupation o%s ORDER BY o.level, o.family, o.occupation_id LIMIT 400""" % where, P)

    body_rows = "".join(
        '<tr><td><a href="/occupation/%s"><code>%s</code></a></td><td>%s</td>'
        '<td class="n">%s</td><td>%s</td><td class="n">%s</td><td class="n">%s</td>'
        '<td class="n">%d</td><td class="n">%d</td><td>%s</td></tr>'
        % (urllib.parse.quote(r["occupation_id"]), esc(r["occupation_id"]),
           esc(r["label_zh"]), r["level"], esc(lbl("CT_JOB_FAMILY", r["family"])),
           r["med"] if r["med"] is not None else "—",
           r["ease"] if r["ease"] is not None else "—",
           r["jds"], r["comps"], esc(lbl("CT_CODE_STATUS", r["code_status"])))
        for r in rows)

    fams = q(c, """SELECT family AS v, count(*) AS n FROM occupation
                   WHERE status='active' AND family IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
    fam_opts = "".join(['<option value="">全部岗位族</option>'] + [
        '<option value="%s" %s>%s（%d）</option>'
        % (esc(x["v"]), "selected" if x["v"] == fam else "",
           esc(lbl("CT_JOB_FAMILY", x["v"])), x["n"]) for x in fams])
    lvl_opts = "".join(['<option value="">全部层级</option>'] + [
        '<option value="%d" %s>%d 级（%d 个）</option>'
        % (i, "selected" if str(i) == lvl else "", i,
           q1(c, "SELECT count(*) FROM occupation WHERE level=%s AND status='active'", (i,)))
        for i in (1, 2, 3)])

    AN = [
        ("岗位族规模（JD / 职业 / 雇主）", """
         SELECT jp.job_family AS "岗位族码", count(*) AS "JD 数",
                count(DISTINCT jp.occupation_id) AS "覆盖职业",
                count(DISTINCT jp.employer_name_raw) AS "雇主数",
                min(jp.salary_min) AS "薪资下限", max(jp.salary_max) AS "薪资上限"
           FROM job_posting jp WHERE jp.verify_status <> 'V4'
          GROUP BY 1 ORDER BY 2 DESC"""),
        ("各岗位族最看重的能力（Top 1）", """
         SELECT DISTINCT ON (o.family) o.family AS "岗位族码",
                c.preferred_label AS "最被要求的能力",
                round(w.importance, 3) AS "重要性",
                w.essentiality AS "必需性", w.sample_size AS "样本"
           FROM job_competency_weight w
           JOIN occupation o ON o.occupation_id = w.occupation_id
           JOIN concept c ON c.concept_id = w.concept_id
          WHERE w.valid_to IS NULL
          ORDER BY o.family, w.importance DESC"""),
        ("医学依赖度 × 转出容易度", """
         SELECT medical_reliance AS "医学依赖度", transition_ease AS "转出容易度",
                count(*) AS "职业数"
           FROM occupation WHERE status='active' AND level=3
          GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC"""),
        ("外部职业码核验进度", """
         SELECT code_status AS "核验状态", count(*) AS "节点数",
                count(DISTINCT family) AS "涉及岗位族"
           FROM occupation WHERE status='active' GROUP BY 1 ORDER BY 2 DESC"""),
        ("职业树的层级结构", """
         SELECT level AS "层级", count(*) AS "节点数",
                count(DISTINCT family) AS "覆盖岗位族",
                count(*) FILTER (WHERE status <> 'active') AS "已退役"
           FROM occupation GROUP BY 1 ORDER BY 1"""),
        ("准入路径与资质要求", """
         SELECT coalesce(array_to_string(license_required, '+'), '（无强制资质）') AS "资质要求",
                count(*) AS "职业数"
           FROM occupation WHERE status='active' AND level=3
          GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),
    ]
    body = """
<div class="sub">职业库（需求端）：职业树 151 个节点 + 690 份结构化 JD + 2970 条要求。
下面 6 个分析实时查库，每个都给出 SQL。</div>
<div class="cards">%s</div>
<div class="card"><form method="get" action="/occupations" class="row">
  <div><label>岗位族</label><select name="family">%s</select></div>
  <div><label>层级</label><select name="level">%s</select></div>
  <div style="flex:0 0 100px"><button type="submit">筛选</button></div>
  <div style="flex:0 0 100px"><a href="/occupations"><button type="button" class="sec">重置</button></a></div>
</form></div>
<div class="card"><h2>职业节点 <span class="muted">· %d 个</span>
<span style="float:right"><a href="/tree">用树形浏览 →</a></span></h2>
<table><tr><th>occupation_id</th><th>名称</th><th>层级</th><th>岗位族</th>
<th class="n">医学依赖</th><th class="n">转出容易</th><th class="n">JD</th>
<th class="n">能力项</th><th>外部码</th></tr>%s</table></div>
%s
""" % ("".join([
        metric(f"{q1(c, 'SELECT count(*) FROM occupation WHERE status=\'active\''):,}", "在用节点"),
        metric(f"{q1(c, 'SELECT count(DISTINCT family) FROM occupation WHERE level=1'):,}", "岗位族"),
        metric(f"{q1(c, 'SELECT count(*) FROM job_posting'):,}", "结构化 JD"),
        metric(f"{q1(c, 'SELECT count(*) FROM job_requirement'):,}", "要求条目"),
        metric(f"{q1(c, 'SELECT count(*) FROM v_competency_current'):,}", "当前能力权重"),
        metric(f"{q1(c, 'SELECT count(*) FROM concept'):,}", "能力概念"),
    ]), fam_opts, lvl_opts, len(rows), body_rows,
        analysis_panel([mini_analysis(c, t, s) for t, s in AN],
                       "分析这个职业库",
                       "6 个分析实时查库，每个都给出它执行的 SQL。"))
    return page("职业库", body, subtitle="需求端 · 浏览 + 在线分析")


def view_occupation_one(c, oid, qs) -> bytes:
    o = q(c, "SELECT * FROM occupation WHERE occupation_id=%s", (oid,))
    if not o:
        raise PortalError(404, "职业节点不存在：%s" % oid)
    o = o[0]
    kv = "".join('<div>%s</div><div>%s</div>' % (esc(k), cell_html("occupation", k, v, 300))
                 for k, v in o.items())

    comps = q(c, """
        SELECT c.preferred_label AS 能力, c.concept_type AS 类型,
               w.importance AS 重要性, w.essentiality AS 必需性,
               w.sample_size AS 样本, w.demand_weight AS 需求权重,
               w.run_id AS 版本, w.period_label AS 期间
          FROM job_competency_weight w JOIN concept c ON c.concept_id = w.concept_id
         WHERE w.occupation_id = %s AND w.valid_to IS NULL
         ORDER BY w.importance DESC, w.sample_size DESC LIMIT 40""", (oid,))
    for r in comps:
        r["类型"] = lbl("CT_CONCEPT_TYPE", r["类型"])
        r["必需性"] = {"essential": "必需", "bonus": "加分"}.get(r["必需性"], r["必需性"])
    comps_html = render_rows(None, list(comps[0].keys()), comps, maxlen=40) if comps else \
        '<p class="muted">这个职业还没有能力权重（样本 &lt; 30 的组合会被跳过，避免小样本结论）。</p>'

    jds = q(c, """SELECT job_id, title_raw AS 岗位, employer_name_raw AS 雇主, city AS 城市,
                         education_req AS 学历, salary_min AS 薪资下限, salary_max AS 薪资上限
                    FROM job_posting WHERE occupation_id=%s ORDER BY job_id LIMIT 30""", (oid,))
    jds_html = render_rows("job_posting", list(jds[0].keys()), jds, maxlen=40) if jds else \
        '<p class="muted">还没有 JD 挂到这个职业节点。</p>'

    mig = q(c, """SELECT migration_id, old_id, new_id, relation, coverage, effective_from, note
                    FROM occupation_migration WHERE old_id=%s OR new_id=%s""", (oid, oid))
    mig_html = render_rows(None, list(mig[0].keys()), mig, maxlen=48) if mig else \
        ('<p class="muted">没有迁移记录。按纪律：<b>ID 不复用，只退役</b>——'
         '节点被拆分/合并后，旧 ID 通过迁移表仍然可追溯到后继节点。</p>')

    chg = q(c, """SELECT change_id, change_type, label_zh, reason, evidence_count, status,
                         created_at
                    FROM occupation_change WHERE from_ids && ARRAY[%s] OR to_ids && ARRAY[%s]
                   ORDER BY created_at DESC LIMIT 20""", (oid, oid))
    chg_html = render_rows(None, list(chg[0].keys()), chg, maxlen=40) if chg else \
        '<p class="muted">没有变更记录（职业树的生长记录，由演化层写入）。</p>'

    sub = q(c, """SELECT occupation_id, label_zh, level, status FROM occupation
                   WHERE parent_id=%s ORDER BY occupation_id""", (oid,))
    sub_html = render_rows(None, list(sub[0].keys()), sub, maxlen=40) if sub else \
        '<p class="muted">没有子节点（这是叶子节点）。</p>'

    body = """
<div class="sub">一个职业节点的全部字段 + 它要什么能力 + 它下面挂着哪些 JD +
它的演化历史。<b>每个数字都能顺着链接追到原始记录。</b></div>
<div class="card"><h2>字段</h2><div class="kv">%s</div></div>
<div class="card"><h2>能力要求 <span class="muted">· %d 项</span></h2>%s</div>
<div class="card"><h2>子节点 <span class="muted">· %d 个</span></h2>%s</div>
<div class="card"><h2>关联 JD <span class="muted">· 前 30 条</span></h2>%s</div>
<div class="grid2">
<div class="card"><h2>迁移记录</h2>%s</div>
<div class="card"><h2>变更记录</h2>%s</div>
</div>
%s
""" % (kv, len(comps), comps_html, len(sub), sub_html, jds_html, mig_html, chg_html,
       sql_box("""SELECT * FROM occupation WHERE occupation_id = '%s';
SELECT * FROM v_competency_current WHERE occupation_id = '%s';
SELECT * FROM occupation_migration WHERE old_id = '%s' OR new_id = '%s';"""
               % (oid, oid, oid, oid)))
    body += analysis_panel([
        # 用**绑定参数**（原来是把值 format 进 SQL，并手写 `.replace("'", "''")` 转义）。
        # 手写转义的问题不是"这次转错了"，而是**它依赖每个调用点都记得转义** ——
        # 而 `oid` 来自 URL。绑定参数把这件事交给驱动，不需要任何人记得。
        mini_analysis(c, "同族里其他职业要、而这个职业没要的能力", """
            SELECT c.preferred_label AS "能力",
                   count(DISTINCT w.occupation_id) AS "同族中几个职业要它",
                   round(avg(w.importance), 3) AS "平均重要性"
              FROM job_competency_weight w
              JOIN occupation o2 ON o2.occupation_id = w.occupation_id
              JOIN concept c ON c.concept_id = w.concept_id
             WHERE w.valid_to IS NULL AND o2.family = %s
               AND w.concept_id NOT IN (
                   SELECT w2.concept_id FROM job_competency_weight w2
                    WHERE w2.occupation_id = %s AND w2.valid_to IS NULL)
             GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 12""",
            params=(o["family"] or "", oid)),
    ], "分析这个职业",
        "「同族有、这个职业没要」的能力，往往正是它区别于同族其他岗位的地方——"
        "这比单看它要什么更能说明问题。")
    return page(o["label_zh"], body, subtitle="职业详情 · %s" % esc(oid), here="职业库",
                crumbs=[("/occupations", "职业库")])


# ---------------------------------------------------------------------------
# ⑫ 职业树浏览器（含 as-of 时间旅行）
# ---------------------------------------------------------------------------
def view_tree(c, qs) -> bytes:
    # `asof` 是用户输入，**必须先校验再进 SQL**。原来直接拼进 `%s::date`：
    # 传 `?asof=昨天` 会让 PostgreSQL 抛参数类型错误，而那是在**取数之后**才发生的，
    # 表现出来是"连接被重置"（连 4xx 都拿不到）—— 同一文件 `_int_param` 的说明里
    # 早写过这个现象，只是这里没有照做。
    asof = (qs.get("asof", [""])[0] or "").strip()
    if asof:
        # 用 Python 的 date.fromisoformat 校验，**不要**交给数据库去试：
        # 正则只挡形状，`2026-13-99` 这种形状对、日期错的仍会让 PostgreSQL 抛错，
        # 而那时的表现是 500 / 连接被重置，用户看不到原因（实测过）。
        import datetime as _dt
        try:
            _dt.date.fromisoformat(asof)
        except ValueError:
            asof_bad, asof = asof, ""
            return page("职业树", '<div class="note err"><b>as-of 日期不合法：</b>'
                        '<code>%s</code>。请用 <code>YYYY-MM-DD</code>（例如 '
                        '<code>2026-01-01</code>）。这里在应用层校验而不是直接丢给数据库，'
                        '是因为数据库报错会表现为 500 或连接被重置，看不到原因。</div>'
                        '<p><a href="/tree">回到当前时点</a></p>'
                        % esc(asof_bad), subtitle="时间旅行")
    if asof:
        rows = q(c, "SELECT * FROM occupation_asof(%s::date)", (asof,))
        src = "occupation_asof('%s')" % asof
    else:
        rows = q(c, "SELECT * FROM occupation WHERE status='active'")
        src = "occupation WHERE status='active'"
    node = {r["occupation_id"]: dict(r) for r in rows}
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)

    jd_n = {x["occupation_id"]: x["n"] for x in q(
        c, "SELECT occupation_id, count(*) AS n FROM job_posting "
           "WHERE occupation_id IS NOT NULL GROUP BY 1")}
    comp_n = {x["occupation_id"]: x["n"] for x in q(
        c, "SELECT occupation_id, count(*) AS n FROM job_competency_weight "
           "WHERE valid_to IS NULL GROUP BY 1")}

    def badge(r):
        b = []
        if r["status"] and r["status"] != "active":
            b.append('<span class="pill p-red">%s</span>' % esc(r["status"]))
        cs = r.get("code_status")
        if cs:
            klass = {"V": "p-t", "E": "p-e", "N": "p-n"}.get(cs, "p-n")
            b.append('<span class="pill %s">码 %s</span>' % (klass, esc(cs)))
        if r.get("valid_from"):
            b.append('<span class="muted">自 %s</span>' % esc(r["valid_from"]))
        n_c = comp_n.get(r["occupation_id"], 0)
        if n_c:
            b.append('<span class="pill p-v">%d 项能力</span>' % n_c)
        n_j = jd_n.get(r["occupation_id"], 0)
        if n_j:
            b.append('<span class="pill p-t">%d 份 JD</span>' % n_j)
        return " ".join(b)

    def render(parent, depth):
        out = []
        for r in sorted(kids.get(parent, []), key=lambda x: x["occupation_id"]):
            mark = {1: "●", 2: "○", 3: "·"}.get(r["level"], "·")
            out.append(
                '<div style="padding:3px 0 3px %dpx">'
                '<span class="muted">%s</span> '
                '<a href="/occupation/%s"><b>%s</b></a> '
                '<code class="muted">%s</code> %s</div>'
                % (depth * 22, mark, urllib.parse.quote(r["occupation_id"]),
                   esc(r["label_zh"]), esc(r["occupation_id"]), badge(r)))
            out.append(render(r["occupation_id"], depth + 1))
        return "".join(out)

    roots = [r for r in rows if not r["parent_id"] or r["parent_id"] not in node]
    tree_html = render(None, 0) if roots else '<p class="muted">这个时点没有节点。</p>'

    # 时间对比。注意：种子节点的 valid_from 是 2026-01-01，所以更早的时点返回 0——
    # 那是"那时这棵树还不存在"，不是查询出错。所以标签必须带上**实际日期**，
    # 否则"一年前 0 节点"看起来像 bug。
    first_day = q1(c, "SELECT min(valid_from) FROM occupation")
    tl = q(c, """
        SELECT '今天' AS "时点", current_date::text AS "日期", count(*) AS "节点数",
               count(*) FILTER (WHERE level=3) AS "岗位数"
          FROM occupation_asof(current_date)
        UNION ALL SELECT '半年前', (current_date - 182)::text, count(*),
               count(*) FILTER (WHERE level=3)
          FROM occupation_asof((current_date - 182)::date)
        UNION ALL SELECT '种子基线起点', %s::text, count(*),
               count(*) FILTER (WHERE level=3)
          FROM occupation_asof(%s::date)
        UNION ALL SELECT '全部节点（含已退役）', '—', count(*),
               count(*) FILTER (WHERE level=3) FROM occupation""",
        (first_day, first_day))
    tl_note = ('<p class="muted">职业树最早的 <code>valid_from</code> 是 <b>%s</b>，'
               '所以比它更早的时点没有节点——<b>0 不是查询出错，是"那时这棵树还不存在"</b>。'
               '这也是为什么本库把 <code>valid_from/valid_to</code> 做成一等公民：'
               '"什么时候有的"和"有没有"是两个不同的事实。</p>' % esc(first_day))
    h = q(c, "SELECT * FROM v_evolution_health")[0]

    body = """
<div class="sub">职业树。<b>枝是可以长的</b>：市场出现新岗位时，演化层会从 JD 里长出候选，
证据够了才提升为正式节点；节点被拆分/合并时旧 ID 退役但可追溯。
这一页还支持 <b>as-of 时间旅行</b>——看"当时"的树长什么样。</div>
<div class="cards">%s</div>
<div class="card"><form method="get" action="/tree" class="row">
  <div><label>看哪一天的职业树（留空=当前）</label>
  <input type="date" name="asof" value="%s"></div>
  <div style="flex:0 0 110px"><button type="submit">穿越</button></div>
  <div style="flex:0 0 130px"><a href="/tree"><button type="button" class="sec">回到今天</button></a></div>
</form>
<p class="muted">数据源：<code>%s</code>（as-of 用 <code>occupation_asof()</code>，
它按 <code>valid_from/valid_to</code> 还原当时的树）。</p></div>

<div class="card"><h2>时点对比</h2>%s%s</div>
<div class="card"><h2>树 <span class="muted">· %d 个节点</span>
<span style="float:right"><a href="/t/occupation">看原始表 →</a></span></h2>%s</div>
%s
""" % ("".join([
        metric(f"{len(rows):,}", "本时点节点数", asof or "今天"),
        metric(f"{len([r for r in rows if r['level'] == 3]):,}", "岗位（3 级）"),
        metric(f"{len([r for r in rows if r['level'] == 1]):,}", "岗位族（1 级）"),
        metric(f"{h['migrations']}", "迁移记录", "ID 只退役不复用"),
        metric(f"{h['occ_ready']}", "岗位候选待评审"),
        metric(f"{h['runs']}", "能力权重版本", "每次重算=一版"),
    ]), esc(asof), esc(src),
        render_rows(None, list(tl[0].keys()), tl, n_right=("节点数", "岗位数")), tl_note,
        len(rows), tree_html,
        sql_box("SELECT * FROM occupation_asof('%s'::date) ORDER BY level, occupation_id;"
                % (asof or "current_date")))
    body += page_analysis(c, "/tree")
    # as-of 日期进页头：它只在 KPI 说明位和 SQL 块里出现过，
    # 截图或收藏这一页时看不出"这是哪一天的树" —— 而这一页的全部意义就是时间旅行。
    return page("职业树", body,
                subtitle="层级浏览 + as-of 时间旅行 · 当前视图：%s"
                         % (asof or "当前时点"), crumbs=[("/tree", "职业树")])


# ---------------------------------------------------------------------------
# ⑬ 能力-职业匹配
# ---------------------------------------------------------------------------
def view_match(c, qs) -> bytes:
    pid = qs.get("person", [""])[0]
    oid = qs.get("occ", [""])[0]
    persons = q(c, """SELECT DISTINCT m.person_id,
                             (SELECT count(*) FROM match_result x
                               WHERE x.person_id = m.person_id) AS n
                        FROM match_result m ORDER BY m.person_id LIMIT 200""")
    if not pid and persons:
        pid = persons[0]["person_id"]
    occs = q(c, """SELECT DISTINCT o.occupation_id, o.label_zh,
                          (SELECT count(*) FROM job_posting j
                            WHERE j.occupation_id = o.occupation_id) AS n
                     FROM occupation o WHERE o.status='active' AND o.level=3
                      AND EXISTS (SELECT 1 FROM job_posting j
                                   WHERE j.occupation_id = o.occupation_id)
                     ORDER BY o.occupation_id LIMIT 300""")

    p_opts = "".join('<option value="%s" %s>%s（%d 条匹配）</option>'
                     % (esc(x["person_id"]), "selected" if x["person_id"] == pid else "",
                        esc(x["person_id"]), x["n"]) for x in persons)
    o_opts = "".join('<option value="%s" %s>%s</option>'
                     % (esc(x["occupation_id"]), "selected" if x["occupation_id"] == oid else "",
                        esc(x["label_zh"])) for x in occs)

    if oid:
        # 反向：这个职业适合哪些人
        res = q(c, """
            SELECT m.rank AS 排名, m.person_id AS 人才, m.score_total AS 得分,
                   (m.score_breakdown->>'met') AS 命中,
                   (m.score_breakdown->>'gap') AS 缺口,
                   (m.score_breakdown->>'unknown') AS 未知,
                   array_to_string(m.matched_concepts, ', ') AS 命中能力,
                   m.explanation AS 解释
              FROM match_result m JOIN job_posting jp ON jp.job_id = m.target_id
             WHERE jp.occupation_id = %s ORDER BY m.score_total DESC LIMIT 40""", (oid,))
        who = oid
        need = q(c, """
            SELECT c.preferred_label AS 能力, w.importance AS 重要性,
                   w.essentiality AS 必需性, w.sample_size AS 样本
              FROM job_competency_weight w JOIN concept c ON c.concept_id = w.concept_id
             WHERE w.occupation_id = %s AND w.valid_to IS NULL
             ORDER BY w.importance DESC LIMIT 20""", (oid,))
        res_html = render_rows(None, list(res[0].keys()), res, maxlen=40) if res else \
            '<div class="empty">这个职业还没有匹配结果。</div>'
        extra = '<div class="card"><h2>这个职业要什么（能力权重）</h2>%s</div>' % (
            render_rows(None, list(need[0].keys()), need, maxlen=40) if need
            else '<div class="empty">没有能力权重。</div>')
        title = "匹配 · 按职业"
    else:
        res = q(c, """
            SELECT m.rank AS 排名, jp.title_raw AS 岗位, jp.job_family AS 岗位族,
                   m.target_id AS job_id, m.score_total AS 得分,
                   (m.score_breakdown->>'met') AS 命中,
                   (m.score_breakdown->>'gap') AS 缺口,
                   (m.score_breakdown->>'unknown') AS 未知,
                   m.matched_concepts AS 命中能力, m.gap_concepts AS 缺口能力,
                   m.explanation AS 解释
              FROM match_result m LEFT JOIN job_posting jp ON jp.job_id = m.target_id
             WHERE m.person_id = %s ORDER BY m.rank LIMIT 40""", (pid,))
        for r in res:
            r["命中能力"] = "、".join(lbl("CT_CONCEPT_TYPE", None) or x for x in
                                     (r["命中能力"] or []))
            r["缺口能力"] = "、".join(r["缺口能力"] or [])
        who = pid
        # 缺口明细：gap_analysis 为空时，用目标职业的能力权重现场推导"补齐路径"
        gaps = q(c, """
            SELECT c.preferred_label AS 缺口能力, w.importance AS 目标职业要求,
                   w.essentiality AS 必需性, w.sample_size AS 样本,
                   o.label_zh AS 目标职业
              FROM match_result m
              JOIN job_posting jp ON jp.job_id = m.target_id
              JOIN occupation o ON o.occupation_id = jp.occupation_id
              JOIN job_competency_weight w ON w.occupation_id = o.occupation_id
                                          AND w.valid_to IS NULL
              JOIN concept c ON c.concept_id = w.concept_id
             WHERE m.person_id = %s AND w.concept_id = ANY(m.gap_concepts)
             ORDER BY w.importance DESC LIMIT 30""", (pid,))
        res_html = render_rows(None, list(res[0].keys()), res, maxlen=36) if res else \
            '<div class="empty">这个人才还没有匹配结果。</div>'
        extra = ('<div class="card"><h2>缺口 → 补齐路径</h2>%s'
                 '<p class="muted">注意口径：<code>gap_analysis</code> 表当前为空，'
                 '所以这里展示的是<b>从能力权重现场推导</b>出的"目标职业对该能力的要求强度"，'
                 '不是系统给出的学习建议。真正的补齐建议需要填充 remedy 库——'
                 '没做就不假装做了。</p></div>'
                 % (render_rows(None, list(gaps[0].keys()), gaps, maxlen=40) if gaps
                    else '<div class="empty">没有识别到缺口。</div>'))
        title = "匹配 · 按人才"

    dist = q(c, """
        SELECT jp.job_family AS 岗位族,
               round(avg((m.score_breakdown->>'met')::numeric), 2) AS 平均命中,
               round(avg((m.score_breakdown->>'gap')::numeric), 2) AS 平均缺口,
               round(avg((m.score_breakdown->>'unknown')::numeric), 2) AS 平均未知,
               count(*) AS 匹配条数
          FROM match_result m JOIN job_posting jp ON jp.job_id = m.target_id
         GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 20""")
    runs = q(c, """SELECT match_run_id AS 批次, algo_version AS 算法版本,
                          person_count AS 人数, job_count AS 岗位数, status AS 状态,
                          started_at AS 开始
                     FROM match_run ORDER BY started_at DESC LIMIT 10""")

    n_res = q1(c, "SELECT count(*) FROM match_result")
    n_runs = q1(c, "SELECT count(*) FROM match_run")
    n_gap = q1(c, "SELECT count(*) FROM match_result "
                  "WHERE (score_breakdown->>'gap')::numeric > 0")
    n_unk = q1(c, "SELECT count(*) FROM match_result "
                  "WHERE (score_breakdown->>'unknown')::numeric > 0")
    n_remedy = q1(c, "SELECT count(*) FROM gap_analysis")

    body = """
<div class="sub">能力 ↔ 职业的匹配。<b>三态是本页的重点</b>：
<code>met</code> 命中，<code>gap</code> 确认缺口，
<code>unknown</code> <b>不是不合格</b>——它表示"没有观测到"，
可能因为观测窗口没覆盖，也可能因为简历没写。</div>
<div class="cards">%s</div>
<div class="grid2">
<div class="card"><form method="get" action="/match" class="row">
  <div><label>按人才看</label><select name="person">%s</select></div>
  <div style="flex:0 0 90px"><button type="submit">查看</button></div>
</form></div>
<div class="card"><form method="get" action="/match" class="row">
  <div><label>按职业反查（哪些人最合适）</label><select name="occ">
  <option value="">（不选）</option>%s</select></div>
  <div style="flex:0 0 90px"><button type="submit">查看</button></div>
</form></div>
</div>
%s
<div class="card"><h2>当前：%s</h2>%s</div>
<div class="grid2">
<div class="card"><h2>各岗位族的匹配构成</h2>%s</div>
<div class="card"><h2>匹配批次</h2>%s</div>
</div>
%s
""" % ("".join([
        metric(f"{n_res:,}", "匹配结果"),
        metric(f"{n_runs:,}", "匹配批次"),
        metric(f"{n_gap:,}", "含缺口"),
        metric(f"{n_unk:,}", "含未知"),
        metric(f"{n_remedy:,}", "补齐建议记录", "当前为空"),
    ]), p_opts, o_opts,
        extra, esc(who), res_html,
        render_rows(None, list(dist[0].keys()), dist, maxlen=30) if dist
        else '<div class="empty">没有匹配数据。</div>',
        render_rows(None, list(runs[0].keys()), runs, maxlen=30) if runs
        else '<div class="empty">没有匹配批次。</div>',
        sql_box("""SELECT * FROM match_result WHERE person_id = '%s' ORDER BY rank;
SELECT * FROM match_run ORDER BY started_at DESC;""" % who))
    body += page_analysis(c, "/match")
    # h1 是「匹配 · 按人才 / 按职业」，跟导航标签「匹配」不相等——不显式指定 here，
    # 这一页一个导航项都不会高亮（改前的实测现象）。
    return page(title, body, subtitle="能力 ↔ 职业 · 三态可解释", here="匹配")


# ---------------------------------------------------------------------------
# ⑭ 扩展与演化：三条扩展机制 + 职业树/能力的持续生长
# ---------------------------------------------------------------------------
def view_real(c, qs) -> bytes:
    """真实公开案例验证报告（/real）。

    为什么单独做一页，而不是混在人才库里：
      合成样本证明的是"结构能装下"，真实案例证明的是"结构够不够用"。
      后者会暴露前者的假象（合成数据里 35/50 维度 100% 覆盖，真实公开数据只有 6–10/50），
      这两种结论必须分开呈现，混在一起会把"合成数据填得满"读成"字段设计得好"。

    页面的每一块都对应一个可证伪的问题，不写没有对应证据的话：
      画像向量 → 这些维度在真人身上取到值了吗？
      匹配结果 → 排上去的岗位对不对？如果不对，是算法错还是语料没覆盖？
      三项体检 → 维度正交吗？两侧够用吗？分数分得开人吗？
    """
    cases = q(c, """
        SELECT p.person_id, p.subject_code, p.access_tier, p.quality_flags,
               p.attrs->>'public_case_label' AS label,
               p.attrs->>'public_case_gaps'  AS gaps,
               p.attrs->>'public_case_unmapped' AS unmapped,
               d.birth_year, d.sex,
               (SELECT count(*) FROM evidence e WHERE e.person_id=p.person_id) AS n_ev,
               (SELECT max(e.cel_level) FROM evidence e WHERE e.person_id=p.person_id) AS cel
          FROM person p LEFT JOIN person_demographics d ON d.person_id=p.person_id
         WHERE p.person_id LIKE 'per_real\\_%'
         ORDER BY p.person_id""")

    tg = {}
    tpath = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "ops", "fixtures", "real_targets.json")
    if os.path.isfile(tpath):
        with open(tpath, encoding="utf-8") as fh:
            for x in json.load(fh).get("cases", []):
                tg[x["person_id"]] = x

    blocks = []
    for cs in cases:
        pid = cs["person_id"]
        # ---- 履历：教育 + 任职，按开始时间排 ----
        hist = []
        for e in q(c, """SELECT degree_level, school_name, major_raw, major_code,
                                start_date, end_date, is_clinical, overseas, attrs
                           FROM education_record WHERE person_id=%s""", (pid,)):
            hist.append(("教育", e["start_date"], "%s · %s%s%s"
                         % (e["school_name"],
                            lbl("CT_DEGREE_LEVEL", e["degree_level"]) or e["degree_level"],
                            "（%s）" % e["major_raw"] if e["major_raw"] else "",
                            "　专业码 %s" % e["major_code"] if e["major_code"] else
                            "　专业码：未归一化")))
        for w in q(c, """SELECT employer_name, employer_type, title_raw,
                                start_date, end_date, is_current
                           FROM employment_record WHERE person_id=%s""", (pid,)):
            hist.append(("任职", w["start_date"], "%s · %s　单位类型 %s"
                         % (w["employer_name"], w["title_raw"] or "（职务未公开）",
                            lbl("CT_EMPLOYER_TYPE", w["employer_type"]) or w["employer_type"])))
        hist.sort(key=lambda x: (x[1] is None, x[1]))
        hist_html = "".join(
            "<tr><td><span class='pill p-n'>%s</span></td><td>%s</td><td>%s</td></tr>"
            % (k, x.strftime("%Y-%m") if x else '<span class="nul">日期未公开</span>',
               esc(t))
            for k, x, t in hist) or '<tr><td colspan="3" class="muted">无公开履历</td></tr>'

        # ---- 画像向量：直接调与人详情页**同一个**函数，避免两处口径 ----
        dv = person_dimensions(c, pid)
        n_have = len([x for x in dv if x["value"]])
        vrows, cur = [], None
        for x in dv:
            if x["group"] != cur:
                cur = x["group"]
                vrows.append('<tr><td colspan="3" style="background:#f6f8fa;'
                             'font-weight:600">%s</td></tr>' % esc(cur))
            vrows.append('<tr><td>%s</td><td>%s</td><td class="muted">%s</td></tr>' % (
                esc(x["title"]),
                ("<b>%s</b>" % esc(trunc(x["value"], 40))) if x["value"]
                else '<span class="nul">公开资料未覆盖</span>',
                esc(x["note"] or "")))

        # ---- 匹配：读落库的 match_result（结论必须可追溯，不在页面上现算） ----
        mm = q(c, """SELECT m.rank, m.score_total, m.explanation, m.target_id,
                            j.title_raw, j.city, j.occupation_id, o.label_zh AS occ_label
                       FROM match_result m
                       JOIN job_posting j ON j.job_id = m.target_id
                       LEFT JOIN occupation o ON o.occupation_id = j.occupation_id
                      WHERE m.person_id=%s ORDER BY m.rank LIMIT 5""", (pid,))
        n_all = q1(c, "SELECT count(*) FROM match_result WHERE person_id=%s", (pid,))
        mm_html = "".join(
            '<tr><td class="n">%s</td><td>%s</td><td>%s</td><td>%s</td>'
            '<td class="muted">%s</td></tr>'
            % (x["rank"], esc(x["title_raw"]), esc(x["occ_label"] or "-"),
               '<b>%.3f</b>' % float(x["score_total"]) if x["score_total"] is not None
               else "NULL", esc(trunc(x["explanation"], 60)))
            for x in mm)

        t = tg.get(pid) or {}
        verdict = ""
        if t:
            verdict = ('<div class="note %s"><b>真实去向对照：</b>%s<br>'
                       '<b>对照节点：</b>%s　（该节点在 690 份 JD 里有 %d 份）<br>'
                       '<b>映射口径：</b>%s'
                       + ('<br><b>⚠ 名次不可作为有效性证据</b>：该去向在语料里没岗位，'
                          '属于覆盖缺口。缺的节点：%s' if t.get("occupation_ids") else '')
                       + '</div>') % (
                "info", esc(t.get("actual_path") or "-"),
                esc("、".join(t.get("labels") or [])), 0, esc(t.get("mapping_reason") or "-"),
                esc("、".join(t.get("nodes_without_jd") or [])))
        blocks.append("""
<div class="card"><h2>%s <span class="muted">· %s · %s · 证据 %d 条（最高 %s）</span></h2>
<p class="muted">%s</p>
<div class="tablewin"><table><thead><tr><th>类型</th><th>起始</th><th>履历</th></tr></thead>
<tbody>%s</tbody></table></div>
%s
</div>

<div class="card"><h2>画像向量 <span class="muted">· 50 个维度中公开资料能取到值的有 %d 个</span></h2>
<div class="tablewin"><table><thead><tr><th>维度</th><th>取值</th><th>取数说明</th></tr></thead>
<tbody>%s</tbody></table></div>
%s%s</div>

<div class="card"><h2>匹配结果 <span class="muted">· 落库 %s 条，取前 5</span></h2>
<div class="tscroll"><table><thead><tr><th>#</th><th>岗位</th><th>职业节点</th>
<th>总分</th><th>可解释性</th></tr></thead><tbody>%s</tbody></table></div>
<p class="muted">总分是<b>只对两侧都有值的维度</b>做的加权归一（空值不计入分母，
不当作 0 分）。因此"总分低"可能是"没观测到"，而不是"不合格"——每一行的
<code>explanation</code> 里写着可评维度数与覆盖比例。</p></div>
""" % (esc(cs["label"] or pid), esc(cs["subject_code"]), esc(pid), cs["n_ev"],
       esc(cs["cel"] or "-"),
       "数据来源：公开报道与机构官网，逐条附链接（evidence 表）；"
       "证据级别封顶第三方记录，未做本人核验。" if cs["n_ev"] else "无来源记录。",
       hist_html, verdict,
       n_have, "".join(vrows),
       ('<div class="note err"><b>公开资料未覆盖：</b>%s</div>' % esc(cs["gaps"]))
       if cs["gaps"] else "",
       ('<div class="note err"><b>词表装不下的原始值：</b>%s</div>' % esc(cs["unmapped"]))
       if cs["unmapped"] else "",
       n_all, mm_html))

    # ---- 三项体检：读缓存（现算要 20+ 秒，页面渲染扛不住；缓存由脚本生成并标注时间）----
    cache = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "analysis", "dimension_check.json")
    check_html = ""
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as fh:
            chk = json.load(fh)
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(cache)))
        s = chk.get("sufficiency") or {}
        o = chk.get("orthogonality") or {}
        e = (chk.get("effectiveness") or {}).get("distribution") or {}
        # 与本轮改动前的记录基线对比。基线是一份**带日期的历史记录**（无法重算，
        # 因为代码已经变了），所以这里明确标出它是哪一轮，不让它冒充当前值。
        bl = chk.get("baseline") or {}
        delta = ""
        if bl:
            rows_d = []
            def pair(name, before, now, unit=""):
                try:
                    d = float(now) - float(before)
                    arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
                    rows_d.append("<tr><td>%s</td><td class='n'>%s%s</td>"
                                  "<td class='n'><b>%s%s</b></td><td class='n'>%s%s</td></tr>"
                                  % (name, before, unit, now, unit, arrow,
                                     ("%.1f" % abs(d)).rstrip("0").rstrip(".")))
                except (TypeError, ValueError):
                    pass
            pair("两侧可评维度（个）", bl.get("two_sided"), len(s.get("two_sided") or []))
            pair("两侧可评权重占比", bl.get("weight_two_sided_pct"),
                 s.get("weight_two_sided_pct"), "%")
            pair("打分维度可评占比", bl.get("weight_score_two_sided_pct"),
                 s.get("weight_score_two_sided_pct"), "%")
            pair("可评门禁（个）", bl.get("gates_evaluable"),
                 len(s.get("gates_evaluable") or []))
            pair("恒定维度（个）", bl.get("constant_dimensions"),
                 len(o.get("constant_dimensions") or []))
            pair("疑似重复计权对（个）", bl.get("redundant_pairs"),
                 len(o.get("redundant_pairs") or []))
            pair("得分标准差", bl.get("effective_std"),
                 (e.get("overall") or {}).get("std"))
            delta = """
<div class="card"><h2>本轮改进 <span class="muted">· 对照 %s</span></h2>
<div class="tscroll"><table><thead><tr><th>指标</th><th class="n">改动前</th>
<th class="n">现在</th><th class="n">变化</th></tr></thead><tbody>%s</tbody></table></div>
<p class="muted">改动内容：把注册表里 <code>derived:</code>（派生口径）从"声明"变成"可执行规则" ——
任职起止求年限、经验要求文本归一化、期望/岗位城市映射城市层级、匿名雇主描述判单位类型、
院校中文标签归一化。刻度一律取自数据字典（<code>CT_EXPERIENCE_BAND</code> /
<code>CT_CITY_TIER</code> / <code>CT_SCHOOL_TIER</code>），找不到刻度的**不派生**。
<br><b>基线是带日期的历史记录，不是当前值</b>（代码已变，无法重算）。</p></div>""" % (
                esc(bl.get("label") or "-"), "".join(rows_d))
        check_html = """
<div class="card"><h2>维度体检 <span class="muted">· 由 code/analytics/dimension_check.py 生成
（%s）</span></h2>
<div class="cards">
%s
</div>
<div class="grid2">
  <div><h3>正交性</h3>
    <p class="muted">阈值：Cramér's V ≥ %.2f / Spearman ≥ %.2f / Jaccard ≥ %.2f 判为疑似重复计权。</p>
    <p>恒定维度（≥90%% 的人同值）：<b>%d</b> 个；疑似重复计权的维度对：<b>%d</b> 对。</p>
    <p class="muted">%s</p>
  </div>
  <div><h3>有效性</h3>
    <p>抽样 %s 人 × %s 岗位：得分标准差 <b>%.3f</b>，不同取值 %s 个 / %s 条。</p>
    <p class="muted">标准差越小说明越分不开人；并列越多，排名越接近字典序。</p>
  </div>
</div>
<p class="muted">原始结论（含每对冗余维度、每个零方差维度的清单）见
<code>analysis/dimension_check.json</code>。</p></div>
""" % (ts,
       "".join(metric(v, t2) for v, t2 in [
           (len(s.get("two_sided") or []), "两侧都有值的维度"),
           ("%.1f%%" % (s.get("weight_two_sided_pct") or 0), "两侧可评权重占比"),
           ("%.1f%%" % (s.get("weight_score_two_sided_pct") or 0), "打分维度可评占比"),
           ("%d/%d" % (len(s.get("gates_evaluable") or []), s.get("gates_total") or 0),
            "门禁可评/总数"),
           (len(o.get("constant_dimensions") or []), "恒定维度"),
           (len(o.get("redundant_pairs") or []), "疑似重复计权对")]),
       o["thresholds"]["cramers_v"], o["thresholds"]["spearman"], o["thresholds"]["jaccard"],
       len(o.get("constant_dimensions") or []), len(o.get("redundant_pairs") or []),
       esc("；".join("%s × %s（%s=%.3f）" % (p["a"], p["b"], p["stat"], p["value"])
                     for p in (o.get("redundant_pairs") or [])[:4]) or "未发现。"),
       e.get("n_person"), e.get("n_job"),
       (e.get("overall") or {}).get("std") or 0,
       (e.get("overall") or {}).get("distinct"), (e.get("overall") or {}).get("n_scores"))
        check_html += delta
    else:
        check_html = """
<div class="card"><h2>维度体检 <span class="muted">· 尚未生成</span></h2>
<p>这一块要跑 120 人 × 690 岗位的统计，现算要 20 秒以上，会把页面渲染拖垮，
所以由脚本生成缓存后在这里展示。生成命令：</p>
<pre>python code/analytics/dimension_check.py --all --json analysis/dimension_check.json</pre>
<p class="muted">体检要回答三个可证伪的问题：维度之间是否正交（有没有同一份数据被
两个维度各计一次权重）、两侧是否够用（能不能真的匹配）、分数是否分得开人。</p></div>"""

    body = """
<div class="sub">拿<b>三位真实、公开、可核查</b>的医学教育背景人物当标尺，验证这套建模
到底够不够用。<b>这一页的结论优先于合成数据</b>：合成样本只能证明结构装得下，
真实公开数据才会暴露字段够不够。</div>

<div class="note err"><b>先说清楚这页的边界，免得把结论读大了：</b>
① 样本量 <b>3</b>，任何比例（1/3、2/3）都没有统计意义，只能当个案看；
② 全部事实来自公开报道与机构官网，是<b>第三方记录</b>，证据级别封顶 E2/E3，<b>未做本人核验</b>；
③ 三人的<b>资格证书、户籍、测评得分</b>在公开资料里系统性缺失，因此这几类维度的
"覆盖率低"反映的是<b>公开数据的边界</b>，不是字段设计缺陷；
④ 690 份 JD 只覆盖 151 个职业节点中的 <b>23 个</b>，"真实去向排第几名"只在语料覆盖范围内可评。
</div>

%s
%s
""" % ("".join(blocks), check_html)
    return page("真实案例验证", body, subtitle="公开案例 · 画像向量 · 匹配对照 · 维度体检")


def public_overview(c) -> str:
    """公开（T0）概览：只放**匿名访客确实读得到**的数字。

    为什么逐个 try：一页里放 6 个数字，其中 1 个读不到就整页 403，是糟糕的取舍。
    这里改成"读得到的就显示、读不到的就说明需要登录"，于是匿名访客看到的永远是**真实**的
    公开信息，而不是一堵墙。
    但**不能把读不到当成 0**：那会把"权限不足"显示成"库里没有数据"——
    正是本项目一直在防的那种误读。所以读不到就明说。
    """
    # 数量一律走**聚合闸门** mt.public_counts()（迁移 031）：它只返回"每张表多少行"，
    # 不返回任何列值，所以**匿名不需要任何列权限**就能拿到数量。
    #
    # 为什么不用 `SELECT count(*) FROM mt.person`（第一版就是这么写的，实测翻车）：
    # PostgreSQL 没有"只准数不准读"的权限粒度 —— 匿名能 count 某张表，纯粹是因为
    # 那张表**恰好还有一列是 T0**。030 把 person_id/subject_code 收进 T1 之后，
    # person 表对匿名就一列可读都没有了，`count(*)` 直接被拒 ——
    # **"数量公开"这条需求被安全加固顺手打掉了**。把聚合做成独立闸门，
    # 两条需求才不会互相牵制。
    items = [
        ("人才档案", "person"),
        ("岗位", "job_posting"),
        ("职业（含层级）", "occupation"),
        ("码表", "code_table"),
        ("码值", "code_value"),
        ("已登记字段", "field_catalog"),
        ("画像维度", "dimension"),
    ]
    cards, denied = [], []
    for label, tbl in items:
        # 必须用 try_read（SAVEPOINT 隔离）：普通 try/except 只捕获异常，
        # 但 PostgreSQL 里**语句失败会让整个事务进入 aborted 状态**，
        # 于是第一个读不到的指标会把后面所有指标一起打死 ——
        # 表现成"匿名首页 403 且理由指向一个毫不相关的表"。
        # 这是本项目第三次踩同一个坑（ops/health.py、字段页各一次），所以这里也写明。
        #
        # **把表名传给闸门**（迁移 042）：不传的话闸门会把全部 89 张表数一遍再让外层
        # WHERE 过滤 —— 实测"取一张表"要 689ms（而直接数这张表只要 5ms），
        # 因为集合返回函数里的循环**不会**被外层 WHERE 下推。
        # 首页要为 7 个指标各查一次，不传参就是 **4.8 秒**的数据库开销，而且是**公网页面**。
        # 传参之后每次约 20ms。教训：把"便宜的查询"做成公开接口之前，先量它的真实代价。
        rows = try_read(c, "SELECT n_rows FROM mt.public_counts(%s)", (tbl,))
        if rows is None:
            denied.append(label)
            continue
        v = list(rows[0].values())[0] if rows else None
        shown = "{:,}".format(v) if isinstance(v, int) else str(v)
        cards.append(metric(shown, label, "公开"))
    html = '<div class="cards">%s</div>' % "".join(cards)
    if denied:
        html += ('<div class="note warn">以下数字需要登录后查看：%s'
                 '（不是"库里没有"，是当前等级读不到）</div>' % esc("、".join(denied)))
    # 年龄段分布：这是"年龄可以公开"的**具体落地形式**（037 用户决策）。
    # 刻意展示**分布**而不是逐人年龄：逐人年龄要带主体标识才能对上人，
    # 而主体标识已按披露控制收进 T1（030）。分布是统计属性，不指向个体。
    rows = try_read(c, "SELECT band_label, n_persons, band FROM mt.v_age_band_public "
                       "ORDER BY band")
    if rows:
        total = sum(r["n_persons"] for r in rows) or 1
        bars = "".join(
            '<tr><td>%s</td><td class="n">%s</td><td class="n">%.1f%%</td></tr>'
            % (esc(r["band_label"] or r["band"]), f"{r['n_persons']:,}",
               100.0 * r["n_persons"] / total)
            for r in rows)
        html += ('<div class="card"><h2>年龄段分布 <span class="muted">· 公开（T0）</span></h2>'
                 '<table><thead><tr><th>年龄段</th><th class="n">人数</th>'
                 '<th class="n">占比</th></tr></thead><tbody>%s</tbody></table>'
                 '<p class="muted">这里公开的是<b>年龄段</b>（统计属性）；'
                 '<b>精确出生年份</b>是个人信息，需要 T2（员工）及以上 —— '
                 '两者是不同的东西，"年龄可以公开"不等于"出生年月可以公开"。'
                 '另外年龄段是<b>按基准年计算的快照</b>（见 <code>mt.age_band_asis</code>），'
                 '会随时间过期，需定期重算。</p></div>' % bars)
    return html


def view_login(c, qs, session=None, err="", msg="") -> bytes:
    """登录页。

    这是"记录访问用户"的入口：没有它，审计只能记"某个进程读了一行"。
    刻意做成极小的表单（邮箱 + 口令），因为真正的身份体系在对方的小程序侧，
    本页只是让"谁是访问者"在本机有一个可追责的落点。
    """
    session = session or ANON
    who = ""
    if session.ok:
        who = ('<div class="note ok">当前身份：<b>%s</b>（等级 <b>%s</b>，会话 %s…）'
               '<br><a href="/logout">退出</a></div>'
               % (esc(session.actor), esc(session.tier), esc((session.session_id or "")[:8])))
    # c 可能为 None（登录失败时复用本函数渲染，而那条请求没有可用的连接）。
    # 用 `c is not None` 显式判断，而不是让 AttributeError 去当"没有连接"的信号 ——
    # 后者会把"复用渲染"这种正常用法变成 500。
    tiers = q(c, "SELECT tier, rank, title_zh, audience, description, is_public "
                 "FROM access_tier ORDER BY rank") \
        if (c is not None and _can_read_tiers(c)) else []
    tier_rows = "".join(
        "<tr><td><span class='pill %s'>%s</span></td><td>%s</td><td>%s</td><td class='muted'>%s</td></tr>"
        % ("p-t" if t["is_public"] else "p-v", esc(t["tier"]), esc(t["title_zh"]),
           esc(t["audience"] or ""), esc(t["description"]))
        for t in tiers)
    body = """
<div class="sub">数据库里的每一行都可能被读取，但<b>不是每一列对每个人都可见</b>。
本页建立的是"谁是访问者"这一件事；"能看到哪些列"由数据库的列级授权决定，
不是由这个页面决定 —— 页面只负责把身份交给数据库。</div>
<div class="card"><h2>公开（T0）概览 <span class="muted">· 未登录也能看到的真实数字</span></h2>
%s</div>
%s
%s
%s
<div class="card"><h2>登录</h2>
<form method="post" action="/login">
  <div class="row">
    <div style="flex:2 1 260px"><label>邮箱</label>
      <input name="email" type="email" required autocomplete="username"></div>
    <div style="flex:2 1 260px"><label>口令</label>
      <input name="password" type="password" required autocomplete="current-password"></div>
    <div style="flex:0 0 120px"><button type="submit">登录</button></div>
  </div>
</form>
<p class="muted">口令以 bcrypt 哈希存储（<code>crypt()/gen_salt('bf')</code>），库里没有明文；
校验在 <code>SECURITY DEFINER</code> 函数里做，所以门户进程<b>读不到任何哈希</b>。
"用户不存在"与"口令错误"返回同一个提示 —— 区分它们等于提供了一个账号枚举接口。
<br>账号由 <code>python ops\\secure.py</code> 创建；<b>系统不预置任何默认账号</b>
（默认口令比没有口令更危险，外面挂着域名时尤其致命）。</p></div>
<div class="card"><h2>等级阶梯 <span class="muted">· 等级决定能读哪些列，而等级来自数据库里的用户行</span></h2>
%s</div>
""" % (public_overview(c) if c is not None else
       '<p class="muted">（这次请求没有可用连接，公开概览略）</p>',
       who, ('<div class="note err">%s</div>' % esc(err)) if err else "",
       ('<div class="note info">%s</div>' % esc(msg)) if msg else "",
       ("<table><thead><tr><th>等级</th><th>名称</th><th>对应谁</th><th>说明</th></tr></thead>"
        "<tbody>%s</tbody></table>" % tier_rows) if tier_rows else
       '<p class="muted">（当前会话看不到等级表：它需要 <code>mt_portal</code> 的读权限）</p>')
    return page("登录", body, subtitle="身份与等级", session=session)


def _can_read_tiers(c) -> bool:
    try:
        q(c, "SELECT 1 FROM access_tier LIMIT 1")
        return True
    except psycopg.Error:
        return False


class NeedsTier(Exception):
    """需要更高等级：携带**已渲染好的**权限不足页，由 do_GET 用 403 发出。

    为什么要这个中间异常（实测缺陷）：`view_audit` / `view_field` 是在渲染过程中
    发现权限不够的，它们能渲染出正确的页面，但**决定不了 HTTP 状态码**（那在 `_send`）。
    直接 return 页面的话，do_GET 会按 200 发出 —— 实测 `/audit` 对匿名返回
    **200 + 权限不足页**。状态码错了，监控、代理、爬虫、以及"这个页面是否可用"的
    任何自动判断都会读错。权限不足必须是 403。
    """

    def __init__(self, body, need):
        super().__init__("需要 %s" % need)
        self.body = body
        self.need = need


def view_no_permission(c, qs, session=None, need="T1", target="", db_error="") -> bytes:
    """权限不足页。**权限不足必须是一个正常状态，不是 500。**

    这是本次接线里最重要的可用性决定：数据库拒绝读某列时抛的是 `InsufficientPrivilege`，
    如果让它冒泡成 500，用户看到的是"服务器错误"——他会以为系统坏了，
    而不是"我的等级不够"。而且 500 会掩盖真正的缺陷。
    所以这里把它渲染成明确的、可行动的页面：缺哪个等级、怎么升级、当前是什么身份。
    """
    session = session or current_session()
    body = """
<div class="note err"><b>权限不足：这个页面需要 <span class="pill p-v">%s</span> 等级，
你当前是 <span class="pill p-n">%s</span>。</b></div>
<div class="card"><h2>这不是错误，是访问控制在工作</h2>
<p>数据库<b>拒绝</b>了这次读取 —— 拒绝发生在 PostgreSQL 的列级授权层，
不是页面上藏了一下列。也就是说：即使这个页面写错了、把不该读的列放进了查询，
数据库也不会把数据交出来。</p>
<p>被拒绝的对象：<code>%s</code></p>
%s
<h3>怎么继续</h3>
<ul>
<li>如果这是你该看的数据：<a href="/login">登录</a>（或请管理员把账号提升到 %s 级）。</li>
<li>如果只想看能公开的部分：<a href="/">回到总览</a> ·
    <a href="/schema">表与视图</a> · <a href="/catalog">数据目录</a>
    —— 这些页面只用到公开（T0）的列。</li>
</ul>
<h3>为什么"看不到"和"没有"要分清楚</h3>
<p>本页如果不说明，你会把"因为权限而读不到"误读成"库里没有这个数据"。
这两种情况的处理方式完全不同，所以界面必须把它们区分开。</p></div>
""" % (esc(need), esc(session.tier), esc(target_label(session, target)),
       db_error_block(session, db_error),
       esc(need))
    return page("权限不足", body, subtitle="列级授权拒绝了这次读取", session=session)


def target_label(session, target: str) -> str:
    """「被拒绝的对象」这一栏的显示名 —— 同样按身份分流。

    路径类目标（`/audit`）对谁都能说；但有的调用点传的是**表名**
    （例如 `mt.access_log`），那对匿名访客就是内部结构信息。
    与 db_error_block 同一个原则：登录后给全，匿名只给不敏感的形态。
    """
    if not target:
        return ""
    if session is not None and getattr(session, "ok", False):
        return target
    return target if target.startswith("/") else "（该页需要更高等级的数据）"


def db_error_block(session, db_error: str) -> str:
    """把数据库原文**按身份分流**显示（独立审查 P2-6）。

    为什么要分流，而不是简单地"一律不显示"：
      · **已登录用户（尤其是运维/管理员）需要原文** —— 它点名是哪个对象被拒
        （"permission denied for view v_dimension_public"），比"你需要 T1"有用得多；
        本项目此前专门加过这一条，是为了让权限问题可诊断。
      · 但**匿名访客在公网上不需要**知道库里的表名、列名、函数名 ——
        那些名字本身就是内部结构信息（`occupation_asof`、`column_policy`…）。
    所以：已登录 → 显示原文 + 追踪号；匿名 → 只给追踪号，原文进 stderr 与审计日志。
    追踪号让"用户报的那条"能被运维直接对上，不需要把内部结构发到匿名页面上。
    """
    if not db_error:
        return ""
    tid = secrets.token_hex(3)
    print("[db-error %s] %s" % (tid, db_error), file=sys.stderr)
    if session is not None and getattr(session, "ok", False):
        return ('<p class="muted">数据库原话 <code>%s</code>：%s</p>'
                '<p class="muted">（本次追踪号 <code>%s</code>，同一条也写进了审计日志）</p>'
                % (esc(tid), esc(db_error), esc(tid)))
    return ('<p class="muted">详细原因已记入审计日志（追踪号 <code>%s</code>），'
            '登录后可看到数据库原话。</p>' % esc(tid))


def view_audit(c, qs, session=None) -> bytes:
    """访问日志页（谁在什么时候读了什么）。

    需求原文：「数据是重要资产，因此需要记录访问用户」。这一页就是那条需求的证据：
    日志不是"设计上会有"，而是**表里真的有多少行、最近一条是什么**。
    """
    session = session or current_session()
    try:
        rows = q(c, """SELECT access_id, actor, actor_role, action, target,
                              access_tier, row_count, purpose, at
                         FROM access_log ORDER BY access_id DESC LIMIT 200""")
        total = q1(c, "SELECT count(*) FROM access_log")
        actors = q(c, """SELECT actor, actor_role, count(*) AS n, max(at) AS last_at
                           FROM access_log GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20""")
    except psycopg.Error as e:
        # access_log 本体是 T2：低等级看不到是**预期**的，不是故障。
        # 抛 NeedsTier 让 do_GET 用 403 发出（第一版直接 return，结果是
        # "200 + 权限不足页" —— 状态码与内容矛盾，自动判断都会读错）。
        raise NeedsTier(
            view_no_permission(c, qs, session, need="T2", target="mt.access_log"),
            "T2")
    body = """
<div class="sub">每一条都是数据库收到的一次读取请求。这个页面本身被访问时也会写一条 ——
审计表不审计自己就等于留了个洞。</div>
<div class="cards">
%s
</div>
<div class="card"><h2>按访问者汇总</h2>
%s</div>
<div class="card"><h2>最近 200 条</h2>
%s
<p class="muted"><b>可信度的边界要说清楚</b>：<code>actor</code> 来自应用声明的会话身份，
应用理论上可以说谎；<code>actor_role</code> 来自 PostgreSQL 的 <code>current_setting('role')</code>，
即**数据库认定的**调用者角色，不可伪造。两者都记，不一致时一眼能看出来。</p></div>
""" % ("".join([metric(f"{total:,}", "日志条数"),
               metric(len(actors), "访问者数（按 角色 分组）")]),
       render_rows("access_log", list(actors[0].keys()) if actors else
                   ["actor", "actor_role", "n", "last_at"], actors) if actors
       else '<p class="muted">（0 行：还没有任何访问被记录）</p>',
       render_rows("access_log", list(rows[0].keys()) if rows else [], rows, maxlen=40)
       if rows else '<p class="muted">（0 行）</p>')
    return page("访问日志", body, subtitle="谁 · 什么时候 · 读了什么 · 多少行", session=session)


def view_extend(c, qs) -> bytes:
    h = q(c, "SELECT * FROM v_evolution_health")[0]
    usage = {
        "attribute_definition": q1(c, "SELECT count(*) FROM attribute_definition"),
        "field_value": q1(c, "SELECT count(*) FROM field_value"),
        "assertion": q1(c, "SELECT count(*) FROM assertion"),
        "category_node": q1(c, "SELECT count(*) FROM category_node"),
        "first_occurrence": q1(c, "SELECT count(*) FROM first_occurrence"),
        "occupation_candidate": q1(c, "SELECT count(*) FROM occupation_candidate"),
        "concept_candidate": q1(c, "SELECT count(*) FROM concept_candidate"),
        "occupation_migration": q1(c, "SELECT count(*) FROM occupation_migration"),
        "occupation_change": q1(c, "SELECT count(*) FROM occupation_change"),
        "competency_run": q1(c, "SELECT count(*) FROM competency_run"),
        "competency_drift": q1(c, "SELECT count(*) FROM competency_drift"),
        "evolution_policy": q1(c, "SELECT count(*) FROM evolution_policy"),
    }

    def used(k):
        n = usage[k]
        return ('<span class="pill p-t">已使用 %s 行</span>' % f"{n:,}") if n else \
            '<span class="pill p-red">库里 0 行 · 机制未被使用</span>'

    apis = q(c, """SELECT p.proname AS 函数, pg_get_function_arguments(p.oid) AS 参数
                     FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'mt' AND p.proname IN
                          ('add_dimension','set_value','assert_option','create_instance',
                           'deprecate_dimension','delete_instance','register_entity',
                           'form_schema','profile_json','occupation_asof','occupation_resolve',
                           'refresh_category_counts','refresh_concept_ancestor')
                    ORDER BY p.proname""")
    api_html = "".join('<tr><td><code>%s</code></td><td><code>%s</code></td></tr>'
                       % (esc(x["函数"]), esc(trunc(x["参数"], 90))) for x in apis)

    form = q(c, """SELECT entity_id, field_id, title, data_type, cardinality, is_required,
                          option_count, form_section
                     FROM v_form_schema ORDER BY entity_id, form_order LIMIT 40""")
    n_empty_ct = q1(c, """SELECT count(*) FROM code_table t
                           WHERE NOT EXISTS (SELECT 1 FROM code_value v
                                              WHERE v.code_table_id = t.code_table_id)""")

    body = """
<div class="sub">这一页回答两个问题：<b>这个库能不能长（结构扩展）</b>、
<b>它有没有在长（演化痕迹）</b>。每块都标注库里到底有没有使用痕迹——
"机制存在"和"真的用了"是两件事。</div>

<div class="cards">%s</div>

<div class="card"><h2>机制 A · 加一行码值（零 DDL）</h2>
<p>新枚举值 = <code>code_value</code> 里加一行。不改表、不改约束。</p>
<table><tr><td>代码表</td><td class="n">%s</td><td>代码值</td><td class="n">%s</td>
<td>空词表</td><td class="n">%s</td></tr></table>
%s</div>

<div class="card"><h2>机制 B · 加一个字段（零 DDL）</h2>
<p>UK Biobank 的 <code>field × instance × array</code>：新字段进 <code>field_catalog</code>，
值进统一的 <code>field_value</code>。表结构一次都不用改。</p>
<table>
<tr><td>已登记字段</td><td class="n">%s</td><td>扩展属性定义</td><td>%s</td></tr>
<tr><td>统一值表 <code>field_value</code></td><td>%s</td>
<td>分类树 <code>category_node</code></td><td>%s</td></tr>
<tr><td>首次发生 <code>first_occurrence</code></td><td>%s</td><td></td><td></td></tr>
</table>
<p class="muted">当前表单 schema（<code>v_form_schema</code>）共 %d 行，
每行的 <code>option_count</code> 就是"这个字段有几个给定选项"——
前端直接消费它渲染下拉框/复选框。</p>
%s</div>

<div class="card"><h2>机制 C · 加一条断言（零 DDL）</h2>
<p>结构化缓冲：<code>(subject, predicate, object)</code> 三元组，
用来装"还没决定归属哪张表"的事实。它必须被周期性提升进正式表，否则会腐化成垃圾堆。</p>
<table><tr><td>断言行数</td><td>%s</td></tr></table>
%s</div>

<div class="card"><h2>职业树的持续生长</h2>
<p>市场变了，树就要长。链路：<code>discover</code>（从 JD 发现候选）→
<code>promote</code>（证据够才提升，写入变更记录）→ 拆分/合并走
<code>occupation_migration</code>（<b>ID 只退役、不复用</b>）→
<code>occupation_asof()</code> 可回溯任一时点的树。</p>
<table>
<tr><td>候选（岗位）</td><td>%s</td><td>候选（能力）</td><td>%s</td></tr>
<tr><td>迁移记录</td><td>%s</td><td>变更记录</td><td>%s</td></tr>
<tr><td>在用节点</td><td class="n">%s</td><td>已退役</td><td class="n">%s</td></tr>
</table>
<p class="muted">健康度视图 <code>v_evolution_health</code> 一句话：
树 %s 节点（退役 %s）｜迁移 %s｜候选待评审 岗位 %s / 能力 %s｜重算版本 %s｜上升信号 %s。</p>
%s</div>

<div class="card"><h2>能力-职业映射的持续生长（版本化）</h2>
<p>每次重算 = <b>一个新版本</b>，不是"删了重算"。旧版本被 <code>valid_to</code> 封口，
两版之间逐 (职业, 能力) 比对产出 <code>competency_drift</code>，
所以可以回答"半年前这项能力的需求是多少"。</p>
<table>
<tr><td>重算版本</td><td class="n">%s</td><td>漂移记录</td><td class="n">%s</td></tr>
<tr><td>当前有效权重行</td><td class="n">%s</td><td>历史权重行</td><td class="n">%s</td></tr>
<tr><td>演化策略</td><td class="n">%s</td><td></td><td></td></tr>
</table>
%s</div>

<div class="card"><h2>动态建模 API（这些就是上面几条机制的入口）</h2>
<table><tr><th>函数</th><th>参数</th></tr>%s</table>
<p class="muted">要在页面上<b>真的调用</b>它们（加列、建行、写值、废弃维度），
去 <a href="/dev">开发者模式</a>——门户本身只读。</p></div>

%s
""" % ("".join([
        metric(f"{q1(c, 'SELECT count(*) FROM code_value'):,}", "代码值", "机制 A"),
        metric(f"{q1(c, 'SELECT count(*) FROM field_catalog'):,}", "已登记字段", "机制 B"),
        metric(f"{usage['field_value']:,}", "统一值表行数", "机制 B 的使用痕迹"),
        metric(f"{usage['assertion']:,}", "断言三元组", "机制 C"),
        metric(f"{h['runs']}", "能力权重版本", "映射在长"),
        metric(f"{h['occ_ready'] + h['con_ready']}", "待评审候选", "树/词表在长"),
    ]),
        f"{q1(c, 'SELECT count(*) FROM code_table'):,}",
        f"{q1(c, 'SELECT count(*) FROM code_value'):,}",
        f"{n_empty_ct:,}",
        "",
        f"{q1(c, 'SELECT count(*) FROM field_catalog'):,}", used("attribute_definition"),
        used("field_value"), used("category_node"), used("first_occurrence"),
        len(form),
        render_rows(None, list(form[0].keys()), form[:12], maxlen=40) if form
        else '<p class="muted">（空）</p>',
        f"{usage['assertion']:,}",
        "",
        f"{usage['occupation_candidate']:,}", f"{usage['concept_candidate']:,}",
        used("occupation_migration"), used("occupation_change"),
        f"{h['active_nodes']:,}", f"{h['retired_nodes']:,}",
        f"{h['active_nodes']}", f"{h['retired_nodes']}", f"{h['migrations']}",
        f"{h['occ_ready']}", f"{h['con_ready']}", f"{h['runs']}", f"{h['rising_signals']}",
        sql_box("SELECT * FROM v_evolution_health;\nSELECT * FROM occupation_asof('2026-01-01');"),
        f"{usage['competency_run']:,}", f"{usage['competency_drift']:,}",
        f"{q1(c, 'SELECT count(*) FROM v_competency_current'):,}",
        f"{q1(c, 'SELECT count(*) FROM job_competency_weight'):,}",
        f"{usage['evolution_policy']:,}",
        "",
        api_html,
        sql_box("""-- 三条机制的入口函数
SELECT mt.add_dimension('person','F_X','新维度','[{"code":"A1","label":"选项一"}]'::jsonb);
SELECT mt.create_instance('person','per_001','{"person_id":"per_001"}'::jsonb);
SELECT mt.set_value('person','per_001','F_X','A1');
SELECT * FROM mt.form_schema('person');"""))
    body += analysis_panel([
        mini_analysis(c, "哪些代码表还没有取值", """
            SELECT t.code_table_id AS "代码表", t.name AS "名称", t.status AS "状态"
              FROM code_table t
             WHERE NOT EXISTS (SELECT 1 FROM code_value v
                                WHERE v.code_table_id = t.code_table_id)
             ORDER BY 1"""),
        mini_analysis(c, "断言的 predicate 分布", """
            SELECT predicate AS "谓词", subject_type AS "主体类型",
                   count(*) AS "条数", count(*) FILTER (WHERE status='active') AS "有效"
              FROM assertion GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15"""),
        mini_analysis(c, "能力漂移（最近版本，非稳定项）", """
            SELECT d.drift_type AS "漂移类型", o.label_zh AS "职业",
                   c.preferred_label AS "能力", d.from_importance AS "上版",
                   d.to_importance AS "本版", d.delta AS "变化"
              FROM competency_drift d
              JOIN occupation o ON o.occupation_id = d.occupation_id
              JOIN concept c ON c.concept_id = d.concept_id
             WHERE d.drift_type <> 'stable'
               AND d.to_run_id = (SELECT run_id FROM competency_run
                                   ORDER BY created_at DESC LIMIT 1)
             ORDER BY abs(coalesce(d.delta,0)) DESC LIMIT 12"""),
    ], "分析这个演化层",
        "三个分析分别看：词表有没有空壳、断言缓冲里堆了什么、能力需求最近一版漂了多少。")
    return page("扩展与演化", body, subtitle="结构能长 · 而且真的在长")


# ---------------------------------------------------------------------------
# ⑮ 数据质量仪表盘
# ---------------------------------------------------------------------------
def view_quality(c, qs) -> bytes:
    m = meta()
    tables = [r for r in m["rels"] if r["kind"] == "table"]
    empty = sorted([r["name"] for r in tables if r["rows"] == 0])
    filled = [r for r in tables if r["rows"] > 0]

    # 关键表的列空值率：精确计算（不用 pg_stats，那只在 ANALYZE 之后才有且是抽样）
    key_tables = ["occupation", "job_posting", "job_requirement", "concept",
                  "code_value", "field_catalog", "person", "provenance"]
    null_rows = []
    for t in key_tables:
        cols = [x["column_name"] for x in m["cols"].get(t, [])]
        n = m["by_name"][t]["rows"]
        if not n:
            continue
        exprs = ", ".join('sum(CASE WHEN "%s" IS NULL THEN 1 ELSE 0 END) AS "%s"'
                          % (cn, cn) for cn in cols)
        r = q(c, 'SELECT %s FROM mt."%s"' % (exprs, t))[0]
        for cn, v in r.items():
            null_rows.append({"表": t, "列": cn, "行数": n, "空值数": v,
                              "空值率%": round(100.0 * (v or 0) / n, 1)})
    null_rows.sort(key=lambda x: (-x["空值率%"], x["表"], x["列"]))
    worst = null_rows[:25]

    # 空表清单：**精确计数，但走聚合闸门 mt.public_counts()**。
    # 曾经这里是对每张表用**会话连接**跑 `count(*)`，而 039 的 `login_attempt`
    # 刻意对应用角色零授权 —— 于是**一张表的收紧把整页打成 403**，
    # 而且提示自相矛盾（"需要 T2，你当前是 T3"）。实测踩到。
    # 正解就是 031 已经建立的闸门：它以属主身份计数，不需要任何列权限，
    # 一次调用拿到全部精确行数（N 次查询变 1 次），且"数量公开"与"列受限"不再互相牵制。
    # 闸门自带排除清单（041 的 public_count_excluded：登录流水等行数也属运维信息）。
    #
    # 这里**不传表名**（要的就是全量清单），所以是闸门里最贵的一次调用（约 470ms，
    # 因为要数 change_log 这类 10 万行级大表）。质量页是需要 T2 的页面、访问量低，
    # 这个代价可以接受；公开落地页则必须传表名（见 public_overview 的说明）。
    try:
        counts = {r["table_name"]: r["n_rows"] for r in q(c, "SELECT * FROM mt.public_counts()")}
        empty = [t for t in sorted(meta()["by_name"]) if counts.get(t, 0) == 0]
    except psycopg.Error:
        # 闸门本身也读不到时（理论上不该发生）明确说明，而不是把整页打成空白
        counts, empty = {}, []

    # 完整性不变量（与 ops/tests/regression_guards.sql 的 11–13 号同口径）
    inv = [
        ("变更记录不得有悬空 to_ids",
         "SELECT count(*) AS n FROM occupation_change ch WHERE coalesce(array_length(ch.to_ids,1),0) > 0 "
         "AND NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = ANY(ch.to_ids))"),
        ("迁移记录不得引用不存在的节点",
         "SELECT count(*) AS n FROM occupation_migration mm WHERE "
         "NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = mm.old_id) OR "
         "NOT EXISTS (SELECT 1 FROM occupation o WHERE o.occupation_id = mm.new_id)"),
        ("每个退役节点必须留下变更记录",
         "SELECT count(*) AS n FROM occupation o WHERE o.status <> 'active' AND NOT EXISTS "
         "(SELECT 1 FROM occupation_change ch WHERE o.occupation_id = ANY(ch.from_ids))"),
        ("外键引用完整性（全库）",
         "SELECT count(*) AS n FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
         "WHERE n.nspname='mt' AND c.contype='f' AND NOT c.convalidated"),
        ("没有主键的基础表",
         "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
         "WHERE n.nspname='mt' AND c.relkind='r' AND NOT EXISTS "
         "(SELECT 1 FROM pg_constraint x WHERE x.conrelid=c.oid AND x.contype='p')"),
    ]
    inv_html = "".join(
        '<tr><td>%s</td><td class="n">%d</td><td>%s</td></tr>'
        % (esc(name), q1(c, sqltext),
           '<span class="pill p-t">通过</span>' if not q1(c, sqltext)
           else '<span class="pill p-red">违反</span>')
        for name, sqltext in inv)

    # 岗位侧质量门（与 code/gates/jd_quality_gate.py 同口径）
    gate = q(c, """
        SELECT count(*) AS jd,
               count(DISTINCT job_family) AS fams,
               round(100.0 * count(*) FILTER (WHERE title_raw IS NOT NULL
                        AND raw_text_ref IS NOT NULL) / greatest(count(*),1), 1) AS required,
               round(100.0 * count(*) FILTER (WHERE raw_sha256 IS NOT NULL)
                     / greatest(count(*),1), 1) AS traceable
          FROM job_posting""")[0]
    mapping = q(c, metrics.CONCEPT_COVERAGE_SQL, metrics.coverage_params())[0]
    cov = metrics.coverage_pct(mapping)

    fresh = q(c, """
        SELECT 'job_posting' AS 表, max(recorded_at) AS 最近写入 FROM job_posting
        UNION ALL SELECT 'provenance', max(fetched_at) FROM provenance
        UNION ALL SELECT 'change_log', max(at) FROM change_log
        UNION ALL SELECT 'ingest_run', max(started_at) FROM ingest_run
        UNION ALL SELECT 'competency_run', max(created_at) FROM competency_run
        UNION ALL SELECT 'backup_run', max(started_at) FROM backup_run""")

    audit = q(c, """SELECT object_type AS 对象类型, object_name AS 对象,
                           count(*) AS 变更次数, max(at) AS 最近
                      FROM change_log GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15""")
    bk_rows = q(c, "SELECT * FROM v_backup_health")   # 原先这条查询跑了两次
    bk = bk_rows[0] if bk_rows else {}
    pol = q(c, "SELECT * FROM access_policy ORDER BY object_type, object_id LIMIT 20")
    rel = q(c, "SELECT release_id, name, version, status, doc_codebook_uri FROM dataset_release")
    n_cons = q1(c, "SELECT count(*) FROM pg_constraint x JOIN pg_namespace n "
                   "ON n.oid = x.connamespace WHERE n.nspname = %s", (SCHEMA,))
    cl_n = q1(c, "SELECT count(*) FROM change_log")   # 页面上两处要用同一个数

    body = """
<div class="sub">"成熟"不是形容词，是一组可核的数字：结构完整性、列填充率、映射覆盖率、
数据新鲜度、审计流水、备份健康、访问控制。<b>空的就是空的</b>，这一页不美化。</div>

<div class="cards">%s</div>

<div class="card"><h2>结构完整性不变量</h2>
<table><tr><th>不变量</th><th class="n">违反行数</th><th>结论</th></tr>%s</table>
<p class="muted">与 <code>ops/tests/regression_guards.sql</code> 的 11–13 号用例同口径。
这些不变量都建立在同一个前提上：<b>节点从不物理删除</b>，
所以任何指向不存在节点的引用都是脏数据。</p></div>

<div class="card"><h2>岗位侧数据质量门</h2>
<table>
<tr><td>结构化 JD</td><td class="n">%s</td><td>岗位族覆盖</td><td class="n">%s / 17</td></tr>
<tr><td>必填字段完整率</td><td class="n">%s%%</td><td>原文可回溯率</td><td class="n">%s%%</td></tr>
<tr><td>概念映射分子</td><td class="n">%s</td><td>分母（剔除资格门槛 %s 条）</td><td class="n">%s</td></tr>
<tr><td>能力概念映射覆盖率</td><td class="n"><b>%s%%</b></td><td>目标</td><td class="n">≥70%%</td></tr>
</table>
<p class="muted">口径说明：学历/经验/证照这类<b>资格门槛</b>由专用字段承载，不计入能力映射分母——
把它们算进去会把覆盖率人为拉低，那不是质量差，是口径错。</p></div>

<div class="grid2">
<div class="card"><h2>表填充率</h2>
<p><b>%d / %d</b> 张基础表有数据。</p>%s</div>
<div class="card"><h2>数据新鲜度</h2>%s</div>
</div>

<div class="card"><h2>列空值率最高（关键表前 25）</h2>%s
<p class="muted">空值率是"结构建了但数据没到"的直接证据。
本库纪律：<b>空值就是空值</b>，不用默认值或推算值填充。</p></div>

<div class="grid2">
<div class="card"><h2>审计流水 Top 15</h2>%s
<p class="muted"><code>change_log</code> 共 %s 行，append-only，由触发器自动写入。</p></div>
<div class="card"><h2>备份与访问控制</h2>
<h3>备份健康</h3>
<table>%s</table>
<h3>访问策略（字段级 × 动作级）</h3>
%s
<h3>数据集发布</h3>
%s
</div>
</div>
%s
""" % ("".join([
        metric(f"{cl_n:,}", "审计流水行数"),
        metric(f"{len(filled)}/{len(tables)}", "非空表"),
        metric(f"{cov}%", "概念映射覆盖率", "目标 ≥70%"),
        metric(f"{n_cons:,}", "约束总数"),
        metric(f"{q1(c, 'SELECT count(*) FROM provenance'):,}", "血缘条数"),
        metric(f"{q1(c, 'SELECT count(*) FROM source_registry'):,}", "来源登记"),
    ]), inv_html,
        f"{gate['jd']:,}", gate["fams"], gate["required"], gate["traceable"],
        f"{mapping['numer']:,}", f"{mapping['qualification']:,}", f"{mapping['denom']:,}", cov,
        len(filled), len(tables),
        bar_table([{"k": r["name"], "v": r["rows"]} for r in
                   sorted(filled, key=lambda x: -x["rows"])[:14]], "k", "v",
                  href=lambda r: "/t/%s" % r["k"]) if filled else "",
        render_rows(None, list(fresh[0].keys()), fresh, maxlen=40),
        render_rows(None, list(worst[0].keys()), worst, maxlen=30) if worst else "",
        render_rows(None, list(audit[0].keys()), audit, maxlen=34) if audit
        else '<p class="muted">（无）</p>',
        f"{cl_n:,}",
        render_rows(None, list(bk.keys()), [bk], maxlen=40) if bk else "<tr><td>（无）</td></tr>",
        render_rows(None, list(pol[0].keys()), pol, maxlen=40) if pol
        else '<p class="muted">（无策略）</p>',
        render_rows(None, list(rel[0].keys()), rel, maxlen=34) if rel else
        ('<p class="muted">还没有发布记录。<b>按设计：没有 codebook 就不发布</b>——'
         '这条规则由 <code>dataset_release</code> 上的 CHECK 约束执行。</p>'),
        sql_box("""-- 空表清单：逐表精确计数（**不是** pg_class.reltuples 估算）
SELECT count(*) AS n FROM mt.<每张基础表>;   -- 在应用层逐表执行，n = 0 即计入
-- 本次实测空表 %d 张：%s
-- 完整性不变量见 ops/tests/regression_guards.sql""" % (
            len(empty), "、".join(empty[:8]) if empty else "无")))
    return page("数据质量", body, subtitle="可核的数字，不是形容词")


# ---------------------------------------------------------------------------
# ⑯ 开发者模式入口（本身只读；真正能写的在另一个进程另一个端口）
# ---------------------------------------------------------------------------
def view_dev_link(c, qs) -> bytes:
    alive = False
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8083/", timeout=2) as r:
            alive = r.status == 200
    except Exception:                                     # noqa: BLE001
        alive = False

    status = ('<span class="pill p-t">在线</span> '
              '<a href="http://127.0.0.1:8083/">打开 →</a>' if alive
              else '<span class="pill p-red">未启动</span>')
    body = """
<div class="note err"><b>开发者模式不在这个进程里。</b>
这个门户（:8082）<u>只读</u>——所有页面、所有路由都不写数据库，
这一点由 <code>ops/tests/portal_test.py</code> 证明（遍历全部页面后
<code>change_log</code> 不增长；<code>SELECT 1 INTO t</code> 被 PostgreSQL 只读事务拒绝）。
<br><br>
能写的开发者模式是<b>另一个进程、另一个端口</b>（:8083）。
分成两个进程不是洁癖：一旦同一个进程里存在能写的路由，
"门户只读"就不再是可证明的结论，而只是"我相信没人误挂路由"。</div>

<div class="card"><h2>开发者模式（:8083） %s</h2>
<table><tr><th>页面</th><th>能做什么</th><th>写库？</th></tr>
<tr><td><code>/sql</code></td><td>多语句、单事务、<b>默认试运行后回滚</b>；勾选提交才落地；
逐条给出影响行数</td><td>可写</td></tr>
<tr><td><code>/model</code></td><td>调用 <code>add_dimension</code> / <code>create_instance</code> /
<code>set_value</code> / <code>deprecate_dimension</code> / <code>register_entity</code>，
每次操作后给出"基础表数与 person 列数不变"的证据</td><td>可写</td></tr>
<tr><td><code>/tools</code></td><td>一键驱动既有 CLI：重建派生层、发现候选、重算版本、
质量门、复位基线、备份、跑指定回归套件</td><td>视工具而定</td></tr>
<tr><td><code>/audit</code></td><td><code>change_log</code> 最近变更 + 按对象统计</td><td>只读</td></tr>
<tr><td><code>/migrate</code></td><td>迁移文件清单、schema 对象统计</td><td>只读</td></tr>
</table></div>

<div class="card"><h2>怎么启动</h2>
<pre>python code\\demo\\portal.py --serve       # 只读门户  http://127.0.0.1:8082
python code\\demo\\portal_dev.py --serve   # 开发者模式 http://127.0.0.1:8083</pre>
<p class="muted">两者都只监听 <code>127.0.0.1</code>。<code>--check</code> 可在不开服务的情况下
自检：门户会遍历渲染每一个页面，开发者模式会验证"试运行必须回滚"。</p></div>

<div class="card"><h2>这一页为什么只读</h2>
<p>因为"扩展能力"和"在线开发"这两件事必须被<b>证明</b>而不是被声称。
门户这一侧负责证明<b>结构可扩展</b>：</p>
<ul>
<li><a href="/extend">扩展与演化</a>：三条扩展机制（加码值 / 加字段 / 加断言）的状态与使用痕迹，
以及职业树、能力-职业映射的演化记录与版本化漂移。</li>
<li><a href="/t/field_catalog">field_catalog</a>：已登记的 112 个字段，
新字段在这里加一行即可，不动表结构。</li>
<li><a href="/t/code_value">code_value</a>：305 个受控码值，新增枚举值在这里加一行。</li>
<li><a href="/t/attribute_definition">attribute_definition</a>：属性登记表——
空表也是信息：说明机制 B 还没被使用过。</li>
</ul>
<p>真正去<b>做</b>这些操作（加列、建行、写值、提升候选、重算版本）请到 :8083。</p></div>
""" % status
    return page("开发者模式", body, subtitle="读写分离 · 两个进程 · 两个端口")


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    # 过长或恶意的请求头不要拖住线程（默认没有上限）
    protocol_version = "HTTP/1.1"
    timeout = 300

    def handle_one_request(self):
        """每个请求先过**并发闸**（见 MAX_CONCURRENT_REQUESTS 的说明）。

        为什么在 `handle_one_request` 而不是 do_GET/do_POST：
        这里能覆盖**全部**方法（GET/POST/HEAD/OPTIONS/未知方法），
        而写两遍一定会漏一个 —— 和 `_send` 统一记审计是同一个道理：**单点**。
        """
        acquired = _REQ_SEM.acquire(timeout=_REQ_WAIT_SECONDS)
        if not acquired:
            self._drain_then_busy()
            return
        try:
            super().handle_one_request()
        finally:
            _REQ_SEM.release()

    def _drain_then_busy(self):
        """排队超时：**先把请求读掉**，再回 503。

        为什么不能直接写响应就关连接（第一版就是这么写的，实测踩到）：
        此时请求行/请求头还在客户端的发送缓冲里，服务端"响应先于请求读完"就
        `close_connection`，客户端看到的是 `URLError`/连接被重置 ——
        而不是我们精心写的 503。**客户端拿到什么，才算我们做到了什么。**
        所以这里先把请求头解析掉（不碰数据库，很便宜），再明确回 503 + Retry-After。
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536 or not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():        # 内含畸形请求的处理（会自己回 4xx）
                return
            self.close_connection = True
            body = page("服务繁忙",
                        '<div class="note warn"><b>服务繁忙，请稍后重试。</b></div>'
                        '<div class="card"><p>当前同时处理的请求已达上限（%d），'
                        '这一请求排队 %.0f 秒仍未轮到，已被拒绝，'
                        '<b>没有</b>执行任何查询。</p>'
                        '<p class="muted">这是过载保护：限制并发是为了不把'
                        '数据库连接吃光（每个请求最多用 3 条连接，'
                        '而 PostgreSQL 的 max_connections 是有限的），'
                        '从而让已进入的请求能正常完成。</p>'
                        '<p><a href="/">稍后重试</a></p></div>'
                        % (MAX_CONCURRENT_REQUESTS, _REQ_WAIT_SECONDS))
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "3")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 客户端已经走了：什么都不用做，也别让异常冒到 socketserver
            pass

    server_version = "MedTalentPortal/1.0"

    def log_message(self, fmt, *args):      # 安静一点，演示时不刷屏
        pass

    def _cookie_flags(self) -> str:
        """会话 cookie 的安全标记。**Secure 只在真的走 HTTPS 时加。**

        为什么要按请求判断，而不是写死：
          · 写死 `Secure` → 本机 http://127.0.0.1 下浏览器**不回传** cookie，
            表现为"登录成功但立刻又是未登录"，很难查；
          · 写死不加 → 公网（Cloudflare 隧道，浏览器看到的是 https）的 cookie
            可以经明文 HTTP 泄露 —— 而公网正是最需要它的地方。
        判断依据：反向代理的 `X-Forwarded-Proto`；没有该头时看 Host 是不是本机。
        `SameSite=Lax` 挡跨站携带，`HttpOnly` 挡 JS 读取，这三项一起才是完整的。
        """
        proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if proto:
            secure = proto == "https"
        else:
            host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
            secure = host not in ("", "127.0.0.1", "localhost", "[::1]")
        return ("; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else ""))

    def _send(self, status, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        extra = dict(extra or {})
        # 导出禁令命中的列：写进响应头。
        # 为什么不写进 CSV 正文：那会破坏表头，让所有按行解析的脚本
        # （含本项目的"CSV 行数 == 页面行数"断言）失效。
        # 为什么必须让调用方知道：静默删列比报错更危险 ——
        # 用户会以为"这一列本来就没有数据"。
        denied = getattr(_LOCAL, "csv_denied", None)
        if denied:
            # ⚠ 响应头**必须是 latin-1**（HTTP 规范）。第一版这里写了中文说明，
            # 结果 send_header 抛 UnicodeEncodeError 把整个请求打成
            # RemoteDisconnected —— 一个"加个提示"的改动弄崩了导出功能。
            # 所以头部一律 ASCII；中文解释放在页面里（页面是 UTF-8，没这个限制）。
            extra["X-Export-Denied"] = ", ".join(denied)
            extra["X-Export-Denied-Note"] = ("columns removed per export policy; "
                                             "still viewable inline")
            _LOCAL.csv_denied = None
        # ------------------------------------------------------------------
        # 审计在这里统一记 —— **唯一的输出点**，所以覆盖全部路由（含 404/405/403）。
        # 为什么不在每个路由里各写一次（第一版就是这么写的）：那样只有"记得加"的页面
        # 才有日志，实测漏掉了 /talent、/search、/analyze 等一大批页面 ——
        # 表现是"审计在跑、也有效率"，但**覆盖面是残缺的**，而残缺的审计比没有更危险：
        # 你会以为"没记录=没发生"。和 `db()` 是唯一的权限注入点同一个道理：
        # 安全性质必须挂在**单点**上，而不是靠每个路由自觉。
        # 登录成功/失败由 do_POST 自己记（它需要区分 login 与 login_failed，语义更细）。
        # ------------------------------------------------------------------
        sess = getattr(self, "_sess", None)
        path = getattr(self, "_path", None)
        if sess is not None and path and not path.startswith("/login") \
                and not path.startswith("/logout"):
            if status == 403:
                action = "denied"
            elif status >= 400:
                action = "error"
            else:
                action = "view"
            # 具体的拒绝原因（缺哪个等级、数据库原话）由处理器放进 _audit_detail
            detail = {"status": status}
            detail.update(getattr(self, "_audit_detail", None) or {})
            log_visit(sess, action, path, detail=detail)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex")
        for k, v in (extra or {}).items():
            # 防御：HTTP 头是 latin-1，任何非 ASCII 值都会让 send_header 抛异常，
            # 而那个异常发生在**已经写好状态行之后** —— 表现为客户端收到
            # RemoteDisconnected（"服务端无响应"），排查时完全看不出是头部编码问题。
            # 实测踩过一次（在导出禁令的提示头里写了中文）。
            # 这里统一兜底：非 latin-1 的值改成 URL 编码，宁可提示不好看，也不能崩掉响应。
            try:
                str(v).encode("latin-1")
            except UnicodeEncodeError:
                v = urllib.parse.quote(str(v))
                print("[!] 响应头 %s 含非 latin-1 字符，已 URL 编码" % k, file=sys.stderr)
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_session(self):
        return session_from_cookie(self.headers.get("Cookie", ""))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        sess = self._cookie_session()
        set_current_session(sess)
        # 供 _send 统一记审计用（见 _send 里的说明）
        self._sess = sess
        self._path = path
        # 每次请求都清空：HTTP/1.1 keep-alive 下同一个 handler 实例会处理多个请求，
        # 不清空会把上一次 403 的"缺哪个等级"带进这一次 200 的日志里
        self._audit_detail = None
        try:
            with db(sess) as c:
                # 登录/登出：它们不需要数据权限，所以放在最前面
                if path == "/login":
                    return self._send(200, view_login(c, qs, sess))
                if path == "/logout":
                    revoke_session(sess.session_id)
                    log_visit(sess, "logout", "web")
                    return self._send(302, b"", extra={
                        "Location": "/",
                        "Set-Cookie": "%s=" % (COOKIE,) + self._cookie_flags() + "; Max-Age=0"
                                      % SESSION_COOKIE})
                if path == "/audit":
                    return self._send(200, view_audit(c, qs, sess))
                if path == "/":
                    # 匿名访客看到的是**公开落地页**（T0 概览 + 登录入口），
                    # 而不是整页失败或一堵 403 墙。
                    # 首页要画职业/岗位的聚合图，那些数据是 T1 —— 所以对匿名访客
                    # 正确的行为不是"报错"，而是"给他能看的，并告诉他还有更多"。
                    if sess.is_anonymous:
                        return self._send(200, view_login(c, qs, sess, msg=(
                            "首页的完整仪表盘需要登录（它包含岗位与职业的聚合，属 T1）。"
                            "下面是公开数据概览。")))
                    return self._send(200, view_home(c, qs))
                if path == "/schema":
                    return self._send(200, view_schema(c, qs))
                if path == "/catalog":
                    return self._send(200, view_catalog(c, qs))
                mm = re.match(r"^/field/([A-Za-z0-9_]+)/([A-Za-z0-9_]+)$", path)
                if mm:
                    return self._send(200, view_field(c, mm.group(1), mm.group(2), qs))
                if path == "/search":
                    return self._send(200, view_search(c, qs))
                if path == "/analyze":
                    return self._send(200, view_analyze(c, qs))
                if path == "/lineage":
                    return self._send(200, view_lineage(c, qs))
                if path == "/sql":
                    return self._send(200, view_sql(c, qs))
                if path == "/talent":
                    return self._send(200, view_talent(c, qs))
                if path == "/talent.csv":
                    return self._send(200, talent_csv(c, qs), "text/csv; charset=utf-8",
                                      {"Content-Disposition":
                                       'attachment; filename="talent.csv"'})
                if path == "/occupations":
                    return self._send(200, view_occupations(c, qs))
                if path == "/tree":
                    return self._send(200, view_tree(c, qs))
                if path == "/match":
                    return self._send(200, view_match(c, qs))
                if path == "/real":
                    return self._send(200, view_real(c, qs))
                if path == "/extend":
                    return self._send(200, view_extend(c, qs))
                if path == "/quality":
                    return self._send(200, view_quality(c, qs))
                if path == "/dev":
                    return self._send(200, view_dev_link(c, qs))
                # 可视化模块在单独文件里（图表库 + 度量注册表），延迟导入以避免循环依赖
                if path == "/viz" or path.startswith("/viz/"):
                    import portal_viz as V
                    if path == "/viz":
                        return self._send(200, V.view_viz(c, qs))
                    if path == "/viz/build":
                        return self._send(200, V.view_build(c, qs))
                    # 顺序要紧：/viz/build.csv 也匹配下面的面板正则（group="build"），
                    # 必须先在这里处理，否则会被当成"未知面板 build"。
                    if path == "/viz/build.csv":
                        # build_csv 同样会经 build_sql 做目录校验；它的 PortalError
                        # 由外层统一的 PortalError 分支接住（这里已在 try 内）。
                        return self._send(200, V.build_csv(c, qs), "text/csv; charset=utf-8",
                                          {"Content-Disposition":
                                           'attachment; filename="build.csv"'})
                    mm = re.match(r"^/viz/([A-Za-z0-9_]+)\.csv$", path)
                    if mm:
                        it = V.by_id(mm.group(1))
                        if not it:
                            raise PortalError(404, "未知面板：%s" % mm.group(1))
                        cols, rows = V.csv_rows(c, it)
                        return self._send(200, to_csv(cols, rows), "text/csv; charset=utf-8",
                                          {"Content-Disposition":
                                           'attachment; filename="%s.csv"' % it["id"]})
                    raise PortalError(404, "没有这个页面：%s" % path)
                mm = re.match(r"^/talent/([A-Za-z0-9_\-]+)$", path)
                if mm:
                    return self._send(200, view_talent_one(c, mm.group(1), qs))
                mm = re.match(r"^/occupation/([A-Za-z0-9_\-]+)$", path)
                if mm:
                    return self._send(200, view_occupation_one(c, mm.group(1), qs))
                if path == "/sql.csv":
                    return self._send(200, view_sql(c, qs, want_csv=True),
                                      "text/csv; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="query.csv"'})
                mm = re.match(r"^/analyze/([a-z0-9]+)(\.csv)?$", path)
                if mm:
                    aid, is_csv = mm.group(1), bool(mm.group(2))
                    body = view_analyze(c, qs, aid=aid, want_csv=is_csv)
                    if is_csv:
                        return self._send(200, body, "text/csv; charset=utf-8",
                                          {"Content-Disposition":
                                           'attachment; filename="%s.csv"' % aid})
                    return self._send(200, body)
                mm = re.match(r"^/t/([A-Za-z0-9_]+)(\.csv)?$", path)
                if mm:
                    name, is_csv = mm.group(1), bool(mm.group(2))
                    require_table(name)
                    if is_csv:
                        return self._send(200, self._table_csv(c, name, qs),
                                          "text/csv; charset=utf-8",
                                          {"Content-Disposition":
                                           'attachment; filename="%s.csv"' % name})
                    return self._send(200, view_table(c, name, qs))
                mm = re.match(r"^/e/([A-Za-z0-9_]+)$", path)
                if mm:
                    return self._send(200, view_entity(c, mm.group(1), qs))
                return self._send(404, page("未找到", '<div class="note err">没有这个页面：%s'
                                            '</div><p><a href="/">回到总览</a></p>'
                                            % esc(path)))
        except PortalError as e:
            return self._send(e.status, page("出错了", '<div class="note err">%s</div>'
                                             '<p><a href="/">回到总览</a></p>' % esc(e.message),
                                             session=sess))
        except NeedsTier as e:
            # 页面在渲染过程中发现权限不够：用它渲染好的页面，但状态码必须是 403
            self._audit_detail = {"need": e.need}
            return self._send(403, e.body)
        except psycopg.OperationalError as e:
            # **数据库连不上/连接数打满**必须与"权限不足"区分开，而且不能是 500。
            # 实测背景：`max_connections = 50`，而门户**每个请求要开 3 条连接**
            # （会话查询 + 页面查询 + 审计各一条），线程模型又是"一请求一线程、无上限"。
            # 公网被并发打满时 PostgreSQL 会回 `sorry, too many clients already` ——
            # 那不是"服务器坏了"，而是**过载**，正确响应是 503 + Retry-After，
            # 让调用方（和 Cloudflare）知道该退避重试，而不是收到一个 500 后不断重试加剧过载。
            msg = str(e).splitlines()[0] if str(e) else ""
            overload = ("too many clients" in msg or "connection" in msg.lower()
                        or "could not connect" in msg.lower())
            if overload:
                self._audit_detail = {"overloaded": True, "db_error": msg[:200]}
                return self._send(503, page(
                    "服务繁忙",
                    '<div class="note warn"><b>服务繁忙，请稍后重试。</b></div>'
                    '<div class="card"><p>当前并发请求超过了数据库连接上限，'
                    '系统主动拒绝了这一请求，<b>没有</b>执行任何查询。'
                    '这是过载保护，不是数据出错。</p>'
                    + db_error_block(sess, msg[:200])
                    + '<p><a href="/">稍后重试</a></p></div>',
                    session=sess), extra={"Retry-After": "3"})
            self._audit_detail = {"db_error": msg[:200]}
            return self._send(503, page(
                "数据库暂不可用",
                '<div class="note err"><b>数据库暂时不可用。</b></div>'
                '<div class="card">' + db_error_block(sess, msg[:200])
                + '<p><a href="/">重试</a></p></div>',
                session=sess), extra={"Retry-After": "5"})
        except psycopg.errors.InsufficientPrivilege as e:
            # **权限不足是正常状态，不是 500。** 让数据库的拒绝变成一句人话。
            need = _required_tier_for(path)
            # 把数据库给的原话也带上：它会**点名**是哪个对象被拒（例如
            # "permission denied for view v_dimension_registry"），
            # 这比"你需要 T1"有用得多 —— 运维据此就知道该给哪个等级授哪张表。
            # 原因交给 _send 统一记（覆盖面单点化，细节不丢）
            self._audit_detail = {"need": need,
                                  "db_error": str(e).splitlines()[0][:200]}
            return self._send(403, view_no_permission(
                None, qs, sess, need=need, target=path,
                db_error=str(e).splitlines()[0]))
        except psycopg.errors.UndefinedTable as e:
            return self._send(404, page("对象不存在", '<div class="note err">%s</div>'
                                        '<p><a href="/schema">表与视图</a></p>'
                                        % esc(str(e).splitlines()[0]), session=sess))
        except psycopg.Error as e:
            return self._send(500, page("数据库错误",
                                        '<div class="note err">%s</div>'
                                        % esc(str(e).splitlines()[0]), session=sess))

    def do_POST(self):
        """只处理登录。门户的其它写操作一律没有 —— 这是"只读门户"的边界。"""
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        sess = self._cookie_session()
        set_current_session(sess)
        if path != "/login":
            return self._send(405, page("不支持", '<div class="note err">门户只读，'
                                        '只接受 /login 的 POST。</div>', session=sess))
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._send(413, page("请求过大", '<div class="note err">表单过大。</div>',
                                        session=sess))
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        email = (form.get("email", [""])[0] or "").strip()
        pw = form.get("password", [""])[0] or ""
        try:
            # 登录要**写**一行会话，所以这条连接不能是只读事务；
            # 而且要 as_role=False —— 认证函数只授给了 mt_portal，
            # 不授给四个等级角色（"能登录"与"能读哪个等级的数据"是两件事）。
            # 这不破坏"门户只读"：写的是 web_session（认证状态），
            # 不是任何业务数据 —— change_log 不会因此增长。
            with db(sess, readonly=False, as_role=False) as c:
                r = q(c, "SELECT * FROM mt.web_login(%s, %s)", (email, pw))[0]
        except psycopg.Error as e:
            return self._send(500, page("登录失败", '<div class="note err">%s</div>'
                                        % esc(str(e).splitlines()[0]), session=sess))
        if r["reason"] != "ok" or not r["session_id"]:
            log_visit(sess, "login_failed", "web", detail={"email_domain":
                      email.split("@")[-1] if "@" in email else "",
                      "reason": r["reason"]})
            # 锁定与"口令错"必须**分别提示**：
            #   · 锁定时告诉用户"多久后可以再试"，否则他会以为口令忘了；
            #   · 但锁定提示**不泄露该邮箱是否存在**（限流是按邮箱计的，
            #     存在与否都会走到这里，所以不构成账号枚举）。
            if r["reason"] == "too_many_attempts":
                until = r.get("locked_until")
                wait = ""
                if until is not None:
                    try:
                        mins = max(1, int((until - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
                        wait = "，约 %d 分钟后可再试" % mins
                    except Exception:                     # noqa: BLE001
                        wait = ""
                return self._send(429, view_login(None, {}, sess,
                                  err="尝试次数过多，该邮箱已被临时锁定%s。"
                                      "（这是防止口令被在线暴力猜解；"
                                      "如果这是你自己的账号，稍后再试即可）" % wait))
            return self._send(200, view_login(None, {}, sess,
                                              err="邮箱或口令不正确。"
                                                  "（系统刻意不区分这两种情况，"
                                                  "否则等于提供账号枚举接口）"))
        log_visit(Session(actor=r["actor"], tier=r["tier"], session_id=r["session_id"],
                          ok=True), "login", "web")
        # 会话 cookie 只放随机 id：等级由数据库里的行决定，不放客户端。
        # 三个标记一起才完整：HttpOnly 挡 JS 读取、SameSite=Lax 挡跨站携带、
        # **Secure 只在真的走 HTTPS 时加**（见 _cookie_flags：
        # 本机 http 下加了浏览器就不回传，表现为"登录成功但仍是未登录"；
        # 而公网走 Cloudflare 隧道时**必须**加，否则 cookie 可经明文泄露）。
        return self._send(302, b"", extra={
            "Location": "/",
            "Set-Cookie": "%s=%s" % (SESSION_COOKIE, r["session_id"])
                          + self._cookie_flags() + "; Max-Age=28800"})

    def _table_csv(self, c, name, qs) -> bytes:
        m = meta()
        cols = [x["column_name"] for x in m["cols"].get(name, [])]
        fcol = qs.get("col", [""])[0]
        fval = qs.get("val", [""])[0]
        where, params = browse_where(name, fcol, fval)
        rows = q(c, sql.SQL("SELECT * FROM {}.{}{}").format(
            sql.Identifier(SCHEMA), sql.Identifier(name), where), params)
        # 这里知道来源表，所以按 (表, 列) 精确判断禁令 —— 比按列名保守拦截更准
        return to_csv(cols, rows, table=name)


def serve(port=PORT):
    meta()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("[✓] 数据库门户已启动：http://127.0.0.1:%d" % port)
    print("    只监听回环地址；全站只读。Ctrl+C 停止。")
    print("    已载入 %d 个对象（表/视图）与全部约束、索引、触发器。"
          % len(META["rels"]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[✓] 已停止")
    finally:
        srv.server_close()


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def cmd_tables():
    m = meta()
    print("%-30s %-8s %-14s %8s %6s" % ("对象", "类型", "域", "行数", "列"))
    print("-" * 74)
    for r in m["rels"]:
        print("%-30s %-8s %-14s %8s %6d"
              % (r["name"], "表" if r["kind"] == "table" else "视图",
                 r["domain"], f"{r['rows']:,}", r["n_cols"]))
    print("-" * 74)
    print("共 %d 个对象，耗时 %.2f 秒" % (len(m["rels"]), m["count_seconds"]))
    return 0


def cmd_check():
    """自检：不开服务，把元数据与每个页面都渲染一遍。"""
    fails = []
    m = meta()
    tables = [r for r in m["rels"] if r["kind"] == "table"]
    unclassified = [r["name"] for r in tables if r["domain"] == "未归类"]
    print("对象：%d（表 %d / 视图 %d）" % (len(m["rels"]), len(tables),
                                          len(m["rels"]) - len(tables)))
    if unclassified:
        fails.append("未归类的表：%s" % "、".join(unclassified))
        print("  [FAIL] 未归类：%s" % "、".join(unclassified))
    else:
        print("  [PASS] 全部基础表都已归入某个域（%d 个域）" % len(DOMAINS))

    # 外键目标必须都在目录里（否则实体链接会 404）
    unknown_fk = sorted({b["child"] for t in m["inbound"] for b in m["inbound"][t]
                         if b["child"] not in m["by_name"]})
    if unknown_fk:
        fails.append("外键指向未知表：%s" % "、".join(unknown_fk))
    print("  [PASS] 外键的子表全部在目录中" if not unknown_fk
          else "  [FAIL] 外键指向未知表：%s" % unknown_fk)

    # 每张表都必须能渲染出详情页。
    # 用**特权连接**：`--check` 回答的是"页面代码本身能不能渲染"，
    # 不是"某个等级能不能看"——后者由 `--check-tiers` 单独回答。
    # 分成两件事的理由：权限不足是**预期状态**，把它混进构建期自检的失败项，
    # 会让"--check 红了"既可能是代码问题也可能是等级问题，等于两个信号互相掩盖。
    with admin_db() as c:
        for r in m["rels"]:
            try:
                b = view_table(c, r["name"], {})
                if b"<table" not in b:
                    fails.append("表 %s 渲染异常" % r["name"])
            except Exception as e:                       # noqa: BLE001
                fails.append("表 %s 渲染失败：%s" % (r["name"], e))
        for fn, args in ((view_home, (c, {})), (view_schema, (c, {})),
                         (view_search, (c, {})), (view_analyze, (c, {})),
                         (view_lineage, (c, {})), (view_sql, (c, {})),
                         (view_talent, (c, {})), (view_occupations, (c, {})),
                         (view_tree, (c, {})), (view_tree, (c, {"asof": ["2026-01-01"]})),
                         (view_match, (c, {})), (view_extend, (c, {})),
                         (view_quality, (c, {})), (view_dev_link, (c, {}))):
            try:
                fn(*args)
            except Exception as e:                       # noqa: BLE001
                fails.append("%s 渲染失败：%s" % (fn.__name__, e))
        # 可视化模块：图表库自检 + 15 个面板逐个渲染 + 每个面板都要能导 CSV
        try:
            import portal_viz as V
            import charts as CH
            for b in CH.self_check():
                fails.append("图表库自检：" + b)
            for it in V.VIZ:
                try:
                    V.panel(c, it)
                except Exception as e:                   # noqa: BLE001
                    fails.append("面板 %s 渲染失败：%s" % (it["id"], e))
                try:
                    V.csv_rows(c, it)
                except Exception as e:                   # noqa: BLE001
                    fails.append("面板 %s 导出失败：%s" % (it["id"], e))
            V.view_viz(c, {})
        except Exception as e:                           # noqa: BLE001
            fails.append("可视化模块失败：%s" % e)
        # 详情页：抽一个真实主键
        for fn, tbl, col in ((view_talent_one, "person", "person_id"),
                             (view_occupation_one, "occupation", "occupation_id")):
            v = q1(c, sql.SQL("SELECT {} FROM {} LIMIT 1").format(
                sql.Identifier(col), sql.Identifier(tbl)))
            if v is None:
                continue
            try:
                fn(c, str(v), {})
            except Exception as e:                       # noqa: BLE001
                fails.append("%s 渲染失败：%s" % (fn.__name__, e))
        for a in ANALYSES:
            try:
                view_analyze(c, {}, aid=a[0])
            except Exception as e:                       # noqa: BLE001
                fails.append("分析 %s 失败：%s" % (a[0], e))
        # 实体页：每张有主键且有数据的表抽一行
        for r in tables:
            pk = m["pk"].get(r["name"])
            if not pk or not r["rows"]:
                continue
            v = q1(c, sql.SQL("SELECT {} FROM {}.{} LIMIT 1").format(
                sql.Identifier(pk), sql.Identifier(SCHEMA), sql.Identifier(r["name"])))
            try:
                view_entity(c, r["name"], {"val": [str(v)]})
            except Exception as e:                       # noqa: BLE001
                fails.append("实体页 %s 失败：%s" % (r["name"], e))

    # 只读 SQL 校验器：反例必须被拒
    bad = ["DROP TABLE person", "SELECT 1; SELECT 2", "UPDATE person SET status='x'",
           "  ", "DELETE FROM person", "CREATE VIEW v AS SELECT 1"]
    for s in bad:
        try:
            validate_sql(s)
            fails.append("非法 SQL 未被拒绝：%r" % s)
        except PortalError:
            pass
    for s in ["SELECT 1", "with x as (select 1) select * from x", "select * from occupation"]:
        try:
            validate_sql(s)
        except PortalError as e:
            fails.append("合法 SQL 被误拒：%r（%s）" % (s, e.message))
    print("  [PASS] SQL 校验器：%d 个反例全部拒绝、3 个正例全部通过" % len(bad))

    if fails:
        print("\n[X] 自检失败 %d 项：" % len(fails))
        for f in fails:
            print("    - %s" % f)
        return 1
    print("\n[✓] 自检全部通过")
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--check", action="store_true", help="自检：元数据 + 每个页面渲染一遍")
    ap.add_argument("--tables", action="store_true", help="命令行列出全部表/视图与行数")
    ap.add_argument("--register-metrics", action="store_true",
                    help="把可视化面板的口径写入 mt.metric_definition（写操作，幂等）")
    a = ap.parse_args()
    if a.tables:
        return cmd_tables()
    if a.register_metrics:
        import portal_viz as V
        return V.register_metrics()
    if a.check:
        return cmd_check()
    if a.serve:
        serve(a.port)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
