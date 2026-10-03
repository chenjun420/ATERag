-- ============================================================
-- seed/schema_full.sql —— 自动生成, 请勿手工编辑 (docgen.ddl)
--
-- 来源章节: V6.0 §18.5 (五分区) + §3.5 / §3.9 / §5.8 / 第六章 /
--           第十六章 / 第十七章 / §18.3.1 / 附录H
-- 重新生成: python -m aterag.docgen.ddl
--
-- PostgreSQL 要求: >= 14
-- TimescaleDB: 已包含 (第 3 分区启用)
-- L0 共享层真相源: alembic/versions/ (本文件第 1 分区由其离线产出)
-- 型号 schema 真相源: src/aterag/storage/model_schema.py
--
-- 用法:
--   psql -v ON_ERROR_STOP=1 -v model_key=pw_sr5400 \
--        -f seed/schema_full.sql -d <db>
--
-- 不传 -model_key= 时用下面的默认值 (仅供演练, 正式部署必须显式传):
--   \set model_key 'pw_sr5400'
-- ============================================================

\if :{?model_key}
\else
\set model_key 'pw_sr5400'
\endif


-- ============================================================
-- 0. 扩展
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- 1. L0 共享 (l0_term, 第六章 / §18.3.1)
-- ============================================================

BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001_l0_base

CREATE EXTENSION IF NOT EXISTS vector;

CREATE EXTENSION IF NOT EXISTS age;

CREATE EXTENSION IF NOT EXISTS pg_textsearch;

CREATE EXTENSION IF NOT EXISTS zhparser;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS "l0_term";

CREATE TABLE l0_term.concept (
            concept_id       TEXT PRIMARY KEY,
            pref_label_zh    TEXT NOT NULL,
            pref_label_en    TEXT,
            aliases          TEXT[],
            forbidden_terms  TEXT[],
            kind             TEXT NOT NULL
                CHECK (kind IN ('measurand', 'instrument', 'condition', 'action', 'policy', 'status_signal', 'telemetry', 'protection', 'setting', 'command')),
            qudt_ref         TEXT,
            maps_to          TEXT,
            owner            TEXT,
            review_cycle     TEXT,
            borrow_scope     TEXT,
            valid_from       TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until      TIMESTAMPTZ,
            created_at       TIMESTAMPTZ DEFAULT now()
        );

CREATE TABLE l0_term.concept_alias (
            alias       TEXT PRIMARY KEY,
            concept_id  TEXT NOT NULL
                REFERENCES l0_term.concept(concept_id) ON DELETE CASCADE,
            alias_type  TEXT NOT NULL
                CHECK (alias_type IN ('synonym', 'abbreviation', 'misspelling', 'legacy')),
            confidence  NUMERIC(3,2) DEFAULT 1.0
                CHECK (confidence >= 0 AND confidence <= 1),
            valid_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until TIMESTAMPTZ
        );

CREATE INDEX idx_alias_concept ON l0_term.concept_alias (concept_id);

CREATE INDEX idx_concept_kind ON l0_term.concept (kind);

CREATE INDEX idx_concept_aliases ON l0_term.concept USING gin (aliases);

CREATE INDEX idx_concept_forbidden ON l0_term.concept USING gin (forbidden_terms);

CREATE TABLE l0_term.formula (
            formula_id      TEXT PRIMARY KEY,
            alias           TEXT[],
            name_zh         TEXT NOT NULL,
            name_en         TEXT,
            domain          TEXT NOT NULL,
            section         TEXT NOT NULL,
            partition       TEXT,
            seq             INT,

            expr_latex      TEXT NOT NULL,
            expr_plaintext  TEXT NOT NULL,
            expr_ascii      TEXT NOT NULL,
            expr_ast        JSONB NOT NULL,

            var_refs        TEXT[] NOT NULL,
            dimension_vec   NUMERIC(8,4)[] NOT NULL,
            dimension_ok    BOOLEAN NOT NULL DEFAULT true,

            derive_from     TEXT[] NOT NULL,
            boundary        TEXT,
            confidence      NUMERIC(3,2) NOT NULL DEFAULT 0.95
                CHECK (confidence >= 0 AND confidence <= 1),

            scope           TEXT NOT NULL DEFAULT 'global'
                CHECK (scope IN ('global', 'shared_l0', 'model')),
            model_key       TEXT,

            equivalent_ids  TEXT[],
            used_by_rule    TEXT[],
            used_by_test    TEXT[],
            used_by_axon    TEXT[],

            domain_tags     TEXT[] NOT NULL,
            errata          TEXT,
            source_ref      TEXT NOT NULL,
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,

            CONSTRAINT ck_scope_model CHECK (
                (scope <> 'model') OR (model_key IS NOT NULL)),
            CONSTRAINT ck_model_scope CHECK (
                (scope = 'model') OR (model_key IS NULL)),
            -- §18.9 G1: 量纲向量的分量数必须等于 7 (A-11)。
            -- 用 cardinality 而不是逐值比对: 值的正确性由
            -- solver/symbolic.py 在入库前保证, DDL 只需保证个数对。
            CONSTRAINT ck_dimension_arity CHECK (
                cardinality(dimension_vec) = 7)
        );

CREATE TABLE l0_term.formula_embedding (
            formula_id TEXT PRIMARY KEY
                REFERENCES l0_term.formula(formula_id) ON DELETE CASCADE,
            content    TEXT NOT NULL,
            embedding  halfvec(1024)
        );

CREATE INDEX idx_formula_emb ON l0_term.formula_embedding USING hnsw (embedding halfvec_cosine_ops);

CREATE INDEX idx_formula_rule ON l0_term.formula USING GIN (used_by_rule);

CREATE INDEX idx_formula_test ON l0_term.formula USING GIN (used_by_test);

CREATE INDEX idx_formula_derive ON l0_term.formula USING GIN (derive_from);

CREATE INDEX idx_formula_equiv ON l0_term.formula USING GIN (equivalent_ids);

CREATE INDEX idx_formula_tags ON l0_term.formula USING GIN (domain_tags);

CREATE INDEX idx_formula_alias ON l0_term.formula USING GIN (alias);

CREATE INDEX idx_formula_domain ON l0_term.formula (domain, section);

CREATE INDEX idx_formula_dim_ok ON l0_term.formula (dimension_ok);

CREATE OR REPLACE FUNCTION l0_term.assert_formula_dimension_ok()
        RETURNS TRIGGER AS $$
        DECLARE
            ok BOOLEAN;
        BEGIN
            SELECT dimension_ok INTO ok
              FROM l0_term.formula
             WHERE formula_id = NEW.formula_id;
            IF ok IS FALSE THEN
                RAISE EXCEPTION
                    '公式 % 的 dimension_ok=false, 不得进入向量索引 (§18.9 G1)',
                    NEW.formula_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_formula_embedding_dimension_ok
        BEFORE INSERT OR UPDATE ON l0_term.formula_embedding
        FOR EACH ROW EXECUTE FUNCTION l0_term.assert_formula_dimension_ok();

CREATE TABLE l0_term.standards_registry (
            std_code      TEXT PRIMARY KEY,
            title_zh      TEXT NOT NULL,
            title_en      TEXT,
            issuing_body  TEXT,
            std_kind      TEXT NOT NULL
                CHECK (std_kind IN (
                    'national', 'industry', 'military', 'international',
                    'company', 'internal')),
            version       TEXT,
            published_on  DATE,
            superseded_by TEXT,
            domain_tags   TEXT[] NOT NULL,
            source_ref    TEXT NOT NULL,
            url           TEXT,
            retrieved_at  TIMESTAMPTZ,
            notes         TEXT,
            valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until   TIMESTAMPTZ
        );

CREATE INDEX idx_standards_kind ON l0_term.standards_registry (std_kind);

CREATE INDEX idx_standards_tags ON l0_term.standards_registry USING GIN (domain_tags);

CREATE INDEX idx_standards_current ON l0_term.standards_registry (std_code) WHERE superseded_by IS NULL;

INSERT INTO alembic_version (version_num) VALUES ('0001_l0_base') RETURNING alembic_version.version_num;

-- Running upgrade 0001_l0_base -> 0002_l0_rules

CREATE TABLE l0_term.rule (
            rule_id          TEXT PRIMARY KEY
                CHECK (rule_id ~ '^(R[1-9]|R1[0-9]|R20|P[1-9]|P1[0-8])$'),
            rule_type        TEXT NOT NULL
                CHECK (rule_type IN ('hard', 'soft')),
            rule_name        TEXT NOT NULL,
            formal_criterion TEXT NOT NULL,

            -- §18.2.2 的 ``body``: Datalog 规则体 / 表达式原文。
            -- 刻意存原文而不存编译产物: 追溯矩阵 CSV 要导出给人看,
            -- 而 §18.10 注 4 要求通道概念只有一个权威定义。
            body             TEXT NOT NULL,
            expr             TEXT,

            -- 附录B.3 的「公式依据」列。R4/R11/P11/P16 在 spec 里就是空,
            -- 所以可空 —— 但可空必须给理由, 见 ck_formula_ref_reason。
            formula_ref      TEXT
                REFERENCES l0_term.formula(formula_id)
                ON DELETE RESTRICT,
            formula_section  TEXT,
            no_formula_reason TEXT,

            severity         TEXT NOT NULL
                CHECK (severity IN ('reject', 'hold', 'degrade', 'hint')),
            fail_action      TEXT NOT NULL,
            domain           TEXT NOT NULL,

            used_by_test     TEXT[],
            standard_ref     TEXT,

            -- §7.8: 每条规则设 owner, 复审周期不超过一个季度。
            owner            TEXT,
            review_cycle     TEXT,

            source_ref       TEXT NOT NULL,
            errata           TEXT,

            -- 刻意**没有** valid_from / valid_until。理由见本函数末尾的注释。

            -- 有公式依据时不得填理由 (理由是给无依据的规则解释「为什么没有」);

-- 无公式依据时必须填理由。这条约束是 §18.10 注 2「不可追溯的值
            -- 记 UNKNOWN, 绝不猜」的 DDL 落点 —— 不允许悄悄留空。
            CONSTRAINT ck_formula_ref_reason CHECK (
                (formula_ref IS NOT NULL AND no_formula_reason IS NULL)
                OR (formula_ref IS NULL AND no_formula_reason IS NOT NULL)
            )
        );

CREATE INDEX idx_rule_type ON l0_term.rule (rule_type);

CREATE INDEX idx_rule_formula ON l0_term.rule (formula_ref);

CREATE INDEX idx_rule_tests ON l0_term.rule USING GIN (used_by_test);

CREATE INDEX idx_rule_owner ON l0_term.rule (owner);

CREATE TABLE l0_term.rule_parameter (
            model_schema TEXT NOT NULL,
            rule_id      TEXT NOT NULL,
            param_name   TEXT NOT NULL,
            param_value  TEXT NOT NULL,
            param_type   TEXT DEFAULT 'string',
            valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until  TIMESTAMPTZ,
            PRIMARY KEY (model_schema, rule_id, param_name, valid_from),
            CONSTRAINT ck_rule_param_type CHECK (
                param_type IN ('string', 'int', 'float', 'bool', 'list'))
        );

CREATE INDEX idx_rule_param_model ON l0_term.rule_parameter (model_schema);

CREATE INDEX idx_rule_param_rule ON l0_term.rule_parameter (rule_id);

CREATE UNIQUE INDEX ux_rule_parameter_current ON l0_term.rule_parameter (model_schema, rule_id, param_name) WHERE valid_until IS NULL;

CREATE TABLE l0_term.rule_version (
            rule_id       TEXT NOT NULL,
            version       TEXT NOT NULL,
            rule_content  TEXT NOT NULL,
            formula_ref   TEXT,
            owner         TEXT,
            review_cycle  TEXT,
            valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until   TIMESTAMPTZ,
            PRIMARY KEY (rule_id, version),
            CONSTRAINT fk_rule_version_rule
                FOREIGN KEY (rule_id)
                REFERENCES l0_term.rule(rule_id) ON DELETE CASCADE,
            CONSTRAINT fk_rule_version_formula
                FOREIGN KEY (formula_ref)
                REFERENCES l0_term.formula(formula_id)
                ON DELETE RESTRICT
        );

CREATE INDEX idx_rule_version_valid ON l0_term.rule_version (rule_id, valid_from, valid_until);

CREATE TABLE l0_term.borrow_rule (
            borrower_schema TEXT NOT NULL,
            lender_schema   TEXT NOT NULL,
            concept_id      TEXT NOT NULL,
            borrow_type     TEXT NOT NULL
                CHECK (borrow_type IN ('term', 'rule', 'ontology', 'template', 'formula')),
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,
            PRIMARY KEY (borrower_schema, lender_schema, concept_id, valid_from),
            -- §5.12.1 的例子里 concept_id 既有 l0_term 的术语 (TRIPPLE_OUTPUT),
            -- 也有规则 ID (P2_RIPPLE_RULE) 与公式 ID (F_J.3_INDUCTOR_RIPPLE)。
            -- 刻意不加到 concept 表的外键: 借用对象跨了三类命名空间,
            -- 加外键等于把这三类强制并成一张表, 而 spec 没有这个意图。
            CONSTRAINT ck_borrow_not_self CHECK (borrower_schema <> lender_schema)
        );

CREATE UNIQUE INDEX ux_borrow_rule_current ON l0_term.borrow_rule (borrower_schema, lender_schema, concept_id) WHERE valid_until IS NULL;

CREATE TABLE l0_term.jev_threshold (
            model_schema    TEXT NOT NULL,
            decision_type   TEXT NOT NULL
                CHECK (decision_type IN ('is_compliant', 'param_limit', 'yx_classify', 'yc_anomaly', 'setting_score')),
            threshold       NUMERIC(4,3) NOT NULL
                CHECK (threshold > 0 AND threshold <= 1),
            fallback_action TEXT NOT NULL,
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,
            PRIMARY KEY (model_schema, decision_type, valid_from)
        );

CREATE UNIQUE INDEX ux_jev_threshold_current ON l0_term.jev_threshold (model_schema, decision_type) WHERE valid_until IS NULL;

CREATE TABLE l0_term.disambiguation_log (
            log_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            term               TEXT NOT NULL,
            context            TEXT,
            resolved_concept_id TEXT
                REFERENCES l0_term.concept(concept_id) ON DELETE SET NULL,
            rule_used          TEXT,
            confidence         NUMERIC(3,2)
                CHECK (confidence IS NULL
                       OR (confidence >= 0 AND confidence <= 1)),
            needs_review       BOOLEAN DEFAULT false,
            reviewed_by        TEXT,
            reviewed_at        TIMESTAMPTZ,
            created_at         TIMESTAMPTZ DEFAULT now(),
            -- reviewed_* 必须同时有值或同时为空: 「已复核」与「复核人」不可分割。
            CONSTRAINT ck_reviewed_pair CHECK (
                (reviewed_by IS NULL AND reviewed_at IS NULL)
                OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
            )
        );

CREATE INDEX idx_disambig_needs_review ON l0_term.disambiguation_log (created_at) WHERE needs_review;

UPDATE alembic_version SET version_num='0002_l0_rules' WHERE alembic_version.version_num = '0001_l0_base';

COMMIT;

-- ============================================================
-- 2. 型号 schema (第三/五章 + 第十六/十七章 + 附录H)
-- ============================================================


CREATE TABLE IF NOT EXISTS document_object (
    object_id         BIGSERIAL PRIMARY KEY,
    doc_id            TEXT NOT NULL,
    category_key      TEXT NOT NULL DEFAULT 'power',
    model_key         TEXT,
    sha256            CHAR(64) NOT NULL,
    mime_type         TEXT NOT NULL
        CHECK (mime_type IN ('application/pdf','text/csv','image/png',
                             'image/jpeg','application/octet-stream','application/json')),
    size_bytes        BIGINT NOT NULL CHECK (size_bytes >= 0),
    page_count        INT CHECK (page_count IS NULL OR page_count > 0),
    backend_type      TEXT NOT NULL DEFAULT 'fs'
        CHECK (backend_type IN ('fs','pg_bytea','s3')),
    storage_key       TEXT NOT NULL,
    upstream_uri      TEXT,
    upstream_sha256   CHAR(64),
    reproducible      BOOLEAN NOT NULL DEFAULT true,
    last_verified_at  TIMESTAMPTZ,
    quarantined       BOOLEAN NOT NULL DEFAULT false,
    quarantine_reason TEXT,
    clause_uid        TEXT,
    retain_until      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_id, sha256),
    -- 声称「可重建」就必须给出上游来源, 否则目录丢失时变成静默空洞。
    CONSTRAINT ck_reproducible CHECK (reproducible = FALSE OR upstream_uri IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_docobj_sha ON document_object (sha256);
CREATE INDEX IF NOT EXISTS idx_docobj_doc ON document_object (doc_id);
CREATE INDEX IF NOT EXISTS idx_docobj_verify ON document_object (last_verified_at)
    WHERE quarantined = false;
CREATE INDEX IF NOT EXISTS idx_docobj_notrepro ON document_object (doc_id)
    WHERE reproducible = false;

CREATE TABLE IF NOT EXISTS document_page (
    page_id      BIGSERIAL PRIMARY KEY,
    object_id    BIGINT NOT NULL
        REFERENCES document_object(object_id) ON DELETE CASCADE,
    page_no      INT NOT NULL,
    -- bbox: PDF 用户空间点 (1/72 inch), 左上原点, 顺序 x0,y0,x1,y1 (§3.9.3)。
    bbox         JSONB,
    text_excerpt TEXT,
    table_json   JSONB,
    UNIQUE (object_id, page_no)
);
CREATE INDEX IF NOT EXISTS idx_docpage_obj ON document_page (object_id, page_no);
CREATE INDEX IF NOT EXISTS idx_docpage_bbox ON document_page USING gin (bbox);

CREATE SCHEMA IF NOT EXISTS :"model_key";

CREATE TABLE :"model_key".doc (
    doc_id             TEXT NOT NULL,
    rev                TEXT NOT NULL,
    title              TEXT NOT NULL,
    -- §3.9 document_object: 可选。规格书原件尚未入库时可空。
    object_id          BIGINT REFERENCES public.document_object(object_id),
    doc_kind           TEXT NOT NULL DEFAULT 'spec'
        CHECK (doc_kind IN ('spec','drawing','test_procedure','report','other')),
    page_count         INT CHECK (page_count IS NULL OR page_count > 0),
    issued_on          DATE,
    superseded_at      TIMESTAMPTZ,
    owner              TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    PRIMARY KEY (doc_id, rev)
);

CREATE INDEX idx_doc_id ON :"model_key".doc (doc_id);

CREATE INDEX idx_doc_object ON :"model_key".doc (object_id);

-- 同一 doc_id 至多一条当前版本 (与 storage/bitemporal.py 同一机制)。
CREATE UNIQUE INDEX ux_doc_current ON :"model_key".doc (doc_id) WHERE valid_until IS NULL;

CREATE TABLE :"model_key".clause (
    clause_uid         TEXT PRIMARY KEY,
    doc_id             TEXT NOT NULL,
    rev                TEXT NOT NULL,
    -- 章节号路径, 如 3.5.2; clause_uid 之外的可读定位。
    clause_path        TEXT,
    heading_path       TEXT[],
    page               INT CHECK (page IS NULL OR page > 0),
    content            TEXT NOT NULL,
    -- 非条款行 (图注/目录/页眉) 标 false, 让溯源约束有作用范围。
    is_normative       BOOLEAN NOT NULL DEFAULT true,
    table_json         JSONB,
    extraction_method  TEXT,
    parse_confidence   NUMERIC(3,2),
    needs_review       BOOLEAN NOT NULL DEFAULT false,
    reviewed_by        TEXT,
    reviewed_at        TIMESTAMPTZ,
    source_ref         TEXT NOT NULL,
    FOREIGN KEY (doc_id, rev) REFERENCES :"model_key".doc (doc_id, rev) ON DELETE CASCADE
);

CREATE INDEX idx_clause_doc ON :"model_key".clause (doc_id, rev);

CREATE INDEX idx_clause_path ON :"model_key".clause (clause_path);

CREATE INDEX idx_clause_normative ON :"model_key".clause (is_normative, needs_review);

CREATE TABLE :"model_key".trace (
    trace_id           BIGSERIAL PRIMARY KEY,
    layer              TEXT NOT NULL
        CHECK (layer IN ('axiom','theorem','formula','rule','test','judgement')),
    ref_id             TEXT NOT NULL,
    -- 链条上的下游与上游 (同一张表自引用)。
    upstream_layer     TEXT,
    upstream_ref       TEXT,
    downstream_layer   TEXT,
    downstream_ref     TEXT,
    derivation         TEXT,
    confidence         NUMERIC(3,2)
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    verified           BOOLEAN NOT NULL DEFAULT false,
    verified_by        TEXT,
    verified_at        TIMESTAMPTZ,
    source_ref         TEXT NOT NULL,
    UNIQUE (layer, ref_id, upstream_layer, upstream_ref, downstream_layer, downstream_ref),
    CONSTRAINT ck_trace_self_ref CHECK (ref_id IS DISTINCT FROM upstream_ref)
);

CREATE INDEX idx_trace_ref ON :"model_key".trace (layer, ref_id);

CREATE INDEX idx_trace_up ON :"model_key".trace (upstream_layer, upstream_ref);

CREATE INDEX idx_trace_down ON :"model_key".trace (downstream_layer, downstream_ref);

CREATE INDEX idx_trace_unverified ON :"model_key".trace (layer, ref_id) WHERE verified = false;

CREATE TABLE :"model_key".doc_chunk (
    id                 BIGSERIAL PRIMARY KEY,
    doc_id             TEXT NOT NULL,
    rev                TEXT NOT NULL,
    page               INT,
    chunk_index        INT NOT NULL,
    content            TEXT NOT NULL,
    vec                halfvec(1024),
    tsv                tsvector,
    concept_ids        TEXT[],
    parse_confidence   NUMERIC(3,2),
    source_type        TEXT DEFAULT 'pdf',
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_doc_chunk_vec_hnsw ON :"model_key".doc_chunk
    USING hnsw (vec halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_doc_chunk_tsv ON :"model_key".doc_chunk USING gin (tsv);

CREATE INDEX idx_doc_chunk_valid ON :"model_key".doc_chunk (valid_from, valid_until);

CREATE INDEX idx_doc_chunk_concept ON :"model_key".doc_chunk USING gin (concept_ids);

CREATE INDEX idx_doc_chunk_doc ON :"model_key".doc_chunk (doc_id, rev, page);

CREATE OR REPLACE FUNCTION :"model_key".update_tsv() RETURNS TRIGGER AS $$
BEGIN
    NEW.tsv := to_tsvector('chinese_zh', NEW.content);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_doc_chunk_tsv
    BEFORE INSERT OR UPDATE ON :"model_key".doc_chunk
    FOR EACH ROW EXECUTE FUNCTION :"model_key".update_tsv();

CREATE TABLE :"model_key".fact (
    fact_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    concept_id         TEXT NOT NULL,
    value              NUMERIC,
    unit               TEXT,
    value_min          NUMERIC,
    value_typ          NUMERIC,
    value_max          NUMERIC,
    condition_vector   JSONB,
    abs_max            NUMERIC,
    spec_lsl           NUMERIC,
    spec_usl           NUMERIC,
    source_doc_id      TEXT,
    source_rev         TEXT,
    source_page        INT,
    source_table       TEXT,
    shacl_passed       BOOLEAN DEFAULT false,
    rules_passed       TEXT[],
    trust_level        TEXT DEFAULT 'UNKNOWN'
        CHECK (trust_level IN ('TRUSTED','DEGRADED','UNKNOWN')),
    -- §5.8.1 的 fact 策略是 column_match (tenant_schema = ctx_model()),
    -- 所以 DEFAULT 必须是**字符串字面量**。写成 DEFAULT "pw_x" 会被
    -- PostgreSQL 当成列引用, 建表直接失败 —— 引号形式不是随手选的。
    tenant_schema      TEXT NOT NULL DEFAULT :'model_key',
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_fact_concept ON :"model_key".fact (concept_id);

CREATE INDEX idx_fact_trust ON :"model_key".fact (trust_level);

CREATE INDEX idx_fact_valid ON :"model_key".fact (valid_from, valid_until);

CREATE INDEX idx_fact_cond ON :"model_key".fact USING gin (condition_vector);

-- §5.8.2 双时态视图。纪律 3: 溯源导出必须走它, 不许直查基表。
CREATE OR REPLACE VIEW :"model_key".fact_as_of AS
SELECT * FROM :"model_key".fact
WHERE valid_from <= now()
  AND (valid_until IS NULL OR valid_until > now());

CREATE TABLE :"model_key".provenance (
    prov_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id          UUID NOT NULL,
    entity_type        TEXT NOT NULL,
    activity           TEXT NOT NULL,
    agent              TEXT,
    source_ref         TEXT,
    source_location    TEXT,
    confidence         NUMERIC(3,2),
    derivation_path    JSONB,
    formula_ref        TEXT,
    generated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE INDEX idx_prov_entity ON :"model_key".provenance (entity_id, valid_from, valid_until);

CREATE INDEX idx_prov_type ON :"model_key".provenance (entity_type, activity);

CREATE TABLE :"model_key".conflict (
    conflict_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    concept_id         TEXT NOT NULL,
    conflict_type      TEXT NOT NULL,
    source_a           JSONB NOT NULL,
    source_b           JSONB NOT NULL,
    -- 默认 never_silent: §1.6 要求冲突不得被静默择一。
    resolution_strategy TEXT DEFAULT 'never_silent',
    resolved           BOOLEAN DEFAULT false,
    resolved_by        TEXT,
    resolved_at        TIMESTAMPTZ,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE INDEX idx_conflict_concept ON :"model_key".conflict (concept_id, resolved);

CREATE TABLE :"model_key".test_requirement (
    req_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sr_id              TEXT NOT NULL,
    concept_id         TEXT NOT NULL,
    measurand          TEXT,
    dut_node           TEXT,
    quantity_kind      TEXT,
    spec               JSONB,
    abs_max            NUMERIC,
    condition_vector   JSONB,
    method             JSONB,
    guardband          JSONB,
    sample_size        INT,
    sequence           JSONB,
    instrument_need    JSONB,
    fixture_need       JSONB,
    safety             JSONB,
    source_ref         JSONB,
    signal_type        TEXT DEFAULT 'TEST'
        CHECK (signal_type IN ('TEST','YX','YC','YK','YT','PROT','SET')),
    formula_ref        TEXT,
    axiom_ref          TEXT,
    coverage_status    TEXT DEFAULT 'PENDING'
        CHECK (coverage_status IN ('PENDING','COVERED','GAP')),
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE INDEX idx_req_sr ON :"model_key".test_requirement (sr_id);

CREATE INDEX idx_req_concept ON :"model_key".test_requirement (concept_id);

CREATE INDEX idx_req_coverage ON :"model_key".test_requirement (coverage_status);

CREATE INDEX idx_req_signal ON :"model_key".test_requirement (signal_type);

CREATE TABLE :"model_key".test_case (
    case_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    refines            UUID[] NOT NULL,
    setup              JSONB NOT NULL,
    condition_vector   JSONB NOT NULL,
    steps              JSONB NOT NULL,
    sample_size        INT,
    pass_criteria      JSONB NOT NULL,
    guardband          JSONB,
    safety_precheck    JSONB,
    provenance         JSONB,
    trust_level        TEXT DEFAULT 'UNKNOWN'
        CHECK (trust_level IN ('TRUSTED','DEGRADED','UNKNOWN')),
    jev_score          NUMERIC(4,3),
    roundtrip_passed   BOOLEAN DEFAULT false,
    rendered_text      TEXT,
    formula_ref        TEXT,
    axiom_ref          TEXT,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE INDEX idx_case_trust ON :"model_key".test_case (trust_level);

CREATE INDEX idx_case_refines ON :"model_key".test_case USING gin (refines);

CREATE TABLE :"model_key".yx_point (
    point_id           TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES l0_term.concept(concept_id),
    point_name_zh      TEXT NOT NULL,
    point_name_en      TEXT,
    signal_source      TEXT NOT NULL CHECK (signal_source IN ('internal','external')),
    -- active_level 与 debounce_ms 是 SHACL Shape 6 的必填项, 故 NOT NULL。
    active_level       TEXT NOT NULL CHECK (active_level IN ('high','low')),
    contact_type       TEXT CHECK (contact_type IN ('dry','wet')),
    isolation_type     TEXT CHECK (isolation_type IN ('optocoupler','relay','none')),
    normally_open      BOOLEAN DEFAULT true,
    debounce_ms        NUMERIC(8,2) NOT NULL CHECK (debounce_ms >= 0),
    debounce_algo      TEXT NOT NULL DEFAULT 'counter'
        CHECK (debounce_algo IN ('rc','schmitt','counter','iir','median','statemachine')),
    debounce_tau_ms    NUMERIC(8,2),
    sample_period_ms   NUMERIC(8,2) NOT NULL CHECK (sample_period_ms > 0),
    alarm_level        TEXT NOT NULL
        CHECK (alarm_level IN ('critical','major','minor','warning')),
    alarm_before_time_ms NUMERIC(10,2),
    power_off_retain   BOOLEAN NOT NULL DEFAULT false,
    -- related_* 的 FK 在第 3 批由 ALTER TABLE 补上 (前向引用)。
    related_protection TEXT,
    related_yc         TEXT,
    related_checkpoint BIGINT,
    protocol           TEXT,
    protocol_address   TEXT,
    register_address   TEXT,
    bit_position       INT CHECK (bit_position BETWEEN 0 AND 7),
    line_supervision   BOOLEAN NOT NULL DEFAULT false,
    supervision_current_ma NUMERIC(6,2),
    eol_resistor_ohm   NUMERIC(8,1),
    double_point_role  TEXT
        CHECK (double_point_role IS NULL OR double_point_role IN ('a','b','single')),
    -- §17.3.2 判定① 用 mandatory 过滤必测点位。
    mandatory           BOOLEAN NOT NULL DEFAULT false,
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_related_not_empty CHECK (
        related_protection IS NOT NULL OR related_yc IS NOT NULL
        OR related_checkpoint IS NOT NULL)
);

CREATE INDEX idx_yx_concept ON :"model_key".yx_point (concept_id);

CREATE INDEX idx_yx_protocol ON :"model_key".yx_point (protocol, register_address);

CREATE INDEX idx_yx_prot ON :"model_key".yx_point (related_protection);

CREATE TABLE :"model_key".yx_soe (
    soe_id             BIGSERIAL,
    point_id           TEXT NOT NULL,
    event_type         TEXT NOT NULL
        CHECK (event_type IN ('rising','falling','change','suppressed','invalid')),
    old_state          SMALLINT CHECK (old_state IN (0,1)),
    new_state          SMALLINT CHECK (new_state IN (0,1)),
    -- 三个时标必须都记: 否则无法区分「防抖吃掉了时间」还是「通信延迟」。
    event_time         TIMESTAMPTZ NOT NULL,
    capture_time       TIMESTAMPTZ NOT NULL,
    store_time         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolution_ms      NUMERIC(8,3) NOT NULL,
    sync_error_ms      NUMERIC(8,3),
    sync_source        TEXT CHECK (sync_source IS NULL OR sync_source IN
        ('manual','rtc','ntp','ntp_gps','irigb','pps','ptp','ieee_c37_238')),
    trigger_reason     TEXT CHECK (trigger_reason IS NULL OR trigger_reason IN
        ('external','protection','self_test','command','power_event')),
    sequence_no        BIGINT,
    old_raw            SMALLINT,
    description        TEXT,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    PRIMARY KEY (soe_id, event_time)
);

CREATE INDEX idx_soe_point ON :"model_key".yx_soe (point_id, event_time DESC);

CREATE INDEX idx_soe_seq ON :"model_key".yx_soe (sequence_no DESC);

CREATE TABLE :"model_key".yc_point (
    point_id           TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES l0_term.concept(concept_id),
    point_name_zh      TEXT NOT NULL,
    point_name_en      TEXT,
    quantity_kind      TEXT NOT NULL
        CHECK (quantity_kind IN ('voltage','current','power','temperature',
                                 'frequency','ratio','energy','time','status')),
    unit               TEXT NOT NULL,
    range_min          NUMERIC NOT NULL,
    range_max          NUMERIC NOT NULL,
    accuracy_reading_pct NUMERIC(6,3),
    accuracy_fs_pct    NUMERIC(6,3),
    tur_min            NUMERIC(5,2) NOT NULL DEFAULT 4,
    scan_period_ms     NUMERIC(10,2) NOT NULL CHECK (scan_period_ms > 0),
    sample_rate_hz     NUMERIC(12,2),
    band_width_hz      NUMERIC(12,2) NOT NULL,
    is_accumulative    BOOLEAN NOT NULL DEFAULT false,
    accum_decimal      INT,
    deadband           NUMERIC NOT NULL DEFAULT 0,
    deadband_pct       NUMERIC(6,3),
    alarm_low_low      NUMERIC,
    alarm_low          NUMERIC,
    alarm_high         NUMERIC,
    alarm_high_high    NUMERIC,
    alarm_deadband     NUMERIC,
    quality_default    TEXT NOT NULL DEFAULT 'good'
        CHECK (quality_default IN ('good','invalid','questionable','stale','test','substituted')),
    stale_multiplier   NUMERIC(4,1) NOT NULL DEFAULT 3,
    protocol           TEXT,
    protocol_address   TEXT,
    register_address   TEXT,
    data_type          TEXT CHECK (data_type IS NULL OR data_type IN
        ('u8','u16','u32','i16','i32','float32','lin16','lin11')),
    scale_factor       NUMERIC,
    offset_value       NUMERIC,
    msv_ma             NUMERIC,
    -- §17.3.2 判定③ 用 MAX(current_a) 算通路压降, 故列必须存在。但该 SQL
    -- 对**点位定义表**取 MAX 恒等于本行值, 判据本身有缺陷 —— 详见
    -- _ALTER_FKS 上方的「刻意不加外键的地方」第 3 条。列语义按
    -- 「通道电流上限」理解; 实测电流走 yc_trend。
    current_a          NUMERIC,
    mandatory          BOOLEAN NOT NULL DEFAULT false,
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_range_valid CHECK (range_max > range_min),
    CONSTRAINT ck_alarm_order CHECK (
        (alarm_low_low IS NULL OR alarm_low IS NULL OR alarm_low_low < alarm_low)
    AND (alarm_low IS NULL OR alarm_high IS NULL OR alarm_low < alarm_high)
    AND (alarm_high IS NULL OR alarm_high_high IS NULL OR alarm_high < alarm_high_high)),
    -- 不加这条, 阈值超量程 ⟹ 测量饱和恒判正常 ⟹ 告警永不触发。
    CONSTRAINT ck_alarm_in_range CHECK (
        (alarm_high IS NULL OR alarm_high <= range_max * 1.05)
    AND (alarm_low  IS NULL OR alarm_low  >= range_min * 0.95))
);

CREATE INDEX idx_yc_concept ON :"model_key".yc_point (concept_id);

CREATE INDEX idx_yc_protocol ON :"model_key".yc_point (protocol, register_address);

CREATE TABLE :"model_key".yc_trend (
    ts                 TIMESTAMPTZ NOT NULL,
    point_id           TEXT NOT NULL,
    value              NUMERIC NOT NULL,
    raw_value          NUMERIC,
    quality            TEXT NOT NULL DEFAULT 'good'
        CHECK (quality IN ('good','invalid','questionable','stale','test','substituted')),
    alarm_state        TEXT DEFAULT 'normal'
        CHECK (alarm_state IN ('normal','low','high','low_low','high_high','latched')),
    alarm_count        INT NOT NULL DEFAULT 0,
    source             TEXT,
    PRIMARY KEY (ts, point_id)
);

CREATE INDEX idx_trend_point ON :"model_key".yc_trend (point_id, ts DESC);

CREATE TABLE :"model_key".yk_command (
    command_id         TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES l0_term.concept(concept_id),
    command_name_zh    TEXT NOT NULL,
    command_name_en    TEXT,
    command_code       TEXT NOT NULL,
    protocol           TEXT,
    protocol_address   TEXT,
    register_address   TEXT,
    command_args       TEXT,
    requires_confirmation BOOLEAN NOT NULL DEFAULT true,
    confirm_timeout_ms NUMERIC(10,2),
    debounce_count     INT NOT NULL DEFAULT 3 CHECK (debounce_count >= 1),
    debounce_window_ms NUMERIC(10,2),
    auto_recovery_ms   NUMERIC(12,2),
    permission_level   TEXT NOT NULL
        CHECK (permission_level IN ('operator','engineer','admin','safety_admin')),
    danger_level       TEXT NOT NULL DEFAULT 'low'
        CHECK (danger_level IN ('low','medium','high','critical')),
    is_nvm_write       BOOLEAN NOT NULL DEFAULT false,
    requires_readback  BOOLEAN NOT NULL DEFAULT true,
    precondition       TEXT,
    expected_result    TEXT,
    result_readback_expr TEXT,
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE TABLE :"model_key".yk_audit (
    audit_id           BIGSERIAL PRIMARY KEY,
    command_id         TEXT NOT NULL,
    operator_id        TEXT NOT NULL,
    station_id         TEXT,
    issued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    args_json          JSONB,
    confirmed          BOOLEAN NOT NULL DEFAULT false,
    result             TEXT NOT NULL
        CHECK (result IN ('accepted','rejected','timeout','failed','confirmed')),
    readback_value     TEXT,
    readback_match     BOOLEAN,
    duration_ms        NUMERIC(12,2),
    source_ip          TEXT,
    reason             TEXT
);

CREATE INDEX idx_yk_audit ON :"model_key".yk_audit (command_id, issued_at DESC);

CREATE TABLE :"model_key".yt_parameter (
    parameter_id       TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES l0_term.concept(concept_id),
    parameter_name_zh  TEXT NOT NULL,
    parameter_name_en  TEXT,
    unit               TEXT NOT NULL,
    range_min          NUMERIC NOT NULL,
    range_max          NUMERIC NOT NULL,
    step               NUMERIC,
    default_value      NUMERIC NOT NULL,
    persistence        TEXT NOT NULL DEFAULT 'volatile'
        CHECK (persistence IN ('volatile','non_volatile')),
    permission_level   TEXT NOT NULL
        CHECK (permission_level IN ('operator','engineer','admin','safety_admin')),
    protocol           TEXT,
    protocol_address   TEXT,
    register_address   TEXT,
    data_type          TEXT,
    scale_factor       NUMERIC,
    offset_value       NUMERIC,
    -- §16.5.1: 改这两类参数必须重跑 R15/R16/P3。
    affects_protection BOOLEAN NOT NULL DEFAULT false,
    affects_stability  BOOLEAN NOT NULL DEFAULT false,
    revalidate_rule    TEXT[],
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_range_valid CHECK (range_max > range_min),
    CONSTRAINT ck_default_in_range CHECK (default_value BETWEEN range_min AND range_max)
);

CREATE TABLE :"model_key".yt_change_log (
    log_id             BIGSERIAL PRIMARY KEY,
    parameter_id       TEXT NOT NULL,
    old_value          NUMERIC,
    new_value          NUMERIC NOT NULL,
    operator_id        TEXT NOT NULL,
    revalidated        TEXT[],
    changed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_yt_change ON :"model_key".yt_change_log (parameter_id, changed_at DESC);

CREATE TABLE :"model_key".protection_setting (
    setting_id         TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES l0_term.concept(concept_id),
    setting_name_zh    TEXT NOT NULL,
    setting_name_en    TEXT,
    protection_type    TEXT NOT NULL
        CHECK (protection_type IN ('ocp','scp','ovp','uvp','opp','otp',
                                   'vin_ovp','uvlo','fan_fail','over_temp')),
    setting_zone       TEXT NOT NULL DEFAULT 'ZONE_1'
        CHECK (setting_zone ~ '^ZONE_[0-9]+$'),
    trip_typ           NUMERIC NOT NULL,
    trip_min           NUMERIC NOT NULL,
    trip_max           NUMERIC NOT NULL,
    abs_max            NUMERIC NOT NULL,
    setting_value      NUMERIC NOT NULL,
    setting_min        NUMERIC NOT NULL,
    setting_max        NUMERIC NOT NULL,
    setting_step       NUMERIC,
    return_ratio       NUMERIC(4,3) NOT NULL DEFAULT 0.90
        CHECK (return_ratio BETWEEN 0.5 AND 1.0),
    return_min         NUMERIC(4,3) DEFAULT 0.80,
    return_max         NUMERIC(4,3) DEFAULT 0.95,
    bandwidth_min      NUMERIC,
    delay_ms           NUMERIC(10,2) NOT NULL DEFAULT 0,
    delay_min_ms       NUMERIC(10,2) NOT NULL DEFAULT 0,
    delay_max_ms       NUMERIC(10,2),
    recovery_mode      TEXT NOT NULL DEFAULT 'auto'
        CHECK (recovery_mode IN ('auto','latch','hiccup','derate_then_latch')),
    hiccup_off_ms      NUMERIC(10,2),
    hiccup_on_ms       NUMERIC(10,2),
    priority           SMALLINT NOT NULL DEFAULT 100,
    related_yc         TEXT,
    related_yx         TEXT,
    related_command    TEXT,
    related_param      TEXT,
    permission_level   TEXT NOT NULL DEFAULT 'safety_admin',
    setting_source     TEXT NOT NULL DEFAULT 'factory'
        CHECK (setting_source IN ('factory','user','calibration','safety_admin')),
    mandatory          BOOLEAN NOT NULL DEFAULT false,
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_setting_in_range
        CHECK (setting_value BETWEEN setting_min AND setting_max),
    CONSTRAINT ck_typ_in_range CHECK (trip_typ BETWEEN trip_min AND trip_max),
    CONSTRAINT ck_return_in_range CHECK (return_ratio BETWEEN return_min AND return_max),
    CONSTRAINT ck_delay_in_range CHECK (delay_ms BETWEEN delay_min_ms AND delay_max_ms),
    -- C8: 定值不得逾越器件绝对最大 (R3/R10)。
    CONSTRAINT ck_trip_below_absmax CHECK (trip_max <= abs_max),
    CONSTRAINT ck_typ_below_setting_max CHECK (trip_typ <= setting_max)
);

CREATE INDEX idx_setting_type ON :"model_key".protection_setting (protection_type);

CREATE INDEX idx_setting_zone ON :"model_key".protection_setting (setting_zone);

CREATE TABLE :"model_key".protection_setting_log (
    log_id             BIGSERIAL PRIMARY KEY,
    setting_id         TEXT NOT NULL,
    old_value          NUMERIC,
    new_value          NUMERIC NOT NULL,
    old_delay_ms       NUMERIC,
    new_delay_ms       NUMERIC,
    operator_id        TEXT NOT NULL,
    changed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    validation_c1_c10  JSONB,
    coordination_check JSONB,
    approved_by        TEXT,
    write_result       TEXT NOT NULL
        CHECK (write_result IN ('ok','readback_mismatch','rejected')),
    readback_value     NUMERIC
);

CREATE INDEX idx_setting_log ON :"model_key".protection_setting_log (setting_id, changed_at DESC);

CREATE TABLE :"model_key".protection_coordination (
    coord_id           BIGSERIAL PRIMARY KEY,
    upper_setting_id   TEXT NOT NULL
        REFERENCES :"model_key".protection_setting(setting_id),
    lower_setting_id   TEXT NOT NULL
        REFERENCES :"model_key".protection_setting(setting_id),
    coord_type         TEXT NOT NULL CHECK (coord_type IN ('time','current','both')),
    upper_external_delay_ms NUMERIC(10,2) DEFAULT 0,
    lower_breaker_open_ms   NUMERIC(10,2) DEFAULT 0,
    required_cti_ms    NUMERIC(10,2),
    actual_cti_ms      NUMERIC(10,2),
    cti_ok             BOOLEAN,
    required_kp        NUMERIC(5,3),
    actual_kp          NUMERIC(5,3),
    kp_ok              BOOLEAN,
    deadband_free      BOOLEAN,
    cti_margin_ok      BOOLEAN,
    validated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    formula_ref        TEXT NOT NULL DEFAULT 'F_L.3_COORDINATION',
    source_ref         TEXT NOT NULL,
    UNIQUE (upper_setting_id, lower_setting_id)
);

CREATE TABLE :"model_key".protection_action (
    action_id          BIGSERIAL,
    setting_id         TEXT NOT NULL,
    protection_type    TEXT NOT NULL,
    action_type        TEXT NOT NULL
        CHECK (action_type IN ('trip','return','reset','derate','retry')),
    -- action_value 是「动作时刻的实测值」。只录 setting_value 则无法验证
    -- 返回系数、无法做 SOA 校核、根因分析失效 (§16.6.6)。
    action_value       NUMERIC NOT NULL,
    setting_value      NUMERIC,
    action_time        TIMESTAMPTZ NOT NULL,
    trip_delay_ms      NUMERIC(12,3) NOT NULL,
    return_value       NUMERIC,
    return_time        TIMESTAMPTZ,
    return_ratio_actual NUMERIC(5,3),
    related_soe_id     BIGINT,
    input_voltage      NUMERIC,
    output_current     NUMERIC,
    output_voltage     NUMERIC,
    temperature        NUMERIC,
    fault_code         TEXT,
    recovery_mode      TEXT,
    hiccup_cycle       INT,
    PRIMARY KEY (action_id, action_time)
);

CREATE INDEX idx_action_setting ON :"model_key".protection_action (setting_id, action_time DESC);

CREATE TABLE :"model_key".comm_protocol (
    protocol_id        TEXT PRIMARY KEY,
    protocol_type      TEXT NOT NULL
        CHECK (protocol_type IN ('pmbus','smbus','i2c','can','canfd','modbus_rtu',
                                 'modbus_tcp','snmp','spi','uart')),
    protocol_version   TEXT,
    role               TEXT NOT NULL DEFAULT 'slave'
        CHECK (role IN ('master','slave','monitor')),
    address            TEXT NOT NULL,
    address_mode       TEXT NOT NULL DEFAULT 'pin'
        CHECK (address_mode IN ('pin','dip','nvm','i2c','dynamic')),
    speed              TEXT,
    data_bits          INT,
    stop_bits          INT,
    parity             TEXT CHECK (parity IS NULL OR parity IN ('none','even','odd')),
    pullup_res_ohm     NUMERIC(8,1),
    bus_cap_pf         NUMERIC(10,2),
    pec_enabled        BOOLEAN NOT NULL DEFAULT false,
    pec_type           TEXT CHECK (pec_type IS NULL OR pec_type IN ('crc8','crc16')),
    pec_polynomial     TEXT,
    timeout_ms         NUMERIC(10,2) NOT NULL,
    heartbeat_period_ms NUMERIC(10,2),
    heartbeat_n        INT NOT NULL DEFAULT 3 CHECK (heartbeat_n >= 1),
    retry_count        INT NOT NULL DEFAULT 3 CHECK (retry_count >= 1),
    proc_time_ms       NUMERIC(10,2),
    smalert_enabled    BOOLEAN NOT NULL DEFAULT false,
    fault_response     TEXT CHECK (fault_response IS NULL OR fault_response IN
        ('nack','cml_illegal_command','cml_value_out_of_range','cml_fault')),
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_pec_enabled CHECK (pec_enabled = FALSE OR pec_type IS NOT NULL),
    -- R17 的结构性落地: 超时必须大于 N x 心跳。
    CONSTRAINT ck_timeout_enough CHECK (
        heartbeat_period_ms IS NULL
        OR timeout_ms > heartbeat_n * heartbeat_period_ms)
);

CREATE TABLE :"model_key".fixture (
    fixture_id         TEXT PRIMARY KEY,
    fixture_name_zh    TEXT NOT NULL,
    fixture_type       TEXT NOT NULL
        CHECK (fixture_type IN ('load_board','relay_matrix','adapter',
                                'fault_injection','load_box','safety_fixture',
                                'emc_fixture','thermal_adapter','fixture_adapter')),
    fixture_role       TEXT NOT NULL CHECK (fixture_role IN ('primary','fallback','adapter')),
    asset_no           TEXT UNIQUE,
    serial_no          TEXT,
    version_rev        TEXT NOT NULL DEFAULT 'A',
    model_key          TEXT NOT NULL,
    product_pn         TEXT,
    firmware_range     TEXT,
    channel_count      INT NOT NULL CHECK (channel_count > 0),
    relay_count        INT NOT NULL DEFAULT 0,
    max_current_a      NUMERIC(10,3),
    max_voltage_v      NUMERIC(10,3),
    contact_res_mohm   NUMERIC(8,3),
    contact_res_max_mohm NUMERIC(8,3),
    isolation_level    TEXT CHECK (isolation_level IS NULL OR
        isolation_level IN ('basic','functional','double','reinforced')),
    withstand_voltage_v NUMERIC(10,3),
    max_leak_ua        NUMERIC(10,3),
    poka_yoke_enabled  BOOLEAN NOT NULL DEFAULT true,
    scan_binding       BOOLEAN NOT NULL DEFAULT true,
    interlock_type     TEXT CHECK (interlock_type IS NULL OR
        interlock_type IN ('mechanical','electrical','magnetic','photoelectric','none')),
    calibration_due    TIMESTAMPTZ,
    last_golden_run    TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','qualifying','qualified','suspended','retired')),
    design_spec_ref    TEXT,
    creator            TEXT NOT NULL,
    reviewer           TEXT,
    approver           TEXT,
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE INDEX idx_fixture_type ON :"model_key".fixture (fixture_type, status);

-- Rete 到期事件 (P15) 的匹配索引。
CREATE INDEX idx_fixture_due ON :"model_key".fixture (calibration_due) WHERE status = 'qualified';

CREATE TABLE :"model_key".test_station (
    station_id         TEXT PRIMARY KEY,
    station_name_zh    TEXT NOT NULL,
    line_id            TEXT NOT NULL,
    process_step       TEXT NOT NULL
        CHECK (process_step IN ('ICT','FCT','BURNIN','ATE','EOL','ENV','SAFETY','EMC','FINAL')),
    fixture_id         TEXT,
    instrument_ids     TEXT[] NOT NULL,
    max_parallel_batches INT NOT NULL DEFAULT 1,
    rated_power_w      NUMERIC(12,2),
    ambient_ctrl       TEXT,
    mes_endpoint       TEXT,
    mes_workorder_op   TEXT,
    plc_endpoint       TEXT,
    scanner_device_id  TEXT,
    status             TEXT NOT NULL DEFAULT 'offline'
        CHECK (status IN ('online','offline','maintenance','fault','calibrating')),
    last_calibration   TIMESTAMPTZ,
    oee_target         NUMERIC(5,2),
    owner              TEXT NOT NULL,
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);

CREATE INDEX idx_station_step ON :"model_key".test_station (process_step, status);

CREATE TABLE :"model_key".fixture_channel_map (
    map_id             BIGSERIAL PRIMARY KEY,
    fixture_id         TEXT NOT NULL REFERENCES :"model_key".fixture(fixture_id),
    channel_no         INT NOT NULL,
    channel_label      TEXT NOT NULL,
    yx_point_id        TEXT,
    yc_point_id        TEXT,
    yk_command_id      TEXT,
    yt_parameter_id    TEXT,
    protection_setting_id TEXT,
    concept_id         TEXT NOT NULL REFERENCES l0_term.concept(concept_id),
    clause_uid         TEXT REFERENCES :"model_key".clause(clause_uid),
    signal_type        TEXT NOT NULL
        CHECK (signal_type IN ('dry_contact','wet_contact','level_hv','level_lv',
                               'analog_hi','analog_lo','current_loop','power_bidir')),
    direction          TEXT NOT NULL
        CHECK (direction IN ('input','output','bidirectional','passive')),
    nominal_value      NUMERIC,
    unit               TEXT,
    -- 通路参数供 R9/P14/P18 校核。
    contact_res_mohm   NUMERIC(8,3) NOT NULL,
    trace_res_mohm     NUMERIC(8,3),
    trace_ind_nh       NUMERIC(10,3),
    trace_cap_pf       NUMERIC(10,3),
    wire_length_mm     NUMERIC(10,2),
    calibrated         BOOLEAN NOT NULL DEFAULT false,
    calibration_ref    TEXT,
    connect_status     TEXT NOT NULL DEFAULT 'pending'
        CHECK (connect_status IN ('pending','verified','mismatch','not_applicable')),
    description        TEXT,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT uq_fixture_channel UNIQUE (fixture_id, channel_no),
    CONSTRAINT uq_point_per_fixture UNIQUE (fixture_id, yx_point_id, yc_point_id,
        yk_command_id, yt_parameter_id, protection_setting_id),
    -- P13: 必须恰好映射一个点位。少映射 = 漏测, 多映射 = 歧义。
    CONSTRAINT ck_exactly_one_target CHECK (
        (yx_point_id IS NOT NULL)::int
      + (yc_point_id IS NOT NULL)::int
      + (yk_command_id IS NOT NULL)::int
      + (yt_parameter_id IS NOT NULL)::int
      + (protection_setting_id IS NOT NULL)::int = 1)
);

CREATE INDEX idx_chanmap_fixture ON :"model_key".fixture_channel_map (fixture_id);

CREATE INDEX idx_chanmap_yx ON :"model_key".fixture_channel_map (yx_point_id);

CREATE INDEX idx_chanmap_yc ON :"model_key".fixture_channel_map (yc_point_id);

CREATE TABLE :"model_key".fixture_checkpoint (
    checkpoint_id      BIGSERIAL PRIMARY KEY,
    fixture_id         TEXT NOT NULL REFERENCES :"model_key".fixture(fixture_id),
    checkpoint_seq     INT NOT NULL,
    level              INT NOT NULL CHECK (level BETWEEN 1 AND 7),
    level_name         TEXT NOT NULL
        CHECK (level_name IN ('mechanical_interlock','polarity_detect',
                              'pin_presence','scan_binding','param_readback',
                              'golden_sample','data_consistency')),
    trigger_point      TEXT NOT NULL
        CHECK (trigger_point IN ('before_load','at_load','before_test',
                                 'after_test','per_shift','per_day','per_batch')),
    method             TEXT NOT NULL
        CHECK (method IN ('interlock_pin','sensor','continuity_scan','barcode',
                          'readback_compare','golden_compare','triple_check')),
    fail_action        TEXT NOT NULL
        CHECK (fail_action IN ('block_load','block_test','raise_alarm',
                               'quarantine_product','quarantine_fixture','stop_line')),
    last_result        TEXT
        CHECK (last_result IS NULL OR last_result IN ('pass','fail','skipped')),
    last_checked_at    TIMESTAMPTZ,
    fail_count_30d     INT NOT NULL DEFAULT 0,
    enabled            BOOLEAN NOT NULL DEFAULT true,
    formula_ref        TEXT,
    rule_ref           TEXT,
    description        TEXT NOT NULL,
    source_ref         TEXT NOT NULL,
    UNIQUE (fixture_id, checkpoint_seq)
);

CREATE TABLE :"model_key".poka_yoke_event (
    event_id           BIGSERIAL PRIMARY KEY,
    checkpoint_id      BIGINT NOT NULL
        REFERENCES :"model_key".fixture_checkpoint(checkpoint_id),
    station_id         TEXT NOT NULL,
    product_sn         TEXT,
    workorder_id       TEXT,
    result             TEXT NOT NULL CHECK (result IN ('pass','fail')),
    detail             JSONB,
    operator_id        TEXT,
    event_time         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_poka_fail ON :"model_key".poka_yoke_event (checkpoint_id, event_time DESC)
    WHERE result = 'fail';

CREATE TABLE :"model_key".instrument_ledger (
    ts                 TIMESTAMPTZ NOT NULL,
    instrument_id      TEXT NOT NULL,
    instrument_type    TEXT NOT NULL,
    model              TEXT,
    channel            INT,
    busy               BOOLEAN DEFAULT false,
    cal_due            DATE,
    probe_life_hours   NUMERIC(10,2),
    failure_rate       NUMERIC(8,6),
    location           TEXT,
    metadata           JSONB
);

CREATE INDEX idx_ledger_inst ON :"model_key".instrument_ledger (instrument_id, ts DESC);

CREATE TABLE :"model_key".fixture_tp_probe (
    ts                 TIMESTAMPTZ NOT NULL,
    fixture_id         TEXT NOT NULL,
    probe_id           TEXT NOT NULL,
    tp_id              TEXT,
    channel            INT,
    life_hours         NUMERIC(10,2),
    wear_level         NUMERIC(3,2),
    status             TEXT
);

CREATE TABLE :"model_key".sched_result (
    sched_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_ids           UUID[] NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('OPTIMAL','FEASIBLE','INFEASIBLE')),
    makespan_ms        BIGINT,
    gap                NUMERIC(5,4),
    schedule           JSONB,
    optimization_levers TEXT[],
    solved_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE :"model_key".jev_gate_log (
    log_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_type      TEXT NOT NULL,
    input_state        JSONB NOT NULL,
    output_decision    JSONB NOT NULL,
    confidence         NUMERIC(4,3),
    threshold          NUMERIC(4,3),
    passed             BOOLEAN,
    category_schema    TEXT NOT NULL,
    stage              TEXT,
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE :"model_key".yx_point ADD CONSTRAINT fk_yx_point_related_protection FOREIGN KEY (related_protection) REFERENCES :"model_key".protection_setting (setting_id);

ALTER TABLE :"model_key".yx_point ADD CONSTRAINT fk_yx_point_related_yc FOREIGN KEY (related_yc) REFERENCES :"model_key".yc_point (point_id);

ALTER TABLE :"model_key".yx_point ADD CONSTRAINT fk_yx_point_related_checkpoint FOREIGN KEY (related_checkpoint) REFERENCES :"model_key".fixture_checkpoint (checkpoint_id);

ALTER TABLE :"model_key".protection_setting ADD CONSTRAINT fk_protection_setting_related_yc FOREIGN KEY (related_yc) REFERENCES :"model_key".yc_point (point_id);

ALTER TABLE :"model_key".protection_setting ADD CONSTRAINT fk_protection_setting_related_yx FOREIGN KEY (related_yx) REFERENCES :"model_key".yx_point (point_id);

ALTER TABLE :"model_key".protection_setting ADD CONSTRAINT fk_protection_setting_related_command FOREIGN KEY (related_command) REFERENCES :"model_key".yk_command (command_id);

ALTER TABLE :"model_key".protection_setting ADD CONSTRAINT fk_protection_setting_related_param FOREIGN KEY (related_param) REFERENCES :"model_key".yt_parameter (parameter_id);

ALTER TABLE :"model_key".test_station ADD CONSTRAINT fk_test_station_fixture_id FOREIGN KEY (fixture_id) REFERENCES :"model_key".fixture (fixture_id);

ALTER TABLE :"model_key".yk_audit ADD CONSTRAINT fk_yk_audit_command_id FOREIGN KEY (command_id) REFERENCES :"model_key".yk_command (command_id);

ALTER TABLE :"model_key".yt_change_log ADD CONSTRAINT fk_yt_change_log_parameter_id FOREIGN KEY (parameter_id) REFERENCES :"model_key".yt_parameter (parameter_id);

ALTER TABLE :"model_key".protection_setting_log ADD CONSTRAINT fk_protection_setting_log_setting_id FOREIGN KEY (setting_id) REFERENCES :"model_key".protection_setting (setting_id);

ALTER TABLE :"model_key".fixture_channel_map ADD CONSTRAINT fk_fixture_channel_map_yx_point_id FOREIGN KEY (yx_point_id) REFERENCES :"model_key".yx_point (point_id);

ALTER TABLE :"model_key".fixture_channel_map ADD CONSTRAINT fk_fixture_channel_map_yc_point_id FOREIGN KEY (yc_point_id) REFERENCES :"model_key".yc_point (point_id);

ALTER TABLE :"model_key".fixture_channel_map ADD CONSTRAINT fk_fixture_channel_map_yk_command_id FOREIGN KEY (yk_command_id) REFERENCES :"model_key".yk_command (command_id);

ALTER TABLE :"model_key".fixture_channel_map ADD CONSTRAINT fk_fixture_channel_map_yt_parameter_id FOREIGN KEY (yt_parameter_id) REFERENCES :"model_key".yt_parameter (parameter_id);

ALTER TABLE :"model_key".fixture_channel_map ADD CONSTRAINT fk_fixture_channel_map_protection_setting_id FOREIGN KEY (protection_setting_id) REFERENCES :"model_key".protection_setting (setting_id);

-- ============================================================
-- 3. hypertable (TimescaleDB)
-- ============================================================

SELECT create_hypertable(format('%I.%I', :'model_key', 'yx_soe'), 'event_time', chunk_time_interval => INTERVAL '1 day');

SELECT create_hypertable(format('%I.%I', :'model_key', 'yc_trend'), 'ts', chunk_time_interval => INTERVAL '7 days');

SELECT create_hypertable(format('%I.%I', :'model_key', 'protection_action'), 'action_time', chunk_time_interval => INTERVAL '7 days');

SELECT create_hypertable(format('%I.%I', :'model_key', 'instrument_ledger'), 'ts', chunk_time_interval => INTERVAL '7 days');

SELECT create_hypertable(format('%I.%I', :'model_key', 'fixture_tp_probe'), 'ts', chunk_time_interval => INTERVAL '7 days');

-- ============================================================
-- 4. RLS 策略 (§5.8)
-- ============================================================

CREATE OR REPLACE FUNCTION "public"."ctx_model"() RETURNS TEXT AS $$ SELECT current_setting('app.current_model', true); $$ LANGUAGE sql STABLE SECURITY DEFINER;

ALTER TABLE :"model_key"."fact" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."fact" FORCE ROW LEVEL SECURITY;

CREATE POLICY "fact_model_isolation" ON :"model_key"."fact" FOR ALL USING ("tenant_schema" = ctx_model()) WITH CHECK ("tenant_schema" = ctx_model());

ALTER TABLE :"model_key"."doc_chunk" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."doc_chunk" FORCE ROW LEVEL SECURITY;

CREATE POLICY "doc_chunk_model_isolation" ON :"model_key"."doc_chunk" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."test_case" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."test_case" FORCE ROW LEVEL SECURITY;

CREATE POLICY "test_case_model_isolation" ON :"model_key"."test_case" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."test_requirement" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."test_requirement" FORCE ROW LEVEL SECURITY;

CREATE POLICY "test_requirement_model_isolation" ON :"model_key"."test_requirement" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."provenance" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."provenance" FORCE ROW LEVEL SECURITY;

CREATE POLICY "provenance_model_isolation" ON :"model_key"."provenance" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."conflict" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."conflict" FORCE ROW LEVEL SECURITY;

CREATE POLICY "conflict_model_isolation" ON :"model_key"."conflict" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."yx_point" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."yx_point" FORCE ROW LEVEL SECURITY;

CREATE POLICY "yx_point_model_isolation" ON :"model_key"."yx_point" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."yc_point" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."yc_point" FORCE ROW LEVEL SECURITY;

CREATE POLICY "yc_point_model_isolation" ON :"model_key"."yc_point" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."yk_command" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."yk_command" FORCE ROW LEVEL SECURITY;

CREATE POLICY "yk_command_model_isolation" ON :"model_key"."yk_command" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."yt_parameter" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."yt_parameter" FORCE ROW LEVEL SECURITY;

CREATE POLICY "yt_parameter_model_isolation" ON :"model_key"."yt_parameter" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."protection_setting" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."protection_setting" FORCE ROW LEVEL SECURITY;

CREATE POLICY "protection_setting_model_isolation" ON :"model_key"."protection_setting" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."fixture" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."fixture" FORCE ROW LEVEL SECURITY;

CREATE POLICY "fixture_model_isolation" ON :"model_key"."fixture" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."test_station" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."test_station" FORCE ROW LEVEL SECURITY;

CREATE POLICY "test_station_model_isolation" ON :"model_key"."test_station" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."fixture_channel_map" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."fixture_channel_map" FORCE ROW LEVEL SECURITY;

CREATE POLICY "fixture_channel_map_model_isolation" ON :"model_key"."fixture_channel_map" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

ALTER TABLE :"model_key"."fixture_checkpoint" ENABLE ROW LEVEL SECURITY;

ALTER TABLE :"model_key"."fixture_checkpoint" FORCE ROW LEVEL SECURITY;

CREATE POLICY "fixture_checkpoint_model_isolation" ON :"model_key"."fixture_checkpoint" FOR ALL USING (ctx_model() = :'model_key') WITH CHECK (ctx_model() = :'model_key');

-- ============================================================
-- 5. 触发器与函数 (第九章; 随第 2 分区输出)
-- ============================================================
-- (无)
