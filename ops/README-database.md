# 数据库运维手册（本机 PostgreSQL 16.8）

> 本机**没有 winget/choco 且当前会话非管理员**，因此采用 EnterpriseDB 的
> `windows-x64-binaries.zip` 便携部署：解压即用、不改注册表、不装服务。
> 全部文件都在 `D:\wbo-workspace\.tools\` 下，删掉该目录即完全卸载。

## 1. 安装位置与连接参数

| 项 | 值 |
|---|---|
| 可执行文件 | `D:\wbo-workspace\.tools\pgsql\bin` |
| 数据目录 | `D:\wbo-workspace\.tools\pgdata` |
| 服务器日志 | `D:\wbo-workspace\.tools\pgdata\server.log` |
| 版本 | PostgreSQL 16.8（Visual C++ 1942, 64-bit） |
| 主机 / 端口 | `127.0.0.1` / **55432**（避开默认 5432，防止与既有实例冲突） |
| 超级用户 | `postgres`（本地 trust 认证，**仅监听回环地址**） |
| 业务库 / 角色 | `medtalent` / `medtalent` |
| Schema | `mt` |

连接：
```powershell
cd D:\wbo-workspace\medtalent-db
python ops\pg.py psql              # 进入交互式 psql（已自动带 mt 搜索路径）
python ops\pg.py psql -c "SELECT count(*) FROM person;"
```
或用任意客户端连 `127.0.0.1:55432`，库 `medtalent`，用户 `postgres`（无密码，仅本机可连）。

## 2. 日常命令

```powershell
python ops\pg.py status      # 查看运行状态 + 版本
python ops\pg.py start       # 启动
python ops\pg.py stop        # 停止
python ops\pg.py apply       # 按序执行 schema\sql\*.sql 全部迁移
python ops\pg.py reset --yes # 删除 mt schema 并重建（数据丢失，仅开发期使用）
python ops\pg.py up          # init+config+start+createdb+apply 一键起库（新机器首次）
```

**开机后不会自动启动**——已配置**登录自启**（免管理员）：
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MedTalentPG.cmd` → 调用 `ops\start-db.cmd`。
手工启停用上面两个命令（也可直接运行 `ops\start-db.cmd` / `ops\stop-db.cmd`，均幂等）。

> 为什么不用 Windows 服务：注册服务需要管理员权限，本机会话非管理员，`schtasks /Create` 亦被拒。
> 启动文件夹方案是用户级、免管理员、可随时删除的等效做法。

## 2.5 环境依赖（Python 侧）

| 依赖 | 状态 | 说明 |
|---|---|---|
| `psycopg[binary]` 3.3.6 | 已装 | 数据库访问（`code/db.py`、演示站点、Python 测试） |
| `jieba` 0.42.1 | 已装 | 中文分词（`search_document.body_tokens`） |

**本机 pip 踩坑记录**：环境变量里有 `HTTP_PROXY=http://127.0.0.1:7890`（本机代理），
pip 默认源是清华 TUNA。直连超时会表现为"卡住不动"。
实测可用的安装命令（腾讯云镜像最快，3.8s）：

```powershell
python -m pip install --index-url https://mirrors.cloud.tencent.com/pypi/simple/ `
  --trusted-host mirrors.cloud.tencent.com --timeout 60 --retries 3 <包名>
```

> 另注：本机 `curl.exe` 与 PowerShell 的 `Invoke-WebRequest` 走 schannel，
> 在沙箱下会报 `SEC_E_NO_CREDENTIALS`；**下载一律用 Python（urllib）**，其走 OpenSSL 正常。

## 3. 备份与恢复

```powershell
# 备份（自定义格式，含数据）
& "D:\wbo-workspace\.tools\pgsql\bin\pg_dump.exe" -h 127.0.0.1 -p 55432 -U postgres `
  -d medtalent -Fc -f "D:\wbo-workspace\medtalent-db\ops\backup\medtalent_$(Get-Date -f yyyyMMdd_HHmm).dump"

# 恢复
& "D:\wbo-workspace\.tools\pgsql\bin\pg_restore.exe" -h 127.0.0.1 -p 55432 -U postgres `
  -d medtalent --clean --if-exists "ops\backup\medtalent_YYYYMMDD_HHMM.dump"
```

## 4. 迁移纪律（重要）

- `schema/sql/*.sql` **按文件名顺序执行**，因此新迁移一律用递增编号：`006_`、`007_`…
- 已应用的迁移**不要改内容**（会与实际库不一致）；要改就新增一个编号文件。
- 每个迁移文件必须自带 `BEGIN; ... COMMIT;`，并 `SET search_path TO mt, public;`。
- 迁移里的 `ALTER TABLE ... ADD CONSTRAINT` 建议写成 `DROP CONSTRAINT IF EXISTS` + `ADD`，
  保证可重复执行（`006_integrity.sql` 即此写法）。
- **PL/pgSQL 函数必须写 `SET search_path = mt, public`**。否则函数体内未限定的表名会按
  调用方的 search_path 解析，实测报 `relation "category_node" does not exist`；
  这同时也是一道防 search_path 劫持的安全措施。
- 改完结构必须跑这些门：
  ```powershell
  python code\validate_catalog.py .      # 数据字典
  python code\validate_schema.py .       # SQL 结构（含保留字检查）
  python ops\pg.py sql ops\tests\code_conformance.sql    # 词表一致性（码 vs 标签混用）
  python ops\pg.py sql ops\tests\regression_guards.sql   # 门禁回归（10 用例）
  python ops\pg.py sql ops\tests\smoke_schema.sql        # 端到端冒烟（7 断言）
  python ops\tests\dynamic_model_test.py                 # 动态建模（28 断言，零 DDL）
  python ops\tests\form_server_test.py                   # 动态表单闭环（30 断言）
  ```

## 5. pgvector（向量检索）——当前未启用

`schema/sql/004_vector.sql` 依赖 pgvector 扩展，**当前环境未安装**，`pg.py apply` 会自动跳过，
数据库其余部分完全可用（匹配引擎退化为纯 BM25 + 结构化打分，符合 `docs/05` §8 的分期约定）。

本地不可直接编译的原因：EDB 的 binaries 包**不含服务端头文件**（`include/postgresql/server/` 缺失），
且本机 PATH 中没有 MSVC 工具链。

启用路径（任选其一）：
1. **官方源码编译**：装 Visual Studio Build Tools（含 C++ 工作负载）→ 下载与 16.8 匹配的
   PostgreSQL 源码 → 配置 `pg_config` 指向 `D:\wbo-workspace\.tools\pgsql` →
   在 pgvector 源码里 `nmake /F Makefile.win` → `nmake install`。
2. **可信预编译包**：从可信发布方获取与 PG16 / Windows x64 / VC 1942 匹配的
   `vector.dll` + `vector.control` + `vector--*.sql`，放入
   `pgsql\lib\` 与 `pgsql\share\extension\`。
   ⚠️ 检索到的 Windows 预编译包多来自低可信聚合站，**不建议直接用于生产数据**。
3. 安装后再执行 `python ops\pg.py sql schema\sql\004_vector.sql` 即可（该文件用了
   `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`，可安全重跑）。

## 6. 已知坑（都已踩过并修复，勿重复）

| 现象 | 原因 | 处置 |
|---|---|---|
| `invalid byte sequence for encoding "UTF8"` | Windows 下 psql.exe 按 ANSI 代码页解释命令行 argv，含中文的 `-c` 参数被破坏 | 已改为写 UTF-8 临时文件后 `-f` 执行（`ops/pg.py`、`code/compliance_check.py` 均已内置） |
| `CREATE TABLE constraint` 语法错误 | `constraint` 是 PostgreSQL 保留字 | 表已改名 `person_constraint`；`validate_schema.py` 增加保留字检查 |
| `value for domain verify_status_t violates check constraint` | **标签与码混用**：域写 `('raw','parsed')`，词表码是 `V0–V4`；`access_tier`、`retention_policy.action`、`incident_report.severity`、`dataset_release.status`、视图 `v_skill_demand` 同病 | 全库统一"**列里存码，标签只在 code_value**"；新增 `ops/tests/code_conformance.sql` 自动检测此类错误 |
| `relation "category_node" does not exist`（函数内） | PL/pgSQL 函数未固定 search_path | 函数一律加 `SET search_path = mt, public` |
| `initdb: could not create restricted token` | 沙箱禁止 `CreateRestrictedToken` | 该步骤失败不影响集群完整性（template0/1 与 postgres 库均已创建），实测可正常启动 |
| `pg.py psql -c` 参数被 argparse 吞掉 | `--` 开头参数 | 已改用 `parse_known_args` 透传 |
| `column reference "kind" is ambiguous` | PL/pgSQL 局部变量名与列名同名 | 局部变量一律加前缀（`v_kind`），不要用裸列名 |
| `only '%s' are allowed as placeholders, got '%'` | psycopg3 在**带参数**时拒绝裸 `%` | LIKE 通配符一律作为参数传：`LIKE %s` + `("F_DEMO%",)` |
| 未提供的列被写成 NULL，绕过列默认值 | `INSERT ... SELECT * FROM jsonb_populate_record(...)` 会把缺省列填 NULL | 只插入 payload 中显式给出的列（`008` 的 `create_instance` 已改） |
| 测试断言 303 却拿到 200 | urllib 默认自动跟随重定向 | 测试改用自定义 `NoRedirect` opener |
| `function mt.set_value(... double precision) does not exist` | Python float 被推断为 `double precision`，函数签名是 `numeric` | 显式转换：`%s::numeric` / `%s::text[]` |
| `.cmd` 脚本报 "not recognized as an internal command" | cmd.exe 按 OEM 代码页（GBK）解析 `.cmd` | 批处理文件一律**纯 ASCII**，注释写英文 |
| `curl.exe` / `Invoke-WebRequest` 报 `SEC_E_NO_CREDENTIALS` | 沙箱下 schannel 取不到凭据 | 网络下载一律走 Python（urllib + OpenSSL） |
| **"映射明明写了，权重却是 0 行"** | 派生器另开连接，读不到调用方事务里未提交的映射（跨连接可见性，静默） | 同一逻辑事务内的多步派生**共用连接**（`recompute(..., db=c)`）；统计只算 `valid_to IS NULL` |
| 职业树凭空少一枝、变更表里有指向不存在节点的记录 | 测试清理**按名字**删（`label LIKE TAG`），而它拆分的源节点是真实种子节点 | 清理**按引用关系**走（`from_ids`/`to_ids`）并恢复源节点；补 3 条完整性不变量进回归 |

## 6.5 迁移文件清单

| 文件 | 内容 |
|---|---|
| `001_core_schema.sql` | 核心 45 张表：治理域 / 语义域 / 人才域 / 机会域 / 连接域 |
| `002_ukb_patterns.sql` | 对标 UK Biobank 与 OMOP：分类树、统一值表、字段级访问策略、观测期、映射血缘等 |
| `003_retrieval.sql` | 全文检索（`search_document`，无需扩展） |
| `004_vector.sql` | 向量检索（需 pgvector，**当前跳过**） |
| `005_gov_compliance.sql` | 合规对象：撤回级联、主体权利、保留期、泄露事件、访问策略种子 |
| `006_integrity.sql` | 语义 CHECK、`updated_at` 自动维护、45 张表的 append-only 变更流水 |
| `007_attr_guard.sql` | 扩展属性门禁：未登记的 `attrs` 键与 `field_id` 在库层被拒 |
| `008_dynamic_model.sql` | **动态建模 API**：加维度 / 建实例 / 写值 / 废弃 / 表单 schema / 画像 |
| `009_occupation.sql` | 职业树外部码纪律：`code_status`（V 已核验 / E 估算 / N 无对应）+ 两个待办视图 |
| `010_bridge.sql` | 小程序 bridge：外部身份、应答会话、经历片段与任务、crosswalk、同步事件、墓碑、删除请求 |
| `011_backup.sql` | 备份与恢复台账：`backup_policy` / `backup_run` / `restore_run` / `v_backup_health` |
| `012_evolution.sql` | **演化层**：职业树时间维与迁移表、候选池、概念候选、**版本化**能力权重与漂移 |

## 7. 验收命令（一键体检）

```powershell
cd D:\wbo-workspace\medtalent-db

# 首选：一条命令跑完 12 套回归 + 2 个收尾动作（会先探活数据库）
python ops\tests\run_all.py
python ops\tests\run_all.py --list          # 看清单
python ops\tests\run_all.py --only 3,4      # 定点排查（不做收尾）
python ops\tests\run_all.py --after-only    # 只做收尾：复位职业树基线 + 重建派生层
```

单套单独跑：

```powershell
python code\validate_catalog.py .
python code\validate_schema.py .
python ops\pg.py sql ops\tests\code_conformance.sql
python ops\pg.py sql ops\tests\regression_guards.sql
python ops\pg.py sql ops\tests\smoke_schema.sql
python ops\tests\dynamic_model_test.py
python ops\tests\form_server_test.py
python ops\tests\collect_test.py
python ops\tests\bridge_test.py
python ops\tests\backup_test.py
python ops\tests\console_test.py
python ops\tests\evolution_test.py
python ops\pg.py psql -c "SELECT count(*) AS tables FROM information_schema.tables WHERE table_schema='mt' AND table_type='BASE TABLE';"
```

### 7.1 这条链路的顺序有语义，别按记忆手工跑

| 陷阱 | 后果 | 收尾动作 |
|---|---|---|
| `backup_test` 重跑 `jd_ingest` | `job_requirement` 是先删后插 → 概念映射与权重矩阵被清空，匹配**静默失效** | `R2` 重建派生层（`build_competency.py`） |
| `evolution_test` 会拆分**真实种子节点** | 种子节点停在 `retired`，职业树凭空少一枝；变更表留下 `to_ids` 悬空记录 | `R1` 复位基线（`restore_baseline.py`，可自愈历史残留） |

`run_all.py` 把这两条固化成了 `R1`/`R2`。手工跑时请自行补上，否则下一次
质量门会报「能力概念映射覆盖率 0.0%」——那不是门坏了，是派生层真的空了。

### 7.2 已知的、可接受的表增长

`job_competency_weight` 是**版本化**的：每次重算追加一版并给旧版写 `valid_to`，
不覆盖历史。跑完整套回归会触发多次重算，所以"全表历史"行数会持续增长
（当前约 900+ 行，当前有效始终 58 行）。这是设计意图，不是泄漏；
`build_competency.py` 的收尾统计会同时打印两个数字以免误读。
