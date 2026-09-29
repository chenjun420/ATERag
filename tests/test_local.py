"""纯本地单测: 注册表 / 类型识别 / 章节解析 (不依赖网络与存储)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "src")

os.environ.setdefault("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("LLM_BASE", "http://x/v1")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBED_BASE", "http://x")
os.environ.setdefault("EMBED_MODEL", "x")
os.environ.setdefault("REGISTRY_PATH", "registry.yaml")

import pytest

from aterag.config import get_settings
from aterag.ingest.classify import classify_by_rules
from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import parse_markdown
from aterag.registry import AmbiguousModel, Registry, UnknownModel


# ---------- registry ----------
def test_registry_resolve_explicit():
    settings = get_settings()
    reg = Registry.load(settings)
    reg.products["TEST-1A"] = type("P", (), {"domain": "power"})()
    r = reg.resolve_query("任意查询", "TEST-1A")
    assert r.model_id == "TEST-1A" and r.domain == "power" and r.source == "explicit"


def test_registry_resolve_inferred():
    settings = get_settings()
    reg = Registry.load(settings)
    reg.products["PA601-D54A"] = type("P", (), {"domain": "power"})()
    r = reg.resolve_query("PA601-D54A 的过流保护点是多少", None)
    assert r.model_id == "PA601-D54A" and r.source == "inferred"


def test_registry_resolve_unknown_failclosed():
    settings = get_settings()
    reg = Registry.load(settings)
    with pytest.raises(UnknownModel):
        reg.resolve_query("完全无关的查询内容 xyz", None)


def test_registry_resolve_ambiguous():
    settings = get_settings()
    reg = Registry.load(settings)
    reg.products["AAA-1A"] = type("P", (), {"domain": "power"})()
    reg.products["BBB-2B"] = type("P", (), {"domain": "power"})()
    with pytest.raises(AmbiguousModel):
        reg.resolve_query("AAA-1A 和 BBB-2B 对比", None)


def test_registry_three_layers():
    settings = get_settings()
    reg = Registry.load(settings)
    reg.products["M-1"] = type("P", (), {"domain": "power"})()
    ws = reg.query_workspaces("M-1")
    assert ws == ["M-1", "_domain_power", "_common"]


# ---------- classification ----------
def test_classify_power():
    text = "# PA601-D54A 定制电源技术规格书\n本电源为定制电源整流器"
    assert classify_by_rules(text) == "power"


def test_classify_rf():
    text = "# RRU 射频功放单元规格书"
    assert classify_by_rules(text) == "rf"


def test_classify_unknown():
    assert classify_by_rules("完全无关的机械结构件文档") is None


# ---------- markdown parser ----------
SAMPLE = """# 文档标题

## 4 技术要求

### 4.3 功能/性能要求

#### 4.3.3 保护功能

| 编号 | 项目 | 单位 | 最小值 | 最大值 | 等级 |
|---|---|---|---|---|---|
| SR-X-1309 | 输出过流保护 | A | 12 | 18 | 强制 |

正文段落一些说明。
"""


def test_parse_sections():
    blocks = parse_markdown(SAMPLE)
    secs = {b.section_path: b.heading for b in blocks if b.section_path}
    assert secs.get("4.3.3") == "4.3.3 保护功能"
    prot = [b for b in blocks if b.section_path == "4.3.3"]
    assert prot and any(b.tables for b in prot)


def test_parse_parent_headings():
    blocks = parse_markdown(SAMPLE)
    b = next(b for b in blocks if b.section_path == "4.3.3")
    assert "4.3 功能/性能要求" in b.parent_headings
    assert "4 技术要求" in b.parent_headings


# ---------- entity extraction on PA601 ----------
def test_pa601_extraction():
    doc = Path("PA601-D54A 定制电源技术规格书.md")
    if not doc.exists():
        pytest.skip("PA601 doc not present")
    blocks = parse_markdown(doc.read_text(encoding="utf-8"))
    ents = extract_from_blocks(blocks, "PA601-D54A", "B")
    by_type: dict[str, int] = {}
    for e in ents:
        by_type[e.etype] = by_type.get(e.etype, 0) + 1
    assert by_type.get("Requirement", 0) > 100
    assert by_type.get("Protection", 0) >= 5
    # 输出过流保护 -54V: 12~18A
    oc = next(
        e for e in ents if e.etype == "Protection" and "过流" in e.eid and e.eid.endswith("-54V")
    )
    assert oc.props["trip_min"] == 12.0 and oc.props["trip_max"] == 18.0
    # 整机效率 86%
    eff = next(e for e in ents if e.props.get("req_id", "").endswith("1210"))
    assert eff.props["min"] == 86.0
    # 信号通流 (-54VRTN 来自 表5-2 的 信号定义 列)
    sig = next(e for e in ents if "-54VRTN" in str(e.props.get("signal_def", "")))
    assert sig.props["current_rating"] == 30.0
