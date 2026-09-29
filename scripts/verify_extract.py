"""实体抽取验证: 用 PA601 规格书跑通并抽查关键实体."""
import sys
from collections import Counter

sys.path.insert(0, "src")

from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import parse_file

blocks = parse_file("PA601-D54A 定制电源技术规格书.md")
ents = extract_from_blocks(blocks, "PA601-D54A", "B")
print("COUNTS:", dict(Counter(e.etype for e in ents)))

for e in ents:
    if e.etype == "Protection":
        keys = ("trip_min", "trip_max", "recovery_min", "recovery_max", "hysteresis_min", "priority")
        print("PROT", e.eid, {k: v for k in keys if (v := e.props.get(k)) is not None})

for e in ents:
    if e.etype == "Requirement" and e.props.get("req_id", "").endswith(("1210", "1309", "1203")):
        p = e.props
        print("REQ", e.eid, "|", p.get("title"), "| min=", p.get("min"), "max=", p.get("max"),
              "rail=", p.get("rail"), "prio=", p.get("priority"), "sec=", p.get("section_path"))

for e in ents:
    if e.etype == "Signal" and e.props.get("pin") in ("S1", "P1", "1"):
        print("SIG", e.eid, "rating=", e.props.get("current_rating"))

# ---- 判定 ----
checks: list[tuple[str, bool]] = []
counts = Counter(e.etype for e in ents)
for etype in ("Requirement", "Protection", "Product"):
    checks.append((f"抽出 {etype} 实体", counts.get(etype, 0) > 0))

prot = [e for e in ents if e.etype == "Protection"]
checks.append(("保护点抽出数值", any(e.props.get("trip_min") is not None for e in prot)))
checks.append(("保护点区分输出轨 (多轨)", len({e.props.get("rail") for e in prot if e.props.get("rail")}) >= 2))
reqs = [e for e in ents if e.etype == "Requirement"]
checks.append(("需求抽出 min/max", any(e.props.get("min") is not None for e in reqs)))
checks.append(("需求抽出优先级", any(e.props.get("priority") for e in reqs)))
checks.append(("需求抽出章节路径", any(e.props.get("section_path") for e in reqs)))
sigs = [e for e in ents if e.etype == "Signal"]
checks.append(("信号抽出电流额定", any(e.props.get("current_rating") is not None for e in sigs)))

# ---- 数值解析回归 (防静默丢失) ----
# 背景: _parse_num 原正则只有 `-?`, 规格书的显式正号 '+3.45' (SR-PA601-D54A-1200#3.45V)
# 匹配不上 -> 该轨额定电压整条丢失且不报错。下列用例固定该行为。
from aterag.ingest.entity_extract import _parse_num, parse_range

PARSE_CASES = [
    ("+3.45", 3.45, "显式正号电压 (第二路额定)"),
    ("-54", -54.0, "负电压 (主轨额定)"),
    ("54", 54.0, "无符号"),
    ("0.1", 0.1, "小数"),
    ("+12A", 12.0, "正号带单位"),
    ("-1.5A", -1.5, "负值带单位"),
    ("-", None, "占位破折号"),
    ("±3%", None, "公差表达式非纯数值"),
]
for raw, want, label in PARSE_CASES:
    got = _parse_num(raw)
    checks.append((f"解析 {label} {raw!r}", got == want, f"got={got} want={want}"))

RANGE_CASES = [
    ("12~18", (12.0, 18.0), "常规区间"),
    ("-5 ~ +5", (-5.0, 5.0), "双极性区间"),
    ("-54~-52", (-54.0, -52.0), "全负区间"),
]
for raw, want, label in RANGE_CASES:
    got = parse_range(raw)
    checks.append((f"区间 {label} {raw!r}", got == want, f"got={got} want={want}"))

# 端到端: 第二路额定电压必须真的落库 (而非解析器单测通过即止)
aux_v = [e for e in reqs if e.props.get("rail") == "3.45V" and e.props.get("title") == "额定输出电压"]
checks.append((
    "第二路 (3.45V) 额定电压已落库",
    bool(aux_v) and aux_v[0].props.get("min") == 3.45,
    f"min={aux_v[0].props.get('min') if aux_v else '实体缺失'}",
))
aux_i = [e for e in reqs if e.props.get("rail") == "3.45V" and e.props.get("title") == "输出电流"]
checks.append((
    "第二路 (3.45V) 额定电流已落库",
    bool(aux_i) and aux_i[0].props.get("max") == 0.1,
    f"max={aux_i[0].props.get('max') if aux_i else '实体缺失'}",
))

for item in checks:
    label, ok = item[0], item[1]
    detail = item[2] if len(item) > 2 else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
npass = sum(1 for c in checks if c[1])
print(f"EXTRACT_VERIFY {'PASS' if npass == len(checks) else 'FAIL'} {npass}/{len(checks)}")
sys.exit(0 if npass == len(checks) else 1)
