"""把 :func:`extract_test_conditions` 的产出**落库**到 ``<schema>.test_requirement``。

## 为什么需要这个模块

抽取管线早已闭合(实测 PA601 产出 95 条条件 / 142 个场景), 但产出物**从不落库** ——
``pw_sr5400.test_requirement`` 与 ``test_case`` 都是 0 行。于是「条件」只活在进程内:
下一次运行重新算一遍, 没人能查询「某条判据的激励条件是什么」, 而门禁/工装软件/
质量闭环都拿不到输入。

## 落什么、不落什么

**落**: 判据(spec)、条件向量(condition_vector)、方法(method)、仪器与工装需求
(instrument_need/fixture_need)、溯源(source_ref)、评估结论(coverage_status)。

**不落 draft 子句进导出**: 业界补齐与注记草稿一律 ``status=draft``, 项目纪律明确
「未人审不得进入导出口」。但它们**仍然落库** —— 落进 ``condition_vector`` 并标
``draft``, 而不是丢弃。理由: 丢弃就等于「没算过」(上一轮踩过这类坑); 落库并标记
才是「算过但没批」。``coverage_status`` 因此取 ``PENDING`` 而不是 ``COVERED``:
41 条条件含未人审提案, 报 COVERED 会让「覆盖率高」这个结论不成立。

## 幂等与可重跑

主键是 ``sr_id``(唯一约束), 用 upsert。重跑同一份抽取结果**不产生重复行**; 判据
变更会覆盖旧值, 而 ``valid_from`` 保留首次入库时间 —— 这样「这条判据是什么时候
开始生效的」可查, 是红线 5(出处必须可查)在时序维度的落点。

## 为什么 ``concept_id`` 允许为空

``test_requirement.concept_id`` 是 NOT NULL, 但抽取产出的 ``TestCondition`` **没有**
concept_id(它只有 req_id/title)。用 ``req_id`` 兜底并在 ``measurand`` 里放标题 ——
概念对齐是另一件事(要过本体层), 在这一层伪造 concept_id 会让「这条判据对应哪个概念」
看起来已对齐, 而实际没有。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aterag.extract.models import (
    CONF_ANNOTATED,
    CONF_PROPOSED,
    SRC_METHOD,
    STATUS_APPROVED,
    TestCondition,
)
from aterag.ingest.entity_extract import variant_suffix_for

#: ``coverage_status`` 取值域(CHECK 约束: PENDING | COVERED | GAP)
COV_PENDING = "PENDING"
COV_COVERED = "COVERED"
COV_GAP = "GAP"

#: 条件子句的可序列化上限。整句文本(备注文本可达数百字)进 JSONB 会让
#: ``condition_vector`` 变成文档副本, 而它要能被**查**(按 kind 检索), 不是被读。
#: 超长文本留在 ``source_ref.notes`` 里, 这里只放结构化值 + 截断的原文片段。
_CLAUSE_TEXT_MAX = 120
_CLAUSE_NOTE_MAX = 200


#: 档位键上限。``(sr_id, variant_key)`` 上有唯一索引, 所以它必须有界 ——
#: variant_key 拼进 notes 原文会让索引项无限长, 而原文已在 ``source_ref`` 里。
_VARIANT_KEY_MAX = 64


@dataclass
class RequirementRow:
    """``test_requirement`` 一行。字段与建表 DDL 一一对应。"""

    sr_id: str
    variant_key: str
    concept_id: str
    measurand: str
    quantity_kind: str
    spec: dict[str, Any]
    condition_vector: dict[str, Any]
    method: dict[str, Any]
    instrument_need: dict[str, Any]
    fixture_need: dict[str, Any]
    source_ref: dict[str, Any]
    coverage_status: str
    signal_type: str = "TEST"
    abs_max: float | None = None
    valid_until: str | None = None


def _clause_payload(clauses: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """条件子句 -> JSONB 可序列化列表; 返回 (子句, 是否含 draft)。

    ``draft`` 必须**带到行里**而不是丢弃: 丢弃等于「没算过」, 而实情是「算过但
    没批」。下游据此决定能不能当产测判据用。
    """
    out: list[dict[str, Any]] = []
    has_draft = False
    for cl in clauses:
        draft = cl.status != STATUS_APPROVED
        has_draft = has_draft or draft
        out.append(
            {
                "kind": cl.kind,
                "text": (cl.text or "")[:_CLAUSE_TEXT_MAX],
                "value": cl.value,
                "source": cl.source,
                "confidence": cl.confidence,
                "status": cl.status,
                "method_ref": cl.method_ref or None,
                # 工艺要求 id: 落库后只凭这一行就能答出「这条判据依赖哪些业界
                # 工艺知识」, 不必为了查它去 JOIN test_methods.yaml。
                "knowledge_ref": list(getattr(cl, "knowledge_ref", ()) or ()) or None,
                # 业界补齐的子句带一条**完整原文**, 只截断会丢掉「为什么这么做」
                # 的后半段 —— 而那恰恰是补齐提案要人审的内容。
                "proposal_note": (str(cl.value.get("note") or "")[:_CLAUSE_NOTE_MAX]
                                   if cl.source == SRC_METHOD else None),
            }
        )
    return out, has_draft


def _condition_vector(cond: TestCondition) -> dict[str, Any]:
    """条件向量: 供检索与工装软件消费的结构化工况。

    保留 ``approved``/``draft`` 两个子列表而不是混在一起 —— 工装软件只应按
    approved 子句配激励点, 而分析工具要能同时看到「已批的」与「待审的」。
    """
    # 第二个返回值是 has_draft 布尔, 列表本身按 status 过滤得到 —— 不重复实现过滤,
    # 免得两处判断不一致(那会让 draft 子句既不在 approved 也不在 draft 里)。
    in_all, _ = _clause_payload(cond.input_conditions)
    out_all, _ = _clause_payload(cond.output_conditions)
    all_clauses = in_all + out_all
    approved = [c for c in all_clauses if c["status"] == STATUS_APPROVED]
    draft = [c for c in all_clauses if c["status"] != STATUS_APPROVED]
    vec: dict[str, Any] = {
        "role": cond.role,
        "rail": cond.rail or None,
        "approved": approved,
        "draft": draft,
        "description": cond.description or None,
    }
    if cond.flags:
        vec["flags"] = list(cond.flags)
    return vec


def _method(cond: TestCondition) -> dict[str, Any]:
    """方法: 补齐用过的业界方法 id + 条件子句引用的方法。

    ``method_ref`` 让「这条常识前提出自哪条标准」可回溯 —— 评审补齐提案时要核对
    依据, 没有它就只能重读备注原文猜。
    """
    refs = sorted(
        {
            cl.method_ref
            for cl in cond.input_conditions + cond.output_conditions
            if cl.method_ref
        }
    )
    sources = sorted(
        {cl.source for cl in cond.input_conditions + cond.output_conditions}
    )
    return {"method_refs": refs, "clause_sources": sources}


def _instrument_need(cond: TestCondition) -> dict[str, Any]:
    """仪器需求: 从条件子句**推**出来的最小集, 明确标注是推导而非选型结果。

    不做选型(CSP 选型是另一件事, 且依赖实际在册仪器)。这里只回答「这条判据
    需要**能做什么**的仪器」, 让工装软件能据此提出需求, 而不是在知识层假装
    已经有了设备型号。
    """
    need: set[str] = set()
    for cl in cond.input_conditions + cond.output_conditions:
        k = cl.kind
        if k in ("input_voltage", "input_frequency"):
            need.add("programmable_ac_source")
        if k == "load":
            need.add("programmable_dc_load")
        if k == "test_mode":
            need.add("programmable_dc_load")  # CR/CP 模式只有电子负载有
        if k in ("output_voltage", "output_current", "output_power", "ripple", "timing"):
            need.add("dmm")
        if k in ("ripple", "timing"):
            need.add("oscilloscope")
        if k == "efficiency":
            need.add("power_analyzer")
        if k == "power_factor":
            need.add("power_analyzer")
        if k == "signal_state":
            need.add("protocol_analyzer")
        if k == "protection_action":
            need.add("protection_tester")
        if k == "thermal":
            need.add("thermal_chamber")
    return {
        "required": sorted(need),
        "derived": True,
        "note": "由条件子句 kind 推导的能力需求, 非型号选型结果",
    }


def _fixture_need(cond: TestCondition) -> dict[str, Any]:
    """工装需求: 只记条件直接蕴含的工装要求, 不猜结构。"""
    need: set[str] = set()
    if cond.rail:
        need.add("per_rail_fixture")
    for cl in cond.input_conditions + cond.output_conditions:
        if cl.kind == "measurement_setup":
            note = str((cl.value or {}).get("note") or "")
            if "uF" in note or "电容" in note:
                need.add("decoupling_cap_bank")
            if "MHz" in note or "带宽" in note:
                need.add("bandwidth_limited_probe")
        if cl.kind == "protection_action":
            need.add("fault_injection_path")
    if cond.role in ("output_spec", "protection_response"):
        need.add("load_switching")
    return {"required": sorted(need), "derived": True}


def _source_ref(cond: TestCondition) -> dict[str, Any]:
    """溯源: 让「这条判据出自哪一节哪一行」可回查(红线 5)。"""
    return {
        "section_path": cond.section_path,
        "heading": cond.heading or None,
        "priority": cond.priority or None,
        "notes": (cond.notes or "")[:400] or None,
        "unit": cond.unit or None,
        "rail": cond.rail or None,
    }


def _signal_type(cond: TestCondition) -> str:
    """按章节/条件形状推信号类型, 对齐 CHECK 约束的七种取值。

    判据: 告警/遥信(有 ``signal_state`` 且无电气限值) -> YX; 有遥测量化限值 ->
    YC; 保护 -> PROT; 遥控命令 -> YK; 其余 TEST。**宁可归 TEST** ——
    归错类型会让信号类查询漏掉这条, 归宽只是多返回。
    """
    lim = cond.limits or {}
    has_limit = any(lim.get(k) is not None for k in ("min", "typ", "max"))
    kinds = {cl.kind for cl in cond.input_conditions + cond.output_conditions}
    if cond.role == "protection_response":
        return "PROT"
    if has_limit and "signal_state" in kinds and "output_voltage" not in kinds:
        return "YC"
    if "signal_state" in kinds and not has_limit:
        return "YX"
    return "TEST"


def variant_key(cond: TestCondition, model_id: str = "", ordinal: int = 0) -> str:
    """条件 -> 档位键。口径由 :func:`entity_extract.variant_suffix_for` 独占。

    这里只做**形状转换**: 把 :class:`TestCondition` 摊成 ``map_row`` 同形的 row。
    档位怎么拼(电压轨 + 工况标签 + 标准 + 单位, 不含判据数值)全在实体层决定 ——
    ``variant_key`` 上有唯一索引, 两层各拼一次就会「实体层认为两个档位、判据层
    合成一个」, upsert 重新变成静默覆盖。

    文本输入取 ``notes`` 而非条件子句: ``_semantic_tags`` 的规则是针对规格书备注文本
    写的(如「25%~50% 负载变化」), 子句的 ``text`` 是渲染后的溯源片段, 规则未必命中。

    ``ordinal`` 是同一 ``req_id`` 内 0 起的出现序号, 与实体层传 ``ctx.seq`` 一致。
    """
    row = {
        "rail": cond.rail or "",
        "unit": cond.unit or "",
        "notes": cond.notes or "",
        "requirement_text": "",
    }
    return variant_suffix_for(row, model_id, ordinal)[:_VARIANT_KEY_MAX]


def requirement_row(
    cond: TestCondition,
    *,
    coverage_status: str = COV_PENDING,
    model_id: str = "",
    ordinal: int = 0,
) -> RequirementRow:
    """一条 :class:`TestCondition` -> 一行 ``test_requirement``。

    ``coverage_status`` **默认 PENDING**: 含未人审补齐提案的条件不能报 COVERED
    (实测 95 条里 41 条含 draft 子句), 报 COVERED 会让「覆盖率高」这个结论
    失去意义 —— 它正是本项目反复出现的那类失败: 数字好看而结论不成立。
    """
    vec = _condition_vector(cond)
    draft_count = len(vec["draft"])
    # approved 数只算**可信的** approved: LLM 提案若被误标 approved, 不能计入。
    approved_count = sum(
        1
        for cl in cond.input_conditions + cond.output_conditions
        if cl.status == STATUS_APPROVED and approved_clause_is_sound(cl)
    )
    if coverage_status == COV_GAP:
        # 显式 GAP 最高优先: assessment 判定 insufficient 说明「有条件不等于条件够」。
        # 放在后面判断会让它被 draft/approved 的推导结果覆盖掉(第一版就错在这),
        # 于是评估结论被静默丢弃 —— 落库后看着 COVERED, 实际判定过覆盖不到。
        status = COV_GAP
    elif not approved_count:
        status = COV_GAP
    elif draft_count:
        status = COV_PENDING
    else:
        status = COV_COVERED
    lim = cond.limits or {}
    abs_max = lim.get("abs_max")
    return RequirementRow(
        sr_id=cond.req_id,
        variant_key=variant_key(cond, model_id, ordinal),
        concept_id=cond.req_id,
        measurand=cond.title,
        quantity_kind=(lim.get("unit") or cond.unit or "") or None,
        spec={k: v for k, v in lim.items() if k in ("min", "typ", "max", "unit", "rail")},
        condition_vector=vec,
        method=_method(cond),
        instrument_need=_instrument_need(cond),
        fixture_need=_fixture_need(cond),
        source_ref=_source_ref(cond),
        coverage_status=status,
        signal_type=_signal_type(cond),
        abs_max=abs_max if isinstance(abs_max, (int, float)) else None,
    )


#: 列顺序必须与 INSERT 的 VALUES 一致 —— 用命名参数更安全, 但 DDL 有 23 列,
#: 显式列表让「漏列」在 code review 里可见。
_COLUMNS = (
    "sr_id", "variant_key", "concept_id", "measurand", "quantity_kind", "spec",
    "abs_max", "condition_vector", "method", "instrument_need", "fixture_need",
    "source_ref", "signal_type", "coverage_status",
)


def upsert_sql() -> str:
    """幂等写入的 SQL。

    冲突目标是 **(sr_id, variant_key)** 而非 ``sr_id``: 一个编号常有多档(实测
    PA601 95 个条件只对应 74 个编号), 按 sr_id 冲突会把同编号的 N 档互相覆盖。

    ``valid_from`` **不更新**: 首次入库时间是判据版本的锚点, 覆盖它就丢掉了
    「这条判据从什么时候开始这样判」。
    """
    cols = ", ".join(_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _COLUMNS)
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in _COLUMNS
        if c not in ("sr_id", "variant_key", "valid_from")
    )
    return (
        f"INSERT INTO {{schema}}.test_requirement ({cols}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (sr_id, variant_key) DO UPDATE SET {updates}"
    )


def approved_clause_is_sound(clause: Any) -> bool:
    """一个 ``approved`` 子句必须是规则命中或人审注记, 不能是 LLM 提案。

    这条不变式是 :func:`requirement_row` 里 ``coverage_status`` 推导的前提:
    ``approved`` 数 >0 才可能报 ``COVERED``。若 LLM 提案能被标成 approved, 覆盖率
    会在**没有任何人审过**的情况下变成「已覆盖」—— 那正是本项目反复出现的那类
    失败: 数字好看而结论不成立。

    单独抽成函数是为了让测试能直接断言它, 而不是只能通过 ``coverage_status``
    间接观察(间接观察分不清「规则命中」与「提案被误标」)。
    """
    if clause.status != STATUS_APPROVED:
        return True
    return clause.confidence in _APPROVED_CANNOT_BE_PROPOSED


def rows_from_result(result: Any) -> list[RequirementRow]:
    """抽取结果 -> 可落库的行。

    ``coverage_status`` 先按条件自身的 draft/approved 形状算; 再用
    ``assessments`` 的 ``insufficient`` 判定把确实覆盖不到的降为 ``GAP`` ——
    「有条件」不等于「条件够」, 前者是抽取成功, 后者是产测可行。
    """
    insufficient = {
        getattr(a, "req_id", "")
        for a in (result.assessments or [])
        if getattr(a, "verdict", "") == "insufficient"
    }
    model_id = str(getattr(result, "model_id", "") or "")
    out: list[RequirementRow] = []
    # 同一 req_id 内的出现序号: AC 110V/AC 220V 这类行轨/工况/单位全同, 判据数值
    # 就是它唯一的身份。序号按抽取结果里的原始顺序, 与实体层的 ctx.seq 同源。
    seen: dict[str, int] = {}
    for cond in result.conditions or []:
        ordinal = seen.get(cond.req_id, 0)
        seen[cond.req_id] = ordinal + 1
        row = requirement_row(
            cond,
            coverage_status=COV_GAP if cond.req_id in insufficient else COV_PENDING,
            model_id=model_id,
            ordinal=ordinal,
        )
        out.append(row)
    assert_unique_variants(out)
    return out


class VariantKeyCollision(ValueError):
    """同一 ``(sr_id, variant_key)`` 出现两行 —— 落库会静默覆盖, 必须报错。"""


def assert_unique_variants(rows: Sequence[RequirementRow]) -> None:
    """落库前的**硬检查**: ``(sr_id, variant_key)`` 必须两两不同。

    没有这条检查时, 落库会成功、95 行变 74 行、**不报任何错** —— 那正是本项目
    反复出现的那类失败(数字好看而结论不成立)。有了它, 冲突在写库之前就炸出来。

    冲突时把两行的可分辨信息(标题/判据)一并抛出: 单看 ``variant_key`` 无法判断
    是不是**真的**同一条判据被算了两次, 还是要合并(比如同一轨的两行备注写法不同
    而标签没抽出来)。前者是 bug, 后者需要加规则。
    """
    seen: dict[tuple[str, str], RequirementRow] = {}
    for row in rows:
        key = (row.sr_id, row.variant_key)
        prev = seen.get(key)
        if prev is not None:
            raise VariantKeyCollision(
                f"档位键冲突: {row.sr_id} / {row.variant_key!r}\n"
                f"  前一行: {prev.measurand} spec={prev.spec}\n"
                f"  后一行: {row.measurand} spec={row.spec}\n"
                f"  判据 {prev.spec} 与 {row.spec} 相同 => 同一档位被抽了两次(抽取 bug); "
                f"不同 => 档位标签没区分开(工况规则缺 variant 标记或备注文本未命中)。\n"
                f"  修法: 不要靠加大 variant_key 长度糊过去。"
            )
        seen[key] = row


#: 一个 ``approved`` 子句允许的置信度: 规则命中 / 人工注记。
#:
#: **不含** ``proposed``(LLM 提案)—— 未人审的提案不得进入导出口(项目纪律), 也不得
#: 让 ``coverage_status`` 报出 ``COVERED``(见 :func:`approved_clause_is_sound`)。
#: 放在函数之后是因为 :func:`approved_clause_is_sound` 在定义处就引用它 ——
#: Python 在调用时才解析, 所以顺序无关, 但把定义放在使用点附近更易读。
_APPROVED_CANNOT_BE_PROPOSED = frozenset({CONF_ANNOTATED, "rule"})

__all__ = [
    "COV_COVERED",
    "COV_GAP",
    "COV_PENDING",
    "CONF_PROPOSED",
    "RequirementRow",
    "VariantKeyCollision",
    "assert_unique_variants",
    "rows_from_result",
    "requirement_row",
    "upsert_sql",
    "variant_key",
]
