"""硬编码清理回归验证: 主轨判定不得依赖轨名白名单 + 可信度不得默认顶格.

覆盖三条已修的硬编码:
  1. server.py  曾写死 ("-54V", "-48V") 轨名白名单 -> 换 -12V/-28V 型号会静默把辅助轨当主轨
  2. engine.py  缺 confidence 时默认 1.0 (最高可信) -> 方向反了, 应为 None(未知)
  3. service.py 未知 workspace 曾标 "model" -> 应标 "unregistered"

同时用源码扫描兜底: 白名单字面量再出现即红灯。

用法: .venv\\Scripts\\python.exe scripts\\verify_no_hardcoded.py
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x")
os.environ.setdefault("LLM_BASE", "http://x/v1")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBED_BASE", "http://x")
os.environ.setdefault("EMBED_MODEL", "x")

from aterag.config import get_settings
from aterag.inference import InferenceEngine
from aterag.mcp_server import server as srv

checks: list[tuple[str, bool, str]] = []


def _ents(rows):
    """构造 query_entities 的替身。"""

    class FakeRag:
        def query_entities(self, model_id, etype=None, keyword=None, section_path=None):
            return rows

    return FakeRag()


# ---------- 1. 主轨动态判定 (跨轨名, 用 -12V / -28V / -5V 等白名单外的轨) ----------
print("=== 1. 主轨判定不依赖轨名白名单 ===\n")
for rail_v, rail_i, want_v, want_i in [
    ("-12V", 25.0, 12.0, 25.0),  # 白名单外
    ("-28V", 10.0, 28.0, 10.0),  # 白名单外
    ("-48V", 20.0, 48.0, 20.0),  # 原白名单内 (回归对照)
    ("-54V", 11.1, 54.0, 11.1),  # 原白名单内 (回归对照)
    ("-5V", 30.0, 5.0, 30.0),  # 单字符轨名, 最易被字面量匹配漏掉
]:
    rows = [
        {
            "title": "额定输出电压",
            "rail": rail_v,
            "typ": None,
            "min": -want_v,
            "max": None,
            "req_id": "SR-X-1200",
            "section_path": "4.3.2",
        },
        {
            "title": "输出电流",
            "rail": rail_v,
            "typ": None,
            "min": 0,
            "max": rail_i,
            "req_id": "SR-X-1203",
            "section_path": "4.3.2",
        },
        # 辅助轨: 电流小, 不应被选为主轨
        {
            "title": "额定输出电压",
            "rail": "3.3V",
            "typ": None,
            "min": 3.3,
            "max": None,
            "req_id": "SR-X-1200",
            "section_path": "4.3.2",
        },
        {
            "title": "输出电流",
            "rail": "3.3V",
            "typ": None,
            "min": 0,
            "max": 2.0,
            "req_id": "SR-X-1203",
            "section_path": "4.3.2",
        },
    ]
    srv.get_rag = lambda _r=rows: _ents(_r)
    f = srv._model_facts("SYNTH-X")
    ok = f.get("main_rail") == rail_v and f.get("voltage") == want_v and f.get("current") == want_i
    checks.append(
        (
            f"主轨判定 {rail_v}",
            ok,
            f"main_rail={f.get('main_rail')} voltage={f.get('voltage')} current={f.get('current')}",
        )
    )

# ---------- 2. 缺电压时 fail-closed (不填默认值) ----------
rows_no_v = [
    {
        "title": "输出电流",
        "rail": "-12V",
        "typ": None,
        "min": 0,
        "max": 25.0,
        "req_id": "SR-X-1203",
        "section_path": "4.3.2",
    },
]
srv.get_rag = lambda: _ents(rows_no_v)
try:
    f = srv._model_facts("SYNTH-X", required=("voltage", "current"))
    checks.append(("缺电压时 fail-closed", False, f"竟返回 {f}"))
except srv.FactUnavailable as e:
    checks.append(("缺电压时 fail-closed", e.missing == ["voltage"], f"missing={e.missing}"))

# ---------- 3. confidence 缺省不得为 1.0 ----------
print("\n=== 2. confidence 缺省不顶格 ===\n")
eng = InferenceEngine(get_settings(), "power", model_facts={})
r = eng.calculate("power", {"voltage": 54, "current": 11.1})
conf = r.get("confidence")
checks.append(
    ("已标注规则 confidence 为数值", isinstance(conf, (int, float)), f"confidence={conf}")
)
checks.append(("confidence_unknown=False (已标注)", r.get("confidence_unknown") is False, "已标注"))

# 摘掉某条真实规则的 confidence, 验证不再顶格为 1.0 (走真实代码路径)
target = next(x for x in eng.rules if x.get("id") == "K-ELEC-001" and x.get("derive"))
saved = target.pop("confidence", "MISSING")
try:
    r2 = eng.calculate("power", {"voltage": 54, "current": 11.1})
    checks.append(
        (
            "缺 confidence 时为 None 而非 1.0",
            r2.get("confidence") is None,
            f"confidence={r2.get('confidence')}",
        )
    )
    checks.append(("显式标记 confidence_unknown", r2.get("confidence_unknown") is True, "已标记"))
    checks.append(
        (
            "缺 confidence 时数值仍正确",
            abs(r2.get("value", 0) - 599.4) < 0.01,
            f"value={r2.get('value')}",
        )
    )
finally:
    if saved != "MISSING":
        target["confidence"] = saved

# ---------- 4. AST 扫描: 白名单字面量不得复现 (排除注释与文档字符串) ----------
print("\n=== 3. AST 扫描 (白名单字面量防复现) ===\n")
RAIL_RE = re.compile(r"^[+-]?\d{1,3}(?:\.\d+)?V$")


def rail_literals(path: str) -> list[tuple[int, str]]:
    """返回源码中**真实代码**里的轨名字面量 (注释/docstring 不算)。

    注释里出现 "-54V" 是记录历史修复, 属正常; 只有参与逻辑判断的字面量才是硬编码。
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    doc_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                doc_nodes.add(id(first.value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_nodes
        ):
            if RAIL_RE.match(node.value.strip()):
                out.append((node.lineno, node.value))
    return out


lits = rail_literals("src/aterag/mcp_server/server.py")
checks.append(
    (
        "server.py 代码中无轨名字面量",
        not lits,
        f"发现 {lits}" if lits else "仅注释/文档提及, 代码无硬编码",
    )
)

esrc = Path("src/aterag/rag/service.py").read_text(encoding="utf-8")
checks.append(("service.py 定义 UNREGISTERED_LAYER", "UNREGISTERED_LAYER" in esrc, "已定义"))
checks.append(
    (
        "service.py 无 layer 默认 'model' 兜底",
        'layer_map.get(h.get("workspace_id", ""), "model")' not in esrc,
        "已清除",
    )
)

ensrc = Path("src/aterag/inference/engine.py").read_text(encoding="utf-8")
checks.append(
    ("engine.py 无 confidence 默认 1.0", 'rule.get("confidence", 1.0)' not in ensrc, "已清除")
)

# ---------- 5. 场景规则不含 PA601 轨名绑定 (轨序须按功耗自动排定) ----------
# 背景: load_derivation 曾声明 priority_rails: ["3.45V", "-54V"]。换轨名的型号
# 该列表整体失配 -> 落进 sorted() 按字母序兜底 -> 主/辅轨颠倒 -> 算出的电流连
# 符号都会错(实测 -48V/12V 型号得 12V=-46.667A)且不报错。
# 轨序现在按额定功耗 U*I_rated 自动排, 与轨名无关, 故这些字段须彻底消失。
from aterag.extract.scenarios import (  # noqa: E402
    ScenarioRules,
    Tier,
    _derive_load,
    parse_tiers,
)

_scen_yaml = Path("config/scenario_rules.yaml").read_text(encoding="utf-8")
# 只看非注释行: 注释里记着"原先此处声明过 priority_rails"是有意的历史说明,
# 删掉反而丢失修复依据。
_scen_active = "\n".join(
    ln for ln in _scen_yaml.splitlines() if not ln.lstrip().startswith("#")
)
checks.append(
    (
        "scenario_rules.yaml 无 priority_rails 字段",
        "priority_rails" not in _scen_active,
        "轨序按额定功耗自动排定",
    )
)
for _field in ("priority_rails",):
    _hits = [
        f"{p}:{i + 1}"
        for p in Path("src").rglob("*.py")
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines())
        if _field in ln and not ln.lstrip().startswith("#")
    ]
    checks.append(
        (
            f"代码中无 {_field} 字段引用",
            not _hits,
            f"发现 {_hits[:3]}" if _hits else "已清除",
        )
    )

# 轨序正确性: 跨型号验证主轨由功耗决定, 且结果恒为非负、不超额定。
# 字母序陷阱用例: "-5V"(150W) 与 "-48V"(384W), 按字母序 -48V 排前会被选去吃
# 剩余功率, 得出 5V 轨承担主轨的荒谬结果。
_rules = ScenarioRules.load("config/scenario_rules.yaml")
_tiers = [Tier(90.0, 176.0, 400.0, ""), Tier(176.0, 286.0, 600.0, "")]
for _name, _rated, _volts, _want_main in [
    ("PA601 -54V/3.45V", {"-54V": 11.1, "3.45V": 0.1}, {"-54V": 54.0, "3.45V": 3.45}, "-54V"),
    ("换轨名 -48V/12V", {"-48V": 20.0, "12V": 2.0}, {"-48V": 48.0, "12V": 12.0}, "-48V"),
    ("换轨名 -12V/+5V", {"-12V": 25.0, "5V": 3.0}, {"-12V": 12.0, "5V": 5.0}, "-12V"),
    ("字母序陷阱 -5V/-48V", {"-5V": 30.0, "-48V": 8.0}, {"-5V": 5.0, "-48V": 48.0}, "-48V"),
    ("单轨 12V", {"12V": 25.0}, {"12V": 12.0}, "12V"),
]:
    try:
        _d = _derive_load(_rules, _tiers, _rated, _volts)
        # 主轨 = 推导电流与额定不同的那条; 它必须等于功耗最大者
        _main = [
            r
            for r, c in _d[(90.0, 176.0, 400.0)].items()
            if abs(c - _rated[r]) > 1e-3
        ]
        _bad = [
            (r, c) for _v in _d.values() for r, c in _v.items() if c < 0 or c > _rated[r] + 1e-3
        ]
        checks.append(
            (
                f"轨序自动排定 {_name}",
                _main in ([], [_want_main]) and not _bad,
                f"受封顶轨={_main} 期望={_want_main}"
                + (f" 越界={_bad}" if _bad else ""),
            )
        )
    except Exception as _e:  # noqa: BLE001
        checks.append((f"轨序自动排定 {_name}", False, repr(_e)))

# ---------- 6. 档位解析不得依赖标题字面量 ----------
# 原先按 title == "输出功率" 精确比对。换型号若写"额定输出总功率"或英文
# "Total Output Power", 档位解析归零, 而轨级推导随之静默失效 —— 场景退回
# 按额定电流判, 低压段击穿功率档。故改判单位(W) + 标题模式(功率|Power)。
from aterag.extract.models import TestCondition as _Cond  # noqa: E402

_tier_cases = [
    ("PA601 原文", "输出功率", "W", "90~176Vac: 400W; 176~286Vac: 600W", 2),
    ("换写法 额定输出总功率", "额定输出总功率", "W", "90~176Vac: 400W", 1),
    ("英文标题 Total Output Power", "Total Output Power", "W", "90~176Vac: 400W", 1),
    ("直流 Vdc", "输出功率", "W", "90~176Vdc: 400W", 1),
    ("无后缀 V", "输出功率", "W", "90~176V: 400W", 1),
    ("小数电压", "输出功率", "W", "110.5~220V: 500W", 1),
    ("小写 w", "输出功率", "W", "90~176Vac: 400w", 1),
]
for _cname, _title, _unit, _notes, _want in _tier_cases:
    _got = parse_tiers(_rules, [_Cond(req_id="SR-X", title=_title, section_path="4.3.2", notes=_notes, limits={"max": 600.0, "unit": _unit}, unit=_unit)])
    checks.append(
        (f"档位解析 {_cname}", len(_got) == _want, f"解析出 {len(_got)} 档, 期望 {_want}")
    )
_not_power = _Cond(
    req_id="SR-Y", title="输出电压", section_path="4.3.2",
    notes="90~176Vac: 400W", limits={"max": 55.0, "unit": "V"}, unit="V",
)
checks.append(
    ("档位解析-非功率条目不误判", not parse_tiers(_rules, [_not_power]), "输出电压(V)未被当成功率条目")
)
checks.append(
    (
        "scenario_rules.yaml 无 from_requirement_title",
        "from_requirement_title" not in _scen_active,
        "改由 from_unit_pattern + from_title_pattern 判定",
    )
)

# ---------- 7. 派生电流判据不得看量纲 (kind 是唯一依据) ----------
# 背景: _is_load_bearing 与 _rated_currents 曾用 "限值单位是 A" 作 fallback。
# 保护动作阈值的单位也是 A, 于是被一起收进来 —— PA601 的 SR-1309 过流动作区间
# 是 12~18A, 却被挂上 SR-1203 的派生值 7.401A, 落在动作区间之下。产测照它设
# 负载不触发保护, 保护功能测不出来却显示通过。
from aterag.extract.models import ConditionClause as _Cl  # noqa: E402
from aterag.extract.scenarios import _is_load_bearing, _rated_currents  # noqa: E402


def _cond(kind: str, unit: str, role: str, rail: str, mx: float | None) -> _Cond:
    lim: dict[str, object] = {"unit": unit, "rail": rail}
    if mx is not None:
        lim["max"] = mx
    return _Cond(
        req_id="SR-K",
        title="示例",
        section_path="4.3.2",
        rail=rail,
        role=role,
        limits=lim,
        output_conditions=[_Cl(kind=kind, text="x", role="output")],
    )


# 单位为 A 但语义是保护动作 -> 不得判成输出电流类, 也不得进额定表
_prot = _cond("protection_action", "A", "protection_response", "-54V", 18.0)
checks.append(
    ("派生判据-保护动作不认电流量纲", not _is_load_bearing(_prot), "kind=protection_action")
)
checks.append(
    (
        "额定表-保护动作点不入表",
        "-54V" not in _rated_currents([_prot]),
        "否则 -54V 额定被记成 18A 而非 11.1A, 封顶基准错一个量级",
    )
)
# 真正的输出电流仍须保留 (不能修过头)
_oc = _cond("output_current", "A", "output_spec", "-54V", 11.1)
checks.append(("派生判据-输出电流仍成立", _is_load_bearing(_oc), "kind=output_current"))
checks.append(
    (
        "额定表-输出电流入表",
        _rated_currents([_oc]).get("-54V") == 11.1,
        "输出电流必须仍取额定值",
    )
)
# 无 output_current 子句时, 任何单位都不该判成输出电流类 (纯量纲不得放行)
for _u in ("A", "mA", "W", "V"):
    checks.append(
        (
            f"派生判据-仅 {_u} 量纲不放行",
            not _is_load_bearing(_cond("signal_state", _u, "output_spec", "-54V", 1.0)),
            "无量纲信号类不得挂派生电流",
        )
    )

print()
for label, ok, detail in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:38s} {detail}")
n = sum(1 for _, ok, _ in checks if ok)
print(f"\nNO_HARDCODED_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
sys.exit(0 if n == len(checks) else 1)
