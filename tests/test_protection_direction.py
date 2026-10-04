"""Protection 聚合的回归测试。

核心是 ``direction`` 的循环变量泄漏(已修): 它原先在「遍历 Requirement」
循环里算出, 却在「遍历 group」循环里使用, 于是每个 group 拿到的都是最后
一条匹配标题的类别 —— PA601 上 9 个实体全成了 'over', 包括本该是
'under' 的输入欠压保护。

测试不依赖仓库外的 PA601 原文: 直接构造 Requirement 实体喂给
``_group_protections``, 把「顺序敏感」这一点显式钉住。
"""

from __future__ import annotations

from aterag.ingest.entity_extract import Entity, _group_protections
from aterag.ingest.table_schema import load_registry

BASE = {
    "model_id": "TEST-1",
    "heading": "4.3.3 保护功能",
    "section_path": "4.3.3",
    "priority": "强制",
    "notes": "",
    "rail": "",
    "exists": "",
    "unit": "Vac",
}


def _req(req_id: str, title: str, **kw) -> Entity:
    return Entity(
        eid=f"{req_id}@{title}",
        etype="Requirement",
        props={**BASE, "req_id": req_id, "title": title, **kw},
    )


def _run(reqs: list[Entity]) -> dict[tuple[str, str], Entity]:
    """按 (protection_type, rail) 索引。

    不能只按 protection_type 索引: 同名保护可以有多个电压轨(SR-1309 的
    -54V 与 3.45V), 只按类型索引会把它们折叠成一个, 测试就看不出分组对不对。
    """
    ents = [*reqs]
    _group_protections(ents, load_registry())
    return {
        (e.props["protection_type"], e.props.get("rail", "")): e
        for e in ents
        if e.etype == "Protection"
    }


def test_undervoltage_protection_gets_under_direction() -> None:
    """输入欠压保护方向必须是 under —— 低于阈值动作、回升到恢复点解除。"""
    prot = _run(
        [
            _req("R-1300", "输入过压保护点", min=305.0),
            _req("R-1301", "输入过压恢复点", min=302.0),
            _req("R-1302", "输入过压保护回差", min=3.0),
            _req("R-1303", "输入欠压保护点", max=80.0),
            _req("R-1304", "输入欠压恢复点", max=83.0),
            _req("R-1305", "输入欠压保护回差", min=3.0),
        ]
    )
    assert set(prot) == {("输入过压保护", ""), ("输入欠压保护", "")}, sorted(prot)
    # 关键断言: 两种方向必须**同时**正确 —— 泄漏时它们会一起变成最后迭代的值
    assert prot[("输入欠压保护", "")].props["direction"] == "under"
    assert prot[("输入过压保护", "")].props["direction"] == "over"


def test_direction_does_not_depend_on_input_order() -> None:
    """把欠压条款挪到最后, 过压的方向不能被带着变。

    这是泄漏的直接证据: 修复前过压会跟着最后一条变成 'under'。
    """
    over = [
        _req("R-1300", "输入过压保护点", min=305.0),
        _req("R-1301", "输入过压恢复点", min=302.0),
    ]
    under = [
        _req("R-1303", "输入欠压保护点", max=80.0),
        _req("R-1304", "输入欠压恢复点", max=83.0),
    ]
    a = _run([*over, *under])
    b = _run([*under, *over])
    for key in (("输入过压保护", ""), ("输入欠压保护", "")):
        assert a[key].props["direction"] == b[key].props["direction"], key
    assert a[("输入过压保护", "")].props["direction"] == "over"
    assert a[("输入欠压保护", "")].props["direction"] == "under"


def test_hysteresis_assembled_from_trip_and_recovery_rows() -> None:
    """回差/恢复点/保护点三条聚合到同一实体, 且回差与点差自洽。"""
    prot = _run(
        [
            _req("R-1300", "输入过压保护点", min=305.0),
            _req("R-1301", "输入过压恢复点", min=302.0),
            _req("R-1302", "输入过压保护回差", min=3.0),
        ]
    )
    p = prot[("输入过压保护", "")].props
    assert p["trip_min"] == 305.0
    assert p["recovery_min"] == 302.0
    assert p["hysteresis_min"] == 3.0
    # 回差应等于保护点 - 恢复点; 不等说明聚合串了行
    assert p["trip_min"] - p["recovery_min"] == p["hysteresis_min"]


def test_same_type_different_rails_stay_separate() -> None:
    """同名保护但不同电压轨 -> 两个独立实体, 不能被合成一个。

    SR-1309 输出过流保护有 -54V (12~18A) 与 3.45V (0.4~1.5A) 两条;
    合成一个会让 3.45V 的保护点被 -54V 的覆盖掉。
    """
    prot = _run(
        [
            _req("R-1309", "输出过流保护", rail="-54V", min=12.0, max=18.0, unit="A"),
            _req("R-1309", "输出过流保护", rail="3.45V", min=0.4, max=1.5, unit="A"),
        ]
    )
    assert set(prot) == {("输出过流保护", "-54V"), ("输出过流保护", "3.45V")}, sorted(prot)
    assert prot[("输出过流保护", "-54V")].props["trip_min"] == 12.0
    assert prot[("输出过流保护", "-54V")].props["trip_max"] == 18.0
    assert prot[("输出过流保护", "3.45V")].props["trip_min"] == 0.4
    assert prot[("输出过流保护", "3.45V")].props["trip_max"] == 1.5


def test_static_and_dynamic_overvoltage_are_distinct_protections() -> None:
    """静态/动态过压是两种保护(58~63V vs 59~65V), 不能并成一条。

    注意 protection_type 的拼装序是 ``{prefix}{ptype}{variant}保护``
    -> 输出过压静态保护, 不是「输出静态过压保护」。
    """
    prot = _run(
        [
            _req("R-1310", "输出静态过压保护", rail="-54V", min=58.0, max=63.0, unit="V"),
            _req("R-1311", "输出动态过压保护", rail="-54V", min=59.0, max=65.0, unit="V"),
        ]
    )
    assert set(prot) == {
        ("输出过压静态保护", "-54V"),
        ("输出过压动态保护", "-54V"),
    }, sorted(prot)
    assert prot[("输出过压静态保护", "-54V")].props["trip_max"] == 63.0
    assert prot[("输出过压动态保护", "-54V")].props["trip_max"] == 65.0


def test_over_temperature_direction_is_not_under() -> None:
    """过温保护按温度升高动作, 归 over; 回归前「欠压」判断不能误伤它。

    顺带锁住「只有欠压才是 under」这条边界 —— 若改成按 '保护' 后缀猜,
    过温会被错判。
    """
    prot = _run(
        [
            _req("R-1300", "输入过压保护点", min=305.0),
            _req("R-1306", "过温保护", rail=""),
            _req("R-1307", "过温保护回差", min=5.0, unit="\u2103"),
        ]
    )
    assert prot[("过温保护", "")].props["direction"] == "over"
    assert prot[("过温保护", "")].props["hysteresis_min"] == 5.0
