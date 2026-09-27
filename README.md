# 医学生人才信息库 · MedTalent KB

> 面向医学硕博的**结构化人才信息数据库**：既能做高自由度的职业分析与人岗匹配，
> 本身又是一个**可以在线浏览、检索、分析、可视化的数据平台**。

[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16.8-336791.svg)](https://www.postgresql.org/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Tables](https://img.shields.io/badge/tables-78%20%2B%2014%20views-2f81f7.svg)](#数据库已就绪)
[![Tests](https://img.shields.io/badge/tests-15%20suites%20%C2%B7%20442%20assertions-2da44e.svg)](#测试)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-none%20for%20portals-8c959f.svg)](#设计纪律)

> 徽章是 README 里唯一的外部引用（只渲染成图片，不执行任何脚本，离线时退化为替代文字）。
> 两个站点本身是**零 JS、零 CDN** 的内联 HTML/SVG。

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心能力](#核心能力)
- [架构](#架构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [配置](#配置)
- [使用](#使用)
- [目录结构](#目录结构)
- [文档索引](#文档索引)
- [测试](#测试)
- [数据来源声明](#数据来源声明)
- [设计纪律](#设计纪律)
- [常见问题排查](#常见问题排查)
- [当前状态与路线图](#当前状态与路线图)
- [贡献](#贡献)
- [许可](#许可)
- [致谢与参考](#致谢与参考)

---

## 它解决什么问题

| 问题 | 现状 | 本项目 |
|---|---|---|
| 医学生不知道除了临床还能做什么 | 信息碎片化、靠口耳相传 | **17 个岗位族的岗位地图** + 进入路径 + 薪酬锚点 |
| 无法回答"我适合什么" | 只有简历文本，没有结构 | **能力主张 × 证据 × 可迁移性** 的结构化人才档案 |
| 无法解释"为什么匹配" | 黑箱打分 | 每条匹配给出**命中能力、缺口能力、补齐方向**，三态可解释 |
| 想加字段就得改代码 | 表结构僵化 | **三条扩展机制**：加码值 / 加字段 / 加断言，都不动核心表 |
| 数据库只是"存东西的地方" | 要另搭 BI 才能看图 | **数据库本身就是分析与可视化平台**：门户内建检索、在线分析、16 个面板与自助图表构建器 |

## 核心能力

- **岗位侧**：690 份结构化 JD → 2,760 条职责 + 2,970 条要求；151 个职业节点（17 族）挂在职业树上；能力概念映射覆盖率 **90.2%**（1380/1530，已剔除资格门槛类）。
- **人才侧**：能力主张 / 证据 / 学历 / 经历 / 偏好全部结构化；每份档案可追溯到原始页面（L0 按内容 sha256 落盘）。当前含 **120 份合成档案**（明确自证，见[数据来源声明](#数据来源声明)）。
- **可解释匹配**：`met / gap / unknown` 三态。**unknown 不是不合格**——它表示"没有观测到"，可能因为观测窗口没覆盖。
- **三条零 DDL 扩展机制**：加码值（`code_value` 一行）、加字段（`field_catalog` + 统一值表 `field_value`）、加断言（三元组缓冲）。
- **演化层**：市场变了，职业树就长——候选发现 → 证据门槛 → 提升/拆分/合并 → **ID 只退役不复用**，`occupation_asof()` 可回溯任一时点的树；能力-职业映射**版本化**（每次重算=一版 + 漂移检测）。
- **数据平台**：`/viz` 16 个可视化面板（口径可核）+ `/viz/build` 自助图表构建器 + `/quality` 成熟度仪表盘 + `/schema` `/t/<表>` `/e/<表>` `/search` `/analyze` `/sql` `/lineage`。

## 架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│  展示与分析层（两个进程，两个端口 —— 这是有意的信任边界）                  │
│                                                                          │
│   :8082 只读门户  portal.py + portal_viz.py + charts.py                  │
│     总览 / 可视化 / 图表构建器 / 人才库 / 职业库 / 职业树 / 匹配          │
│     扩展与演化 / 表与视图 / 实体页 / 检索 / 分析 / 质量 / 血缘 / SQL      │
│     ↑ 全站只读：写操作由 PostgreSQL 的 READ ONLY 事务拒绝                │
│                                                                          │
│   :8083 开发者模式 portal_dev.py（可写，独立进程）                        │
│     SQL 控制台（默认试运行后回滚 · 写走 POST）/ 动态建模 / 运维工具       │
│                        │                                                 │
└────────────────────────┼─────────────────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  语义与分析层  metrics.py（度量的单一定义）· metric_definition（注册表）  │
│                code/analytics · code/gates · code/evolve                 │
└────────────────────────┬─────────────────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  L2 语义层   概念本体 · 代码表 · 字段目录 · 分类树 · 访问策略              │
├──────────────────────────────────────────────────────────────────────────┤
│  L1 规范层   78 张表 + 14 个视图：治理域 / 语义域 / 人才域 / 机会域 / 连接域│
│              95 个外键 · 触发器强制的不变量 · append-only 变更流水        │
├──────────────────────────────────────────────────────────────────────────┤
│  L0 原始层   data/raw/<来源>/<日期>/<sha256>.html + .src 侧车（只增不改） │
└──────────────────────────────────────────────────────────────────────────┘
```

对标与借鉴：**UK Biobank**（字段×实例×数组、字段级访问策略、分类树双向计数）、
**OMOP**（原文与标准码并存、映射血缘、观测期纪律）、**Croissant**（数据集描述用 JSON-LD + SemVer + sha256）、
**CHARLS**（无 codebook 不发布）。详见 [`docs/02-对标借鉴清单.md`](docs/02-对标借鉴清单.md)。

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| PostgreSQL | **16.8** | 便携版已随仓库部署方案安装（`ops/install_postgres.py`），**无需管理员权限** |
| Python | **3.11+** | 实际在 3.14 上验证 |
| psycopg | 3.3.6 | 唯一必需的运行期依赖（`psycopg[binary]`） |
| 可选：jieba / openpyxl / pandas | — | 分别用于中文分词、XLSX 导出、数据分析 |
| 可选：pgvector | — | 向量检索模块 `004_vector.sql`，**本机不可用时按设计跳过** |
| 可选：Chrome / Edge | — | 仅在生成静态页 PDF 时需要 |

两个门户站点**不需要任何前端框架、构建步骤或 CDN**（标准库 `http.server` + 内联 SVG）。

## 快速开始

```powershell
# 1) 进入项目
cd D:\wbo-workspace\medtalent-db

# 2) 启动数据库（便携版，仅监听 127.0.0.1:55432）
python ops\pg.py start

# 3) 建库 + 应用 12 个迁移（幂等，可重复执行）
python ops\pg.py up

# 4) 导入数据字典（49 代码表 / 302 代码值 / 112 字段目录）
python code\load_catalog.py

# 5) 造演示数据：岗位侧 + 人才侧（全部是合成语料，见"数据来源声明"）
python ops\fixtures\build_job_side.py       # JD 语料 → 采集 → 解析 → 映射 → 质量门
python ops\fixtures\gen_talent.py           # 120 份合成人才档案
python code\demo\console.py --seed 12       # 12 份演示人才 + 匹配结果

# 6) 一条命令起两个站点（会先探活数据库并做一次渲染自检）
python code\demo\serve_all.py
```

第 6 步会打印可点的 URL。首次跑完应该看到：

```
  [OK  ] PostgreSQL 已在运行（127.0.0.1:55432 / medtalent）
  [OK  ] 只读门户 portal.py           8.8 秒
  [OK  ] 开发者模式 portal_dev.py      7.5 秒
  [OK  ] 只读门户    http://127.0.0.1:8082
  [OK  ] 开发者模式（可写） http://127.0.0.1:8083
```

只想校验不想起服务：`python code\demo\serve_all.py --check-only`（约 16 秒）。
Windows 上也可以直接双击 `serve.cmd`。

## 配置

| 项 | 默认值 | 在哪里改 |
|---|---|---|
| 数据库连接 | `host=127.0.0.1 port=55432 dbname=medtalent user=postgres`（trust 认证，仅回环） | `code/db.py` 与各脚本顶部的 `DSN` |
| 数据目录 | `.tools/pgsql`（程序）、`.tools/pgdata`（数据） | `ops/pg.py` |
| 只读门户端口 | `8082` | `python code\demo\portal.py --serve --port N` |
| 开发者模式端口 | `8083` | `python code\demo\portal_dev.py --serve --port N` |
| 业务控制台端口 | `8081` | `python code\demo\console.py --serve --port N` |
| 表单站点端口 | `8080` | `python code\demo\app.py --serve --port N` |
| 质量门目标 | 族覆盖 17 / 必填 99% / 可回溯 100% / 概念映射 70% | **`code/metrics.py`（单一定义，不要在多处各写一遍）** |
| 备份保留策略 | 日 7 / 周 4 / 月 6，至少 3 份 | `ops/backup/backup.py` |
| 采集礼貌延迟 | 见 `code/collect/run.py --delay` | 命令行参数 |

## 使用

### 1. 浏览与分析（只读门户，:8082）

```
/viz             16 个可视化面板：数据规模 / 质量 / 岗位市场 / 能力图谱 / 人才供给 / 匹配 / 演化
/viz/build       自助图表构建器：选表 → 选维度 → 选度量 → 图 + SQL + CSV
/quality         成熟度仪表盘：完整性不变量、质量门、填充率、空值率、新鲜度、审计、备份
/talent          人才库 + 6 个在线分析          /occupations  职业库 + 6 个在线分析
/tree            职业树 + as-of 时间旅行        /match        能力-职业匹配（三态可解释）
/extend          三条扩展机制 + 职业树生长的使用痕迹
/schema /t/<表> /e/<表>?val=<主键>  表清单 / 表结构与真实数据 / 实体页交叉引用
/search /analyze /sql /lineage      跨 7 源检索 / 8 个分析 / 只读 SQL / 血缘与 L0
```

**每个数字旁边都有它的 SQL**，这是本项目对"口径可核"的具体做法。

### 2. 自助分析（不改一行代码）

`/viz/build` 让用户或开发者自己组合：选一张表 → 选维度列 → 选度量列与聚合 → 出图。
表名列名先与系统目录比对，再用 `psycopg.sql.Identifier` 转义，**所以这条路径不引入注入面**，
也不需要给人开写权限。结果可导 CSV，执行的 SQL 直接显示在图上。

### 3. 现场扩展（开发者模式，:8083）

```
/sql      多语句、单事务，默认"试运行后回滚"，勾选提交才落地；逐条给出影响行数
/model    调用 add_dimension / create_instance / set_value / deprecate_dimension
          每次操作后给出证据表：基础表数与 person 列数必须"不变"（= 零 DDL）
/tools    一键驱动既有 CLI：重建派生层 / 发现候选 / 重算版本 / 质量门 / 复位基线 / 备份
/audit    审计流水        /migrate  迁移清单与结构统计
```

### 4. 命令行

```powershell
python code\demo\portal.py --check          # 门户自检：把每个页面渲染一遍，不起服务
python code\demo\portal.py --tables         # 列出全部表/视图与精确行数
python code\demo\portal.py --register-metrics   # 把 16 个面板的口径写入 metric_definition
python code\gates\jd_quality_gate.py        # 岗位侧质量门（四项指标 + 每族抽检 CSV）
python code\evolve\discover.py              # 从 JD 里发现新的职业/能力候选
python code\evolve\recompute.py --drift     # 看最新一版的能力漂移
python ops\backup\backup.py backup          # 全库逻辑备份 + 用户数据 XLSX + sha256
```

## 目录结构

```
medtalent-db/
├─ docs/                    13 份方案与实施文档（先读 00，再读 01）
├─ research/                调研原始产出（证据留痕，不参与运行）
├─ schema/
│   ├─ sql/001..012         PostgreSQL 迁移，唯一的结构变更入口
│   ├─ catalog/             数据字典即代码（代码表 / 字段目录 / 职业树 / 概念词表 CSV）
│   ├─ json/ ontology/ examples/
├─ code/
│   ├─ db.py metrics.py     访问层 · **度量的单一定义**
│   ├─ validate_*.py        两个静态校验器（词表 / Schema）
│   ├─ load_catalog.py compliance_check.py
│   ├─ collect/ parse/      采集管道（合规门禁 + L0 去重 + 逐条血缘）· JD 解析器
│   ├─ analytics/ gates/    概念与职业映射 · 岗位侧质量门
│   ├─ evolve/              演化层：候选发现 / 提升 / 版本化重算 / 基线复位
│   ├─ bridge/              小程序接入：交换包 / outbox / 投影 / 匹配 / HTTP API
│   └─ demo/                两个站点 + 图表库 + 业务控制台 + 表单站点
│       ├─ serve_all.py     ★ 一条命令起两个站点（给人校验用）
│       ├─ portal.py        只读门户（21 个页面）
│       ├─ portal_viz.py    可视化：16 个面板 + 图表构建器 + 度量注册表
│       ├─ charts.py        纯 Python 内联 SVG 图表库（零依赖、可断言）
│       ├─ portal_dev.py    开发者模式（可写，独立进程）
│       └─ console.py app.py static_site.py
├─ ops/
│   ├─ pg.py install_postgres.py start-db.cmd stop-db.cmd serve.cmd
│   ├─ backup/backup.py     逻辑备份 + XLSX 副本 + GFS 保留 + 恢复核对
│   ├─ fixtures/            合成语料与合成人才档案生成器
│   ├─ tests/run_all.py     ★ 一条命令跑完全套回归（含耗时与 --fast 档）
│   └─ README-database.md   数据库运维手册（连接/启停/备份/迁移纪律/已知坑）
└─ data/raw/                L0 原始层：只增不改，永不删除
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/00-实施方案总纲.md`](docs/00-实施方案总纲.md) | ★ 入口：目标、架构、路线图、验收 |
| [`docs/01-数据模型设计.md`](docs/01-数据模型设计.md) | ★ 数据主干（实体、扩展机制、全局约定） |
| [`docs/02-对标借鉴清单.md`](docs/02-对标借鉴清单.md) | UK Biobank / OMOP / Croissant / CHARLS 的可移植设计 |
| [`docs/03-岗位图谱构建方案.md`](docs/03-岗位图谱构建方案.md) · [`04-人才档案与证据模型.md`](docs/04-人才档案与证据模型.md) | 需求端 / 供给端怎么建 |
| [`docs/05-匹配与分析引擎方案.md`](docs/05-匹配与分析引擎方案.md) · [`06-合规与数据治理.md`](docs/06-合规与数据治理.md) | 匹配算法与可解释性 · PIPL/GDPR、访问分级、审计 |
| [`docs/07-数据字典与扩展机制.md`](docs/07-数据字典与扩展机制.md) · [`09-动态建模与前端表单方案.md`](docs/09-动态建模与前端表单方案.md) | 字段目录/代码表操作手册 · 行/列动态增删（含实测） |
| [`docs/08-实施计划与任务分解.md`](docs/08-实施计划与任务分解.md) | ★ 逐任务可执行计划与执行状态 |
| [`docs/10`](docs/10-岗位侧实施记录.md)–[`13`](docs/13-演化层-职业树与能力的持续生长.md) | 岗位侧实施记录 · 小程序接入 · 数据安全与演示 · 演化层 |
| [`ops/README-database.md`](ops/README-database.md) | 数据库运维手册 + **全部已踩过的坑** |

## 测试

```powershell
python ops\tests\run_all.py            # 完整档：15 套 + 2 个收尾动作，打印每步耗时
python ops\tests\run_all.py --fast     # 快速档：改动后随手跑
python ops\tests\run_all.py --only 4,13    # 定点排查
python ops\tests\run_all.py --list
```

**15 套 / 442 项断言**，全部通过：

| 套件 | 断言 | 套件 | 断言 |
|---|---|---|---|
| 1 目录与词表静态校验 | 0 错 0 警 | 9 小程序 bridge | 50 |
| 2 Schema 静态校验 | 0 错 0 警 | 10 备份/保留/恢复 | 32 |
| 3 码值一致性（存码不存标签） | 2 | 11 控制台端到端 | 39 |
| 4 门禁回归 + 完整性不变量 | 13 | 12 演化层（职业树生长） | 33 |
| 5 Schema 冒烟 | 7 | 13 数据库门户 | 122 |
| 6 动态行列增删（零 DDL） | 28 | 14 开发者模式（可写） | 39 |
| 7 动态表单服务 | 30 | 15 可视化与分析层 | 31 |
| 8 L0 采集与去重 | 16 | 收尾 R1/R2 | 复位基线 · 重建派生层 |

几条**值得单独说**的断言，它们守的是容易骗人的地方：

- 页面行数 == 库里 `count(*)` 的精确值（不是 `pg_class.reltuples` 估算）；
- 外键链接**逐个回库确认目标行存在**（交叉引用不能是死链）；
- 遍历全部 92 个对象的页面后 `change_log` **不增长**（证明门户一个字节都没写）；
- `SELECT 1 INTO t` 被 PostgreSQL 只读事务拒绝（证明真正的防线在数据库，不是我们的正则）；
- **同一个指标在质量门脚本 / 门户 / 可视化三处的取值必须完全相同**（实测踩过 90.2% vs 61.7%）；
- 加一个维度后**基础表数与 person 列数不变**（这才是"零 DDL"的证据）。

> ⚠️ **静态校验是必要不充分的**：至今 **41 个问题**全部只在真库执行/回归测试时才暴露，
> 静态检查一个都没抓到。新增迁移后至少跑 `run_all.py --only 1,2,3,4,5`。

## 数据来源声明

> **本仓库内的岗位语料与人才档案全部是合成数据**，不对应任何真实个人、机构或招聘。
> 目的只是让岗位图谱、技能供给、缺口分析、匹配这些能力有足够结构化的样本可看。

合成数据是**自证**的，一句话就能筛出可对外与不可对外的行：

| 标记 | 含义 |
|---|---|
| `person.quality_flags = {synthetic_fixture}` | 一眼可辨；`/talent` 页顶部同样标注份数 |
| `person_id LIKE 'per_mock_%'`，`subject_code = 'MT-MOCK-####'` | 人才侧前缀 |
| 证据 URI 使用 `.invalid` 保留域；论文 DOI 使用 `10.99999/mock-*` 合成号段 | **永不解析到真实来源** |
| 机构名统一带"（虚构机构 / 虚构企业）"后缀；期刊写"合成示例…（虚构，非真实刊物）" | |
| **不写 `person_pii`** | 档案里没有任何姓名/电话/邮箱，连加密字段都不产生 |

`ops/fixtures/README.md` 记录了生成/清除方法、证据口径与已知限制。
`ops/fixtures/probe_constraints.py` 用 12 条探针验证"哪些写法会被数据库拒绝"（12/12 全部被拒）。
清除：`python ops\fixtures\reset_talent.py`（只删 `per_mock_%` 及其从属行）。

## 设计纪律

这些不是口号，每一条都有对应的测试或数据库约束在守：

1. **原始层不可变**。任何解析结果都能追溯到原文；换更好的解析器可以全量重跑。
2. **字段必须登记**。新字段先进 `field_catalog` / `attribute_definition`，否则数据库直接拒绝写入（触发器执行）。
3. **结论必须带证据**。能力主张无证据则置信度封顶 0.5；对外结论必须能按证据分级（A/B/C/D）过滤。
4. **枚举存码不存标签**。界面上永远显示中文，库里永远存码——由 `code_conformance.sql` 守住。
5. **空就是空**。不用默认值、行业均值或推算值填充；空表、未使用的机制、0 行的分析都如实标注。
6. **行数用精确 `count(*)`**，不用 `reltuples` 估算（实测它已经过时：`code_table` 估 53 / 实 49）。
7. **一个指标只有一个定义**。所有度量口径集中在 `code/metrics.py`，跨组件一致性由测试断言。
8. **只读就是能被证明的只读**。门户与开发者模式分成两个进程，正是为了让"门户写不进去"可被验证，而不是靠相信。
9. **零 JS / 零 CDN**。图表是服务端生成的内联 SVG，拷走一个 HTML 文件就能看。

## 常见问题排查

**`psql: error: ... No such file or directory` 或中文乱码**
含中文的 SQL 必须写成 `.sql` 文件再 `python ops\pg.py sql <文件>`。
Windows 下 `psql -c "中文…"` 会走 ANSI argv，中文会被损坏。

**`.cmd` 脚本报 "not recognized as an internal command"**
批处理文件必须**纯 ASCII**：cmd.exe 按 OEM 代码页（GBK）解析 `.cmd`，中文注释会破坏语法。

**数据库没在跑 / 连接被拒**
```powershell
python ops\pg.py status
python ops\pg.py start
```
数据库**不作为 Windows 服务自启**（`schtasks` 需要管理员权限），
已改为在启动文件夹放一个 `MedTalentPG.cmd` 登录自启。详见 `ops/README-database.md`。

**`function mt.set_value(... double precision) does not exist`**
Python `float` 被推断为 `double precision`，而函数签名是 `numeric`。显式转换：`%s::numeric`。

**`syntax error at or near "$1"`**
`SET` 这类工具语句**不接受参数占位符**（扩展协议把它发成 `$1`）。常量用 `sql.Literal` 内联。

**某个图表/分析口径和别处对不上**
不要在两处各算一遍。所有指标口径在 `code/metrics.py`；`ops/tests/viz_test.py` 里有一条
跨组件一致性断言会直接失败。**这个坑真实发生过**（质量门 90.2% vs 可视化页 61.7%，
差别只在分母用 `IN (RT5..RT8)` 还是 `NOT IN (RT5,RT6)`）。

**中文文件名/内容被写坏**
改含中文的 `.py` **必须用 Python 的 `io.open(..., encoding='utf-8')`**，
不要用 PowerShell 的 `Get-Content`/`Set-Content` 做字符串替换（已因此毁过一次文件）。

**端口冲突**
`/viz` 等页面用 8082、开发者模式 8083、业务控制台 8081、表单站点 8080；测试各自用 8097–8104。
全部只监听 `127.0.0.1`。

更多见 [`ops/README-database.md`](ops/README-database.md)（含全部已踩过的坑与 pgvector 启用步骤）。

## 当前状态与路线图

**已完成**

- [x] **T01–T05** 数据库引导 / 字典导入 / 完整性与审计 / 属性登记门禁 / 采集合规校验
- [x] **T06–T10** 职业分类骨架（151 节点）· 采集管道 · JD 解析器 · 岗位侧质量门 · 能力权重矩阵
- [x] **T11** 小程序接入适配器（交换包八条规则 / outbox / 投影 / 匹配 / 令牌保护的 HTTP API）
- [x] **T12** 数据安全（全库备份 + XLSX 副本 + sha256 + GFS 保留 + 恢复到新库并逐表核对）
- [x] **T13** 演化层（职业树生长 / 能力生长 / 版本化映射与漂移检测）
- [x] **数据门户**（只读，21 页）· **开发者模式**（可写，6 页）· **可视化与分析层**（16 面板 + 自助构建器）· **质量仪表盘**
- [x] **人才侧合成语料**（120 份，自证 + 幂等 + 内容指纹门禁）· **15 套回归 442 项断言**

**未开始 / 待决策**

- [ ] **T14–T16** 人才侧真实档案（当前为合成数据）与真实的院校/机构主数据
- [ ] **T17–T20** 匹配引擎 v1（当前是规则版 `concept_overlap`）与 marts
- [ ] **T21–T23** 访问控制落地 / 数据集发布 / 质量看板对外
- [ ] 接入 **1–2 家真实合规岗位来源**替换 mock 语料（需先确认来源与授权）
- [ ] 对方正式的 `exchange.schema.json` 与 crosswalk 字典（当前故意留空，适配器报 `unmapped*` 而不猜测）
- [ ] 111 个 `code_status='E'`（估算）的 ISCO-08 外部码待官方核验
- [ ] pgvector 启用（本机无编译器，EDB 二进制不含服务端头文件）

## 贡献

1. 改结构只能改 `schema/sql/` 下的迁移文件（`BEGIN; … COMMIT;` + `SET search_path TO mt, public;`，
   约束用 `DROP … IF EXISTS` + `ADD` 写成可重放）。
2. 改完至少跑 `python ops\tests\run_all.py --only 1,2,3,4,5`；提交前跑 `--fast`。
3. **新增指标口径必须写进 `code/metrics.py`**，不要在任何页面里另写一遍。
4. 新增测试请断言**数据库状态**而不是页面文本——页面文案会变，数据不该变。
5. 提交前用 `python code\demo\serve_all.py --check-only` 确认两个站点仍能渲染。

## 许可

**尚未指定许可证。** 这需要仓库所有者决定：未指定时默认保留所有权利，
他人不得复制、分发或用于衍生作品。若要开源，请在 `LICENSE` 中明确选择
（MIT / Apache-2.0 / GPL-3.0 各有不同的专利与传染性条款）。

在此之前，本仓库中的**合成数据**可自由用于评估；**代码**的使用请联系作者。

## 致谢与参考

- 对标设计参考：UK Biobank、OMOP CDM、Croissant 1.0、CHARLS、ESCO / ISCO-08 / O*NET
- 可视化与语义层设计参考：[BI & Visualization Patterns](https://github.com/vasilyu1983/AI-Agents-public/blob/main/frameworks/shared-skills/skills/data-lake-platform/references/bi-visualization-patterns.md)（Metabase / Superset / Grafana 的分工与"语义层"模式）、[README Best Practices](https://github.com/vasilyu1983/AI-Agents-public/blob/main/frameworks/shared-skills/skills/docs-codebase/references/readme-best-practices.md)
- 小程序接入契约：`medtalent-integration-schema` v1.0.0（见 [`docs/11-小程序接入适配器.md`](docs/11-小程序接入适配器.md)）
