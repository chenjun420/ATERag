"""PN1000-48A 迷你规格书解析与抽取验证 (隔离测试夹具预检)."""

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import parse_file

blocks = parse_file("tests/fixtures/PN1000-48A 迷你规格书.md")
ents = extract_from_blocks(blocks, "PN1000-48A", "A")
from collections import Counter

print("COUNTS:", dict(Counter(e.etype for e in ents)))
for e in ents:
    if e.etype == "Protection":
        print("PROT", e.eid, "trip", e.props.get("trip_min"), "-", e.props.get("trip_max"))
    if e.etype == "Requirement" and e.props.get("req_id", "").endswith("1203"):
        print("REQ", e.eid, "max=", e.props.get("max"))
m = blocks and None
import re

text = open("tests/fixtures/PN1000-48A 迷你规格书.md", encoding="utf-8").read()
mm = re.search(r"\b([A-Z]{2,8}\d[A-Z0-9]*(?:-[A-Z0-9]+)+)\b", text[:500])
print("MODEL_ID:", mm.group(1) if mm else "NOT FOUND")

# ---- 判定 ----
checks: list[tuple[str, bool]] = []
checks.append(("型号 ID 识别为 PN1000-48A", bool(mm) and mm.group(1) == "PN1000-48A"))
prot = [e for e in ents if e.etype == "Protection"]
checks.append(("抽出保护实体", len(prot) > 0))
checks.append(("保护点数值非空", all(e.props.get("trip_min") is not None for e in prot)))
reqs = [e for e in ents if e.etype == "Requirement"]
checks.append(("抽出需求实体", len(reqs) > 0))
r1203 = [e for e in reqs if e.props.get("req_id", "").endswith("1203")]
checks.append(("1203 额定电流抽出", bool(r1203) and r1203[0].props.get("max") is not None))
# 隔离关键: PN1000 的 -48V 额定电流 (20.8A) 必须不同于 PA601 的 -54V/11.1A
checks.append(
    (
        "与 PA601 参数不冲突 (20.8A vs 11.1A)",
        bool(r1203) and r1203[0].props.get("max") == 20.8,
    )
)
for label, ok in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
npass = sum(1 for _, ok in checks if ok)
print(f"PN1000_VERIFY {'PASS' if npass == len(checks) else 'FAIL'} {npass}/{len(checks)}")
sys.exit(0 if npass == len(checks) else 1)
