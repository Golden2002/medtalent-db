# ops/fixtures —— 本地 fixture（**全部是合成数据**）

> ⚠️ **声明：本目录生成的一切语料与档案都是合成数据，不是真实个人、不是真实机构、不是真实招聘。**
> 目的只是让"岗位图谱 / 技能供给 / 缺口分析 / 匹配"这些在线分析有足够结构化的样本可看。
> 任何一条记录都不得当作真实来源引用、对外发布或与真实主体对应。

## 目录内容

| 文件 | 作用 |
|---|---|
| `gen_jd_corpus.py` | 生成 mock 招聘页 HTML（17 个岗位族，刻意植入各种写法供解析器练手） |
| `build_job_side.py` | 一键复现整条**岗位侧**流水线（语料 → 采集 → 解析 → 概念映射 → 质量门） |
| `reset_fixture.py` | 清除岗位侧 fixture（仅 `src_fixture_*` 来源） |
| `gen_talent.py` | **生成 mock 人才档案（约 120 份，人才侧）** |
| `reset_talent.py` | **只清除 `per_mock_%` 人才及其从属行**（绝不碰非 mock 数据） |
| `check_talent.sql` | 人才侧验收查询（行数 / 学历 / 专业 / 技能覆盖 / 证据分级 / 越界率） |
| `check_talent_analytics.sql` | 用 `v_skill_supply` / `v_skill_demand` / `v_gap_candidates` 验证"能被分析" |
| `probe_constraints.py` | 逐条探针验证"哪些写法会被 CHECK / 触发器 / 外键拒绝"（每条 rollback，不留数据） |

## ID 命名空间约定（**别复用别人的前缀**）

踩过的坑（问题 #38）：`dynamic_model_test.py` 原本用 `per_mock_01…20` 建实例，
并断言 `count(*) FROM person WHERE person_id LIKE 'per_mock_%'` 等于 20。
人才夹具生成 120 份 `per_mock_0001…0120` 之后，这个断言数出 140，
测试失败——**但它失败的根因不是被测代码坏了，而是两个组件共用了同一个 ID 前缀**。
更危险的是 `reset_talent.py` 删的正是 `per_mock_%`，会连带删掉别的组件的行。

| 前缀 | 归属 | 是否持久 |
|---|---|---|
| `per_mock_*` | **只属于人才夹具**（`gen_talent.py` / `reset_talent.py`） | 持久 |
| `per_dmtest_*` | 动态建模测试（`ops/tests/dynamic_model_test.py`） | 事务内回滚 |
| `per_dev_*` | 开发者模式演示（`code/demo/portal_dev.py`） | 持久，需手工清 |
| `per_live_*` | 业务控制台演示（`code/demo/console.py --seed`） | 持久，可 `--reset` |
| `src_fixture_*` | 岗位侧夹具来源 | 持久 |
| `src_evotest_*` | 演化层测试来源 | 测试自清 |

**约定**：任何会**持久化**的夹具或测试，ID 前缀必须独占；
`reset_*` 只允许删自己那个前缀。这样"按名前缀清理"才是安全的。

---

## 一、人才侧 mock 档案（`gen_talent.py`）

### 生成 / 清除

```powershell
cd D:\wbo-workspace\medtalent-db

# 起库（已完成则跳过）
python ops\pg.py status
python ops\pg.py start

# 生成 120 份（默认 --count 120 --seed 42）
python ops\fixtures\gen_talent.py

# 先清旧 mock 人才再生成（换了 seed 或改了生成器都要加 --reset）
python ops\fixtures\gen_talent.py --reset --count 120 --seed 42

# 只清除，不生成
python ops\fixtures\reset_talent.py

# 验收
python ops\pg.py sql ops\fixtures\check_talent.sql
python ops\pg.py sql ops\fixtures\check_talent_analytics.sql
python ops\fixtures\probe_constraints.py
```

### 覆盖的维度与配额

| 维度 | 设计 |
|---|---|
| 学历（`education_record` 每人最高学历） | 硕士 60%（72）、博士 22.5%（27）、本科 17.5%（21） |
| 专业方向（12 个） | 临床 26、基础 12、药学 12、护理 12、预防 10、生医工 8、生统 8、中医 8、口腔 7、影像 6、临床药学 6、麻醉 5 |
| 能力主张 | 每人 3–10 条，共约 845 条，**覆盖 `concept` 表 39/40 个概念** |
| 证据分级 `cel_level` | E0 自述 / E1 证书 / E2 第三方评估 / E3 作品 / E4 履职记录，五级都有 |
| 等级依据 `level_basis` | LB1 自述、LB2 测评、LB3 证书、LB4 产出、LB5 上级评价、LB6 临床例数，六级都有 |
| 岗位族偏好不一致 | 33/120 人（28%）的 `preference(PF3)` 与其技能方向刻意不匹配 |

**技能分布按方向差异化**：每个方向有 2–3 个"招牌能力"必出现（`sig`），其余按权重抽，
所以 `check_talent.sql` 的"各专业方向最强概念"能看出结构差异 ——
基础医学→实验与转化研究、卫生统计→统计与数据分析、生物医学工程→软件与算法/器械、
预防医学→流行病学与公卫、药学→合规与质量体系、护理学→共情与用户理解、影像→器械与IVD。

### 证据口径（照 `docs/04-人才档案与证据模型.md`）

| 证据种类 | `evidence_type` | `cel_level` | `verifiability` | 对应 `level_basis` | 置信度上限 |
|---|---|---|---|---|---|
| 自述 | EV7 | E0 | 0 | LB1（level ≤ L3） | 0.50 ×V1 修正 = **0.45** |
| 证书 | EV1 | E1 | 5 | LB3 | 0.90 |
| 测评 | EV6 | E2 | 3 | LB2 | 0.80 |
| 上级/同行评价 | EV5 | E2 | 4 | LB5 | 0.80 |
| 论文/专利（DOI） | EV2 | E3 | 5 | LB4 | 0.85 |
| 作品/交付物 | EV3 | E3 | 3 | LB4 | 0.75 |
| 项目验收 | EV4 | E4 | 2 | LB4 | 0.75 |
| 出科/轮转记录 | EV8 | E4 | 2 | LB6（level ≤ L4） | 0.70 |

`skill_assertion` 的 `evidence_id` **恒非空**（有证据率 100%，docs/04 阈值 ≥70%）；
`level ≥ 4` 且 `level_basis = LB1` 的越界条数恒为 **0**。

### 合成数据是怎么"自证"的

* `person.quality_flags = {synthetic_fixture}`，一眼可辨；
* 全部 `person_id` 以 `per_mock_` 开头，`subject_code` 形如 `MT-MOCK-0001`；
* **不写 `person_pii`**：档案里没有任何姓名/电话/邮箱，连加密字段都不产生；
* 机构名统一带"（虚构机构 / 虚构企业）"后缀，期刊/会议写"合成示例…（虚构，非真实刊物）"；
* 论文 DOI 用 `10.99999/mock-*` 合成号段，证据 URI 用 `.invalid` 保留域（**永不解析到真实来源**）；
* 真实院校名只出现在学历字段（学历口径需要它，院校本身不是个人隐私）；
* `source_registry` 登记为 `src_fixture_talent_mock`（SRC7 人工录入 / credibility 0.50 / evidence_grade D）。

### 幂等与可复现

* 随机流由 `(seed, 序号)` 派生，**与生成顺序无关**；同 `--seed` 同 `--count` 逐字节可复现。
* 所有 ID 由前缀 + 序号（或内容键，如 `preference_id = prf_<pid>_<PF类型>`）决定，
  写入一律 `ON CONFLICT DO NOTHING` —— 重复执行**不会产生重复人**。
* `ingest_run.params.fingerprint` 存了本次生成内容的 SHA-1 指纹（对全部待写入行做规范化哈希）：
  * 指纹一致 → 报告"新增行：无"，静默跳过；
  * 指纹不一致且库里已有 mock 人才 → **拒绝执行并提示加 `--reset`**（避免半新半旧的混合样本）。
* 要换 seed 或改了生成逻辑：必须 `--reset`。

### 已知限制

1. `CON-K1-FIN`（财务分析与建模）是唯一没被覆盖的概念：它只属于跨赛道族 F07 的入门能力，
   而 33 个"偏好不一致"的人里恰好没人抽到它。其余 39/40 个概念都有覆盖（要求是 ≥25）。
2. 护理方向没有独立的"护士"职业节点，`employment_record.occupation_id` 借用
   `OCC-F15-02-05`（临床研究护士）/ `OCC-F01-02`（医学技术与医技），是近邻而非精确对应。
3. 观测窗（`observation_window`）按 docs/04 §6 给每人建了 `W1/W2` 两条，
   时间范围是按"生成基准日 2026-09-30"推定的，不是真实声明。
4. `gen_talent.py` 不写 `field_value`（动态模型表）与 `person_constraint`，人才侧的动态维度演示
   仍走 `code/demo/console.py --seed`。
