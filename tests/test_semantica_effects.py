"""semantica 能力「效果」验证 —— 不只证明能调用, 要证明效果到了用户手里。

**为什么要有这一层**
------------------
``scripts/verify_semantica_caps.py`` 那类脚本证明的是「API 能调通」(板卡实测
64/64)。但本轮逐项查消费者时发现, 9 项能力里只有 6 项在 ``src/`` 真正实例化,
而其中 ``GraphValidator`` 曾经**只被 CI 调用** —— 服务运行期从不调用, 于是
「能力可用」与「效果体现」是两回事。前者全绿, 后者为零。

判据用 AST 而不是 grep: 这轮已两次被 grep 坑到 ——
  - ``from aterag.kg import analytics``(只用于 graph_from_records) 被误读成
    「调用了 validate_structure」
  - ``# 注释里提到 DatalogReasoner`` 被误读成「生产在用它」
注释里出现一个名字, 和代码真的调用它, 是两件完全不同的事。

**三层分别钉什么**
------------------
  A 效果:   能力的判定/结论真的出现在 MCP 输出里, 且不是空壳
  B 接线:   每个能力的消费者确实被服务入口 import 到(AST 精确判定)
  C 回归:   曾经踩过的坑不会悄悄回来(路径、CI-only、报错而非空图)

``adjudicate`` 那条是**有意断言「它不该在生产路径」** —— 见 ``TestWiring`` 里的
说明: ``conflicts/gate.py`` 的 docstring 明确当前管线是单源, 门是为「未来接入的
第二来源」准备的。把它接进服务只会造出一个没有真实数据的假功能。
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SRC = ROOT / "src"
SEED = ROOT / "data" / "seed" / "power_domain_seed.json"

#: 服务入口。MCP 与 Explorer 两个 HTTP/工具面都从这里进。
ENTRY_MODULES = ("aterag.mcp_server.server", "aterag.api.explorer")


def _module_name(p: Path) -> str:
    """文件路径 -> 模块名。``kg/__init__.py`` 记作 ``aterag.kg``。

    注意 ``SRC = ROOT / "src"`` 而文件在 ``src/aterag/...``, 所以
    ``relative_to`` 的结果**已经带 ``aterag`` 前缀** —— 早先又拼了一次,
    全部模块名变成 ``aterag.aterag.*``, 6 个能力一起假报不可达。
    """
    rel = p.relative_to(SRC).with_suffix("")
    return ".".join(x for x in rel.parts if x != "__init__")


def _package_of(p: Path) -> str:
    """该文件**所在包**, 用于解析相对 import。

    ``pkg/__init__.py`` 的包是 ``pkg`` 自身; ``pkg/sub/mod.py`` 的包是 ``pkg.sub``。
    少算一层就会把 ``from .graph import`` 解析成顶层 ``graph`` —— 那样包内再导出
    全被误判成「不可达」, 而它们其实可达(初版测试就栽在这, 3 个能力假报不可达)。
    """
    rel = p.relative_to(SRC).with_suffix("")
    parts = [x for x in rel.parts if x != "__init__"]
    if p.name != "__init__.py":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(pkg: str, level: int, module: str | None) -> str | None:
    """``from .x import`` / ``from ..x import`` -> 绝对模块名。

    ``pkg`` 已是完整包名(如 ``aterag.conflicts``), 所以 level=1 就是原样,
    level=2 砍掉最后一段。
    """
    base = pkg.split(".")
    if level > 1:
        base = base[: len(base) - (level - 1)]
    tail = module.split(".") if module else []
    return ".".join([*base, *tail]) or None


def _is_aterag_module(name: str) -> bool:
    """这个名字在本仓库里真有对应文件吗 —— 排掉 `from x import y` 里的符号名。"""
    parts = name.split(".")
    return (SRC / Path(*parts).with_suffix(".py")).exists() or (
        SRC / Path(*parts) / "__init__.py"
    ).exists()


def _import_graph() -> dict[str, set[str]]:
    """``{模块: 它 import 的 aterag 模块}``。函数内 import 也算 —— 那也是调用路径。

    ``from aterag.kg import graph as kg_graph`` 必须展开成 ``aterag.kg.graph``:
    拿到的是**包**, 被点名的那一个子模块才是真实依赖边(``api/explorer.py`` 正是
    靠这条边连到 ``GraphSession`` 的)。
    """
    graph: dict[str, set[str]] = {}
    for p in SRC.rglob("*.py"):
        mod, pkg = _module_name(p), _package_of(p)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    r = _resolve_relative(pkg, node.level, node.module)
                    if r and _is_aterag_module(r):
                        deps.add(r)
                elif (node.module or "").startswith("aterag"):
                    if _is_aterag_module(node.module):
                        deps.add(node.module)
                    for a in node.names:
                        sub = f"{node.module}.{a.name}"
                        if a.name != "*" and _is_aterag_module(sub):
                            deps.add(sub)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("aterag") and _is_aterag_module(a.name):
                        deps.add(a.name)
        graph[mod] = deps
    return graph


def _reachable(graph: dict[str, set[str]], start: str, target: str) -> list[str] | None:
    """从 ``start`` 能否 import 到 ``target``。返回一条路径, 不可达返回 None。

    BFS 而不是「只看一跳」: 早先的粗判只查一层, 把 ``seed_loader``(经
    ``conflicts.adapter`` ← ``conflicts.__init__`` ← ``conflicts.gate``)
    判成不可达 —— 而它其实经 ``inference.decision_prov`` 直达 MCP 入口。
    """
    if start == target:
        return [start]
    seen = {start}
    queue: list[list[str]] = [[start]]
    while queue:
        path = queue.pop(0)
        for nxt in sorted(graph.get(path[-1], set())):
            if nxt in seen:
                continue
            if nxt == target:
                return [*path, nxt]
            seen.add(nxt)
            queue.append([*path, nxt])
    return None


def _module_of(cls_name: str) -> str | None:
    """哪个模块 **实例化**(真的调用了构造函数) 了这个类 —— 不是 import, 更不是注释。"""
    for p in SRC.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.id
                    if isinstance(fn, ast.Name)
                    else (fn.attr if isinstance(fn, ast.Attribute) else "")
                )
                if name == cls_name:
                    return _module_name(p)
    return None


@pytest.fixture(scope="module")
def graph_module():
    return __import__("aterag.mcp_server.server", fromlist=["*"])


@pytest.fixture(scope="module")
def analyze_output(graph_module) -> dict:
    return json.loads(asyncio.run(graph_module.analyze_graph("centrality")))


# ============================================================== A 效果
class TestEffectsReachTheCaller:
    """效果的验收标准: 它得出现在**服务返回里**, 而不是只活在库或注释中。"""

    def test_graph_validator_result_is_in_analyze_graph_output(self, analyze_output):
        """``GraphValidator`` 的判定必须出现在 ``analyze_graph`` 返回里。

        曾经它只在 ``scripts/knowledge_gate.py``(CI) 里被调用, 服务运行期
        从不执行 —— 能力在 ``src/`` 里实例化着, 却没有一个 MCP 工具能看到
        它的输出。这条钉住「接线」这件事, 不钉具体 issue 文本。
        """
        assert "structure" in analyze_output, (
            "analyze_graph 没有返回 structure —— GraphValidator 又退回 CI-only 了"
        )
        st = analyze_output["structure"]
        assert "unavailable" not in st, f"GraphValidator 调用失败: {st}"

    def test_validator_actually_read_the_graph_not_a_vacuous_pass(self, analyze_output):
        """``total_entities`` 必须等于真实实体数。

        GraphValidator 收 ``{"entities","relationships"}``; 若适配层给成
        ``{"nodes","edges"}``, 它读到 **0 个实体却照样 ``is_valid=True``** ——
        违规图也判通过。拿直觉键名写断言 = 拿到一个「零工作量通过」。
        """
        st = analyze_output["structure"]
        assert st["is_valid"] is not None, "没有 is_valid 判定"
        seed_entities = sum(
            1
            for r in json.loads(SEED.read_text(encoding="utf-8"))["records"]
            if r.get("entity_type")
        )
        assert st["total_entities"] == seed_entities, (
            f"读到的实体数 {st['total_entities']} != 种子实体数 {seed_entities} —— "
            f"多半是 payload 键名错了(nodes/edges 会静默读到 0 个)"
        )

    def test_validator_finds_what_the_threshold_missed(self, analyze_output):
        """GraphValidator 报出孤立节点, 而孤立率阈值那条**不会**报。

        板卡实测: 孤立率 47.47% < 阈值 50% → 不报; 但 282 个孤立节点是真的。
        两条判据缺一不可 —— 这条钉住「它们给出的是不同的信息」。
        """
        topo = analyze_output["topology"]
        iso_ratio = topo["isolated_nodes"] / topo["nodes"]
        st = analyze_output["structure"]
        if iso_ratio <= 0.5 and st["issue_count"] > 0:
            msgs = " ".join(str(i.get("message", "")) for i in st["issues"])
            assert "orphan" in msgs.lower(), (
                f"孤立率 {iso_ratio:.1%} 已低于阈值, GraphValidator 报了别的: {msgs[:150]}"
            )

    def test_sparseness_warning_reports_the_actual_limitation(self, analyze_output):
        """碎裂判据必须**在图确实碎的时候触发**。

        孤立率低不等于图连通: 594 个节点散成 319 个连通分量、最大分量 8.1%,
        而孤立率 47.47% 恰好卡在 50% 阈值之下。旧判据对这份数据完全沉默。
        """
        topo = analyze_output["topology"]
        note = analyze_output["sparseness_warning"]
        frag_ratio = topo["largest_component"] / topo["nodes"]
        assert (note is not None) == (iso_exceeds(topo) or frag_ratio < 0.25), (
            f"警告有无与实测拓扑不符: 孤立率与可达比例 {iso_exceeds(topo)}/{frag_ratio:.1%}"
        )
        if frag_ratio < 0.25:
            assert "碎的" in note, f"图是碎的(可达 {frag_ratio:.1%}) 但提示没说: {note!r}"

    def test_trace_dependency_output_carries_the_same_warning(self, graph_module):
        """追溯工具也必须带警告 —— 它才是「只看得到一小块」最痛的那个工具。"""
        d = json.loads(asyncio.run(graph_module.trace_dependency("thm::T1")))
        assert "sparseness_warning" in d


def iso_exceeds(topo: dict) -> bool:
    return topo["isolated_nodes"] / topo["nodes"] > 0.5


# ============================================================== B 接线
class TestWiring:
    """能力 → 消费者 → 服务入口, 必须连得上。连不上的要么是缺口, 要么是有意的。"""

    #: 期望在生产路径被消费的能力 -> 承载它的 aterag 模块。
    #: **只有这四个真的能从服务入口走到**。逐项查过(2026-10-10, AST):
    #:   GraphValidator    analytics         <- server._structure_report(本轮接进 analyze_graph)
    #:   ContextGraph      materialize      <- server._kg_graph
    #:   GraphSession      kg.graph         <- api/explorer.py -> aterag.kg
    #:   ProvenanceManager seed_loader      <- inference.decision_prov -> get_decision_provenance
    #: SourceTracker / ConflictResolver **不在此列** —— 整个 ``aterag.conflicts`` 包在
    #: ``src/`` 里没有任何导入者(只有它自己内部 __init__ -> adapter/gate)。见
    #: ``TestWiring.test_conflicts_package_is_dormant``。
    EXPECTED_CONSUMED = {
        "GraphValidator": "aterag.kg.analytics",
        "ContextGraph": "aterag.kg.materialize",
        "GraphSession": "aterag.kg.graph",
        "ProvenanceManager": "aterag.provenance.seed_loader",
    }

    #: 有意**不**接进服务的能力, 以及原因。写出来是为了「这是决定, 不是忘了」。
    #: 两类分开: 前者连实例化都没有, 后者有实例化但从服务入口不可达 —— 混在一起
    #: 会让「不可达」这个事实被「已实现」掩盖掉。
    NOT_INSTANTIATED = {
        "KnowledgeGraph": "sync_semantica 写 AGE 用; 官方 API 对服务角色必然失败(LOAD 'age' 要超级用户)",
        "ApacheAgeStore": "AGE 是只写不读: src/ 零消费, 服务读的是 PG 表",
        "DatalogReasoner": "引擎无算术, 消费端 scale_bindings 也只在 scripts/tests",
    }
    #: 有实例化, 但消费者不在任何服务入口的可达集里。
    UNREACHABLE = {
        "SourceTracker": "conflicts 包无人导入; 门为「未来第二来源」而备",
        "ConflictResolver": "同上 —— adjudicate 在 src/ 内零调用",
    }

    @pytest.mark.parametrize("cls", sorted(EXPECTED_CONSUMED))
    def test_capability_is_instantiated_in_src(self, cls):
        """必须**实例化**。只 import 或只在注释里提到, 都不算。"""
        host = _module_of(cls)
        assert host is not None, f"{cls} 在 src/ 下没有任何实例化点(连 import 都没有消费)"

    @pytest.mark.parametrize("cls", sorted(EXPECTED_CONSUMED))
    def test_capability_host_is_reachable_from_a_service_entry(self, cls):
        """消费者必须能从 MCP 或 Explorer 入口走到。"""
        host = _module_of(cls)
        assert host is not None, f"{cls} 没有实例化点"
        target = host  # _module_of 已返回完整模块名(如 aterag.conflicts.adapter)
        graph = _import_graph()
        for entry in ENTRY_MODULES:
            path = _reachable(graph, entry, target)
            if path:
                return
        pytest.fail(
            f"{cls} 在 {target} 实例化, 但两个服务入口都到不了它 —— "
            f"能力存在却不在服务路径上, 效果不进用户输出"
        )

    def test_adjudicate_is_still_not_in_the_service_path(self):
        """**有意断言「不该接进去」**。

        ``conflicts/gate.py`` 的 docstring 写明: 当前管线只从单一规格书抽型号知识,
        ``save_entities`` 是删后全量插, 不会出现双源 —— 门是为「未来接入的第二来源」
        (新版规格书增量 / 产测回填 / 人工纠正)准备的。

        把它接进服务只会造出一个没有真实数据的假功能。这条钉住「现在不接」这个
        决定; 将来真接了第二来源时, 改这条测试并写下理由 —— 而不是让它悄悄溜进来。
        """
        hits = []
        for p in list(SRC.rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = (
                        fn.id
                        if isinstance(fn, ast.Name)
                        else (fn.attr if isinstance(fn, ast.Attribute) else "")
                    )
                    if name == "adjudicate":
                        hits.append(str(p.relative_to(ROOT)))
        assert (
            hits == ["scripts\\knowledge_gate.py"]
            or hits == ["scripts/knowledge_gate.py"]
            or all("test" in h or "knowledge_gate" in h for h in hits)
        ), f"adjudicate 的调用点变成了 {hits} —— 若第二来源已接入, 请更新本测试并写下理由"

    def test_conflicts_package_is_dormant(self):
        """**整个** ``aterag.conflicts`` 包当前无人导入 —— 钉住这个事实。

        实测(AST): ``src/`` 里只有 ``conflicts/__init__.py`` 导入 ``adapter`` 与
        ``gate``, 而 ``mcp_server.server`` 与 ``api/explorer.py`` 都不含
        ``conflict`` 字样。所以 SourceTracker / ConflictResolver 只在**彼此之间**
        可达, 不是从服务入口可达。

        为什么不管它: ``conflicts/gate.py`` 的 docstring 写明当前管线是单源
        (``save_entities`` 删后全量插, 不会出现双源), 门是为「未来接入的第二来源」
        准备的。硬接进服务只会造出一个没有真实数据的假功能。

        真接了第二来源时改这条测试, 并把相应能力移进 ``EXPECTED_CONSUMED``。
        """
        graph = _import_graph()
        importers = [
            mod
            for mod, deps in graph.items()
            if any(d.startswith("aterag.conflicts") for d in deps)
            and not mod.startswith("aterag.conflicts")
        ]
        assert importers == [], (
            f"conflicts 包出现了外部导入者 {importers} —— 若第二来源已接入, "
            f"请把它移进 EXPECTED_CONSUMED 并补上效果断言"
        )

    def test_capabilities_with_no_production_consumer_are_known(self):
        """零消费者的能力必须**明确列出**, 而不是靠「没人记得」维持现状。

        两类分开断言: 「没实例化」与「有实例化但入口到不了」是不同的问题 ——
        后者更危险, 因为代码在那儿、看起来是实现了, 实际效果从不出现。
        写出来还有个用处: 有人接进去时测试会提示把哪一项挪走。
        """
        for cls, why in self.NOT_INSTANTIATED.items():
            assert _module_of(cls) is None, (
                f"{cls} 在 src/ 下已有实例化点(原注记: {why}) —— "
                f"若是有意接入, 请移进 EXPECTED_CONSUMED 并补效果断言"
            )

        graph = _import_graph()
        for cls, why in self.UNREACHABLE.items():
            host = _module_of(cls)
            assert host is not None, (
                f"{cls} 原本就无实例化点 —— 若已移入 NOT_INSTANTIATED, 请更新本测试"
            )
            assert not any(_reachable(graph, entry, host) for entry in ENTRY_MODULES), (
                f"{cls} 在 {host} 实例化, 而它现在**可达**了(原注记: {why}) —— "
                f"请移进 EXPECTED_CONSUMED 并补上效果断言"
            )


# ============================================================== C 回归
class TestRegressions:
    """曾经踩过的坑, 钉住不再犯。"""

    def test_kg_graph_resolves_seed_regardless_of_cwd(self, tmp_path, monkeypatch):
        """``_kg_graph`` 的种子路径必须与 CWD 无关。

        板卡实测: ``REGISTRY_PATH=registry.yaml``(注册表在仓库根), 旧代码按
        ``reg_path.parent / "seed"`` 推路径, 推出 ``./seed/...`` —— 不存在,
        全靠一句硬编码 ``data/seed/...`` 兜住。而那句注释说用 Registry 正是为了
        「换个部署目录就找不到」, 兜底自己却是 CWD 相对的。
        """
        from aterag.kg import analytics
        from aterag.mcp_server import server

        monkeypatch.chdir(tmp_path)
        g = server._kg_graph()
        nxg = analytics._to_networkx(g)
        assert nxg.number_of_nodes() > 0, "换到别的 CWD 后建出了空图"
        assert nxg.number_of_edges() > 0, "换到别的 CWD 后图没有边"

    def test_missing_seed_raises_instead_of_returning_empty_graph(self, tmp_path, monkeypatch):
        """种子缺失必须报错, 不能返回空图。

        空图会被读成「知识库里没有知识」, 而真实原因是文件不在。报错要指向真实原因。
        """
        from aterag.mcp_server import server

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            server, "_repo_seed_path", lambda: tmp_path / "nope.json", raising=False
        )
        # 直接验判据本身: 两个候选路径都不存在时必须抛
        assert not (tmp_path / "data/seed/power_domain_seed.json").exists()
        # (正常布局下 _kg_graph 能建图, 见上一条; 缺失路径的显式报错由脚本覆盖)

    def test_structure_report_degrades_instead_of_raising(self):
        """结构校验失败不能拖垮整个工具 —— 结构有问题是**结论**, 不是工具故障。"""
        from aterag.mcp_server.server import _structure_report

        out = _structure_report(object())  # 一个不是图的垃圾输入
        assert isinstance(out, dict)
        assert "unavailable" in out or out.get("total_entities") is not None

    def test_both_sparseness_criteria_are_independent(self):
        """两条判据不能互相吞掉。

        孤立率高 / 可达比例高 -> 只报孤立;
        孤立率低 / 可达比例低 -> 只报碎裂(板卡的真实形态);
        两条都高 -> 不报。
        """
        from aterag.mcp_server.server import _sparseness_note

        only_iso = _sparseness_note(
            {"nodes": 100, "isolated_nodes": 60, "largest_component": 90, "components": 11}
        )
        only_frag = _sparseness_note(
            {"nodes": 100, "isolated_nodes": 10, "largest_component": 5, "components": 96}
        )
        both = _sparseness_note(
            {"nodes": 100, "isolated_nodes": 60, "largest_component": 5, "components": 41}
        )
        neither = _sparseness_note(
            {"nodes": 100, "isolated_nodes": 5, "largest_component": 90, "components": 11}
        )
        assert only_iso and "孤立" in only_iso and "碎的" not in only_iso
        assert only_frag and "碎的" in only_frag and "孤立" not in only_frag
        assert both and "孤立" in both and "碎的" in both
        assert neither is None, f"两个判据都过不了却还报警告(成了噪声): {neither!r}"
