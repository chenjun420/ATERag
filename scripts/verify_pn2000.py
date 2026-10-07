"""PN2000-24A 夹具预检: 实体抽取 + 条件抽取 (与 verify_pn1000.py 对称).

存在的理由 (方案 §11.4 A19): PN2000-24A 长期**登记在册却进不来** ——
它的章节树 (2 技术要求 / 2.1 工作环境条件 / ...) 与中兴系模板
(4.3 功能/性能要求) 完全不是一套, 而文档档案选择只存在于 MCP 工具的可选
参数里, 没人记得为它指定档案时抽取就抛 SectionKeywordNotFound, 且没有任何
测试会发现。

本脚本把三件事钉成可执行断言:
  1. 实体抽取: 五张表全部命中 schema (信号接口/遥测两张曾因表头不同而落空);
  2. 条件抽取: 走注册表绑定的档案端到端跑通, 且 12 条需求零未解析;
  3. 型号隔离: -48V/20.0A 与 PA601 的 -54V/11.1A 不串味。
"""

import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

# 条件抽取链要 Settings (LLM/Embedding/DSN), 本脚本不调模型, 只需值存在
for k, v in {
    "POSTGRES_DSN": "postgresql://x:x@127.0.0.1/x",
    "LLM_BASE": "http://x/v1",
    "LLM_MODEL": "x",
    "EMBED_BASE": "http://x",
    "EMBED_MODEL": "x",
}.items():
    os.environ.setdefault(k, v)

from aterag.extract import extract_test_conditions, load_annotations
from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import parse_file

FIXTURE = "tests/fixtures/PN2000-24A 定制电源技术规格书.md"
MODEL = "PN2000-24A"

blocks = parse_file(FIXTURE)
ents = extract_from_blocks(blocks, MODEL, "A")
print("COUNTS:", dict(Counter(e.etype for e in ents)))
for e in ents:
    if e.etype == "Signal":
        print("SIG ", e.eid, "|", e.props.get("level"), "|", e.props.get("src_dst"))

# ---- 条件抽取: 从夹具现生成 blocks 侧车, 不依赖 rag_storage (不入库也能跑) ----
tmpdir = Path(tempfile.mkdtemp(prefix="pn2000_blocks_"))
(tmpdir / f"{MODEL}.jsonl").write_text(
    "\n".join(json.dumps(b.to_dict(), ensure_ascii=False) for b in blocks),
    encoding="utf-8",
)
try:
    result = extract_test_conditions(
        MODEL, doc_version="A", blocks_dir=tmpdir, annotations=load_annotations(MODEL)
    )
except Exception as e:  # noqa: BLE001  预检脚本要报出失败原因而不是堆栈
    print(f"EXTRACT_RAISED {type(e).__name__}: {e}")
    result = None

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, bool(ok), detail))


# ---- 1. 实体抽取 ----
reqs = [e for e in ents if e.etype == "Requirement"]
sigs = [e for e in ents if e.etype == "Signal"]
check("型号 ID 识别为 PN2000-24A", MODEL == "PN2000-24A")
# 15 = 参数 12 (2.1 三条 + 2.2 五条 + 2.3 四条) + 遥测 3 (2.5)。信号接口
# (2.4) 走 signal_table 产 Signal 实体, 不产 Requirement。
check("抽出需求实体 (参数 12 + 遥测 3 = 15)", len(reqs) == 15, f"got={len(reqs)}")
check("抽出信号实体 (信号接口 3 条)", len(sigs) == 3, f"got={len(sigs)}")

r1203 = [e for e in reqs if e.props.get("req_id", "").endswith("1203")]
check("1203 电压纹波抽出最大值", bool(r1203) and r1203[0].props.get("max") == 120.0)
r1201 = [e for e in reqs if e.props.get("req_id", "").endswith("1201")]
check(
    "1201 输出电压抽出 -48.5~-47.5",
    bool(r1201) and (r1201[0].props.get("min"), r1201[0].props.get("max")) == (-48.5, -47.5),
)
# 型号隔离关键: 轨道列必须真的映射上 (param_table 的「轨道」列), 否则保护点
# 分不出输出轨, 而这条列 PN2000 有、PA601 没有 (复合列写法不同)
check(
    "「轨道」列已映射 (输出轨 -48V)",
    bool(r1201) and r1201[0].props.get("rail") == "-48V",
    f"rail={r1201[0].props.get('rail') if r1201 else '实体缺失'}",
)
r1309 = [e for e in reqs if e.props.get("req_id", "").endswith("1309")]
check("保护条目抽出数值 (22.0~30.0A)", bool(r1309) and r1309[0].props.get("min") == 22.0)
check(
    "型号隔离: 20.0A 与 PA601 的 11.1A 不串味",
    all(e.props.get("max") != 11.1 for e in reqs),
)
# 遥测项同义列 (A19 加的映射): 缺它 2.5 整表不命中 -> fail-closed 抛错
tel = [e for e in reqs if e.props.get("req_id", "").endswith("1501")]
check("遥测项列已映射 (1501 检出)", bool(tel), f"got={len(tel)}")
# 信号接口表的管脚/编号: 表头不同 (编号|信号名称|管脚|说明|等级)
pwok = [e for e in sigs if "PWOK" in e.eid]
check("信号接口表抽出 PWOK@S2", bool(pwok), f"eids={[e.eid for e in sigs]}")

# ---- 2. 条件抽取 (走注册表绑定的档案) ----
if result is not None:
    check("条件抽取未抛错 (注册表档案绑定生效)", True)
    # 条件只来自 2.1/2.2/2.3 (档案 section_keywords 逐节点名选, 不含 2.4/2.5):
    # 2.5 遥测虽产 Requirement 实体, 但它不是产测条件 (无限值列)。
    check(
        "条件条数 = 参数表 12 条",
        len(result.conditions) == 12,
        f"got={len(result.conditions)}",
    )
    unres = result.stats.get("unresolved_text", 0)
    check("零未解析文本", unres == 0, f"got={unres}")
    unmapped = result.stats.get("unmapped_table_rows", 0)
    check("零未映射表行", unmapped == 0, f"got={unmapped}")
else:
    check("条件抽取未抛错 (注册表档案绑定生效)", False, "见上方 EXTRACT_RAISED")

for label, ok, detail in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
npass = sum(1 for _, ok, _ in checks if ok)
print(f"PN2000_VERIFY {'PASS' if npass == len(checks) else 'FAIL'} {npass}/{len(checks)}")
sys.exit(0 if npass == len(checks) else 1)
