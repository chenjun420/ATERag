"""ATERag MCP Server (streamable-http).

业务工具 (8): search_requirements / query_parameters / calculate /
validate_constraints / get_test_cases / get_fixture_spec / search_cases /
optimize_process
管理工具: ingest_document / build_domain_kb / get_section_tree /
list_domain_rules / health

所有业务工具的 model_id 可选: 缺省时从 query 文本自动识别型号与产品类型。
"""
from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from aterag.config import get_settings
from aterag.inference import InferenceEngine
from aterag.rag.service import RagService
from aterag.registry import AmbiguousModel, Registry, UnknownModel

settings = get_settings()
registry = Registry.load(settings)
_rag: RagService | None = None


def get_rag() -> RagService:
    global _rag
    if _rag is None:
        from aterag.models import EmbeddingClient

        _rag = RagService(settings, registry, EmbeddingClient(settings))
    return _rag


def get_llm():
    from aterag.models import LLMClient

    return LLMClient(settings)


mcp = MCPServer(
    name="power-spec-rag",
    version="0.1.0",
    instructions=(
        "产测规格书 RAG 服务: 型号隔离 + 产品类型域隔离 + 章节级过滤 + "
        "Datalog 公式计算 + SHACL 约束验证 + 决策溯源。"
        "model_id 可省略 (从 query 自动识别)。"
    ),
)


class FactUnavailable(RuntimeError):
    """型号关键事实缺失 (fail-closed)。

    产测判据不允许兜底: 规格书没声明 / 抽取失败时必须显式报错, 绝不能拿默认值
    冒充检索结果。此前 `_model_facts` 的 `or 54` 会在缺数据时静默填 54V, 而 PA601
    恰好就是 54V, 错误在当前型号下完全不可见, 换型号才暴露。
    """

    def __init__(self, model_id: str, missing: list[str]) -> None:
        self.model_id = model_id
        self.missing = missing
        super().__init__(f"{model_id} 缺少必需事实: {missing}")

    def to_dict(self) -> dict:
        return {
            "error": "missing_model_facts",
            "model_id": self.model_id,
            "missing": self.missing,
            "message": (
                f"型号 {self.model_id} 的规格书未提供以下必需事实: {self.missing}。"
                f"系统不做默认值兜底 —— 宁可报错也不返回未经规格书确认的数值。"
            ),
            "hint": (
                "检查该型号规格书是否声明『额定输出电压』『输出电流』条目, "
                "以及抽取结果 (SELECT * FROM aterag_entities WHERE model_id='"
                f"{self.model_id}' AND etype='Requirement')。"
            ),
        }


def _model_facts(model_id: str, required: tuple[str, ...] = ()) -> dict:
    """从 PG 实体提取型号关键数值供 Datalog 推导。

    反幻觉约定:
    - 数值只能来自 ``aterag_entities`` 的结构化字段, 任何分支都不得填默认值
    - 主轨 (main rail) 按"额定输出电流最大者"动态选取, 不写死轨名
      (此前写死 ``rail in ("-54V", "")``, 换 -48V 型号会静默取不到电流)
    - 实体查询异常直接向上抛, 不再 ``except: pass`` 吞掉
    - ``required`` 中的事实缺失即抛 :class:`FactUnavailable`, 由调用方转成错误上报
    """
    facts: dict = {}
    prov: dict = {}

    ents = get_rag().query_entities(model_id, etype="Requirement")  # 异常直接上抛

    volts: dict[str, float] = {}   # rail -> 额定输出电压
    currs: dict[str, float] = {}   # rail -> 额定输出电流
    for e in ents:
        title = e.get("title", "")
        rail = e.get("rail", "") or "main"
        if "额定输出电压" in title:
            raw = e.get("typ")
            if raw is None:
                raw = e.get("min") or e.get("max")
            try:
                volts.setdefault(rail, abs(float(raw)))
            except (TypeError, ValueError):
                continue
            prov[f"voltage_{rail}"] = {"req_id": e.get("req_id"), "section_path": e.get("section_path")}
        elif "输出电流" in title:
            try:
                currs[rail] = float(e["max"])
            except (KeyError, TypeError, ValueError):
                continue
            prov[f"current_{rail}"] = {"req_id": e.get("req_id"), "section_path": e.get("section_path")}
        elif "整机效率" in title and e.get("min") is not None:
            facts["efficiency_min"] = e["min"]
            prov["efficiency_min"] = {"req_id": e.get("req_id"), "section_path": e.get("section_path")}

    for rail, v in volts.items():
        facts[f"voltage_{rail}"] = v

    # 主轨 = 额定输出电流最大的轨 (即主功率轨)
    if currs:
        main_rail = max(currs, key=lambda r: currs[r])
        facts["current"] = currs[main_rail]
        facts["main_rail"] = main_rail
        if main_rail in volts:
            facts["voltage"] = volts[main_rail]

    facts["_provenance"] = prov

    lack = [r for r in required if facts.get(r) is None]
    if lack:
        raise FactUnavailable(model_id, lack)
    return facts


def _resolve_or_error(query: str, model_id: str | None):
    """型号解析 (fail-closed); 返回 (resolved, None) 或 (None, error_dict)。"""
    try:
        return get_rag().resolve(query, model_id), None
    except AmbiguousModel as e:
        return None, {"error": "ambiguous_model", "message": str(e)}
    except UnknownModel as e:
        return None, {"error": "unknown_model", "message": str(e)}


# ---------------- 业务工具 ----------------
@mcp.tool()
async def search_requirements(
    query: str,
    model_id: str = "",
    section_path: str = "",
    category: str = "all",
    priority: str = "all",
    top_k: int = 8,
) -> str:
    """检索需求项 (SR 编号/标题/判据), 支持章节/类别/优先级过滤。

    model_id 可省略, 自动从 query 识别。
    """
    r, err = _resolve_or_error(query, model_id or None)
    if err:
        return json.dumps(err, ensure_ascii=False)
    result = await get_rag().search(
        query=query,
        model_id=r.model_id,
        section_path=section_path or None,
        category=category,
        priority=priority,
        top_k=top_k,
    )
    # 附加强制需求实体 (PG 直查, 精确)
    ents = get_rag().query_entities(
        r.model_id, etype="Requirement",
        keyword=query[:40] if len(query) <= 40 else None,
        section_path=section_path or None,
    )
    result["matched_requirements"] = ents[:top_k]
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def query_parameters(
    query: str,
    model_id: str = "",
    section_path: str = "",
    param_name: str = "",
) -> str:
    """查询参数值/条件/判据 (精确实体查询 + 语义检索兜底)。"""
    r, err = _resolve_or_error(query, model_id or None)
    if err:
        return json.dumps(err, ensure_ascii=False)
    # 关键词提取: 去掉型号 ID, 逐词检索并合并 (型号 ID 会污染 ILIKE)
    rest = query.replace(r.model_id, " ") if r.model_id else query
    tokens = [t for t in rest.replace("，", " ").replace(",", " ").split() if len(t) >= 2]
    if param_name:
        tokens = [param_name] + tokens
    if not tokens:
        tokens = [query]
    ents: list[dict] = []
    prots: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for tok in tokens[:6]:
        for e in get_rag().query_entities(
            r.model_id, etype="Requirement", keyword=tok,
            section_path=section_path or None,
        ):
            key = (e.get("etype", ""), e.get("eid", ""))
            if key not in seen:
                seen.add(key)
                ents.append(e)
        for p in get_rag().query_entities(
            r.model_id, etype="Protection", keyword=tok,
            section_path=section_path or None,
        ):
            key = (p.get("etype", ""), p.get("eid", ""))
            if key not in seen:
                seen.add(key)
                prots.append(p)
    if not ents and not prots:
        fallback = await get_rag().search(
            query, model_id=r.model_id, section_path=section_path or None, top_k=6
        )
        return json.dumps({"model_id": r.model_id, "entities": [], "protections": [],
                           "semantic_fallback": fallback["results"]},
                          ensure_ascii=False, default=str)
    return json.dumps(
        {"model_id": r.model_id, "model_id_source": r.source,
         "entities": ents[:20], "protections": prots[:10]},
        ensure_ascii=False, default=str,
    )


@mcp.tool()
async def calculate(
    formula_type: str,
    model_id: str = "",
    inputs: dict | None = None,
    rule_id: str = "",
) -> str:
    """执行领域公式计算 (功率/效率/公差链/探针选型/节拍/寿命等)。

    缺失输入自动从型号事实与其他规则链式推导。
    """
    if model_id:
        if model_id not in registry.products:
            return json.dumps({"error": "unknown_model", "message": model_id})
        domain = registry.products[model_id].domain
        try:
            facts = _model_facts(model_id)
        except FactUnavailable as e:
            # 不阻断: calculate 可能只需显式 inputs; 缺事实由引擎按需报 KeyError
            facts = {"_provenance": {}, "_fact_error": e.to_dict()}
    else:
        # 无型号上下文: 仅共享规则 + 显式输入
        domain = "power"
        facts = {}
    eng = InferenceEngine(settings, domain, model_facts=facts)
    try:
        result = eng.calculate(formula_type, inputs, rule_id or None)
    except (KeyError, ValueError) as e:
        return json.dumps({"error": "calculation_failed", "message": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def validate_constraints(data_graph: str) -> str:
    """SHACL 约束验证: 传入 Turtle 数据图, 返回符合性报告。"""
    eng = InferenceEngine(settings, "power")
    try:
        result = eng.validate(data_graph)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "validation_failed", "message": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_test_cases(requirement_id: str, model_id: str = "") -> str:
    """获取/生成指定需求 (SR-xxx) 的测试用例框架。"""
    rid = requirement_id
    if not model_id:
        r, err = _resolve_or_error(rid, None)
        if err:
            return json.dumps(err, ensure_ascii=False)
        model_id = r.model_id
    ents = get_rag().query_entities(model_id, keyword=rid)

    def _pick(pred):
        return next((e for e in ents if pred(e)), None)

    def has_min(e: dict) -> bool:
        return e.get("min") is not None or e.get("max") is not None
    # 主轨动态判定: 复用 _model_facts 的"额定输出电流最大者"逻辑, 不用硬编码轨名白名单。
    # 原实现写死 ("-54V", "-48V"), 换 -12V/-28V 型号时主轨不匹配会静默降级,
    # 可能把辅助轨 (如 3.45V 0.1A) 当主轨生成判据 —— 与已修的 _model_facts 同类隐患。
    try:
        main_rail = str(_model_facts(model_id).get("main_rail", ""))
    except FactUnavailable:
        main_rail = ""
    # 优先级: 精确 req_id + 主轨 > 精确 req_id 有数值 > req_id 前缀 > 任意
    req = _pick(lambda e: str(e.get("req_id", "")) == rid and has_min(e)
                and bool(main_rail) and str(e.get("rail", "")) == main_rail)
    req = req or _pick(lambda e: str(e.get("req_id", "")) == rid and has_min(e))
    req = req or _pick(lambda e: str(e.get("req_id", "")).startswith(rid) and has_min(e))
    req = req or _pick(lambda e: rid in str(e.get("req_id", "")) or rid in e.get("eid", ""))
    if not req:
        hits = await get_rag().search(rid, model_id=model_id, top_k=5)
        return json.dumps({"model_id": model_id, "requirement": None,
                           "semantic_hits": hits["results"]}, ensure_ascii=False, default=str)
    case = {
        "testCaseId": f"TC:{model_id}:{req.get('req_id', rid)}:01",
        "requirementRef": req.get("req_id", rid),
        "section_path": req.get("section_path", ""),
        "title": f"{req.get('title', rid)} 测试",
        "criterion": _criterion_from(req),
        "priority": req.get("priority", ""),
        "steps": [
            {"step": 1, "action": f"按 {req.get('section_path', '')} 章节条件建立测试环境", "expected": "环境就绪"},
            {"step": 2, "action": f"执行 {req.get('title', rid)} 测量", "expected": "读数有效"},
            {"step": 3, "action": "比对判据", "expected": _criterion_from(req)},
        ],
        "generated_by": "aterag-rag",
    }
    return json.dumps({"model_id": model_id, "requirement": req, "test_case": case},
                      ensure_ascii=False, default=str)


def _safe_calc(eng, formula_type: str, inputs: dict) -> dict:
    """规则求值失败时返回结构化错误, 而非抛异常中断整个工具。

    注意: 不做默认值兜底 —— 缺输入就是缺输入, 错误信息原样透出给调用方。
    """
    try:
        return eng.calculate(formula_type, inputs)
    except (KeyError, ValueError) as e:
        return {"error": "calculation_failed", "formula_type": formula_type, "message": str(e)}


def _criterion_from(req: dict) -> str:
    mn, mx = req.get("min"), req.get("max")
    unit = req.get("unit", "")
    if mn is not None and mx is not None:
        return f"{mn} ≤ 值 ≤ {mx} {unit}"
    if mn is not None:
        return f"值 ≥ {mn} {unit}"
    if mx is not None:
        return f"值 ≤ {mx} {unit}"
    return str(req.get("notes", ""))[:80]


@mcp.tool()
async def get_fixture_spec(model_id: str, query: str = "探针选型 工装参数") -> str:
    """获取工装规格与探针选型建议 (基于领域规则 + 型号参数)。

    缺型号事实 (额定输出电压/输出电流) 时 fail-closed 报错, 不用默认值兜底。
    公差链需要调用方传入 components —— 不再内置示例分量, 避免示例值被当成实测值。
    """
    if model_id not in registry.products:
        return json.dumps({"error": "unknown_model", "message": model_id})
    try:
        facts = _model_facts(model_id, required=("voltage", "current"))
    except FactUnavailable as e:
        return json.dumps(e.to_dict(), ensure_ascii=False)
    eng = InferenceEngine(settings, registry.products[model_id].domain, model_facts=facts)
    out: dict = {"model_id": model_id, "facts": facts}
    for ft, inp in (
        ("probe_selection", {"current": facts["current"]}),
        ("fixture_precision", {"param_tolerance": 0.3}),
    ):
        try:
            out[ft] = eng.calculate(ft, inp)
        except (KeyError, ValueError) as e:
            out[ft] = {"error": str(e)}
    # 公差链: 无型号公差数据时不计算, 显式说明缺什么 (原实现硬编码 [0.3, 0.3] 示例值)
    out["tolerance"] = {
        "status": "not_computed",
        "reason": "公差链需各测点公差分量, 规格书未提供结构化公差数据",
        "required_input": "components=[各测点公差百分比, ...]",
        "note": "如需计算请用 calculate(formula_type='tolerance', inputs={'components': [...]}) 显式传入",
    }
    hits = await get_rag().search(query, model_id=model_id, top_k=5)
    out["references"] = hits["results"]
    return json.dumps(out, ensure_ascii=False, default=str)


@mcp.tool()
async def search_cases(query: str, model_id: str = "", top_k: int = 5) -> str:
    """检索历史工装/测试案例 (跨型号共享层 + 当前型号)。"""
    r, err = _resolve_or_error(query, model_id or None)
    if err:
        return json.dumps(err, ensure_ascii=False)
    result = await get_rag().search(query, model_id=r.model_id, top_k=top_k)
    shared = [h for h in result["results"] if h.get("layer") in ("domain", "common")]
    model_hits = [h for h in result["results"] if h.get("layer") == "model"]
    return json.dumps(
        {"model_id": r.model_id, "domain": r.domain,
         "model_cases": model_hits, "domain_knowledge": shared},
        ensure_ascii=False, default=str,
    )


@mcp.tool()
async def optimize_process(
    model_id: str,
    target_throughput: int = 1000,
    test_time_s: int = 120,
    probe_rated_life: int | None = None,
    probe_used_count: int | None = None,
) -> str:
    """工艺优化建议: 通道数/探针寿命/节拍 (基于领域规则计算)。

    探针寿命依赖实际探针批次的使用次数, 属工装台账数据而非规格书数据, 因此不内置
    示例值: 未显式传入 probe_rated_life / probe_used_count 时该项标记 not_computed。
    """
    if model_id not in registry.products:
        return json.dumps({"error": "unknown_model", "message": model_id})
    try:
        facts = _model_facts(model_id, required=("current",))
    except FactUnavailable as e:
        return json.dumps(e.to_dict(), ensure_ascii=False)
    eng = InferenceEngine(settings, registry.products[model_id].domain, model_facts=facts)
    out: dict = {"model_id": model_id, "facts": facts}
    out["channel_count"] = _safe_calc(
        eng, "channel_count",
        {"throughput": target_throughput, "test_time": test_time_s, "available_time": 86400},
    )
    if probe_rated_life is not None and probe_used_count is not None:
        out["probe_life"] = _safe_calc(
            eng, "probe_life",
            {"rated_life": probe_rated_life, "used_count": probe_used_count},
        )
    else:
        out["probe_life"] = {
            "status": "not_computed",
            "reason": "探针寿命需工装台账的实际使用次数, 非规格书数据",
            "required_input": "probe_rated_life / probe_used_count",
        }
    out["advice"] = [
        "按所需通道数配置工装并预留 20% 冗余",
        "探针剩余寿命低于阈值时触发更换 (K-FIX-007), 需传入台账数据后计算",
        f"当前型号 domain={registry.products[model_id].domain}, 推导链已记入决策溯源",
    ]
    return json.dumps(out, ensure_ascii=False, default=str)


# ---------------- 管理工具 ----------------
@mcp.tool()
async def ingest_document(doc_path: str) -> str:
    """导入产品规格书 (MD), 自动识别型号与产品类型, 构建型号知识库。"""
    from aterag.ingest.pipeline import ingest_spec
    from aterag.models import EmbeddingClient

    try:
        result = await ingest_spec(
            doc_path, settings, registry, EmbeddingClient(settings), get_llm()
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "ingest_failed", "message": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def build_domain_kb(domain: str) -> str:
    """构建/更新产品类型通用知识库 (如 power)。"""
    from aterag.ingest.pipeline import build_domain
    from aterag.models import EmbeddingClient

    try:
        result = await build_domain(
            domain, settings, registry, EmbeddingClient(settings), get_llm()
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "build_failed", "message": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_models() -> str:
    """列出已注册型号与产品类型域。"""
    return json.dumps(
        {
            "products": {k: v.domain for k, v in registry.products.items()},
            "domains": {k: v.kb_status for k, v in registry.domains.items()},
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def list_domain_rules(category: str = "") -> str:
    """列出可用的领域公式/约束规则 (含来源与置信度)。"""
    from aterag.inference.rules import load_domain_rules

    rules, shapes = load_domain_rules(settings.domain_rules_dir, "power")
    out = []
    for r in rules:
        if category and r.get("category") != category:
            continue
        out.append({
            "id": r.get("id"),
            "statement": r.get("statement"),
            "category": r.get("category"),
            "domain": r.get("_domain"),
            "has_formula": bool((r.get("derive") or {}).get("expr")),
            "has_constraint": bool((r.get("constraint") or {}).get("shape")),
            "confidence": r.get("confidence"),
            "source": r.get("source"),
        })
    return json.dumps({"rules": out, "shacl_shapes": len(shapes)}, ensure_ascii=False)


@mcp.tool()
async def health() -> str:
    """服务健康检查 (存储/模型全量自检)。"""
    from aterag.checks import report, run_all_checks

    results = await run_all_checks(settings)
    return json.dumps({"ok": all(r.ok for r in results), "report": report(results)},
                      ensure_ascii=False)


def main() -> None:
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
