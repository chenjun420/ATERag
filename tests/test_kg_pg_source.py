"""``aterag.kg.pg_source`` 的单元测试 —— 用假 cursor, 不连真库。

真库路径由部署后的冒烟覆盖; 这里守的是**映射规则**, 而映射规则是最容易在
重构里悄悄变掉的部分(节点 id 用什么、Product 节点谁说了算、属性挑哪些)。

实测的实体形态(板卡 192.168.5.25, PA601-D54A)::

    Requirement  eid='SR-PA601-D54A-0100@表'  keys=[max,min,typ,rail,unit,notes,title,exists,req_id,heading]
    Signal       eid='PA601-D54A:-54VRTN@P1'    keys=[pin,notes,heading,model_id,connector,signal_def,...]
    Protection   eid='PA601-D54A:输出短路保护#-54V'  keys=[rail,notes,req_id,heading,model_id,priority,...]
    Product      eid='PA601-D54A'
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aterag.kg import pg_source


class FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeConn:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        return FakeCursor(self._rows)

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    """把 ``psycopg.connect`` 换成返回预置行的假连接。"""

    def install(rows: list[tuple]) -> None:
        import psycopg

        monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConn(rows))

    return install


def _props(**kw: Any) -> str:
    """模拟 JSONB 列: psycopg 会把它解析成 dict。"""
    return kw  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 节点 id: 直接用 eid
# ---------------------------------------------------------------------------


class TestNodeId:
    def test_node_id_is_eid_itself(self, fake_pg) -> None:
        """节点 id 就是 eid —— 抽取层已经把型号编进 eid 了(实测
        ``PA601-D54A:-54VRTN@P1``), 再套一层就是型号出现两次。"""
        fake_pg(
            [
                (
                    "PA601-D54A",
                    "Signal",
                    "PA601-D54A:-54VRTN@P1",
                    {"section_path": "4.2.4.2", "signal_name": "-54VRTN"},
                )
            ]
        )
        ents, _rels = pg_source.read_model_records("dsn")
        ids = [e["id"] for e in ents]
        assert "PA601-D54A:-54VRTN@P1" in ids
        assert not any(i.startswith("PA601-D54A/Signal/") for i in ids)

    def test_cross_model_eid_collision_raises(self, fake_pg) -> None:
        """两个型号的 eid 撞了必须报错, 不能静默合并成一个节点。

        静默合并是知识层最坏的失败: 界面看起来正常, 但节点属性只来自其中一个
        型号, 另一个型号的知识被吞掉且不可见。
        """
        fake_pg(
            [
                ("MODEL-A", "Signal", "SHARED-NAME", {"signal_name": "A"}),
                ("MODEL-B", "Signal", "SHARED-NAME", {"signal_name": "B"}),
            ]
        )
        with pytest.raises(ValueError, match="跨型号冲突"):
            list(pg_source.iter_model_records("dsn"))

    def test_same_eid_same_model_is_fine(self, fake_pg) -> None:
        """同一型号内 eid 重复(不同 etype)不算冲突 —— 那靠上游保证唯一。"""
        fake_pg(
            [
                ("PA601-D54A", "Signal", "DUP", {"signal_name": "x"}),
                ("PA601-D54A", "Attribute", "DUP", {"description": "y"}),
            ]
        )
        ents, _ = pg_source.read_model_records("dsn")
        assert [e["id"] for e in ents].count("DUP") == 2


# ---------------------------------------------------------------------------
# Product 节点: 谁说了算
# ---------------------------------------------------------------------------


class TestProductNode:
    def test_no_synthetic_product_when_extractor_produced_one(self, fake_pg) -> None:
        """抽取层已产出 Product 实体时**不得**再合成一个。

        同一个 id 进两次 add_nodes, 后者覆盖前者, 而两次的 content/metadata
        不同 —— 结果是「属性来自谁」取决于遍历顺序。
        """
        fake_pg(
            [
                ("PA601-D54A", "Product", "PA601-D54A", {"model_id": "PA601-D54A"}),
                ("PA601-D54A", "Signal", "PA601-D54A:S1", {"signal_name": "S1"}),
            ]
        )
        ents, _ = pg_source.read_model_records("dsn")
        products = [e for e in ents if e["entity_type"].endswith("/Product")]
        assert len(products) == 1, f"Product 节点重复: {products}"

    def test_synthetic_product_when_extractor_omitted_it(self, fake_pg) -> None:
        fake_pg([("PA601-D54A", "Signal", "PA601-D54A:S1", {"signal_name": "S1"})])
        ents, _ = pg_source.read_model_records("dsn")
        products = [e for e in ents if e["entity_type"].endswith("/Product")]
        assert len(products) == 1
        assert products[0]["id"] == "PA601-D54A"

    def test_product_has_no_self_loop(self, fake_pg) -> None:
        fake_pg(
            [
                ("PA601-D54A", "Product", "PA601-D54A", {}),
                ("PA601-D54A", "Signal", "PA601-D54A:S1", {}),
            ]
        )
        _ents, rels = pg_source.read_model_records("dsn")
        for r in rels:
            assert r["source_id"] != r["target_id"], f"has 边成了自环: {r}"
        assert len(rels) == 1, "只有非 Product 实体才该有 has 边"


# ---------------------------------------------------------------------------
# 属性映射
# ---------------------------------------------------------------------------


class TestMetadataMapping:
    def test_model_entities_are_namespaced_by_type(self, fake_pg) -> None:
        """节点类型带 ``model/`` 前缀 —— 两类知识权威不同, 追溯要能分清。"""
        fake_pg([("PA601-D54A", "Protection", "P1", {"req_id": "SR-1"})])
        ents, _ = pg_source.read_model_records("dsn")
        by_id = {e["id"]: e for e in ents}
        assert by_id["P1"]["entity_type"] == "model/Protection"

    def test_authority_kind_is_spec(self, fake_pg) -> None:
        """型号知识权威是规格书(``spec``), 不是标准也不是 unverified。"""
        fake_pg([("PA601-D54A", "Signal", "S1", {})])
        ents, _ = pg_source.read_model_records("dsn")
        by_id = {e["id"]: e for e in ents}
        assert by_id["S1"]["metadata"]["authority_kind"] == "spec"
        assert by_id["S1"]["source"] == "spec:PA601-D54A"

    def test_section_carries_section_path(self, fake_pg) -> None:
        fake_pg([("PA601-D54A", "Protection", "P1", {"section_path": "4.3.3"})])
        ents, _ = pg_source.read_model_records("dsn")
        by_id = {e["id"]: e for e in ents}
        assert by_id["P1"]["section"] == "4.3.3"

    def test_numeric_limits_carried(self, fake_pg) -> None:
        """保护动作门限是保护整定的输入, 不能在映射里丢掉。"""
        fake_pg(
            [
                (
                    "PA601-D54A",
                    "Protection",
                    "P1",
                    {"trip_min": 8.1, "trip_max": 18.0, "rail": "-54V", "priority": "high"},
                ),
            ]
        )
        ents, _ = pg_source.read_model_records("dsn")
        m = {e["id"]: e for e in ents}["P1"]["metadata"]
        assert m["trip_min"] == 8.1
        assert m["trip_max"] == 18.0
        assert m["rail"] == "-54V"
        assert m["priority"] == "high"

    def test_label_prefers_chinese_title(self, fake_pg) -> None:
        """content 用 eid + 中文标签 —— Explorer 的搜索是子串匹配, 只放 eid
        的话「输出过流」这类中文查询命中不了型号节点。"""
        fake_pg([("PA601-D54A", "Requirement", "SR-1", {"title": "工作温度范围"})])
        ents, _ = pg_source.read_model_records("dsn")
        by_id = {e["id"]: e for e in ents}
        assert "工作温度范围" in by_id["SR-1"]["text"]

    def test_label_falls_back_to_eid(self, fake_pg) -> None:
        fake_pg([("PA601-D54A", "Protection", "PA601-D54A:过温保护", {})])
        ents, _ = pg_source.read_model_records("dsn")
        by_id = {e["id"]: e for e in ents}
        assert by_id["PA601-D54A:过温保护"]["text"] == "PA601-D54A:过温保护"

    def test_props_as_string_fails_loudly(self, fake_pg) -> None:
        """``props`` 是 JSONB 列。若上游(或未来的迁移)给成字符串, 必须**报错**。

        静默退化是这里最坏的失败: ``{}.get("section_path")`` 恒为 None, 于是
        全部 231 个型号节点的章节出处一起消失, 而服务正常、日志干净、图也能
        画出来 —— 只是每条知识的「出处」都空了。
        """
        fake_pg([("PA601-D54A", "Signal", "S1", '{"section_path": "4.2"}')])
        with pytest.raises((ValueError, TypeError, AttributeError)):
            list(pg_source.iter_model_records("dsn"))

    def test_props_as_dict_reads_section(self, fake_pg) -> None:
        """对照组: dict 形态正常读出章节。"""
        fake_pg([("PA601-D54A", "Signal", "S1", {"section_path": "4.2"})])
        ents, _ = pg_source.read_model_records("dsn")
        by_id = {e["id"]: e for e in ents}
        assert by_id["S1"]["section"] == "4.2"


# ---------------------------------------------------------------------------
# 与领域知识的形状一致性
# ---------------------------------------------------------------------------


class TestSeedShapeCompatibility:
    """产出的记录必须能被 :func:`build_context_graph` 当成种子记录处理。

    契约只要求**物化层消费的那几个键**在位, 不要求字段集相同 —— 种子的实体
    带 ``definition``/``domain``/``ratio``/``qudt_ref`` 等领域专有字段, 型号实体
    带 ``trip_min``/``pin``/``connector`` 等, 强行对齐反而是错的。
    """

    #: materialize.build_context_graph 实际读的键。
    ENTITY_KEYS = {"id", "entity_type", "text", "source", "section", "metadata"}
    RELATION_KEYS = {"source_id", "target_id", "relationship_type"}

    def test_entity_records_carry_required_keys(self, fake_pg) -> None:
        fake_pg([("PA601-D54A", "Protection", "P1", {"section_path": "4.3.3"})])
        ents, _rels = pg_source.read_model_records("dsn")
        for e in ents:
            assert self.ENTITY_KEYS <= set(e), f"实体缺键: {self.ENTITY_KEYS - set(e)}"

    def test_relation_records_carry_required_keys(self, fake_pg) -> None:
        fake_pg([("PA601-D54A", "Signal", "S1", {})])
        _ents, rels = pg_source.read_model_records("dsn")
        for r in rels:
            assert self.RELATION_KEYS <= set(r), f"关系缺键: {self.RELATION_KEYS - set(r)}"

    def test_real_seed_uses_the_same_keys(self) -> None:
        """复核: 真实种子的实体/关系确实用同一组键(否则合并会静默丢东西)。"""
        from pathlib import Path

        seed = Path("data/seed/power_domain_seed.json")
        if not seed.exists():
            pytest.skip("种子文件不在")
        records = json.loads(seed.read_text(encoding="utf-8"))["records"]
        for r in records:
            if r.get("source_id") and r.get("target_id"):
                assert self.RELATION_KEYS <= set(r), f"种子的关系记录缺键: {r}"
            elif r.get("id"):
                assert {"id", "entity_type"} <= set(r), f"种子的实体记录缺键: {sorted(r)}"
