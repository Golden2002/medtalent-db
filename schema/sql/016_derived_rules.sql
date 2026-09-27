-- ============================================================================
-- 医学生人才信息库 · 016 开通派生口径与岗位侧落点 v1.0.0
--
-- 背景：docs/16 的体检结论第一条就是"让不可评的门禁可评"，并指出 8 个门禁里
-- 只有 3 个可评、权重 1.0 的 DIM_WORK_YEARS 因派生未实现而**从不拦人**。
-- 本迁移把"派生口径"从**注册表声明**变成**可执行规则**，并补上两处被漏掉的落点。
--
-- 【1】哪些维度现在真的有派生规则了
--   code/analytics/vector.py 的 DERIVE_PERSON / DERIVE_JOB 实现了：
--     人侧：DIM_WORK_YEARS（任职起止求和）、DIM_CITY_TIER（期望城市→层级）、
--           DIM_SCHOOL_TIER（院校标签→层次）、DIM_RESEARCH_LEVEL（作者位次→层级）
--     岗位侧：DIM_WORK_YEARS（经验要求文本→经验档）、DIM_CITY_TIER（岗位城市→层级）、
--             DIM_EMPLOYER_TYPE（雇主描述→单位类型）、
--             DIM_ACCEPT_CROSS_INDUSTRY（job_family=F16 即完全跨行）
--   刻度的来源：经验档读 CT_EXPERIENCE_BAND 的**标签边界**（1年以内/1-3年/…），
--   城市层级读 CT_CITY_TIER.external_mapping 里登记的城市清单 —— 都不在代码里
--   另写一套数字。没有刻度的（如"哪些专业算临床类"）不派生。
--
-- 【2】DIM_EMPLOYER_TYPE 的岗位侧落点是错的（不是缺列，是漏认）
--   注册表写 job_locator='unavailable'，job_gap 写"需新增 job_posting.employer_type
--   （该列确实不存在）"。但实测 job_posting.employer_name_raw **有值**，而且语料把
--   雇主匿名化成「某三甲医院」「某CRO公司」「某证券公司」这类**自带类型信息**的描述：
--     SELECT count(*) FROM job_posting WHERE employer_name_raw IS NOT NULL;  → 690/690
--   所以这不是缺字段，是漏认了已有的列。改为 derived:employer_name_raw。
--   ⚠ 该规则依赖本语料的匿名化命名约定，换成真实 JD 必须改为雇主主数据匹配 ——
--     这一点已写进 dimension.note，避免被当成通用方法沿用。
--
-- 【3】DIM_ACCEPT_CROSS_INDUSTRY 的岗位侧落点写成 derived:job_posting.job_family，
--   现在有了实现，note 里补上判据（F16 = 完全跨行，口径来自本表同族维度的 job_source）。
--
-- 可重放：三条 UPDATE 写成确定值；不修改 001-015；不建表不加列。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- 1. 岗位侧单位类型：从"无落点"改为"由雇主描述派生"
UPDATE dimension
   SET job_locator = 'derived:employer_name_raw',
       job_source  = 'job_posting.employer_name_raw（语料中的匿名雇主描述）',
       job_gap     = '规则依赖本语料的匿名命名约定（某三甲医院/某CRO公司…）；'
                     '接入真实 JD 后必须改为雇主主数据匹配，不要沿用关键词规则',
       note = COALESCE(note || ' ', '') ||
              '【016 修正】原 job_locator 标为 unavailable、job_gap 写"需新增 employer_type 列"，'
              '但 employer_name_raw 实测 690/690 有值且自带类型信息 —— 是漏认已有列，不是缺字段。',
       version = '1.2.0', updated_at = now()
 WHERE dimension_id = 'DIM_EMPLOYER_TYPE';

-- 2. 跨行判定：补上判据
UPDATE dimension
   SET note = COALESCE(note || ' ', '') ||
              '【016】岗位侧判据：job_family = F16 即"完全跨行"，计为 Y；其余计为 N。',
       version = '1.2.0', updated_at = now()
 WHERE dimension_id = 'DIM_ACCEPT_CROSS_INDUSTRY';

-- 3. 已开通派生的维度：把"未实现"的旧说法改掉，并写明刻度来源
UPDATE dimension
   SET note = COALESCE(note || ' ', '') ||
              '【016 已实现】派生刻度取自 CT_EXPERIENCE_BAND 的标签边界（1年以内/1-3年/…），'
              'rank 用词表内序号而非 sort_order（否则 cmp_ordinal 的"差一档给 0.5"永不触发）。',
       version = '1.2.0', updated_at = now()
 WHERE dimension_id = 'DIM_WORK_YEARS';

UPDATE dimension
   SET note = COALESCE(note || ' ', '') ||
              '【016 已实现】层级取自 CT_CITY_TIER.external_mapping 登记的城市清单；'
              '二线及以下故意未登记（分界有争议），未登记城市如实记为缺映射而不猜。',
       version = '1.2.0', updated_at = now()
 WHERE dimension_id = 'DIM_CITY_TIER';

UPDATE dimension
   SET note = COALESCE(note || ' ', '') ||
              '【016 已实现】院校标签（中文）→ CT_SCHOOL_TIER 码，取层次最高者。'
              '岗位侧仍无落点，所以只补全人侧向量，不参与匹配。',
       version = '1.2.0', updated_at = now()
 WHERE dimension_id = 'DIM_SCHOOL_TIER';

UPDATE dimension
   SET note = COALESCE(note || ' ', '') ||
              '【016 已实现】按作者位次与产出类型判定：RO5 专利→RL5、通讯/主持→RL3、'
              '一作→RL2、其它学术产出→RL1、无→RL0。文献与科普作品**不计入**科研。',
       version = '1.2.0', updated_at = now()
 WHERE dimension_id = 'DIM_RESEARCH_LEVEL';

-- 4. 变更留痕，说明这一轮"从声明到可执行"动了哪些维度
INSERT INTO change_log (actor, object_type, object_name, change_type, to_version, detail)
SELECT session_user, 'dimension', x.did, 'update', '1.2.0',
       jsonb_build_object('reason', x.why, 'impl', 'code/analytics/vector.py')
  FROM (VALUES
    ('DIM_EMPLOYER_TYPE',        '落点从 unavailable 改为 derived:employer_name_raw（漏认已有列）'),
    ('DIM_WORK_YEARS',           '派生规则实现（任职起止求和 → EX 档）；这是权重 1.0 的门禁'),
    ('DIM_CITY_TIER',            '派生规则实现（城市 → 一线/新一线，映射已登记进字典）'),
    ('DIM_SCHOOL_TIER',          '派生规则实现（院校标签 → ST 码，仅补全人侧）'),
    ('DIM_RESEARCH_LEVEL',       '派生规则实现（作者位次 → RL 档，仅补全人侧）'),
    ('DIM_ACCEPT_CROSS_INDUSTRY','岗位侧判据写清（job_family = F16）')
  ) AS x(did, why)
 WHERE EXISTS (SELECT 1 FROM dimension d WHERE d.dimension_id = x.did);

COMMIT;
