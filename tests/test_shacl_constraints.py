"""SHACL 约束的**反向**验证: 注入违规数据, 确认约束真的拦得住。

为什么必须构造反例
------------------
``conforms=True violations=0`` 有两种成因, 报告长得一模一样:

1. 数据真的合规;
2. 约束压根没被应用。

第二种在缺 ``rdf:type`` 时就会出现 —— ``sh:targetClass`` 匹配不到任何节点,
于是「零违规」, 而报告照样写 conforms。只跑一遍合规数据, 两种情况无法区分,
「9 条约束全部通过」这句话就无从谈起。

所以每条约束都必须配一个**能触发它的反例**; 另有一条合规负例, 确认约束
不是靠「一律报错」来通过的。

这些用例曾以探针脚本 ``.probe_shapes.py`` 的形式存在, 未纳入版本库、也不进
CI —— 约束一旦被改坏, 没有任何东西会拦住。现固化为此文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from scripts.build_constraints import NS  # noqa: E402

rdflib = pytest.importorskip("rdflib")

from rdflib import Graph, Literal, Namespace, URIRef  # noqa: E402

semantica_validator = pytest.importorskip("semantica.ontology.ontology_validator")

EX = Namespace(NS)
RDF_TYPE = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
SHAPES = Path("data/seed/power_domain_shapes.ttl")


def _ttl(entries: list[tuple[str, str, list[tuple[str, object]]]]) -> str:
    """造一份最小 Turtle。

    **必须显式给 ``rdf:type``** —— ``sh:targetClass`` 按类型定位目标, 没有类型
    的节点根本不会被校验。这正是「约束静默失效」那条路径, 所以这里显式补上。
    """
    g = Graph()
    g.bind("ex", EX)
    for rid, cls, props in entries:
        s = URIRef(NS + rid)
        g.add((s, RDF_TYPE, EX[cls]))
        for key, value in props:
            g.add((s, EX[key], Literal(value)))
    return g.serialize(format="turtle")


def _violations(entries: list[tuple[str, str, list[tuple[str, object]]]]) -> int:
    shapes = SHAPES.read_text(encoding="utf-8")
    report = semantica_validator.run_shacl_validation(_ttl(entries), shapes)
    return len(report.violations)


# ---------------------------------------------------------------------------
# 溯源: 权威类型与权威出处必须自洽
# ---------------------------------------------------------------------------


def test_authority_kind_must_be_one_of_five() -> None:
    """authority_kind 是受控词表; 出现表外值必须被拦。

    ``sh:targetSubjectsOf`` 而非 ``sh:targetObjectsOf`` —— 后者定位的是三元组的
    **对象**而非主体, 非法值会零违规通过。
    """
    assert _violations([("t1", "PowerConcept", [("authority_kind", "由方案给出")])]) >= 1


def test_standard_without_authority_ref_is_rejected() -> None:
    """标为 standard 却拿不出标准号 —— 等于自称有依据而实际没有。"""
    assert _violations([("t2", "PowerConcept", [("authority_kind", "standard")])]) >= 1


# ---------------------------------------------------------------------------
# 公式: 工况折算与量纲
# ---------------------------------------------------------------------------


def test_half_load_must_be_half_of_full_load() -> None:
    """半载 = 50%载 = 满载 x 50% (工况词建模的硬约束)。"""
    assert _violations(
        [("m1", "ModelSpec", [("full_load_power", 1000), ("half_load_power", 600)])]
    ) >= 1


def test_output_power_must_equal_v_times_i() -> None:
    """P = V x I (基础电路原理)。"""
    assert _violations(
        [("m2", "ModelSpec", [("vout_nom", 12), ("iout_max", 10), ("pout_max", 150)])]
    ) >= 1


def test_efficiency_outside_unit_interval_is_rejected() -> None:
    """效率写成 95 (而非 0.95) —— 量纲事故, 只有同时校验 0..1 区间才拦得住。"""
    assert _violations(
        [("m7", "ModelSpec", [("pin", 200), ("pout_max", 190), ("efficiency", 95)])]
    ) >= 1


# ---------------------------------------------------------------------------
# 公理: 标称 <= 额定, 峰值 >= 连续
# ---------------------------------------------------------------------------


def test_nominal_voltage_must_not_exceed_rated() -> None:
    assert _violations(
        [("m3", "ModelSpec", [("vout_nom", 24), ("vout_rated", 12)])]
    ) >= 1


def test_peak_current_must_not_be_below_continuous() -> None:
    assert _violations(
        [("m4", "ModelSpec", [("iout_max", 10), ("iout_peak", 8)])]
    ) >= 1


# ---------------------------------------------------------------------------
# 硬件特性: 结温关系式与恒压/恒流交叠
# ---------------------------------------------------------------------------


def test_junction_temperature_must_respect_limit() -> None:
    """结温 = 环温 + 功耗 x 热阻。

    约束里**不写死任何数值上限** —— 上限由具体规格书给出, 凭空填一个 125℃
    就是编数据。这里只校验关系式本身。
    """
    assert _violations(
        [
            (
                "m5",
                "ModelSpec",
                [
                    ("ambient_temp", 85),
                    ("total_loss", 100),
                    ("rth_ja", 0.5),
                    ("junction_temp_limit", 125),
                ],
            )
        ]
    ) >= 1


def test_cc_threshold_must_reach_rated_output_current() -> None:
    """恒流门限电流 < 额定输出电流 => 还没到额定负载就被限流, 拿不到额定输出。

    **同量纲比较**。这条曾写成拿 vout_max(V) 比 current_limit_threshold(A) ——
    量纲不同, 结论无意义; 而旧反例恰好喂了 vout_max+current_limit_threshold,
    注入的违规数据在错误约束下「碰巧」也能触发, 所以反向验证没抓到它。
    """
    assert (
        _violations(
            [("m6", "ModelSpec", [("iout_max", 12), ("current_limit_threshold", 10)])]
        )
        >= 1
    )


# ---------------------------------------------------------------------------
# 负例: 数据真的合规时不得报违规
# ---------------------------------------------------------------------------


def test_compliant_data_reports_no_violation() -> None:
    """12V x 10A = 120W, 半载 60W, 标称=额定, 峰值 >= 连续。

    少了这一条, 上面的用例即使全部「通过」也说明不了什么 —— 约束完全可能
    对任何输入都报错。
    """
    assert (
        _violations(
            [
                (
                    "m8",
                    "ModelSpec",
                    [
                        ("vout_nom", 12),
                        ("iout_max", 10),
                        ("pout_max", 120),
                        ("full_load_power", 120),
                        ("half_load_power", 60),
                        ("vout_rated", 12),
                        ("iout_peak", 20),
                    ],
                )
            ]
        )
        == 0
    )


def test_shapes_file_declares_every_constraint() -> None:
    """形状文件里出现过的约束, 一条都不许没有反向用例覆盖。

    新增 shape 而忘了配反例, 正是「约束形同虚设却报通过」最容易发生的时刻。
    """
    text = SHAPES.read_text(encoding="utf-8")
    declared = {ln.split(":", 1)[1].split()[0] for ln in text.splitlines() if " a sh:NodeShape" in ln}
    covered = {
        "ProvenanceKindShape",
        "AuthorityRequiredShape",
        "LoadScalingShape",
        "PowerConsistencyShape",
        "EfficiencyShape",
        "VoltageOrderingShape",
        "CurrentCapabilityShape",
        "ThermalShape",
        "CVCCOverlapShape",
    }
    assert declared == covered, f"形状文件与反向用例不同步: 多={declared - covered} 缺={covered - declared}"
