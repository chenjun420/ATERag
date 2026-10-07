"""型号 schema (``pw_<model_key>``) 的业务表 DDL。

依据 V6.0（每张表标注权威章节）:
    §3.5.2    doc_chunk
    §3.5.3    fact / provenance / conflict / fact_as_of (视图)
    §3.5.4    test_requirement / test_case
    §3.5.5    instrument_ledger / fixture_tp_probe / sched_result / jev_gate_log
    §16.2.2   yx_point          §16.2.3  yx_soe        (hypertable)
    §16.3.2   yc_point          §16.3.4  yc_trend      (hypertable)
    §16.4.2   yk_command / yk_audit
    §16.5.2   yt_parameter      §16.5.3  yt_change_log
    §16.6.2   protection_setting      §16.6.4 protection_setting_log
    §16.6.5   protection_coordination §16.6.6 protection_action (hypertable)
    §16.7.2   comm_protocol
    §17.2.1   fixture           §17.2.2  test_station
    §17.3.1   fixture_channel_map
    §17.4.2   fixture_checkpoint / poka_yoke_event
    §18.5     执行顺序的权威位置汇总

与 alembic 的分工
----------------
L0 共享层在 ``alembic/versions/`` 里; 型号 schema **不在**。理由见
``alembic/env.py`` 的「型号 schema 的处理」—— 型号是部署期事实
(``data/registry.yaml``), 不是代码。本模块是该事实的可执行形态。

三处刻意偏离 spec
-----------------
1. **FK 目标**。spec 里 ``yx_point.related_protection`` 等自引用型外键指向
   同 schema 的表, 且存在前向引用 (``yx_point`` 引 ``fixture_checkpoint``,
   而 ``fixture_checkpoint`` 引 ``fixture``)。建表顺序必须拓扑排序, 所以本
   模块把建表分成若干 **批**, 批内无依赖, 批间顺序固定。

2. **§18.5 表名与章节表名不一致**。§18.5 第 2 分区写「基础: doc / clause /
   fact / graph_node / graph_edge / trace」, 但全文 59 处 ``CREATE TABLE``
   里没有 ``doc``/``clause``/``graph_node``/``graph_edge``/``trace``;
   §3.5 权威定义只有 ``doc_chunk``/``fact``/``provenance``/``conflict``。
   本模块按**章节权威定义**建表 (§3.5), 不按 §18.5 的概览名。理由:
   §18.5 自己写「各模块的 DDL 以『权威位置』为准」。§3.9 的
   ``document_object``/``document_page`` 放在 ``public`` 而非型号 schema
   (spec 的 DDL 里它们无 schema 前缀, 且 §3.9 说「PG 为唯一真相源」),
   故由 :func:`public_ddl` 单独生成。

3. **hypertable 前置检查**。``create_hypertable`` 需要 TimescaleDB。板卡与
   容器的可用扩展集不同, 所以 :func:`hypertable_ddl` 只**生成**语句,
   是否执行由调用方在确认扩展存在后决定 (见 :func:`hypertable_available_sql`)。

本模块只生成 SQL 字符串, 不连库 —— 与 ``storage.schema`` / ``storage.rls``
同风格, 便于单元测试在无数据库环境下覆盖全部生成逻辑。
"""

from __future__ import annotations

from collections.abc import Callable

from .rls import (
    SchemaIdent,
    SchemaRef,
    rls_ddl_for_ref,
    schema_ref,
)
from .schema import (
    L0_SCHEMA,
    SchemaError,
    model_schema_name,
)
from .tables import MODEL_TABLES  # noqa: F401  (对外转出, 见 __all__)

__all__ = [
    "HYPERTABLES",
    "MODEL_TABLES",
    "PUBLIC_TABLES",
    "hypertable_available_sql",
    "hypertable_ddl",
    "hypertable_ddl_for_ref",
    "model_ddl",
    "model_ddl_for_ref",
    "model_schema_ddl",
    "model_schema_ddl_for_ref",
    "public_ddl",
    "table_batches",
    "table_batches_for_ref",
]

#: TimescaleDB 扩展名。§18.5 第 3 分区需要它。
#: 注意是 ``timescaledb`` 而非 ``timescale`` —— 后者是产品名与 schema 名。
#: 板卡实测 `pg_available_extensions WHERE name LIKE 'timescale%'` 只返回
#: `timescaledb`。同 OPTIONAL_EXTENSIONS 处的说明。
TIMESCALE_EXTENSION = "timescaledb"

#: §3.5.2 文档向量的维度。与 ADR-013 一致 (统一 halfvec(1024))。
EMBED_DIM = 1024


# --------------------------------------------------------------------------
# 表定义
# --------------------------------------------------------------------------
# 每项是一个返回 DDL 文本的零参闭包。闭包而非裸字符串, 是为了让
# 「取某一批的 DDL」和「取全量」共用同一份定义, 避免两处漂移。


def _doc(s: SchemaIdent) -> str:
    """``doc`` —— 文档台账 (§18.6 Step 10)。

    **列集是推导的, 不是 spec 原文。** §18.5 第 2 分区点名 ``doc``, §18.6
    Step 10 要求装载「文档与条款 (clause_uid 体系)」, 但全文 59 处
    ``CREATE TABLE`` 里没有它。推导依据三条:

    1. §3.5.2 的 ``doc_chunk`` 以 ``(doc_id, rev)`` 为事实来源 —— 那两列
       必须在 ``doc`` 里有主键, 否则 chunk 指向不存在的文档。
    2. §3.9 的 ``document_object`` 是**物理附件**台账 (sha256 / 页数 /
       存储位置 / 可重建性), ``doc`` 是**逻辑文档**台账 (一次规格书有
       若干修订版)。两者不是一回事, 故 ``object_id`` 是可选外键而非主键。
    3. §5.8.2 的双时态: 文档换版是「多一版」不是「改一版」, 所以主键含
       ``rev``, 且另加 partial unique index 保证同一 ``doc_id`` 至多一条
       当前版本。

    双时态列照 §3.5 各表的既有形态 (valid_from NOT NULL + valid_until NULL)。
    """
    return f"""
CREATE TABLE {s}.doc (
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
CREATE INDEX idx_doc_id ON {s}.doc (doc_id);
CREATE INDEX idx_doc_object ON {s}.doc (object_id);
-- 同一 doc_id 至多一条当前版本 (与 storage/bitemporal.py 同一机制)。
CREATE UNIQUE INDEX ux_doc_current ON {s}.doc (doc_id) WHERE valid_until IS NULL;
"""


def _clause(s: SchemaIdent) -> str:
    """``clause`` —— 条款表 (§18.6 Step 10)。

    **列集是推导的, 理由同 :func:`_doc`。** 方案唯一定义 ``clause_uid``
    语义的地方是 §17.4 的硬约束「不得编造条款号: clause_uid 必须来自
    clause 表」—— 也就是说这张表是 clause_uid 的**唯一合法来源**, 而
    ``yx_point`` / ``yc_point`` / ``yk_command`` / ``yt_parameter`` /
    ``protection_setting`` / ``fixture_channel_map`` / ``comm_protocol``
    七张表都带 ``clause_uid`` 列。不建它, 这些列全是无约束的自由文本,
    溯源链在第一步就断了。

    主键是 ``clause_uid`` 单列, **刻意不带 valid_from/valid_until**:
    clause_uid 本身已含文档修订版 (约定形如 ``SR-PA601-D54A-1213@B#3.5.2``),
    修订版即版本轴, 再加时间列就是装饰列 —— 与 ``rule`` 表同一个理由
    (见 ADR-019 决策 2)。文档换版产生新的 clause_uid, 不覆盖旧的。

    ``is_normative``: 规格书里大量图注/目录/页眉不是条款。标出来才能
    让「不得编造条款号」这条约束有作用范围, 否则溯源指向一个页眉行
    也会被判为已追溯。
    """
    return f"""
CREATE TABLE {s}.clause (
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
    FOREIGN KEY (doc_id, rev) REFERENCES {s}.doc (doc_id, rev) ON DELETE CASCADE
);
CREATE INDEX idx_clause_doc ON {s}.clause (doc_id, rev);
CREATE INDEX idx_clause_path ON {s}.clause (clause_path);
CREATE INDEX idx_clause_normative ON {s}.clause (is_normative, needs_review);
"""


def _trace(s: SchemaIdent) -> str:
    """``trace`` —— 五级可追溯链 (§18.6 Step 17 / §1.6)。

    **列集是推导的, 理由同 :func:`_doc`。** §1.6 要求六级链
    公理 → 定理 → 公式 → 规则 → 测试 → 判据, §18.6 Step 17 要求装载
    ``trace``。方案没给 DDL, 但 §18.7 的回归场景与 §18.9 的 G 门禁都依赖
    「某个测试失败时能反查到是哪条公式、哪条规则、哪个公理」—— 没有这张
    表, 追溯只能靠跨四个 schema 临时 JOIN, 而那正是 §18.2 反对的
    「散在各处」形态。

    六级用 ``layer`` + ``ref_id`` 表达而非六个独立外键列: 六个列会让
    CHECK 约束写成「恰有一个非空」, 换一级就要改约束。分层表示则
    ``layer`` 的 CHECK 就是全部。

    时间历史归 ``provenance`` (那里已有 valid_from/valid_until 与
    derivation_path), 本表只存「当前拓扑」。理由同 :func:`_clause`:
    给溯源索引加时间列会让它变成装饰列, 而真正需要时间旅行的是被指向的
    事实本身, 已有 provenance 承载。
    """
    return f"""
CREATE TABLE {s}.trace (
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
CREATE INDEX idx_trace_ref ON {s}.trace (layer, ref_id);
CREATE INDEX idx_trace_up ON {s}.trace (upstream_layer, upstream_ref);
CREATE INDEX idx_trace_down ON {s}.trace (downstream_layer, downstream_ref);
CREATE INDEX idx_trace_unverified ON {s}.trace (layer, ref_id) WHERE verified = false;
"""


def _doc_chunk(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.doc_chunk (
    id                 BIGSERIAL PRIMARY KEY,
    doc_id             TEXT NOT NULL,
    rev                TEXT NOT NULL,
    page               INT,
    chunk_index        INT NOT NULL,
    content            TEXT NOT NULL,
    vec                halfvec({EMBED_DIM}),
    tsv                tsvector,
    concept_ids        TEXT[],
    parse_confidence   NUMERIC(3,2),
    source_type        TEXT DEFAULT 'pdf',
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_doc_chunk_vec_hnsw ON {s}.doc_chunk
    USING hnsw (vec halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_doc_chunk_tsv ON {s}.doc_chunk USING gin (tsv);
CREATE INDEX idx_doc_chunk_valid ON {s}.doc_chunk (valid_from, valid_until);
CREATE INDEX idx_doc_chunk_concept ON {s}.doc_chunk USING gin (concept_ids);
CREATE INDEX idx_doc_chunk_doc ON {s}.doc_chunk (doc_id, rev, page);

CREATE OR REPLACE FUNCTION {s}.update_tsv() RETURNS TRIGGER AS $$
BEGIN
    NEW.tsv := to_tsvector('chinese_zh', NEW.content);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_doc_chunk_tsv
    BEFORE INSERT OR UPDATE ON {s}.doc_chunk
    FOR EACH ROW EXECUTE FUNCTION {s}.update_tsv();
"""


def _fact(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.fact (
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
    tenant_schema      TEXT NOT NULL DEFAULT {s.literal},
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_fact_concept ON {s}.fact (concept_id);
CREATE INDEX idx_fact_trust ON {s}.fact (trust_level);
CREATE INDEX idx_fact_valid ON {s}.fact (valid_from, valid_until);
CREATE INDEX idx_fact_cond ON {s}.fact USING gin (condition_vector);

-- §5.8.2 双时态视图。纪律 3: 溯源导出必须走它, 不许直查基表。
CREATE OR REPLACE VIEW {s}.fact_as_of AS
SELECT * FROM {s}.fact
WHERE valid_from <= now()
  AND (valid_until IS NULL OR valid_until > now());
"""


def _provenance(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.provenance (
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
CREATE INDEX idx_prov_entity ON {s}.provenance (entity_id, valid_from, valid_until);
CREATE INDEX idx_prov_type ON {s}.provenance (entity_type, activity);
"""


def _conflict(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.conflict (
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
CREATE INDEX idx_conflict_concept ON {s}.conflict (concept_id, resolved);
"""


def _test_requirement(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.test_requirement (
    req_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sr_id              TEXT NOT NULL,
    -- 档位键: 一个 sr_id 常有多行(多电压轨/多负载档/长期短期)。实测 PA601
    -- 95 个条件只对应 74 个编号, 18 个编号有 2~3 档 —— 只有 sr_id 时无法
    -- 定位一行, 而按 sr_id upsert 会**静默覆盖**(丢掉另外 21 行)。
    -- 语义与 entity_extract 的 eid 档位后缀一致: 轨 + 工况标签, 不含判据数值
    -- (判据一改主键就变, 判据便无法版本化)。
    -- 缺省为 '' 而不是 NOT NULL: 迁移前的老行没有这个概念, 用空串表示
    -- 「未标注档位」, 与 entity_extract 里 rail='' 的既有语义一致。
    variant_key        TEXT NOT NULL DEFAULT '',
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
CREATE INDEX idx_req_sr ON {s}.test_requirement (sr_id);
-- (sr_id, variant_key) 唯一: 落库幂等的依据。**不带这个唯一约束就只能靠
-- 应用层去重**, 而去重逻辑一旦漏判就是静默丢行(实测: 18 个编号多档位)。
CREATE UNIQUE INDEX uq_req_sr_variant ON {s}.test_requirement (sr_id, variant_key);
CREATE INDEX idx_req_concept ON {s}.test_requirement (concept_id);
CREATE INDEX idx_req_coverage ON {s}.test_requirement (coverage_status);
CREATE INDEX idx_req_signal ON {s}.test_requirement (signal_type);
"""


def _test_case(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.test_case (
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
CREATE INDEX idx_case_trust ON {s}.test_case (trust_level);
CREATE INDEX idx_case_refines ON {s}.test_case USING gin (refines);
"""


def _instrument_ledger(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.instrument_ledger (
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
CREATE INDEX idx_ledger_inst ON {s}.instrument_ledger (instrument_id, ts DESC);
"""


def _fixture_tp_probe(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.fixture_tp_probe (
    ts                 TIMESTAMPTZ NOT NULL,
    fixture_id         TEXT NOT NULL,
    probe_id           TEXT NOT NULL,
    tp_id              TEXT,
    channel            INT,
    life_hours         NUMERIC(10,2),
    wear_level         NUMERIC(3,2),
    status             TEXT
);
"""


def _sched_result(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.sched_result (
    sched_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_ids           UUID[] NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('OPTIMAL','FEASIBLE','INFEASIBLE')),
    makespan_ms        BIGINT,
    gap                NUMERIC(5,4),
    schedule           JSONB,
    optimization_levers TEXT[],
    solved_at          TIMESTAMPTZ DEFAULT now()
);
"""


def _jev_gate_log(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.jev_gate_log (
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
"""


def _yx_point(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yx_point (
    point_id           TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES {L0_SCHEMA}.concept(concept_id),
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
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_related_not_empty CHECK (
        related_protection IS NOT NULL OR related_yc IS NOT NULL
        OR related_checkpoint IS NOT NULL)
);
CREATE INDEX idx_yx_concept ON {s}.yx_point (concept_id);
CREATE INDEX idx_yx_protocol ON {s}.yx_point (protocol, register_address);
CREATE INDEX idx_yx_prot ON {s}.yx_point (related_protection);
"""


def _yx_soe(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yx_soe (
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
CREATE INDEX idx_soe_point ON {s}.yx_soe (point_id, event_time DESC);
CREATE INDEX idx_soe_seq ON {s}.yx_soe (sequence_no DESC);
"""


def _yc_point(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yc_point (
    point_id           TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES {L0_SCHEMA}.concept(concept_id),
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
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
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
CREATE INDEX idx_yc_concept ON {s}.yc_point (concept_id);
CREATE INDEX idx_yc_protocol ON {s}.yc_point (protocol, register_address);
"""


def _yc_trend(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yc_trend (
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
CREATE INDEX idx_trend_point ON {s}.yc_trend (point_id, ts DESC);
"""


def _yk_command(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yk_command (
    command_id         TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES {L0_SCHEMA}.concept(concept_id),
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
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ
);
"""


def _yk_audit(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yk_audit (
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
CREATE INDEX idx_yk_audit ON {s}.yk_audit (command_id, issued_at DESC);
"""


def _yt_parameter(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yt_parameter (
    parameter_id       TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES {L0_SCHEMA}.concept(concept_id),
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
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
    description        TEXT,
    source_ref         TEXT NOT NULL,
    valid_from         TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until        TIMESTAMPTZ,
    CONSTRAINT ck_range_valid CHECK (range_max > range_min),
    CONSTRAINT ck_default_in_range CHECK (default_value BETWEEN range_min AND range_max)
);
"""


def _yt_change_log(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.yt_change_log (
    log_id             BIGSERIAL PRIMARY KEY,
    parameter_id       TEXT NOT NULL,
    old_value          NUMERIC,
    new_value          NUMERIC NOT NULL,
    operator_id        TEXT NOT NULL,
    revalidated        TEXT[],
    changed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_yt_change ON {s}.yt_change_log (parameter_id, changed_at DESC);
"""


def _protection_setting(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.protection_setting (
    setting_id         TEXT PRIMARY KEY,
    concept_id         TEXT NOT NULL REFERENCES {L0_SCHEMA}.concept(concept_id),
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
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
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
CREATE INDEX idx_setting_type ON {s}.protection_setting (protection_type);
CREATE INDEX idx_setting_zone ON {s}.protection_setting (setting_zone);
"""


def _protection_setting_log(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.protection_setting_log (
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
CREATE INDEX idx_setting_log ON {s}.protection_setting_log (setting_id, changed_at DESC);
"""


def _protection_coordination(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.protection_coordination (
    coord_id           BIGSERIAL PRIMARY KEY,
    upper_setting_id   TEXT NOT NULL
        REFERENCES {s}.protection_setting(setting_id),
    lower_setting_id   TEXT NOT NULL
        REFERENCES {s}.protection_setting(setting_id),
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
"""


def _protection_action(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.protection_action (
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
CREATE INDEX idx_action_setting ON {s}.protection_action (setting_id, action_time DESC);
"""


def _comm_protocol(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.comm_protocol (
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
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
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
"""


def _fixture(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.fixture (
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
CREATE INDEX idx_fixture_type ON {s}.fixture (fixture_type, status);
-- Rete 到期事件 (P15) 的匹配索引。
CREATE INDEX idx_fixture_due ON {s}.fixture (calibration_due) WHERE status = 'qualified';
"""


def _test_station(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.test_station (
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
CREATE INDEX idx_station_step ON {s}.test_station (process_step, status);
"""


def _fixture_channel_map(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.fixture_channel_map (
    map_id             BIGSERIAL PRIMARY KEY,
    fixture_id         TEXT NOT NULL REFERENCES {s}.fixture(fixture_id),
    channel_no         INT NOT NULL,
    channel_label      TEXT NOT NULL,
    yx_point_id        TEXT,
    yc_point_id        TEXT,
    yk_command_id      TEXT,
    yt_parameter_id    TEXT,
    protection_setting_id TEXT,
    concept_id         TEXT NOT NULL REFERENCES {L0_SCHEMA}.concept(concept_id),
    clause_uid         TEXT REFERENCES {s}.clause(clause_uid),
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
CREATE INDEX idx_chanmap_fixture ON {s}.fixture_channel_map (fixture_id);
CREATE INDEX idx_chanmap_yx ON {s}.fixture_channel_map (yx_point_id);
CREATE INDEX idx_chanmap_yc ON {s}.fixture_channel_map (yc_point_id);
"""


def _fixture_checkpoint(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.fixture_checkpoint (
    checkpoint_id      BIGSERIAL PRIMARY KEY,
    fixture_id         TEXT NOT NULL REFERENCES {s}.fixture(fixture_id),
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
"""


def _poka_yoke_event(s: SchemaIdent) -> str:
    return f"""
CREATE TABLE {s}.poka_yoke_event (
    event_id           BIGSERIAL PRIMARY KEY,
    checkpoint_id      BIGINT NOT NULL
        REFERENCES {s}.fixture_checkpoint(checkpoint_id),
    station_id         TEXT NOT NULL,
    product_sn         TEXT,
    workorder_id       TEXT,
    result             TEXT NOT NULL CHECK (result IN ('pass','fail')),
    detail             JSONB,
    operator_id        TEXT,
    event_time         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_poka_fail ON {s}.poka_yoke_event (checkpoint_id, event_time DESC)
    WHERE result = 'fail';
"""


#: 批次 -> 表定义。批内表之间无依赖; 批间顺序不可换 (见模块 docstring 的偏离 1)。
#: 表名顺序与 §18.5 第 2 分区的分组对应, 便于逐组核对。
_TABLE_BATCHES: tuple[tuple[str, tuple[Callable[[SchemaIdent], str], ...]], ...] = (
    (
        # 知识与事实 (§18.5 「基础」)
        #
        # 批内顺序也是依赖顺序: doc -> clause 的复合外键,
        # 且 trace 无出边 FK, 故三者排在最前不占 alter_fk 批。
        "knowledge",
        (
            _doc,
            _clause,
            _trace,
            _doc_chunk,
            _fact,
            _provenance,
            _conflict,
            _test_requirement,
            _test_case,
        ),
    ),
    (
        # 四遥 (§18.5 「四遥」) —— 同批内无互引, FK 留空待 alter_fk 批 ALTER。
        "telemetry",
        (
            _yx_point,
            _yx_soe,
            _yc_point,
            _yc_trend,
            _yk_command,
            _yk_audit,
            _yt_parameter,
            _yt_change_log,
        ),
    ),
    (
        # 保护 (§18.5 「四遥」续)
        "protection",
        (
            _protection_setting,
            _protection_setting_log,
            _protection_coordination,
            _protection_action,
            _comm_protocol,
        ),
    ),
    (
        # 工装与工位 (§18.5 「工装」)
        "fixture",
        (
            _fixture,
            _test_station,
            _fixture_channel_map,
            _fixture_checkpoint,
            _poka_yoke_event,
        ),
    ),
    (
        # 仪器台账与调度 (§18.5 「仪器」「调度」+ §3.5.5)
        "instrument",
        (
            _instrument_ledger,
            _fixture_tp_probe,
            _sched_result,
            _jev_gate_log,
        ),
    ),
)

#: 第 3 批补的 FK。spec 把它写成建表时的 REFERENCES, 但被引表在后面的批次
#: 才建 (yx_point -> fixture_checkpoint / protection_setting 等), 必须后补。
_ALTER_FKS: tuple[tuple[str, str, str, str], ...] = (
    # (表, 列, 目标表, 目标列)
    ("yx_point", "related_protection", "protection_setting", "setting_id"),
    ("yx_point", "related_yc", "yc_point", "point_id"),
    ("yx_point", "related_checkpoint", "fixture_checkpoint", "checkpoint_id"),
    ("protection_setting", "related_yc", "yc_point", "point_id"),
    ("protection_setting", "related_yx", "yx_point", "point_id"),
    ("protection_setting", "related_command", "yk_command", "command_id"),
    ("protection_setting", "related_param", "yt_parameter", "parameter_id"),
    ("test_station", "fixture_id", "fixture", "fixture_id"),
    ("yk_audit", "command_id", "yk_command", "command_id"),
    ("yt_change_log", "parameter_id", "yt_parameter", "parameter_id"),
    ("protection_setting_log", "setting_id", "protection_setting", "setting_id"),
    ("fixture_channel_map", "yx_point_id", "yx_point", "point_id"),
    ("fixture_channel_map", "yc_point_id", "yc_point", "point_id"),
    ("fixture_channel_map", "yk_command_id", "yk_command", "command_id"),
    ("fixture_channel_map", "yt_parameter_id", "yt_parameter", "parameter_id"),
    (
        "fixture_channel_map",
        "protection_setting_id",
        "protection_setting",
        "setting_id",
    ),
)

#: 刻意**不加**外键的地方, 以及原因。写成显式清单而不是「忘了加」, 是因为
#: 「为什么这里没有」是审阅者必然要问的问题。
#:
#: 1. **hypertable 出边** —— yx_soe / yc_trend / protection_action /
#:    instrument_ledger / fixture_tp_probe 是 TimescaleDB hypertable
#:    (§18.5 第 3 分区)。TimescaleDB 对 hypertable 上的外键限制严格,
#:    且 chunk 会让一条逻辑外键的实际存储散在多张物理表里。强行加约束
#:    会在部分小版本上直接建不出来, 换版本又可能静默不生效 ——
#:    静默不生效的约束比没有约束更危险。这些表的引用完整性由应用层
#:    (``fixture.channelmap`` / ``generation.testcase``) 与 Rete 校验承担。
#:
#: 2. ``yx_soe.point_id`` -> ``yx_point`` —— 同上, hypertable 出边。
#:    §16.2.3 的 spec DDL 里就没有这条 REFERENCES (它只写 ``point_id TEXT
#:    NOT NULL``), 所以这不是我的削减, 是照抄 spec。
#:
#: 3. ``yc_point.current_a`` —— §17.3.2 判定③ 的 SQL 写
#:    ``SELECT COALESCE(MAX(current_a),0) FROM yc_point WHERE point_id=...``,
#:    但 yc_point 是**点位定义表**, 一行一个点位, 对它自己的 current_a
#:    取 MAX 恒等于该值, 而判定③ 要的显然是测试时的**实测电流**。
#:    spec 该处 SQL 有缺陷。列保留 (否则判定③ 跑不了), 但语义按
#:    「通道额定电流上限」理解, 且不加自引用外键。真实电流走 yc_trend。
#:    见 ADR-018 决策 5。


def _alter_fk_ddl(s: SchemaIdent) -> str:
    stmts = [
        f"ALTER TABLE {s}.{tbl} ADD CONSTRAINT fk_{tbl}_{col} "
        f"FOREIGN KEY ({col}) REFERENCES {s}.{ref_tbl} ({ref_col});"
        for tbl, col, ref_tbl, ref_col in _ALTER_FKS
    ]
    return "\n".join(stmts) + "\n"


#: §16.2.3 / §16.3.4 / §16.6.6 的 hypertable。§3.5.5 的两个台账表 spec 也标了
#: create_hypertable, 一并纳入。
#:
#: 键是 (表名, 时间列, chunk 间隔)。
HYPERTABLES: tuple[tuple[str, str, str], ...] = (
    ("yx_soe", "event_time", "1 day"),
    ("yc_trend", "ts", "7 days"),
    ("protection_action", "action_time", "7 days"),
    ("instrument_ledger", "ts", "7 days"),
    ("fixture_tp_probe", "ts", "7 days"),
)


def _public_document_tables() -> str:
    """§3.9.3 附件台账。

    建在 ``public`` 而非型号 schema: §3.9 说「PG 为唯一真相源」, 且 spec 的
    DDL 里这两张表无 ``pw_<model_key>.`` 前缀。附件是跨型号的受控产物,
    按 §3.9.4 的写序协议入库。
    """
    return """
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
"""


#: public schema 下由本模块管理的表 (§3.9)。
PUBLIC_TABLES: tuple[str, ...] = ("document_object", "document_page")


def public_ddl() -> list[str]:
    """生成 public schema 的附件表 DDL。幂等 (``IF NOT EXISTS``)。"""
    return [_public_document_tables()]


def table_batches_for_ref(ref: SchemaRef) -> tuple[tuple[str, list[str]], ...]:
    """返回 ``(批次名, 该批 DDL 语句列表)``。

    批间顺序不可换 —— ``alter_fk`` 批依赖前四批已建的表。

    供汇出 时按批分组输出, 使文件
    里的分区顺序与实际执行顺序肉眼可对照 (§18.5 ②「按『L0 共享 → 型号
    schema』顺序输出」)。
    """
    token = ref.as_ident()
    out: list[tuple[str, list[str]]] = []
    for name, builders in _TABLE_BATCHES:
        group: list[str] = []
        for build in builders:
            group.extend(_split_statements(build(token)))
        out.append((name, group))
    out.append(("alter_fk", _split_statements(_alter_fk_ddl(token))))
    return tuple(out)


def table_batches() -> tuple[tuple[str, list[str]], ...]:
    """用占位 schema 名渲染各批 DDL (供只看结构、不关心具体型号的调用方)。"""
    return table_batches_for_ref(schema_ref("pw_placeholder"))


def model_ddl_for_ref(ref: SchemaRef) -> list[str]:
    """生成一个型号 schema 的全部 DDL (不含 RLS, 不含 hypertable)。

    顺序: CREATE SCHEMA -> 五个建表批 -> ALTER 补 FK。
    不含 ``search_path`` 改动 —— 每条语句都带全限定名, 不依赖会话状态。

    CREATE SCHEMA 的分号在此统一补上, 免得下游各自记得 (忘了就是静默并条)。
    """
    token = ref.as_ident()
    stmts: list[str] = [f"CREATE SCHEMA IF NOT EXISTS {token};"]
    for _name, builders in _TABLE_BATCHES:
        for build in builders:
            stmts.extend(_split_statements(build(token)))
    stmts.extend(_split_statements(_alter_fk_ddl(token)))
    return stmts


def model_ddl(model_key: str) -> list[str]:
    """:func:`model_ddl_for_ref` 的型号键版 (走白名单校验)。"""
    return model_ddl_for_ref(schema_ref(model_schema_name(model_key)))


def model_schema_ddl_for_ref(ref: SchemaRef) -> list[str]:
    """型号 schema 的完整 DDL: 表 + RLS + hypertable。

    §18.5 的分区顺序是「0 扩展 / 1 L0 / 2 型号 / 3 hypertable / 4 RLS /
    5 触发器」。本函数把 2、3、4 串起来; 0、1 由 alembic 负责, 5 分散在各表
    定义里 (update_tsv 触发器、fact_as_of 视图)。

    hypertable 在 RLS 之前: TimescaleDB 会把 hypertable 拆成子表, FORCE RLS
    必须作用在父表上才覆盖全部 chunk。
    """
    return (
        model_ddl_for_ref(ref)
        + hypertable_ddl_for_ref(ref)
        + rls_ddl_for_ref(ref)
    )


def model_schema_ddl(model_key: str) -> list[str]:
    """:func:`model_schema_ddl_for_ref` 的型号键版。"""
    return model_schema_ddl_for_ref(schema_ref(model_schema_name(model_key)))


def hypertable_available_sql() -> str:
    """查询 TimescaleDB 是否已装。

    查 ``pg_extension`` (装没装) 而不是 ``pg_available_extensions`` (装得了吗)
    —— 与 :func:`storage.schema.check_extensions` 同一纪律。§5.9 的教训正是
    只看后者会得到「向量库可用」的假结论。
    """
    return f"SELECT 1 FROM pg_extension WHERE extname = '{TIMESCALE_EXTENSION}'"


def hypertable_ddl_for_ref(ref: SchemaRef) -> list[str]:
    """生成 ``create_hypertable`` 语句。§18.5 第 3 分区。

    不检查扩展是否存在 —— 那是调用方的决定 (板卡与容器可用扩展集不同)。
    需要条件执行时用 :func:`hypertable_available_sql` 先查。
    """
    # 末尾分号与 model_ddl_for_ref 的约定一致: 本模块返回的每条语句都必须
    # 是可直接交给 psycopg / 原样写进 psql DDL 的完整语句。
    return [
        f"SELECT create_hypertable({ref.hypertable_target(tbl)}, '{tscol}', "
        f"chunk_time_interval => INTERVAL '{interval}');"
        for tbl, tscol, interval in HYPERTABLES
    ]


def hypertable_ddl(model_key: str) -> list[str]:
    """:func:`hypertable_ddl_for_ref` 的型号键版。"""
    return hypertable_ddl_for_ref(schema_ref(model_schema_name(model_key)))


def _split_statements(block: str) -> list[str]:
    """把一个 DDL 块拆成单条语句。

    按 ``;`` 切分在 PL/pgSQL 上不可靠 (函数体里有分号), 所以改为按
    「顶层 ``;`` 之后紧跟换行且下一行非空白」切, 并跳过
    ``DO $$``/``CREATE FUNCTION`` 这类 dollar-quoted 块的整体。
    """
    stmts: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in block.splitlines():
        stripped = line.strip()
        if "$$" in line:
            # 成对的 $$ (LANGUAGE 前) 开, 单独一行或行尾的 $$ 关。
            if line.count("$$") % 2 == 1:
                in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and stripped.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        raise SchemaError(f"DDL 块末尾有未闭合语句, 疑似缺分号:\n{tail[:200]}")
    return stmts
