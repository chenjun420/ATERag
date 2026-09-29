import sys

sys.path.insert(0, "src")

from aterag.ingest.entity_extract import _PROTECTION_TITLE_RE, extract_from_blocks
from aterag.ingest.markdown_parser import parse_file

for t in [
    "输入过压保护点",
    "输入过压恢复点",
    "输入过压保护回差",
    "输入欠压恢复点",
    "输出静态过压保护",
]:
    m = _PROTECTION_TITLE_RE.match(t)
    print(t, "->", m.groups() if m else "NO MATCH")

blocks = parse_file("PA601-D54A 定制电源技术规格书.md")
ents = extract_from_blocks(blocks, "PA601-D54A", "B")
reqs = [e for e in ents if e.etype == "Requirement"]
groups: dict = {}
for e in reqs:
    m = _PROTECTION_TITLE_RE.match(e.props.get("title", ""))
    if m:
        base = f"{m.group(1) or ''}{m.group(2)}{m.group(3) or ''}保护"
        key = (base, e.props.get("rail", ""))
        groups.setdefault(key, []).append((m.group(4), e.props.get("req_id"), e.props.get("min")))
for k, v in sorted(groups.items()):
    print("GROUP", k, v)
