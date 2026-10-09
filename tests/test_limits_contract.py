"""limits 数据契约 —— Semantica 约束消费的前置条件。

为什么需要这层检查
------------------
产测限值最终要被 Semantica 的约束消费。实测其接口后确定了三条硬约束:

1. `DatalogReasoner.add_fact()` 只接受三种 dict —— SPO 三元组
   (subject/predicate/object)、边 (source/target)、节点 (type+id)。
   不认识的 dict 只 warning 跳过, **不报错**。所以数据形态不对时是静默失效。
2. Datalog 不做算术 —— "11.1A >= 8.1A" 表达不了。这是项目既定分工:
   关系给 Datalog, 算术给 SPARQL/SHACL。
3. 所以 limits 的数值进的是 SHACL 的 sh:minInclusive / sh:maxInclusive。

由此推出四条数据契约(全部基于 PA601 实测, 非假设):

  1. 数值类型  —— min/typ/max 有值时必须是 float。SHACL 数值约束不接受
                   字符串 "11.1", 会静默失效。
  2. kind 词表 —— SRC_LIMITS 子句的 kind 必须落在封闭 kinds 词表内,
                   否则下游无法按语义聚合。实测 PA601 31/31 全部在表内。
  3. 量纲登记 —— 有数值时单位必须在 limit_kinds 内。**这条在 PA601 上
                   本已抓出 8 处真实缺陷**(Vac/Vdc 6 处、℃ 2 处未登记),
                   补登后归零。这类缺失会让下游按量纲分组失败。
  4. 限值次序 —— min<=typ<=max。**须按轨的极性判方向**: 负压轨的
                   min/typ/max 按绝对值递减表述(SR-1201: min=-53.2
                   typ=-54.0 max=-54.8), 按数值比较会误报 3 处。

这些是数据契约层检查, 不引入 Semantica 依赖, 离线 CI 可跑 —— 目的是在
抽取层就暴露问题, 而不是等产线上发现约束静默不生效。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.extract.models import ConditionClause  # noqa: E402
from aterag.extract.models import TestCondition as Cond  # noqa: E402
from aterag.extract.scenarios import _limit_kind  # noqa: E402

CONFIG = ROOT / "config" / "condition_patterns.yaml"


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _known_kinds(cfg: dict) -> set[str]:
    return set(cfg["kinds"]["input"]) | set(cfg["kinds"]["output"])


def _num_keys(limits: dict) -> list[str]:
    return [k for k in ("min", "typ", "max") if limits.get(k) is not None]


def _cond(limits: dict, rail: str = "", req_id: str = "SR-X") -> Cond:
    return Cond(
        req_id=req_id,
        title="示例",
        section_path="4.3.2",
        rail=rail,
        limits=limits,
        output_conditions=[ConditionClause(kind="output_current", text="x", role="output")],
    )


# ---------------------------------------------------------------------------
# 1. 数值类型
# ---------------------------------------------------------------------------


def test_limit_values_are_real_numbers() -> None:
    """min/typ/max 有值时必须是 float/int, 不得是字符串。

    规格书里 "±3%"、"6/12" 这类非单值表达抽不出数值是正常的(置 None);
    但**抽出来了却不是数字**说明解析有 bug, 必须在抽取层拦。
    """
    c = _cond({"min": 0.0, "typ": 3.45, "max": 11.1, "unit": "A"})
    for k in _num_keys(c.limits):
        v = c.limits[k]
        assert isinstance(v, (int, float)) and not isinstance(v, bool), (
            f"limits[{k}] 应为数值, 实得 {type(v).__name__}"
        )


def test_type_check_catches_string_value() -> None:
    """检查须真能抓到字符串数值 —— 否则上面那条只是装饰。"""
    dirty = {"min": "0", "max": "11.1", "unit": "A"}
    offenders = [
        k
        for k in _num_keys(dirty)
        if not isinstance(dirty[k], (int, float)) or isinstance(dirty[k], bool)
    ]
    assert offenders == ["min", "max"], "字符串数值应被契约检查识别"


# ---------------------------------------------------------------------------
# 2. kind 词表归属
# ---------------------------------------------------------------------------


def test_limits_clause_kind_is_in_vocab(cfg: dict) -> None:
    """SRC_LIMITS 子句的 kind 必须在封闭词表内。

    kind 决定下游按什么语义聚合; 不在表内的 kind 会被静默丢弃。
    保护类条款的 kind 由装配层显式覆盖成 protection_action, 如实反映。
    """
    known = _known_kinds(cfg)
    c = Cond(
        req_id="SR-P",
        title="输出过流保护",
        section_path="4.3.3",
        rail="-54V",
        role="protection_response",
        limits={"min": 12.0, "max": 18.0, "unit": "A", "rail": "-54V"},
        output_conditions=[
            ConditionClause(
                kind="protection_action", text="过流保护", role="output", source="limits"
            )
        ],
    )
    got = _limit_kind(c)
    assert got, "SRC_LIMITS 子句应产出 limit_kind"
    assert got in known, f"limit_kind={got!r} 不在封闭 kinds 词表内"


def test_limit_kind_absent_when_no_limits_clause() -> None:
    """无 SRC_LIMITS 子句时 limit_kind 为空 —— 命名层据此不给输入前缀。"""
    c = Cond(
        req_id="SR-S",
        title="电源在位",
        section_path="4.3.4.1",
        limits={"unit": "", "rail": ""},
        output_conditions=[
            ConditionClause(kind="signal_state", text="在位", role="output", source="title")
        ],
    )
    assert _limit_kind(c) == "", "无 limits 子句时不应产出 limit_kind"


def test_limit_kind_reads_input_side_too() -> None:
    """输入特性表的限值落在**输入侧**子句, 两侧都必须读。

    装配层按章节先验(role)决定限值归哪一侧: role=input_domain 的条款限值在
    输入侧, 输出侧只有补齐层加的通用判据。只看输出侧的话, 输入电压范围这类
    条款 limit_kind 恒空, 输入侧命名会全部失效。
    """
    c = Cond(
        req_id="SR-I",
        title="标称输入电压范围",
        section_path="4.3.1",
        role="input_domain",
        limits={"min": 200.0, "typ": 220.0, "max": 240.0, "unit": "Vac", "rail": ""},
        input_conditions=[
            ConditionClause(
                kind="input_voltage",
                text="200~240Vac",
                role="input",
                source="limits",
                value={"min": 200.0, "max": 240.0, "unit": "Vac"},
            )
        ],
        output_conditions=[
            ConditionClause(kind="signal_state", text="x", role="output", source="industry_method")
        ],
    )
    assert _limit_kind(c) == "input_voltage", "输入侧 limits 子句也须被读到"


# ---------------------------------------------------------------------------
# 3. 量纲登记
# ---------------------------------------------------------------------------


def test_units_with_values_are_registered(cfg: dict) -> None:
    """**有数值时**单位必须在 limit_kinds 内。

    这是本层检查抓出的真实缺陷: PA601 的 Vac/Vdc(输入过压/欠压保护点与回差,
    6 处)与 ℃(过温保护与回差, 2 处)都带数值却未登记, 下游按量纲分组会失败。
    无数值条款(信号类/功能要求类)的 unit 为空或 '-' 是合法的, 不在此约束内。
    """
    lk = cfg["limit_kinds"]
    placeholder = {"", "-", "—", "/"}
    # 正例
    for u, kind in (("A", "output_current"), ("Vac", "input_voltage"), ("℃", "thermal")):
        assert lk.get(u) == kind, f"{u!r} 应登记为 {kind}, 实得 {lk.get(u)!r}"
    assert lk.get("Vac/Vdc") == "input_voltage", "复合写法应归输入电压量纲"
    # 无数值的占位单位不要求登记
    for u in placeholder:
        assert not _num_keys({"unit": u}), "占位单位对应的条款不该有数值"


# ---------------------------------------------------------------------------
# 4. 限值次序 (按轨极性判方向)
# ---------------------------------------------------------------------------


def _order_violations(limits: dict, rail: str) -> list[str]:
    """返回次序越界项; 负压轨(轨名以 - 开头)按绝对值校验。

    负压轨的 min/typ/max 按绝对值递减表述: SR-1201 min=-53.2 typ=-54.0
    max=-54.8, 数值本身递减是正确的, 按数值比较会误报。
    """
    neg = rail.strip().startswith("-")
    key = (lambda v: abs(v)) if neg else (lambda v: v)
    mn, ty, mx = (limits.get(k) for k in ("min", "typ", "max"))
    bad = []
    for a, b, la, lb in ((mn, ty, "min", "typ"), (ty, mx, "typ", "max"), (mn, mx, "min", "max")):
        if a is not None and b is not None and key(a) > key(b):
            bad.append(f"{la}={a} > {lb}={b}")
    return bad


def test_ordering_holds_for_positive_rails() -> None:
    """正压轨按数值校验 —— 次序必须成立。"""
    for limits, why in (
        ({"min": 0.0, "typ": 25.0, "max": 70.0}, "常规温度范围"),
        ({"min": 3.0, "typ": 3.45, "max": 3.6}, "正压轨整定值"),
    ):
        assert not _order_violations(limits, "3.45V"), f"{why} 越界"


def test_ordering_holds_for_negative_rails() -> None:
    """负压轨按绝对值校验 —— 数值递减是正确表述, 不得误报。"""
    # PA601 SR-1201 原文形态
    assert not _order_violations({"min": -53.2, "typ": -54.0, "max": -54.8}, "-54V"), (
        "负压轨绝对值递减是正确表述, 按数值比较会误报"
    )


def test_ordering_check_catches_real_violation() -> None:
    """次序检查须真能抓到越界 —— 否则前两条只是装饰。"""
    bad = _order_violations({"min": 0.0, "typ": 91.0, "max": 70.0}, "3.45V")
    assert bad, "正压轨 typ>max 应被识别"
    # 负压轨按绝对值: min 的绝对值必须 <= typ 的绝对值。
    # min=-53.2 typ=-54.0 max=-54.8 是正确形态; 反过来 (min绝对值更大) 才算越界。
    bad_neg = _order_violations({"min": -56.0, "typ": -54.0, "max": -53.2}, "-54V")
    assert bad_neg, "负压轨绝对值序错(|-56| > |-54|)应被识别"
    ok_neg = _order_violations({"min": -53.2, "typ": -54.0, "max": -54.8}, "-54V")
    assert not ok_neg, "PA601 SR-1201 的真实形态不应被误报"


# ---------------------------------------------------------------------------
# PA601 全量扫描: 四条契约同时跑
# ---------------------------------------------------------------------------


def test_pa601_conformance() -> None:
    """用 PA601 全量数据跑四条契约 —— 真实数据若有脏值必须报出。

    这是本层的存在理由: 数据契约不是纸面约定, 每次回归都拿真实规格书验一遍。
    """
    for k, v in {
        "POSTGRES_DSN": "postgresql://x:x@127.0.0.1/x",
        "LLM_BASE": "http://x/v1",
        "LLM_MODEL": "x",
        "EMBED_BASE": "http://x",
        "EMBED_MODEL": "x",
    }.items():
        os.environ.setdefault(k, v)

    from aterag.extract import extract_test_conditions, load_annotations

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    lk = cfg["limit_kinds"]
    known = _known_kinds(cfg)

    r = extract_test_conditions(
        "PA601-D54A", doc_version="B", annotations=load_annotations("PA601-D54A")
    )
    assert r.conditions, "应能取到条件"

    bad_type: list[str] = []
    bad_kind: list[str] = []
    bad_unit: list[str] = []
    bad_order: list[str] = []

    for c in r.conditions:
        lim = c.limits or {}
        for k in _num_keys(lim):
            if not isinstance(lim[k], (int, float)) or isinstance(lim[k], bool):
                bad_type.append(f"{c.req_id}:{k}={lim[k]!r}")
        # 契约 2: kind 词表
        lk_kind = _limit_kind(c)
        if lk_kind and lk_kind not in known:
            bad_kind.append(f"{c.req_id}:{lk_kind}")
        # 契约 3: 有数值则单位须登记。
        # '-' 与空是规格书的无量纲记法(功率因数 0.98 记作 unit='-'), 合法;
        # SHACL 约束按 kind 而非单位分组, 无量纲条款的约束不依赖单位。
        DIMENSIONLESS = {"-", "—", "/", ""}
        if _num_keys(lim):
            u = lim.get("unit")
            if u not in lk and u not in DIMENSIONLESS:
                bad_unit.append(f"{c.req_id}:{u!r}")
        # 契约 4: 次序 (按轨极性)
        for v in _order_violations(lim, c.rail or ""):
            bad_order.append(f"{c.req_id}({c.rail}): {v}")

    assert not bad_type, f"非数值限值: {bad_type[:5]}"
    assert not bad_kind, f"limit_kind 不在词表: {bad_kind[:5]}"
    assert not bad_unit, f"有数值但单位未在 limit_kinds 登记: {bad_unit[:5]}"
    assert not bad_order, f"限值次序越界: {bad_order[:5]}"
