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
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
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
    ("-12V", 25.0, 12.0, 25.0),      # 白名单外
    ("-28V", 10.0, 28.0, 10.0),      # 白名单外
    ("-48V", 20.0, 48.0, 20.0),      # 原白名单内 (回归对照)
    ("-54V", 11.1, 54.0, 11.1),      # 原白名单内 (回归对照)
    ("-5V", 30.0, 5.0, 30.0),        # 单字符轨名, 最易被字面量匹配漏掉
]:
    rows = [
        {"title": "额定输出电压", "rail": rail_v, "typ": None, "min": -want_v, "max": None,
         "req_id": "SR-X-1200", "section_path": "4.3.2"},
        {"title": "输出电流", "rail": rail_v, "typ": None, "min": 0, "max": rail_i,
         "req_id": "SR-X-1203", "section_path": "4.3.2"},
        # 辅助轨: 电流小, 不应被选为主轨
        {"title": "额定输出电压", "rail": "3.3V", "typ": None, "min": 3.3, "max": None,
         "req_id": "SR-X-1200", "section_path": "4.3.2"},
        {"title": "输出电流", "rail": "3.3V", "typ": None, "min": 0, "max": 2.0,
         "req_id": "SR-X-1203", "section_path": "4.3.2"},
    ]
    srv.get_rag = lambda _r=rows: _ents(_r)
    f = srv._model_facts("SYNTH-X")
    ok = f.get("main_rail") == rail_v and f.get("voltage") == want_v and f.get("current") == want_i
    checks.append((f"主轨判定 {rail_v}", ok,
                   f"main_rail={f.get('main_rail')} voltage={f.get('voltage')} current={f.get('current')}"))

# ---------- 2. 缺电压时 fail-closed (不填默认值) ----------
rows_no_v = [
    {"title": "输出电流", "rail": "-12V", "typ": None, "min": 0, "max": 25.0,
     "req_id": "SR-X-1203", "section_path": "4.3.2"},
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
checks.append(("已标注规则 confidence 为数值", isinstance(conf, (int, float)), f"confidence={conf}"))
checks.append(("confidence_unknown=False (已标注)", r.get("confidence_unknown") is False, "已标注"))

# 摘掉某条真实规则的 confidence, 验证不再顶格为 1.0 (走真实代码路径)
target = next(x for x in eng.rules if x.get("id") == "K-ELEC-001" and x.get("derive"))
saved = target.pop("confidence", "MISSING")
try:
    r2 = eng.calculate("power", {"voltage": 54, "current": 11.1})
    checks.append(("缺 confidence 时为 None 而非 1.0", r2.get("confidence") is None,
                   f"confidence={r2.get('confidence')}"))
    checks.append(("显式标记 confidence_unknown", r2.get("confidence_unknown") is True, "已标记"))
    checks.append(("缺 confidence 时数值仍正确", abs(r2.get("value", 0) - 599.4) < 0.01,
                   f"value={r2.get('value')}"))
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
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_nodes.add(id(first.value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_nodes:
            if RAIL_RE.match(node.value.strip()):
                out.append((node.lineno, node.value))
    return out


lits = rail_literals("src/aterag/mcp_server/server.py")
checks.append(("server.py 代码中无轨名字面量", not lits,
               f"发现 {lits}" if lits else "仅注释/文档提及, 代码无硬编码"))

esrc = Path("src/aterag/rag/service.py").read_text(encoding="utf-8")
checks.append(("service.py 定义 UNREGISTERED_LAYER", "UNREGISTERED_LAYER" in esrc, "已定义"))
checks.append(("service.py 无 layer 默认 'model' 兜底",
               'layer_map.get(h.get("workspace_id", ""), "model")' not in esrc, "已清除"))

ensrc = Path("src/aterag/inference/engine.py").read_text(encoding="utf-8")
checks.append(("engine.py 无 confidence 默认 1.0",
               'rule.get("confidence", 1.0)' not in ensrc, "已清除"))

print()
for label, ok, detail in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:38s} {detail}")
n = sum(1 for _, ok, _ in checks if ok)
print(f"\nNO_HARDCODED_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
sys.exit(0 if n == len(checks) else 1)
