"""表结构覆盖报告 + 映射提案生成 (T1/T2/T3 的运维工具).

三个用途:
  1. 验收    —— 新规格书入库前确认所有表都被映射 (无 MISS)
  2. 接入    —— 换文档模板时, 由本工具列出待映射表头, 人工补 config/table_schemas.yaml
  3. 提案    —— --propose 生成候选 schema 骨架 (T1 确定性), --llm 再叠加语义映射建议

设计约束:
  * 运行时永不调 LLM。本工具的 LLM 提案是**线下**产物, 落 proposals/ 由人审,
    审核后并入 config/table_schemas.yaml 才会被运行时使用。
  * 报告里 MISS = 有行但无实体, 即"缺口", 必须显式可见而非静默丢失。

用法:
  .venv\\Scripts\\python.exe scripts/table_schema_report.py
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --doc "PN2000-24A 规格书.md"
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --blocks rag_storage/blocks/X.jsonl
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --scope 4.3   # 只看某章节 (CI 门禁用)
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --propose
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --propose --llm

退出码: 有缺口返回 1 (可用于 CI 门禁); 配合 --scope 可只对目标章节设门禁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from aterag.config import get_settings  # noqa: E402
from aterag.ingest.markdown_parser import Block, parse_file  # noqa: E402
from aterag.ingest.table_schema import (  # noqa: E402
    DEFAULT_SCHEMA_PATH,
    SchemaRegistry,
    load_registry,
    signature_of,
)

PROPOSAL_DIR = "proposals"
MAX_SAMPLE_ROWS = 3

PASS = "✅"
FAIL = "❌"
IGNORED = "➖"


def schema_path() -> str:
    """表结构档案路径。

    本工具是纯静态分析, 不该因为缺 PG/Qdrant/LLM 环境变量就跑不起来
    (CI 与开发者本机的环境变量集合本就不同)。故只在环境变量可用时读 Settings,
    否则退回默认相对路径。
    """
    try:
        return get_settings().table_schemas_path
    except Exception:  # noqa: BLE001 缺 POSTGRES_DSN 等必填项时用默认值
        return DEFAULT_SCHEMA_PATH


def collect_tables(
    blocks: list[Block], scope: str = ""
) -> list[tuple[str, list[str], list[list[str]]]]:
    """(section_path, 表头, 数据行) —— 逐表收集, 便于定位。

    scope 非空时只收该章节号前缀之下的表 (带点边界: 4.3 命中 4.3.1, 不命中 4.31)。
    """
    out = []
    for b in blocks:
        sp = b.section_path or ""
        if scope and not (sp == scope or sp.startswith(scope + ".")):
            continue
        for t in b.tables:
            if t:
                out.append((sp or "?", t[0], t[1:]))
    return out


def load_blocks(args) -> tuple[list[Block], str]:
    if args.blocks:
        blocks = [
            Block(
                chunk_id=d["chunk_id"],
                heading=d["heading"],
                level=d["level"],
                parent_headings=d.get("parent_headings", []),
                section_path=d.get("section_path", ""),
                text=d.get("text", ""),
                tables=d.get("tables", []),
                is_table_block=d.get("is_table_block", False),
            )
            for d in (
                json.loads(line)
                for line in Path(args.blocks).read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        ]
        return blocks, args.blocks
    doc = args.doc or "PA601-D54A 定制电源技术规格书.md"
    return parse_file(doc), doc


def report(blocks: list[Block], reg: SchemaRegistry, scope: str = "") -> tuple[dict, dict]:
    """聚合每个表头签名的结论。scope 非空时只统计该章节前缀下的表。"""
    agg: dict[str, dict] = OrderedDict()
    for section, header, rows in collect_tables(blocks, scope):
        sig = signature_of(header)
        det = reg.detect(header, rows)
        entry = agg.setdefault(
            sig,
            {
                "signature": sig,
                "header": list(det.header),
                "schema": det.schema.name if det.schema else None,
                "entity": det.schema.entity if det.schema else None,
                "meta": bool(det.schema and det.schema.meta),
                "reference": bool(det.schema and det.schema.reference),
                "unmapped": det.unmapped_headers(),
                "roles": det.roles,
                "count": 0,
                "rows": 0,
                "sections": set(),
                "samples": [],
            },
        )
        entry["count"] += 1
        entry["rows"] += len(rows)
        entry["sections"].add(section)
        if len(entry["samples"]) < MAX_SAMPLE_ROWS and rows:
            entry["samples"].append([" | ".join(c) for c in rows[:MAX_SAMPLE_ROWS]])
    return agg, {}


def print_report(agg: dict, reg: SchemaRegistry, source: str, scope: str = "") -> list[str]:
    scope_txt = f"  范围: {scope}*" if scope else "  范围: 全文档"
    print(f"=== 表结构覆盖报告: {source} ===")
    print(f"档案: {reg.source_path}  ({len(reg.schemas)} 个 schema)")
    print(f"{scope_txt}\n")
    missing: list[str] = []
    for sig, e in agg.items():
        if e["meta"]:
            tag, mark = "已知忽略", IGNORED
        elif e["reference"]:
            tag, mark = f"{e['schema']} -> 参考表(不产实体)", IGNORED
        elif e["schema"]:
            tag, mark = f"{e['schema']} -> {e['entity']}", PASS
        else:
            tag, mark = "缺口 (T1 仅兜底, 不产实体)", FAIL
            missing.append(sig)
        secs = ",".join(sorted(s for s in e["sections"] if s != "?")[:4]) or "?"
        print(f"{mark} [{tag}]  x{e['count']} 表 / {e['rows']} 行  章节: {secs}")
        print(f"    表头: {sig[:110]}")
        if e["unmapped"] and e["schema"]:
            print(f"    未映射列(按 schema 忽略): {e['unmapped']}")
        if not e["schema"]:
            print(
                "    T1 兜底角色: "
                + ", ".join(f"{r.header or r.index}={r.role}" for r in e["roles"])
            )
            for s in e["samples"]:
                print(f"    样例: {s[:110]}")
    total = len(agg)
    n_meta = sum(1 for e in agg.values() if e["meta"])
    n_ref = sum(1 for e in agg.values() if e["reference"])
    print(f"\n{'=' * 60}")
    print(
        f"表头签名 {total} 个 | 已映射 {total - len(missing) - n_ref} | 参考表 {n_ref} | "
        f"已知忽略 {n_meta} | 缺口 {len(missing)}"
    )
    if missing:
        print("\n缺口清单 (行已保留在 blocks.jsonl, 但不产生实体):")
        for i, sig in enumerate(missing, 1):
            print(f"  {i}. {sig[:100]}")
    return missing


def _llm_prompt(header: list[str], samples: list[str], fields: list[str]) -> str:
    return f"""你是电源/通信规格书解析专家。下面是一张从技术规格书中抽出的表格。

表头: {" | ".join(header)}
数据行样例:
{chr(10).join("  " + s for s in samples)}

请判断该表属于哪一类, 并把表头映射到规范字段。规范字段只能从下列白名单中选:
{", ".join(fields)}

回答要求:
1. `table_kind`: 表格类型 (如 parameter / signal_definition / telemetry / test_matrix / reference / meta)
2. `entity`: requirement | signal | attribute | none
3. `columns`: 表头 -> 规范字段 的映射; 无法归类的表头直接省略
4. `reason`: 一句话理由

只输出 JSON, 不要任何解释文字。示例:
{{"table_kind": "telemetry", "entity": "requirement", "columns": {{"编号": "req_id", "遥测量": "subject"}}, "reason": "遥测量+检测范围"}}
"""


def build_proposals(agg: dict, reg: SchemaRegistry, use_llm: bool) -> dict:
    """为缺口表头生成候选 schema (T1 确定性骨架, --llm 时叠加语义建议)。"""
    fields = sorted(reg.known_fields)
    schemas: dict = {}
    for sig, e in agg.items():
        if e["schema"] or e["meta"]:
            continue
        name = f"auto_{len(schemas) + 1:02d}_{'_'.join(r.role for r in e['roles'][:2])}"
        skeleton: dict = {
            "match": {"require": []},
            "entity": "none",
            "row_strategy": "positional",
            "columns": {},
        }
        for r in e["roles"]:
            skeleton["columns"][r.header or f"col_{r.index}"] = ""
        if use_llm:
            try:
                from aterag.models import LLMClient

                client = LLMClient(get_settings())
                raw = asyncio.run(
                    client.chat(
                        [
                            {
                                "role": "user",
                                "content": _llm_prompt(e["header"], e["samples"], fields),
                            }
                        ],
                        temperature=0.0,
                        max_tokens=600,
                    )
                )
                obj = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
                cols = {
                    str(k): str(v)
                    for k, v in (obj.get("columns") or {}).items()
                    if str(v) in fields
                }
                skeleton["columns"] = cols or skeleton["columns"]
                skeleton["entity"] = obj.get("entity") or "none"
                skeleton["row_strategy"] = (
                    "anchor_align" if skeleton["entity"] != "none" else "free"
                )
                skeleton["_proposal"] = {
                    "table_kind": obj.get("table_kind", ""),
                    "reason": obj.get("reason", ""),
                    "status": "待人工审核 (LLM 建议, 未经确认不得直接并入档案)",
                }
            except Exception as e:  # noqa: BLE001  提案失败不影响确定性骨架
                skeleton["_proposal"] = {"error": f"LLM 提案失败: {e}"}
        schemas[name] = skeleton
    return {"version": 1, "note": "由 table_schema_report.py --propose 生成", "schemas": schemas}


def main() -> int:
    ap = argparse.ArgumentParser(description="表结构覆盖报告与映射提案")
    ap.add_argument("--doc", help="规格书 md 路径 (默认 PA601)")
    ap.add_argument("--blocks", help="已解析的 blocks.jsonl 路径 (比读 md 快)")
    ap.add_argument("--propose", action="store_true", help="为缺口表头生成候选 schema 提案")
    ap.add_argument("--llm", action="store_true", help="提案时调用 LLM 给出语义映射建议 (线下)")
    ap.add_argument(
        "--scope",
        help="只检查该章节号前缀之下的表 (如 4.3); 与缺口判定组合即可做范围门禁",
    )
    ap.add_argument("--out", help="提案输出路径 (默认 proposals/table_schemas.proposed.yaml)")
    args = ap.parse_args()

    reg = load_registry(schema_path())
    blocks, source = load_blocks(args)
    agg, _ = report(blocks, reg, args.scope)
    missing = print_report(agg, reg, source, args.scope)

    if not args.propose:
        return 1 if missing else 0

    out = Path(args.out or (Path(PROPOSAL_DIR) / "table_schemas.proposed.yaml"))
    doc = build_proposals(agg, reg, args.llm)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# 由 scripts/table_schema_report.py --propose 生成 —— 提案, 非生效配置\n"
        "# 生效配置是 config/table_schemas.yaml; 本文件须经人工审核后手动合并\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\n提案已写入: {out}  ({len(doc['schemas'])} 个候选 schema)")
    print("下一步: 人工审核列映射 -> 合并进 config/table_schemas.yaml -> 重跑本报告验收")
    if args.llm:
        print("注意: LLM 建议仅为草案, 未经确认不会影响运行时行为")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
