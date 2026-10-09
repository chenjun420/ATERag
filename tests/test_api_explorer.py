"""HTTP 门面的测试: 路由分流、鉴权、以及**子 app 的 lifespan**。

最要紧的一条是 :class:`TestExplorerLifespan` —— 少了它, ``/api/health`` 照样
200, 页面照样打开, 只是所有 ``/api/graph/*`` 返回 503「没有图」。那种失败在
浏览器里看起来像「这个型号本来就没数据」, 极难定位。

这些用例不连真库: ``kg.graph.collect_records`` 被替换成固定数据, 所以它们
在离线包环境与 CI 里都能跑。图谱内容的正确性由 ``test_kg_materialize.py``
与部署后的冒烟覆盖。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """一个不连库、不读真实种子的 app 客户端。"""
    from aterag.api import explorer as mod
    from aterag.config import Settings

    ents = [
        {
            "id": "HYSTERESIS",
            "entity_type": "power_concept",
            "text": "回差",
            "source": "YD/T 1817-2017",
            "section": None,
            "metadata": {"authority_kind": "standard"},
        },
        {
            "id": "PA601-D54A:输出过流保护#-54V",
            "entity_type": "model/Protection",
            "text": "输出过流保护",
            "source": "spec:PA601-D54A",
            "section": "4.3.3",
            "metadata": {"authority_kind": "spec", "trip_min": 8.1},
        },
    ]
    rels = [
        {
            "source_id": "PA601-D54A",
            "target_id": "PA601-D54A:输出过流保护#-54V",
            "relationship_type": "has",
        },
    ]

    monkeypatch.setattr(mod.kg_graph, "collect_records", lambda dsn, **kw: (ents, rels))
    monkeypatch.setattr(
        mod.kg_graph, "loaders_status", lambda dsn: {"seed": {"ok": True}, "postgres": {"ok": True}}
    )
    monkeypatch.setattr(mod.kg_graph, "build_session", _fake_session)
    # build_graph 也必须换掉: /aterag/graph/summary 直接调它算 relations_built,
    # 不换的话它会去读真实种子与 PG, 而假的 Product->Protection 边在真图里
    # 是悬空的(PA601-D54A 不是种子节点), 于是断言会看到一个与本用例无关的 0。
    monkeypatch.setattr(
        mod.kg_graph,
        "build_graph",
        lambda dsn, **kw: (_FAKE_GRAPH, {"nodes": 2, "edges": 1}),
    )

    s = Settings()
    object.__setattr__(s, "explorer_api_key", "test-key")
    monkeypatch.setattr(mod, "Settings", lambda: s)
    monkeypatch.setenv("SEMANTICA_API_KEY", "test-key")
    monkeypatch.delenv("SEMANTICA_ALLOW_ANONYMOUS", raising=False)

    with TestClient(mod.create_app(s)) as c:
        yield c


def _fake_graph():
    from semantica.context import ContextGraph

    g = ContextGraph()
    g.add_nodes(
        [
            {"id": "HYSTERESIS", "type": "power_concept", "content": "回差"},
            {
                "id": "PA601-D54A:输出过流保护#-54V",
                "type": "model/Protection",
                "content": "输出过流保护",
            },
        ]
    )
    g.add_edges(
        [
            {
                "source_id": "HYSTERESIS",
                "target_id": "PA601-D54A:输出过流保护#-54V",
                "edge_type": "has",
            }
        ]
    )
    return g


#: 惰性单例, 免得每个用例都重建一张图。
_FAKE_GRAPH = _fake_graph()


def _fake_session(dsn, **kw):
    from semantica.explorer.session import GraphSession

    return GraphSession(_FAKE_GRAPH), {"nodes": 2, "edges": 1}


# ---------------------------------------------------------------------------
# 路由分流
# ---------------------------------------------------------------------------


class TestRouting:
    def test_aterag_routes_win_over_the_mount(self, client: TestClient) -> None:
        """``Mount("/")`` 会匹配一切, 所以 ``/aterag/*`` 必须注册在它之前。"""
        assert client.get("/aterag/live").status_code == 200

    def test_root_reaches_explorer(self, client: TestClient) -> None:
        """根路径必须给 Explorer 的 index.html, 不是 ATERag 的。"""
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_static_assets_are_served(self, client: TestClient) -> None:
        """前端用**绝对路径**引资源, 所以任何一个 404 都会让页面白屏。"""
        import re

        html = client.get("/").text
        refs = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
        assert refs, "index.html 里没解析到资源引用, 测试本身失效"
        for ref in refs:
            r = client.get(ref)
            assert r.status_code == 200, f"{ref} -> {r.status_code}"
            assert len(r.content) > 0, f"{ref} 是空响应"


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


class TestAuth:
    def test_live_needs_no_key(self, client: TestClient) -> None:
        """存活探针给负载均衡器用, 它们不持有 API key。"""
        assert client.get("/aterag/live").status_code == 200

    def test_summary_requires_key(self, client: TestClient) -> None:
        assert client.get("/aterag/graph/summary").status_code == 401

    def test_wrong_key_rejected(self, client: TestClient) -> None:
        r = client.get("/aterag/graph/summary", headers={"X-API-Key": "nope"})
        assert r.status_code == 401

    def test_correct_key_accepted(self, client: TestClient) -> None:
        r = client.get("/aterag/graph/summary", headers={"X-API-Key": "test-key"})
        assert r.status_code == 200
        assert r.json()["entities"] == 2

    def test_explorer_api_requires_key(self, client: TestClient) -> None:
        """Explorer 自己的 ``/api/*`` 也必须鉴权 —— 那是全量图的读接口。"""
        assert client.get("/api/graph/stats").status_code == 401
        assert client.get("/api/graph/stats", headers={"X-API-Key": "test-key"}).status_code == 200

    def test_no_anonymous_escape_hatch(self, client: TestClient) -> None:
        """项目**不**启用上游的 ``SEMANTICA_ALLOW_ANONYMOUS`` 逃生口。

        那类「开发用匿名开关」在生产环境活下来的概率远高于被关掉的概率。
        """
        assert "SEMANTICA_ALLOW_ANONYMOUS" not in os.environ


class TestMissingKeyIsFailClosed:
    def test_503_when_no_key_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没配 key 时是 503 而不是放行 —— 与上游 Explorer 同一口径。

        必须连 ``require_key`` 读的那份 Settings 一起换掉: 它在**每次请求**
        里重新构造 Settings, 只改 create_app 那个实参的话, 端点仍会读到
        开发机 .env 里的真 key, 于是拿到 401 而不是 503 —— 断言会「通过」但
        测的根本不是这一条。
        """
        from aterag.api import explorer as mod
        from aterag.config import Settings

        ents = [
            {
                "id": "X",
                "entity_type": "power_concept",
                "text": "x",
                "source": "",
                "section": None,
                "metadata": {},
            }
        ]
        monkeypatch.setattr(mod.kg_graph, "collect_records", lambda dsn, **kw: (ents, []))
        monkeypatch.setattr(mod.kg_graph, "build_session", _fake_session)
        monkeypatch.delenv("SEMANTICA_API_KEY", raising=False)
        monkeypatch.delenv("SEMANTICA_ALLOW_ANONYMOUS", raising=False)

        s = Settings()
        object.__setattr__(s, "explorer_api_key", "")
        monkeypatch.setattr(mod, "Settings", lambda: s)

        with TestClient(mod.create_app(s)) as c:
            r = c.get("/aterag/graph/summary", headers={"X-API-Key": "anything"})
            assert r.status_code == 503, f"实际 {r.status_code}: {r.text[:150]}"
            assert "fail-closed" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 子 app 的 lifespan —— 少了就 503
# ---------------------------------------------------------------------------


class TestExplorerLifespan:
    def test_graph_api_serves_our_session(self, client: TestClient) -> None:
        """``/api/graph/stats`` 必须返回**我们**建的图, 不是它自建的空图。

        少了 lifespan 组合时这里返回 503, 而 ``/api/health`` 照样 200 ——
        所以「服务健康」不等于「有数据」。
        """
        r = client.get("/api/graph/stats", headers={"X-API-Key": "test-key"})
        assert r.status_code == 200, f"/api/graph/stats -> {r.status_code} {r.text[:200]}"
        assert r.json()["node_count"] == 2

    def test_nodes_from_model_knowledge_are_reachable(self, client: TestClient) -> None:
        r = client.get(
            "/api/graph/nodes?type=model%2FProtection",
            headers={"X-API-Key": "test-key"},
        )
        assert r.status_code == 200
        ids = [n["id"] for n in r.json()["nodes"]]
        assert "PA601-D54A:输出过流保护#-54V" in ids

    def test_health_endpoint_alone_does_not_prove_data_is_loaded(self, client: TestClient) -> None:
        """把那条失效模式钉成断言: ``/api/health`` 200 **不能**当作有数据的证据。"""
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/graph/stats").status_code == 401  # 未带 key
        assert client.get("/api/graph/stats", headers={"X-API-Key": "test-key"}).status_code == 200


# ---------------------------------------------------------------------------
# /aterag/health 的分源报告
# ---------------------------------------------------------------------------


class TestLoaderStatus:
    def test_both_loaders_reported_separately(self, client: TestClient) -> None:
        """「种子没找到」与「PG 连不上」是不同的故障, 不能合成一个布尔。"""
        j = client.get("/aterag/health", headers={"X-API-Key": "test-key"}).json()
        assert set(j["loaders"]) == {"seed", "postgres"}
        assert j["auth_configured"] is True

    def test_summary_reports_raw_and_built_relations(self, client: TestClient) -> None:
        """原始关系数与实际建边数都报出来 —— 两者不等(外部引用/自环/悬空被舍)。"""
        j = client.get("/aterag/graph/summary", headers={"X-API-Key": "test-key"}).json()
        assert j["relations_raw"] == 1
        assert j["relations_built"] == 1
        assert "relations_raw" in j and "relations_built" in j


def test_no_hardcoded_model_ids() -> None:
    """门面里不许出现具体型号 —— 那是数据不是代码。"""
    from pathlib import Path

    src = Path("src/aterag/api/explorer.py").read_text(encoding="utf-8")
    for bad in ("PA601", "PN1000", "SR-", "l0_term"):
        assert bad not in src, f"HTTP 门面里硬编码了 {bad}"


def test_module_has_main_entrypoint() -> None:
    """``python -m aterag.api.explorer`` 要能起服务(deploy 脚本会这么调)。"""
    from aterag.api import explorer as mod

    assert callable(mod.main)
    assert callable(mod.create_app)


def test_settings_expose_explorer_port() -> None:
    from aterag.config import Settings

    s = Settings()
    assert s.explorer_port > 0
    assert s.explorer_host
    # 默认不配 key —— fail-closed 靠的是「默认空」而不是「文档里记得配」
    assert Settings.model_fields["explorer_api_key"].default == ""


def _unused(_: Any) -> None:  # pragma: no cover
    pass
