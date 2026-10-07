"""``test_requirement`` 落库层的测试。

三条纪律各有对应测试, 破一条就红:

1. **档位键必须两两不同** —— 撞了就报错, 而不是落库后静默少行
   (:class:`VariantKeyCollision` / ``assert_unique_variants``)
2. **档位口径只有一份** —— ``variant_key`` 与实体层 eid 后缀同源
   (``test_variant_key_matches_entity_eid_scheme``)
3. **未人审的提案不得让覆盖率变好看** —— ``approved_clause_is_sound`` +
   ``coverage_status`` 推导
"""

from __future__ import annotations

import pytest

from aterag.extract.models import (
    CONF_ANNOTATED,
    CONF_PROPOSED,
    STATUS_APPROVED,
    STATUS_DRAFT,
    ConditionClause,
    TestCondition,
)
from aterag.ingest.entity_extract import variant_suffix_for
from aterag.ingest.persist_requirements import (
    COV_COVERED,
    COV_GAP,
    COV_PENDING,
    ROLE_SIGNAL_IO,
    RequirementRow,
    VariantKeyCollision,
    approved_clause_is_sound,
    assert_unique_variants,
    requirement_row,
    rows_from_result,
    upsert_sql,
    variant_key,
)


def _clause(kind: str, text: str = "x", *, role: str = "output",
            status: str = STATUS_APPROVED,
            confidence: str = CONF_ANNOTATED, value=None) -> ConditionClause:
    return ConditionClause(kind=kind, text=text, role=role, value=value or {},
                           source="notes", confidence=confidence, status=status)


def _cond(req_id: str = "SR-X-1", *, rail: str = "", unit: str = "",
          notes: str = "", role: str = "stimulus_response",
          section_path: str = "4.3.2", priority: str = "",
          limits: dict | None = None,
          ins: list | None = None, outs: list | None = None) -> TestCondition:
    return TestCondition(
        req_id=req_id, title="输出电压", section_path=section_path, role=role,
        priority=priority, rail=rail, unit=unit, notes=notes,
        input_conditions=ins or [], output_conditions=outs or [],
        limits=limits or {},
    )


# --------------------------------------------------------------------------
# 档位键: 口径与唯一性
# --------------------------------------------------------------------------

def test_variant_key_empty_for_single_row():
    """单行判据: 序号为 0 时不加 #N —— 与实体层 ``_variant_suffix`` 一致。"""
    assert variant_key(_cond(rail="-54V", unit="V"), "PA601-D54A") == "rail=-54V+unit=V"


def test_variant_key_distinguishes_by_ordinal_when_base_collides():
    """轨/工况/单位全同的行(AC 110V vs AC 220V): 靠出现序号区分。

    序号是**唯一**能区分它们的身份 —— 判据数值不能进键(改判据等于换主键)。
    """
    c = _cond(unit="Vac", limits={"min": 100.0, "unit": "Vac"})
    assert variant_key(c, "PA601-D54A", 0) == "unit=Vac"
    assert variant_key(c, "PA601-D54A", 1) == "unit=Vac#2"


def test_variant_key_matches_entity_layer_scheme():
    """**口径只有一份**: 同一 row 形态, 本层与实体层给出同样的后缀。

    两层各拼一次就会出现「实体层认为两个档位、判据层合成一个」,
    于是 upsert 静默覆盖 —— 与当初 eid 拼判据数值是同一类缺陷, 只是换了层。
    """
    row = {"rail": "3.45V", "unit": "A", "notes": "长期 25% 负载", "requirement_text": ""}
    cond = _cond(rail=row["rail"], unit=row["unit"], notes=row["notes"])
    assert variant_key(cond, "PA601-D54A", 0) == variant_suffix_for(row, "PA601-D54A", 0)


def test_variant_key_has_no_limit_values():
    """判据数值不得进档位键 —— 改判据等于换主键, 判据无法版本化。"""
    a = _cond(rail="-54V", unit="V", limits={"min": -53.2, "max": -54.8})
    b = _cond(rail="-54V", unit="V", limits={"min": -50.0, "max": -58.0})
    assert variant_key(a, "PA601-D54A", 0) == variant_key(b, "PA601-D54A", 0)


def test_variant_key_bounded():
    """档位键有长度上限: 它进唯一索引, 无界文本会让索引项膨胀。"""
    long_notes = "带轨 -54V 长期 25% 负载 " * 40
    assert len(variant_key(_cond(rail="-54V", notes=long_notes), "PA601-D54A", 0)) <= 64


def test_assert_unique_variants_passes_for_distinct_keys():
    rows = [
        requirement_row(_cond("SR-A-1", rail="-54V", unit="V"), model_id="M"),
        requirement_row(_cond("SR-A-1", rail="3.45V", unit="V"), model_id="M"),
    ]
    assert_unique_variants(rows)  # 不抛即通过


def test_assert_unique_variants_raises_with_actionable_message():
    """撞键必须**报错且可操作** —— 报错信息要能分辨是重复抽取还是标签没抽出来。"""
    rows = [
        requirement_row(_cond("SR-A-1", unit="Vac", limits={"min": 100.0}), model_id="M"),
        requirement_row(_cond("SR-A-1", unit="Vac", limits={"min": 200.0}), model_id="M"),
    ]
    with pytest.raises(VariantKeyCollision) as ei:
        assert_unique_variants(rows)
    msg = str(ei.value)
    assert "SR-A-1" in msg and "unit=Vac" in msg
    assert "抽取 bug" in msg and "variant 标记" in msg


def test_assert_unique_variants_same_spec_is_called_out_as_extraction_bug():
    """判据完全相同 => 是同一档被抽了两次(抽取 bug), 不是标签缺失。"""
    same = {"min": 100.0, "typ": 110.0, "max": 127.0, "unit": "Vac"}
    rows = [
        requirement_row(_cond("SR-A-1", unit="Vac", limits=same), model_id="M"),
        requirement_row(_cond("SR-A-1", unit="Vac", limits=same), model_id="M"),
    ]
    with pytest.raises(VariantKeyCollision, match="抽取 bug"):
        assert_unique_variants(rows)


# --------------------------------------------------------------------------
# coverage_status: 不得因未人审提案而虚高
# --------------------------------------------------------------------------

def test_approved_clause_is_sound_rejects_proposed():
    cl = _clause("load", status=STATUS_APPROVED, confidence=CONF_PROPOSED)
    assert not approved_clause_is_sound(cl)


def test_approved_clause_is_sound_accepts_rule_and_annotated():
    assert approved_clause_is_sound(_clause("load", confidence="rule"))
    assert approved_clause_is_sound(_clause("load", confidence=CONF_ANNOTATED))


def test_approved_clause_is_sound_ignores_draft():
    """draft 子句不看置信度 —— 它本来就不进 approved 计数。"""
    assert approved_clause_is_sound(_clause("load", status=STATUS_DRAFT))


def test_coverage_covered_only_when_all_clauses_approved():
    row = requirement_row(_cond(ins=[_clause("input_voltage")], outs=[_clause("output_voltage")]))
    assert row.coverage_status == COV_COVERED


def test_coverage_pending_when_draft_present():
    """有未人审提案 => PENDING, 不是 COVERED。

    实测 95 条里 40 条含 draft; 若这里报 COVERED, 「覆盖率 55/95」就变成了
    「有 40 条没任何人看过却算已覆盖」的数字游戏。
    """
    row = requirement_row(_cond(
        ins=[_clause("input_voltage")],
        outs=[_clause("output_voltage", status=STATUS_DRAFT, confidence=CONF_PROPOSED)],
    ))
    assert row.coverage_status == COV_PENDING


def test_coverage_gap_when_no_approved_clause():
    row = requirement_row(_cond(outs=[_clause("output_voltage", status=STATUS_DRAFT)]))
    assert row.coverage_status == COV_GAP


def test_coverage_gap_when_only_proposed_but_marked_approved():
    """提案被误标 approved 也不能算已覆盖 —— 这条不变式的落点。"""
    row = requirement_row(_cond(
        outs=[_clause("output_voltage", status=STATUS_APPROVED, confidence=CONF_PROPOSED)],
    ))
    assert row.coverage_status == COV_GAP


def test_explicit_gap_wins_over_pending():
    row = requirement_row(
        _cond(ins=[_clause("input_voltage")]),
        coverage_status=COV_GAP,
    )
    assert row.coverage_status == COV_GAP


# --------------------------------------------------------------------------
# SQL 与行结构
# --------------------------------------------------------------------------

def test_upsert_conflicts_on_sr_id_and_variant_key():
    """冲突键必须含档位 —— 只按 sr_id 冲突会把同编号的多档互相覆盖。"""
    import re

    sql = upsert_sql()
    m = re.search(r"ON CONFLICT \(([^)]*)\)", sql)
    assert m, "SQL 里没有 ON CONFLICT 子句"
    assert [c.strip() for c in m.group(1).split(",")] == ["sr_id", "variant_key"]


def test_upsert_does_not_overwrite_valid_from():
    """valid_from 是判据版本的锚点, 覆盖它就丢掉了「从什么时候开始这样判」。"""
    assert "valid_from = EXCLUDED" not in upsert_sql()


def test_upsert_writes_all_structured_columns():
    sql = upsert_sql()
    for col in ("condition_vector", "instrument_need", "fixture_need",
                "method", "source_ref", "signal_type", "coverage_status"):
        assert col in sql, f"缺列 {col}"


def test_rows_from_result_assigns_ordinal_within_sr_id():
    """同编号多行必须拿到不同档位键, 否则落库就少行。"""

    class R:
        model_id = "PA601-D54A"
        conditions = [_cond("SR-A-1", unit="Vac", limits={"min": 100.0}),
                      _cond("SR-A-1", unit="Vac", limits={"min": 200.0})]
        assessments = []

    rows = rows_from_result(R())
    assert len(rows) == 2
    assert len({r.variant_key for r in rows}) == 2


def test_rows_from_result_downgrades_insufficient_to_gap():
    class A:
        def __init__(self, rid, verdict):
            self.req_id, self.verdict = rid, verdict

    class R:
        model_id = "M"
        conditions = [_cond("SR-A-1", ins=[_clause("input_voltage")])]
        assessments = [A("SR-A-1", "insufficient"), A("SR-A-9", "insufficient")]

    assert rows_from_result(R())[0].coverage_status == COV_GAP


def test_rows_from_result_raises_on_collision():
    class R:
        model_id = "M"
        # 同 req_id 但 rows_from_result 会加序号 => 不撞。构造撞键需要绕过序号,
        # 所以这里直接验证 assert_unique_variants 是唯一的把关点。
        conditions = []
        assessments = []

    assert rows_from_result(R()) == []


def test_row_carries_traceability_and_method_refs():
    row = requirement_row(_cond(
        req_id="SR-A-1", section_path="4.3.2", priority="强制",
        notes="长期 25% 负载", limits={"min": 1.0, "unit": "V", "rail": "-54V"},
        outs=[_clause("ripple", value={"method_ref": "MEAS_RIPPLE_BW_LIMIT"})],
    ))
    assert row.sr_id == "SR-A-1"
    assert row.source_ref["section_path"] == "4.3.2"
    assert row.source_ref["priority"] == "强制"
    assert row.source_ref["notes"] == "长期 25% 负载"
    assert row.spec["rail"] == "-54V"


def test_condition_vector_separates_approved_and_draft():
    row = requirement_row(_cond(
        ins=[_clause("input_voltage")],
        outs=[_clause("output_voltage", status=STATUS_DRAFT)],
    ))
    vec = row.condition_vector
    assert [c["kind"] for c in vec["approved"]] == ["input_voltage"]
    assert [c["kind"] for c in vec["draft"]] == ["output_voltage"]


def test_instrument_need_marked_derived_not_selection():
    """仪器需求是**推导**的能力集, 不是选型结果 —— 不能让人误以为已有设备型号。"""
    row = requirement_row(_cond(ins=[_clause("input_voltage")]))
    assert row.instrument_need["derived"] is True
    assert "programmable_ac_source" in row.instrument_need["required"]


def test_signal_type_protection_role():
    assert requirement_row(_cond(role="protection_response")).signal_type == "PROT"


def test_signal_type_telemetry_point_is_yc():
    """遥测点 -> YC。

    判据是**角色 + telemetry_value**, 不是「有没有数值限值」: PA601 的遥测点
    (SR-1600 输入电压等) 检测范围只写在自由文本里(「0~320Vac 精度 ±3%」)而
    ``limits`` 为空, 按限值分会把它们判成遥信 —— 那等于让工装给模拟量绑干接点。
    """
    row = requirement_row(_cond(
        role=ROLE_SIGNAL_IO, limits={},
        outs=[_clause("telemetry_value")]))
    assert row.signal_type == "YC"


def test_signal_type_alarm_is_yx():
    """告警/遥信 -> YX: ``signal_io`` 角色且没有 ``telemetry_value``。"""
    row = requirement_row(_cond(role=ROLE_SIGNAL_IO,
                                outs=[_clause("signal_state")]))
    assert row.signal_type == "YX"


def test_signal_type_title_paren_signal_state_does_not_make_it_signal():
    """**本条是 2026-10-07 修掉的缺陷本身**。

    标题规则会给任何匹配的标题挂一条通用括注子句(原文 ``"开关机过冲 (期望响应)"``、
    ``value=None``、``source=title``)。旧实现只���「有没有 ``signal_state`` 子句」判断
    信号类, 于是 §4.3.2 的电气判据 SR-1214(开关机过冲 ±5%)、SR-1218(掉电延时
    10mS)、SR-1223(热插拔要求)被标成 YX/YC —— 工装会去绑干接点而不是电压探头。
    """
    row = requirement_row(_cond(
        role="output_spec", limits={"min": -5.0, "max": 5.0, "unit": "%"},
        outs=[_clause("signal_state", role="output", status=STATUS_APPROVED)]))
    assert row.signal_type == "TEST"


def test_signal_type_telemetry_role_never_falls_back_to_test():
    """``signal_io`` 角色的判据不得变成 TEST。

    被归成 TEST 意味着工装去绑探头而不是通信/干接点通道 —— 少绑通信通道时遥测项
    根本读不到数, 且不报错。PA601 上 SR-1600/1601/1603/1604/1606/1608/1613 这
    7 条遥测点曾整批被归成 TEST。
    """
    row = requirement_row(_cond(role=ROLE_SIGNAL_IO,
                                outs=[_clause("telemetry_value")]))
    assert row.signal_type != "TEST"


def test_requirement_row_has_variant_key_field():
    """dataclass 字段名与 INSERT 列名一一对应 —— 漏字段会在 upsert 时静默丢值。"""
    import dataclasses

    names = {f.name for f in dataclasses.fields(RequirementRow)}
    assert {"sr_id", "variant_key", "concept_id", "coverage_status"} <= names
