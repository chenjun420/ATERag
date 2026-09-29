"""调试: 保护功能分组 (点/恢复点/回差 -> Protection 实体).

标题模式与章节范围原先硬编码在 entity_extract, 现已外置 config/table_schemas.yaml
的 grouping.protection, 本脚本改为从档案读取 —— 与运行时完全一致的认知来源。

用法: .venv\\Scripts\\python.exe scripts/debug_groups.py
"""

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import parse_file
from aterag.ingest.table_schema import load_registry

reg = load_registry(get_settings().table_schemas_path)
rule = reg.grouping_protection
if rule is None:
    raise SystemExit("档案未配置 grouping.protection")

print(f"档案: {reg.source_path}")
print(f"保护章节: {rule.sections}")
print(f"标题模式: {rule.title_pattern.pattern}")
print()

for t in [
    "输入过压保护点",
    "输入过压恢复点",
    "输入过压保护回差",
    "输入欠压恢复点",
    "输出静态过压保护",
]:
    m = rule.title_pattern.match(t)
    print(t, "->", m.groups() if m else "NO MATCH")

blocks = parse_file("PA601-D54A 定制电源技术规格书.md")
ents = extract_from_blocks(blocks, "PA601-D54A", "B", registry=reg)
reqs = [e for e in ents if e.etype == "Requirement"]
groups: dict = {}
for e in reqs:
    m = rule.title_pattern.match(e.props.get("title", ""))
    if m:
        base = f"{m.group(1) or ''}{m.group(2)}{m.group(3) or ''}保护"
        key = (base, e.props.get("rail", ""))
        groups.setdefault(key, []).append((m.group(4), e.props.get("req_id"), e.props.get("min")))
for k, v in sorted(groups.items()):
    print("GROUP", k, v)
