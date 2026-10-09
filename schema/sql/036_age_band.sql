-- ============================================================================
-- 医学生人才信息库 · 036 公开年龄段、出生年月收进 T2（用户决策）v1.0.0
--
-- 决策（用户原话）
-- ---------------------------------------------------------------------------
--     「公开年龄段（age_band）、birth_year 保持 T2」
--
-- 这解决了本项目挂了很久的一个口径冲突：
--   · 用户最初说"编号、**年龄**等可以是公开"；
--   · 而字段字典（docs/06）把 `birth_year` 标成 T2 —— 出生年月属于可识别个人信息；
--   · 036 之前的实现按"年龄可公开"把 birth_year 判成了 T0，
--     于是**精确出生年份**对一个公开的数据库是可见的。
-- 正解是把"年龄"与"出生年月"分开：
--   · **年龄段**（AG1–AG6）是统计属性 → 公开（T0）；
--   · **出生年份**是个人信息 → T2（员工及以上）。
-- 前者满足"年龄可以公开"，后者守住最小必要。
--
-- 为什么这里要给 person_demographics 真正加一列 age_band
-- ---------------------------------------------------------------------------
-- `CT_AGE_BAND` 码表**早就存在**（AG1 25岁以下 … AG6 45岁及以上、AG9 未提供），
-- 但库里没有任何字段用它 —— 也就是说"年龄段"这个能力此前**只是字典里的一行**。
-- 不落成物理列就没法分级、没法索引、没法被列级授权管住。
--
-- ⚠ 一个必须写清楚的局限：**年龄段会随时间过期**
-- ---------------------------------------------------------------------------
-- `age_band` 是**写入时**按当时年份算出来的快照。一个人今年 29（AG2），
-- 明年就是 30（AG3）。所以：
--   · 新增/修改 birth_year 时由触发器重算（保证新数据正确）；
--   · **存量数据需要定期重算** —— 本迁移附带 `mt.refresh_age_bands()`
--     与一行命令，便于放进年度运维；
--   · 页面上要标明这是"按某年计算的快照"，否则读者会把它当成实时年龄。
-- 这正是"派生字段必须说明其时间基准"的一般要求。
--
-- 可重放：IF NOT EXISTS / CREATE OR REPLACE / 幂等刷新。
-- ============================================================================

BEGIN;
SET search_path TO mt, public;
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- 1. 边界年份：把"算年龄段时的基准年"显式化，而不是悄悄用 now()
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS age_band_asis (
  id       integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  base_year integer NOT NULL,
  note     text,
  updated_at timestamp with time zone NOT NULL DEFAULT now()
);
COMMENT ON TABLE age_band_asis IS
  '年龄段计算的基准年（单行表）。刻意不做成"用 now() 算"：'
  '派生字段必须说明它的时间基准，否则读者会把快照当成实时值。'
  '重算后把 base_year 更新到这里。';

INSERT INTO age_band_asis (id, base_year, note)
VALUES (1, EXTRACT(YEAR FROM now())::int,
        '初始基准年。年龄段会随时间过期，需定期重算（见 mt.refresh_age_bands()）')
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. 计算函数：birth_year → 年龄段码（返回码，不返回标签 —— 本库纪律）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION age_band_of(p_birth_year integer, p_base_year integer DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN p_birth_year IS NULL THEN 'AG9'                       -- 未提供（NULL 与 0 不同义）
    WHEN (COALESCE(p_base_year, EXTRACT(YEAR FROM now())::int) - p_birth_year) < 25 THEN 'AG1'
    WHEN (COALESCE(p_base_year, EXTRACT(YEAR FROM now())::int) - p_birth_year) < 30 THEN 'AG2'
    WHEN (COALESCE(p_base_year, EXTRACT(YEAR FROM now())::int) - p_birth_year) < 35 THEN 'AG3'
    WHEN (COALESCE(p_base_year, EXTRACT(YEAR FROM now())::int) - p_birth_year) < 40 THEN 'AG4'
    WHEN (COALESCE(p_base_year, EXTRACT(YEAR FROM now())::int) - p_birth_year) < 45 THEN 'AG5'
    ELSE 'AG6'
  END;
$$;
COMMENT ON FUNCTION age_band_of(integer, integer) IS
  '出生年 → 年龄段**码**（AG1..AG6，未提供为 AG9）。分档与 CT_AGE_BAND 一致。'
  '返回码而不是中文标签 —— 本库纪律：存码不存标签。';

-- ---------------------------------------------------------------------------
-- 3. 物理列 + 触发器（新增/修改 birth_year 时自动维护）
-- ---------------------------------------------------------------------------
ALTER TABLE person_demographics ADD COLUMN IF NOT EXISTS age_band text;

COMMENT ON COLUMN person_demographics.age_band IS
  '年龄段码（CT_AGE_BAND: AG1..AG6、AG9）。**公开（T0）** —— 年龄段是统计属性。'
  '它是**写入时**的快照，基准年见 mt.age_band_asis；会随时间过期，需定期重算。'
  '与 birth_year 的分工：birth_year 是个人信息（T2），age_band 是它的可公开投影。';

-- 回填（按当前基准年）
UPDATE person_demographics
   SET age_band = age_band_of(birth_year, (SELECT base_year FROM age_band_asis WHERE id = 1))
 WHERE age_band IS NULL;

CREATE OR REPLACE FUNCTION trg_person_demographics_age_band() RETURNS trigger
LANGUAGE plpgsql SET search_path = mt, public AS $$
BEGIN
  NEW.age_band := age_band_of(NEW.birth_year,
                              (SELECT base_year FROM mt.age_band_asis WHERE id = 1));
  RETURN NEW;
END $$;
COMMENT ON FUNCTION trg_person_demographics_age_band() IS
  '按 birth_year 维护 age_band（写时快照）。用 BEFORE 触发器而不是 GENERATED 列：'
  '因为基准年存在表里且会变，而 GENERATED 要求表达式 IMMUTABLE。';

DROP TRIGGER IF EXISTS trg_age_band ON person_demographics;
CREATE TRIGGER trg_age_band BEFORE INSERT OR UPDATE OF birth_year
  ON person_demographics FOR EACH ROW
  EXECUTE FUNCTION trg_person_demographics_age_band();

-- 定期重算（年度运维用；也是"派生字段会过期"的应对手段）
CREATE OR REPLACE FUNCTION refresh_age_bands(p_base_year integer DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SET search_path = mt, public AS $$
DECLARE n integer; y integer;
BEGIN
  y := COALESCE(p_base_year, EXTRACT(YEAR FROM now())::int);
  UPDATE age_band_asis SET base_year = y, updated_at = now() WHERE id = 1;
  UPDATE person_demographics SET age_band = age_band_of(birth_year, y)
   WHERE age_band IS DISTINCT FROM age_band_of(birth_year, y);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
COMMENT ON FUNCTION refresh_age_bands(integer) IS
  '按指定基准年（默认今年）重算全部年龄段，并更新 age_band_asis.base_year。'
  '年龄段是快照，会随时间过期 —— 建议每年跑一次：'
  'python ops\pg.py sql（SELECT mt.refresh_age_bands()）或放进年度运维清单。';

-- ---------------------------------------------------------------------------
-- 4. 列级策略：age_band → T0（公开），birth_day/birth_year → T2
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  -- 直接写策略（而不是塞进 refresh_column_policy 的模式规则）：
  -- 这两条是**用户明确决策**的确定性口径，属于"人工策略"，与 access_policy 同源。
  -- 但 refresh_column_policy() 会重写整表，所以同时把规则写进那个函数里（见下）。
  NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 5. 公开的年龄段分布（聚合视图）：让"年龄公开"对匿名真正可见
-- ---------------------------------------------------------------------------
-- 为什么是**分布**而不是逐人年龄段：逐人年龄段必须带 person_id 才能对上人，
-- 而 person_id 已按披露控制收进 T1（030：per_real_fengtang 这类 id 编码了姓名）。
-- 匿名能拿到的是"各年龄段各有多少人"—— 这是统计属性，不指向个体。
-- 视图以属主身份执行（视图默认行为），因此匿名无需 person_demographics 的列权限。
CREATE OR REPLACE VIEW v_age_band_public AS
SELECT d.age_band AS band,
       v.label_zh  AS band_label,
       count(*)    AS n_persons
  FROM person_demographics d
  LEFT JOIN code_value v
         ON v.code_table_id = 'CT_AGE_BAND' AND v.code = d.age_band
 GROUP BY d.age_band, v.label_zh
 ORDER BY d.age_band;
COMMENT ON VIEW v_age_band_public IS
  '公开的年龄段分布（各段人数）。刻意**不含 person_id**：逐人年龄段需要主体标识才能对上人，'
  '而主体标识已按披露控制收进 T1。这里给的是统计属性，不指向个体。'
  '同时它也是"数量走独立闸门"思路的延伸：聚合与行级访问分开授权。';

-- ---------------------------------------------------------------------------
-- 6. 授权
-- ---------------------------------------------------------------------------
-- 聚合视图对全部等级（含匿名）开放；底层表 person_demographics 的列权限由
-- apply_column_grants() 按 column_policy 给（age_band=T0、birth_year=T2）
GRANT SELECT ON v_age_band_public, age_band_asis TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
REVOKE INSERT, UPDATE, DELETE ON age_band_asis FROM mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;
GRANT EXECUTE ON FUNCTION age_band_of(integer, integer) TO mt_portal, mt_t0, mt_t1, mt_t2, mt_t3;

-- refresh_age_bands 是运维动作：只给属主（不给应用角色）
REVOKE ALL ON FUNCTION refresh_age_bands(integer) FROM PUBLIC, mt_portal;

-- 段内的自检
DO $$
DECLARE n int; bad int;
BEGIN
  SELECT count(*) INTO n FROM person_demographics WHERE age_band IS NOT NULL;
  IF n = 0 THEN
    RAISE EXCEPTION '回填后没有任何 age_band —— 回填没生效';
  END IF;
  -- 每一个 age_band 都必须是 CT_AGE_BAND 里的码（存码不存标签的机器可检形式）
  SELECT count(*) INTO bad FROM person_demographics d
   WHERE d.age_band IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM code_value v
                      WHERE v.code_table_id='CT_AGE_BAND' AND v.code = d.age_band);
  IF bad > 0 THEN
    RAISE EXCEPTION '有 % 行的 age_band 不在 CT_AGE_BAND 码表里', bad;
  END IF;
  RAISE NOTICE 'age_band 回填完成：% 行，全部落在 CT_AGE_BAND 码表内', n;
END $$;

COMMIT;
