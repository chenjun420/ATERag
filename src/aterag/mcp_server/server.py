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
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from aterag.config import get_settings
from aterag.extract.quantity_aliases import QuantityAliasBook
from aterag.inference import InferenceEngine
from aterag.inference.decision_explain import explain, to_json_dict
from aterag.inference.decision_prov import DecisionRecorder
from aterag.inference.decision_prov import get_decision_provenance as _query_decision_provenance
from aterag.rag.service import RagService
from aterag.registry import AmbiguousModel, Registry, UnknownModel

settings = get_settings()
registry = Registry.load(settings)
_rag: RagService | None = None
_decision_prov: DecisionRecorder | None = None
_aliases: QuantityAliasBook | None = None


def _alias_book() -> QuantityAliasBook:
    """标题别名表, 全局一份 (进程内配置不会变, 每次重载只是白费 IO)。"""
    global _aliases
    if _aliases is None:
        _aliases = QuantityAliasBook.load(settings.quantity_aliases_path)
    return _aliases


def get_decision_recorder() -> DecisionRecorder:
    """``calculate`` 的记账通道, 全局一份。

    板卡实测教训: 读路径每次泄漏一个事务会让 PG 写入卡死在
    ``wait=Lock/transactionid``。所以 manager 必须是**全局单例**, 而不是每条
    calculate 一个 —— 它内部走 autocommit 连接, 单例复用是安全的。
    """
    global _decision_prov
    if _decision_prov is None:
        from aterag.provenance import build_manager

        _decision_prov = DecisionRecorder(build_manager(settings.postgres_dsn))
    return _decision_prov


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


def _model_facts(
    model_id: str, required: tuple[str, ...] = (), aliases: "QuantityAliasBook | None" = None
) -> dict:
    """从 PG 实体提取型号关键数值供 Datalog 推导。

    反幻觉约定:
    - 数值只能来自 ``aterag_entities`` 的结构化字段, 任何分支都不得填默认值
    - 标题怎么认由 ``config/quantity_aliases.yaml`` 声明 (方案 §11.7), 不写死
      子串: 规格书换个措辞("标称输出电压")时, 取数应当仍然成立, 而不是静默变空
    - 主轨 (main rail) 按"额定输出电流最大者"动态选取, 不写死轨名
      (此前写死 ``rail in ("-54V", "")``, 换 -48V 型号会静默取不到电流)
    - 实体查询异常直接向上抛, 不再 ``except: pass`` 吞掉
    - ``required`` 中的事实缺失即抛 :class:`FactUnavailable`, 由调用方转成错误上报

    ``aliases`` 参数只给测试注入用; 运行期走配置路径加载。
    """
    facts: dict = {}
    prov: dict = {}

    ents = get_rag().query_entities(model_id, etype="Requirement")  # 异常直接上抛
    book = aliases or _alias_book()

    volts: dict[str, float] = {}  # rail -> 额定输出电压
    currs: dict[str, float] = {}  # rail -> 额定输出电流
    for e in ents:
        title = e.get("title", "")
        rail = e.get("rail", "") or "main"
        spec = book.match_fact(title)
        if spec is None:
            continue
        # 别名跨事实互斥已在加载期保证 (QuantityAliasBook.validate), 命中即唯一。
        val = spec.value_of(e)
        if val is None:
            # 有这个标题却取不出值 -> 跳过但不猜。值只能来自实体的结构化字段。
            continue
        if spec.name == "voltage":
            volts.setdefault(rail, val)
            prov[f"voltage_{rail}"] = {
                "req_id": e.get("req_id"),
                "section_path": e.get("section_path"),
                "title": title,
            }
        elif spec.name == "current":
            currs[rail] = val
            prov[f"current_{rail}"] = {
                "req_id": e.get("req_id"),
                "section_path": e.get("section_path"),
                "title": title,
            }
        else:
            facts[spec.name] = val
            prov[spec.name] = {
                "req_id": e.get("req_id"),
                "section_path": e.get("section_path"),
                "title": title,
            }

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
        r.model_id,
        etype="Requirement",
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
            r.model_id,
            etype="Requirement",
            keyword=tok,
            section_path=section_path or None,
        ):
            key = (e.get("etype", ""), e.get("eid", ""))
            if key not in seen:
                seen.add(key)
                ents.append(e)
        for p in get_rag().query_entities(
            r.model_id,
            etype="Protection",
            keyword=tok,
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
        return json.dumps(
            {
                "model_id": r.model_id,
                "entities": [],
                "protections": [],
                "semantic_fallback": fallback["results"],
            },
            ensure_ascii=False,
            default=str,
        )
    return json.dumps(
        {
            "model_id": r.model_id,
            "model_id_source": r.source,
            "entities": ents[:20],
            "protections": prots[:10],
        },
        ensure_ascii=False,
        default=str,
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
    eng = InferenceEngine(settings, domain, model_facts=facts, decision_recorder=get_decision_recorder())
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
async def get_decision_provenance(decision_id: str) -> str:
    """按 decision_id 查推理谱系 (calculate 决策的持久化审计).

    calculate 每次都把推理链写进 ``l0_term.provenance``:
    用了哪条规则 / 输入数值来自哪个 SR 条目 / 出处可信度多少。
    进程重启后仍可查 —— 内存里的 explain() 只覆盖本进程。
    """
    try:
        entry = _query_decision_provenance(settings.postgres_dsn, decision_id)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "provenance_query_failed", "message": str(e)}, ensure_ascii=False)
    if entry is None:
        return json.dumps({"error": "decision_not_found", "decision_id": decision_id}, ensure_ascii=False)
    return json.dumps(entry, ensure_ascii=False, default=str)


@mcp.tool()
async def explain_decision(decision_id: str) -> str:
    """把一条决策谱系讲成人读的审计文本 (calculate 的解释侧)。

    ``get_decision_provenance`` 给原始行; 这里给**推理路径**: 每个输入
    数值 <- 它的 SR 条目、用的哪条规则、出处可信度多少。
    """
    try:
        entry = _query_decision_provenance(settings.postgres_dsn, decision_id)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "provenance_query_failed", "message": str(e)}, ensure_ascii=False)
    if entry is None:
        return json.dumps({"error": "decision_not_found", "decision_id": decision_id}, ensure_ascii=False)
    explanation, audit_text = explain(entry)
    return json.dumps(
        {
            "decision_id": decision_id,
            "audit_text": audit_text,
            # to_json_dict 而不是 __dict__: 后者会把推理路径序列化成一行
            # Python repr(板上实测), 机器侧拿不到可解析的 JSON
            "explanation": to_json_dict(explanation),
        },
        ensure_ascii=False,
        default=str,
    )


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
    req = _pick(
        lambda e: (
            str(e.get("req_id", "")) == rid
            and has_min(e)
            and bool(main_rail)
            and str(e.get("rail", "")) == main_rail
        )
    )
    req = req or _pick(lambda e: str(e.get("req_id", "")) == rid and has_min(e))
    req = req or _pick(lambda e: str(e.get("req_id", "")).startswith(rid) and has_min(e))
    req = req or _pick(lambda e: rid in str(e.get("req_id", "")) or rid in e.get("eid", ""))
    if not req:
        hits = await get_rag().search(rid, model_id=model_id, top_k=5)
        return json.dumps(
            {"model_id": model_id, "requirement": None, "semantic_hits": hits["results"]},
            ensure_ascii=False,
            default=str,
        )
    case = {
        "testCaseId": f"TC:{model_id}:{req.get('req_id', rid)}:01",
        "requirementRef": req.get("req_id", rid),
        "section_path": req.get("section_path", ""),
        "title": f"{req.get('title', rid)} 测试",
        "criterion": _criterion_from(req),
        "priority": req.get("priority", ""),
        "steps": [
            {
                "step": 1,
                "action": f"按 {req.get('section_path', '')} 章节条件建立测试环境",
                "expected": "环境就绪",
            },
            {"step": 2, "action": f"执行 {req.get('title', rid)} 测量", "expected": "读数有效"},
            {"step": 3, "action": "比对判据", "expected": _criterion_from(req)},
        ],
        "generated_by": "aterag-rag",
    }
    return json.dumps(
        {"model_id": model_id, "requirement": req, "test_case": case},
        ensure_ascii=False,
        default=str,
    )


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
    eng = InferenceEngine(settings, registry.products[model_id].domain, model_facts=facts, decision_recorder=get_decision_recorder())
    out: dict = {"model_id": model_id, "facts": facts}
    for ft, inp in (
        ("probe_selection", {"current": facts["current"]}),
        ("fixture_precision", {"param_tolerance": 0.3}),
        # 工装负载余量: 只填 DUT 侧, 负载额定是工装台账数据。
        # 缺的那半不填默认值 —— 填了就等于「负载够用」被当成算过的结论。
        ("load_current_margin", {"dut_rated_current": facts["current"]}),
    ):
        try:
            out[ft] = eng.calculate(ft, inp)
        except (KeyError, ValueError) as e:
            out[ft] = {"error": str(e)}
    # 工装侧输入缺失的公式: 逐条点名缺什么。「没列」与「不适用」在返回值上
    # 一模一样, 而人无法区分 —— 所以缺也要出现在输出里。
    out["needs_fixture_input"] = {
        "max_load_risetime": "dut_response_time (被测电源瞬态响应时间; 规格书未给)",
        "fault_coverage_complete": "can_inject_open / can_inject_short_inter_channel / can_inject_short_to_rail (工装能力台账)",
        "default_path_continuous": "default_state (故障注入通道默认态; 工装台账)",
        "switch_current_margin": "switch_current_rating / injected_fault_current (切换矩阵额定; 工装台账)",
        "note": "用 calculate(formula_type=..., inputs={...}) 显式传入即可算",
    }
    # 公差链: 无型号公差数据时不计算, 显式说明缺什么 (原实现硬编码 [0.3, 0.3] 示例值)
    out["tolerance"] = {
        "status": "not_computed",
        "reason": "公差链需各测点公差分量, 规格书未提供结构化公差数据",
        "required_input": "components=[各测点公差百分比, ...]",
        "note": "如需计算请用 calculate(formula_type='tolerance', inputs={'components': [...]}) 显式传入",
    }
    # fixture 类目规则清单: 此前 18 条 K-FIX 一条都不可达 —— 规则在库里有出处有
    # selftest, 但调用方只被告知其中 2 条能算。「有什么可用」必须是可查的事实。
    from aterag.inference.rules import load_domain_rules

    fx_rules, _shapes = load_domain_rules(
        settings.domain_rules_dir, registry.products[model_id].domain
    )
    out["fixture_rules"] = [
        {
            "id": r.get("id"),
            "statement": r.get("statement"),
            "scope": r.get("scope"),
            "computable": bool((r.get("derive") or {}).get("expr")),
            "constraint": bool((r.get("constraint") or {}).get("shape")),
            "confidence": r.get("confidence"),
            "source": (r.get("source") or {}).get("name"),
            "url": (r.get("source") or {}).get("url"),
        }
        for r in fx_rules
        if r.get("category") == "fixture"
    ]
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
    shared = [h for h in result["results"] if h.get("layer") in ("domain",)]
    model_hits = [h for h in result["results"] if h.get("layer") == "model"]
    return json.dumps(
        {
            "model_id": r.model_id,
            "domain": r.domain,
            "model_cases": model_hits,
            "domain_knowledge": shared,
        },
        ensure_ascii=False,
        default=str,
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
    eng = InferenceEngine(settings, registry.products[model_id].domain, model_facts=facts, decision_recorder=get_decision_recorder())
    out: dict = {"model_id": model_id, "facts": facts}
    out["channel_count"] = _safe_calc(
        eng,
        "channel_count",
        {"throughput": target_throughput, "test_time": test_time_s, "available_time": 86400},
    )
    if probe_rated_life is not None and probe_used_count is not None:
        out["probe_life"] = _safe_calc(
            eng,
            "probe_life",
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


@mcp.tool()
async def extract_test_conditions(
    model_id: str = "",
    profile: str = "",
    section_keyword: str = "",
    source: str = "blocks",
    include_excluded: bool = False,
) -> str:
    """抽取产测输入/输出条件 (激励->响应对), 按章节关键字过滤并剔除"不要求"条目。

    输入条件 = 产品依赖的外部状态 (供电/环境温度/负载/命令/故障激励);
    输出条件 = 产品自身信号状态 (电压轨/告警信号/遥测值/保护动作/时序)。

    model_id 省略时取注册表里第一个型号。section_keyword 覆盖档案默认关键字
    (如 "功能/性能要求")。source=blocks 走离线确定性通道, postgres 读 RAG 落库实体。
    剔除项默认不返回, include_excluded=True 时附上, 便于人工核对剔了什么、为什么剔。
    """
    from aterag.extract import (
        PatternBook,
        ProfileBook,
        load_annotations,
    )
    from aterag.extract import (
        extract_test_conditions as _extract,
    )
    from aterag.extract.api import DocProfile

    mid = model_id or (sorted(registry.products)[0] if registry.products else "")
    if not mid:
        return json.dumps(
            {"error": "no_model", "message": "注册表为空, 请先导入规格书"}, ensure_ascii=False
        )
    if mid not in registry.products:
        return json.dumps(
            {
                "error": "model_not_registered",
                "model_id": mid,
                "registered": sorted(registry.products),
            },
            ensure_ascii=False,
        )
    try:
        profiles = ProfileBook.load(settings.doc_profiles_path)
        if section_keyword:
            base = profiles.get(profile or None)
            profiles.profiles[base.name] = DocProfile(
                name=base.name,
                section_keywords=(section_keyword,),
                exclude_words=base.exclude_words,
                include_prose=base.include_prose,
                section_priors=base.section_priors,
                default_role=base.default_role,
                default_limits_to=base.default_limits_to,
                description=base.description,
            )
        result = _extract(
            mid,
            doc_version=registry.products[mid].doc_version,
            profile_name=profile or None,
            profiles=profiles,
            patterns=PatternBook.load(settings.condition_patterns_path),
            annotations=load_annotations(mid, settings.annotations_dir),
            source=source,
            dsn=settings.postgres_dsn,
        )
    except Exception as e:  # noqa: BLE001
        # fail-closed: 归档/章节不匹配等一律显式报错, 不用空结果冒充"该章节无产测条件"
        return json.dumps(
            {"error": type(e).__name__, "message": str(e), "model_id": mid},
            ensure_ascii=False,
        )
    out = result.to_dict()
    if not include_excluded:
        out["excluded"] = out["excluded"][:0]
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
        out.append(
            {
                "id": r.get("id"),
                "statement": r.get("statement"),
                "category": r.get("category"),
                "domain": r.get("_domain"),
                "has_formula": bool((r.get("derive") or {}).get("expr")),
                "has_constraint": bool((r.get("constraint") or {}).get("shape")),
                "confidence": r.get("confidence"),
                "source": r.get("source"),
            }
        )
    return json.dumps({"rules": out, "shacl_shapes": len(shapes)}, ensure_ascii=False)


def _kg_graph():
    """从种子构建分析用图 (方案 §4.4)。

    每次重建而不是缓存: ``materialize.py`` 已论证「落盘/缓存 = 第二份副本,
    忘了同步就出现界面旧知识/推理新知识」, 而本图只有 641 节点 / 65 边, 重建是
    毫秒级 —— 为省这点时间引入副本不划算。

    种子路径走 :class:`Registry` 解析出的同一套兜底规则, 而不是自己拼
    ``registry_path`` 的父目录 —— 那只在本机成立(``data/``), 换个部署目录就找不到,
    而症状是「图分析报空」。
    """
    from aterag.kg import analytics
    from aterag.kg.materialize import load_seed_records

    reg_path = Registry._resolve_path(settings)
    seed = reg_path.parent / "seed" / "power_domain_seed.json"
    if not seed.exists():
        seed = Path("data/seed/power_domain_seed.json")
    ents, rels = load_seed_records(str(seed))
    return analytics.graph_from_records([*ents, *rels])


#: 图稀疏到这个比例以下, 分析结论就不该被当真 —— 返回里必须带这句。
#: 实测 87%。设这个阈值不是为了让结论好看, 而是让「结论不可信」这件事出现在
#: 输出里, 而不是留在实现者的脑子里。
SPARSE_GRAPH_RATIO = 0.5


def _sparseness_note(topo: dict) -> str | None:
    n = topo.get("nodes") or 0
    if not n:
        return None
    ratio = topo.get("isolated_nodes", 0) / n
    if ratio <= SPARSE_GRAPH_RATIO:
        return None
    return (
        f"注意: {topo['isolated_nodes']}/{n} 个节点孤立 ({ratio:.0%}), 最大连通分量仅 "
        f"{topo['largest_component']}。下面的排名与追溯在这种稀疏度下信息量有限 —— "
        f"大量分数为 0 是因为节点没进任何边, 不是因为它不重要。"
    )


@mcp.tool()
async def analyze_graph(metric: str = "centrality") -> str:
    """知识图谱分析 (方案 §4.4)。metric 目前支持 centrality。"""
    from aterag.kg import analytics

    if metric != "centrality":
        return json.dumps(
            {
                "error": "unknown_metric",
                "message": f"不支持的 metric: {metric!r}",
                "hint": "目前实现: centrality (结构健康请跑 scripts/knowledge_gate.py)",
            },
            ensure_ascii=False,
        )
    try:
        graph = _kg_graph()
        topo = analytics.topology(graph)
        report = analytics.centrality_report(graph)
    except Exception as e:  # noqa: BLE001 分析失败不能报成「分析结果为空」
        return json.dumps(
            {"error": "graph_analysis_failed", "message": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "metric": metric,
            "topology": topo,
            "sparseness_warning": _sparseness_note(topo),
            **report,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def trace_dependency(
    node_id: str, direction: str = "downstream", max_depth: int = 5
) -> str:
    """追溯某节点在知识图谱里的依赖 (方案 §4.4)。

    direction:
      downstream —— 我引用了谁 (概念依据哪份标准)
      upstream   —— 谁引用了我 (某定理由哪条公理推出; **has_theorem 边要反着走**)

    方向读反不会报错, 只会得到一个看起来合理的空答案 —— 所以两个方向都显式返回。
    """
    from aterag.kg import analytics

    if direction not in {"downstream", "upstream"}:
        return json.dumps(
            {
                "error": "bad_direction",
                "message": f"direction 只能是 downstream / upstream, 实际 {direction!r}",
            },
            ensure_ascii=False,
        )
    try:
        graph = _kg_graph()
        topo = analytics.topology(graph)
        res = analytics.trace_dependencies(
            graph, node_id, direction=direction, max_depth=max_depth
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {"error": "trace_failed", "message": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "sparseness_warning": _sparseness_note(topo),
            **res,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def get_condition_detail(
    model_id: str = "",
    req_id: str = "",
    profile: str = "",
) -> str:
    """查单条需求抽出的全部输入/输出条件, 附规格书原文与判据来源。

    回答"这条需求到底测什么、判据从哪来"。逐条给出 kind / 原文 / 结构化值 /
    source / status, 而不是汇总数字 —— 汇总看不出某个判据是规格书写的还是
    业界方法补的提案。

    反幻觉约定(与本文件其余工具一致): 只回传抽取到的值, 缺失字段给 null,
    绝不补默认值。任何"看起来合理"的猜测都会变成产线上的假判据。
    """
    mid = _require_model(model_id)
    result = _extract_for(mid, profile=profile)
    target = _find_requirement(result, req_id)
    if target is None:
        siblings = _sibling_rows(result, req_id)
        return json.dumps(
            {
                "error": (
                    "requirement_ambiguous" if siblings else "requirement_not_found"
                ),
                "req_id": req_id,
                "message": (
                    "该规格编号下有多行(不同轨/限值形态), 请用带消歧后缀的编号重查"
                    if siblings
                    else "未找到该需求"
                ),
                "siblings": siblings,
                "available": sorted({c.req_id for c in result.conditions})[:50],
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "model_id": mid,
            "req_id": target.req_id,
            "title": target.title,
            "section_path": target.section_path,
            "rail": target.rail,
            "role": target.role,
            "notes": target.notes,
            "limits": target.limits,
            "flags": target.flags,
            "assessment": _assessment_of(result, target.req_id),
            "input_conditions": [_clause_payload(c) for c in target.input_conditions],
            "output_conditions": [_clause_payload(c) for c in target.output_conditions],
            "scenarios": [
                {
                    "scenario_id": s.scenario_id,
                    "seq": getattr(s, "seq", 0),
                    "name": s.name,
                    "rail": s.rail,
                    "bindings": s.bindings,
                    "derived": s.derived,
                    "basis": s.basis,
                    "source": getattr(s, "source", ""),
                }
                for s in _scenarios_of(result, target.req_id)
            ],
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def list_pending_review(
    model_id: str = "",
    profile: str = "",
    status: str = "draft",
) -> str:
    """列出待人审的条件 (业界补齐提案), 供评审工作台排工。

    这是"红线"的执行面: status=draft 的条件不得作为产测判据, 因此下游规划器
    会把它们排除在执行序列之外。评审员需要知道被排除的到底有多少、都是什么。

    只读。不提供"自动批准"工具 —— 未签字的条件变成判据, 必须有人签字,
    这个动作不能由 Agent 代劳。
    """
    mid = _require_model(model_id)
    result = _extract_for(mid, profile=profile)
    out: list[dict] = []
    for cond in result.conditions:
        for clause in list(cond.input_conditions) + list(cond.output_conditions):
            if clause.status != status:
                continue
            out.append(
                {
                    "req_id": cond.req_id,
                    "title": cond.title,
                    "section_path": cond.section_path,
                    "role": clause.role,
                    "kind": clause.kind,
                    "text": clause.text,
                    "value": clause.value,
                    "source": clause.source,
                    "confidence": clause.confidence,
                    "status": clause.status,
                    "method_ref": clause.method_ref,
                }
            )
    by_method: dict[str, int] = {}
    for c in out:
        by_method[c["method_ref"] or "(none)"] = by_method.get(c["method_ref"] or "(none)", 0) + 1
    return json.dumps(
        {
            "model_id": mid,
            "status_filter": status,
            "count": len(out),
            "by_method_ref": by_method,
            "items": out[:200],
            "truncated": len(out) > 200,
            "note": (
                "这些条件未经人审, 已排除在产测执行序列之外。"
                "评审签字后重新导出 bundle 即可纳入。"
            ),
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def get_coverage_summary(model_id: str = "", profile: str = "") -> str:
    """抽取覆盖统计: 需求数 / 条件数 / 充分性分布 / 剔除原因 / 单边限值占比。

    回答"这份规格书被处理成了什么样, 哪些地方还需要人看"。数字全部来自
    抽取结果的结构化字段, 任何一项为空就报 null 而不是 0 —— "剔除 0 条"和
    "没统计到"是两回事, 混为一谈会让人以为覆盖完整。
    """
    mid = _require_model(model_id)
    result = _extract_for(mid, profile=profile)

    verdicts: dict[str, int] = {}
    for a in getattr(result, "assessments", []) or []:
        v = getattr(a, "verdict", "") or "(none)"
        verdicts[v] = verdicts.get(v, 0) + 1

    kinds: dict[str, int] = {}
    sides = {"input": 0, "output": 0}
    for cond in result.conditions:
        for clause in cond.input_conditions:
            kinds[clause.kind] = kinds.get(clause.kind, 0) + 1
            sides["input"] += 1
        for clause in cond.output_conditions:
            kinds[clause.kind] = kinds.get(clause.kind, 0) + 1
            sides["output"] += 1

    excl_reasons: dict[str, int] = {}
    for e in result.excluded:
        r = e.reason or "(none)"
        excl_reasons[r] = excl_reasons.get(r, 0) + 1

    both = sum(
        1
        for c in result.conditions
        if c.input_conditions and c.output_conditions
    )
    one_sided = sum(
        1
        for c in result.conditions
        if bool(c.input_conditions) != bool(c.output_conditions)
    )
    return json.dumps(
        {
            "model_id": mid,
            "doc_version": registry.products[mid].doc_version,
            "requirements": len(result.conditions),
            "clauses": sides["input"] + sides["output"],
            "clauses_by_side": sides,
            "kinds": kinds,
            "kind_count": len(kinds),
            "sufficiency": verdicts or None,
            "both_sides": both,
            "one_sided": one_sided,
            "excluded": len(result.excluded),
            "excluded_by_reason": excl_reasons or None,
            "needs_review": len(result.needs_review),
        },
        ensure_ascii=False,
    )


def _require_model(model_id: str) -> str:
    """Resolve a model id, refusing to guess.

    The tools below return extracted criteria that end up as production
    pass/fail bounds. Defaulting to "the first registered model" when the
    caller named none is how the wrong product's numbers reach a line — the
    existing tools allow it for exploratory use, but anything a plan is built
    from must name its model.
    """
    if model_id:
        if model_id not in registry.products:
            raise ValueError(
                f"型号未注册: {model_id}; 已注册: {sorted(registry.products)}"
            )
        return model_id
    raise ValueError(
        "必须显式指定 model_id。这些工具的输出会作为产测判据, "
        "落到第一个注册型号上等于把别家的数值送上线。"
    )


def _extract_for(model_id: str, *, profile: str = ""):
    """Run the deterministic extraction for a model (no LLM)."""
    from aterag.extract import extract_test_conditions as _extract

    return _extract(
        model_id,
        doc_version=registry.products[model_id].doc_version,
        profile_name=profile or None,
    )


def _find_requirement(result, req_id: str):
    """Resolve a requirement code to exactly one row, or None.

    Returning the first of several matches would be a silent lie. PA601's
    ``SR-PA601-D54A-1100`` spans four rows (different rails / limit forms), and
    an agent asking "what does this test?" would get one of them with no hint
    that the others exist — then quote it as the requirement's criteria. None
    is the honest answer; the caller re-asks with the bundle's disambiguated
    code, which is unique.
    """
    exact = [c for c in result.conditions if c.req_id == req_id]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    # The bundle disambiguates multi-row codes; match on the part before the
    # disambiguating suffix so callers can use the plain spec number.
    stem = req_id.split("__")[0]
    hits = [c for c in result.conditions if c.req_id.split("__")[0] == stem]
    return hits[0] if len(hits) == 1 else None


def _sibling_rows(result, req_id: str) -> list[dict]:
    """Rows sharing a spec number, so an ambiguous lookup can say what it saw."""
    stem = req_id.split("__")[0]
    out = [
        {
            "req_id": c.req_id,
            "title": c.title,
            "rail": c.rail,
            "limits": c.limits,
        }
        for c in result.conditions
        if c.req_id.split("__")[0] == stem
    ]
    return out if len(out) > 1 else []


def _assessment_of(result, req_id: str) -> dict | None:
    for a in getattr(result, "assessments", []) or []:
        if getattr(a, "req_id", "") == req_id:
            return {
                "verdict": getattr(a, "verdict", ""),
                "rule_id": getattr(a, "rule_id", ""),
                "basis": getattr(a, "basis", ""),
                "detail": getattr(a, "detail", ""),
            }
    return None


def _scenarios_of(result, req_id: str) -> list:
    """All scenarios of one requirement, ordered by ``seq``.

    ``seq`` is what the downstream case_code is built from, so a response that
    omitted it would give the caller no way to tell which case it is looking at.
    """
    out = [s for s in getattr(result, "scenarios", []) or [] if getattr(s, "req_id", "") == req_id]
    out.sort(key=lambda s: getattr(s, "seq", 0))
    return out


def _clause_payload(clause) -> dict:
    return {
        "kind": clause.kind,
        "text": clause.text,
        "role": clause.role,
        "value": clause.value,
        "source": clause.source,
        "confidence": clause.confidence,
        "status": clause.status,
        "method_ref": clause.method_ref,
    }


@mcp.tool()
async def health() -> str:
    """服务健康检查 (存储/模型全量自检)。"""
    from aterag.checks import report, run_all_checks

    results = await run_all_checks(settings)
    return json.dumps(
        {"ok": all(r.ok for r in results), "report": report(results)}, ensure_ascii=False
    )


def main() -> None:
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
