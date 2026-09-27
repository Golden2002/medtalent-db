-- ============================================================================
-- 词表一致性门：所有"形如码"的 CHECK 字面量，必须能在 code_value 中找到。
--
-- 为什么需要它：本项目已发生 3 次同类事故——
--   · 域 CHECK 写 ('raw','parsed') 而词表码是 V0–V4
--   · 视图过滤 IN ('skill','knowledge') 而列里存 'RT5','RT6'
--   · retention_policy.action CHECK 写 ('delete','anonymize') 而词表码是 RA1–RA3
-- 这三处都是"标签与码混用"，静态 SQL 校验查不出来，跑数据才发现。
-- 本测试把这类错误变成可自动检测的。
--
-- 用法：python ops\pg.py sql ops\tests\code_conformance.sql
-- ============================================================================
SET search_path TO mt, public;

DO $$
DECLARE
  r    record;
  lit  text;
  bad  text[] := ARRAY[]::text[];
  n    int := 0;
BEGIN
  FOR r IN
    SELECT con.conname,
           con.conrelid::regclass::text AS tbl,
           pg_get_constraintdef(con.oid) AS def
    FROM pg_constraint con
    JOIN pg_namespace nsp ON nsp.oid = con.connamespace
    WHERE nsp.nspname = 'mt' AND con.contype = 'c'
  LOOP
    FOR lit IN
      SELECT m[1] FROM regexp_matches(r.def, '''([A-Za-z0-9_-]+)''', 'g') AS m
    LOOP
      -- 只检查"形如码"的字面量：1–4 个大写字母 + 数字，如 V1 / T2 / RT5 / RA3 / W1
      IF lit ~ '^[A-Z]{1,4}[0-9]+$' THEN
        n := n + 1;
        IF NOT EXISTS (SELECT 1 FROM code_value WHERE code = lit) THEN
          bad := bad || (r.conname || ' -> ' || lit);
        END IF;
      END IF;
    END LOOP;
  END LOOP;

  RAISE NOTICE '已检查 % 个形如码的 CHECK 字面量', n;
  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION 'FAIL: 以下码值不在 code_value 中（标签与码混用）：%',
      array_to_string(bad, ' | ');
  END IF;
  RAISE NOTICE 'PASS: 全部码值均可在 code_value 中找到';
END $$;

-- 反向检查：各受控列的现有数据是否都在允许的码集合内
DO $$
DECLARE
  bad text[] := ARRAY[]::text[];
  r   record;
  q   text;
  v   text;
  cand text[][] := ARRAY[
    ARRAY['person','verify_status'],
    ARRAY['person','access_tier'],
    ARRAY['evidence','access_tier'],
    ARRAY['skill_assertion','verify_status'],
    ARRAY['job_posting','verify_status'],
    ARRAY['observation_window','window_type'],
    ARRAY['retention_policy','action'],
    ARRAY['incident_report','severity'],
    ARRAY['dataset_release','status'],
    ARRAY['subject_request','request_type'],
    ARRAY['concept','concept_type'],
    ARRAY['concept','reusability'],
    ARRAY['occupation','code_status']
  ];
  i int;
BEGIN
  FOR i IN 1 .. array_length(cand, 1) LOOP
    q := format('SELECT DISTINCT %I::text FROM mt.%I WHERE %I IS NOT NULL',
                cand[i][2], cand[i][1], cand[i][2]);
    BEGIN
      FOR v IN EXECUTE q LOOP
        IF NOT EXISTS (SELECT 1 FROM code_value WHERE code = v) THEN
          bad := bad || (cand[i][1] || '.' || cand[i][2] || '=' || v);
        END IF;
      END LOOP;
    EXCEPTION WHEN undefined_table OR undefined_column THEN
      NULL;   -- 表/列尚未建，跳过
    END;
  END LOOP;

  IF array_length(bad, 1) > 0 THEN
    RAISE EXCEPTION 'FAIL: 列中出现了不在 code_value 的取值：%', array_to_string(bad, ' | ');
  END IF;
  RAISE NOTICE 'PASS: 受控列的实际取值全部命中词表';
END $$;

DO $$ BEGIN RAISE NOTICE '=== 词表一致性门通过 ==='; END $$;
