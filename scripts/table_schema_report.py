"""表结构覆盖报告 + 映射提案生成 (T1/T2/T3 的运维工具).

三个用途:
  1. 验收    —— 新规格书入库前确认所有表都被映射 (无 MISS)
  2. 接入    —— 换文档模板时, 由本工具列出待映射表头, 人工补 config/table_schemas.yaml
  3. 提案    —— --propose 生成候选 schema 骨架 (T1 确定性), --llm 再叠加语义映射建议
              --propose-profile 生成候选**档案画像** (章节树/关键词/section_priors),
              含与 test_methods.yaml 的 role 预检 (方案 §11.6, A20 的痛点)

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
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --propose-profile
  .venv\\Scripts\\python.exe scripts/table_schema_report.py --propose-profile --llm

退出码: 有缺口返回 1 (可用于 CI 门禁); 配合 --scope 可只对目标章节设门禁。
        --propose-profile 且 role 预检不通过返回 2 (角色配不平, 与"表没映射"区分)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from aterag.config import get_settings  # noqa: E402
from aterag.extract.supplement import ROLE_ANY, ROLE_ANY_EXCEPT, MethodBook  # noqa: E402
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

只输出 JSON, 不要任何解释文字。示例(注意冒号后有空格, 必须整体加引号):
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


# ---------------- 出口二: 模板画像提案 (方案 §11.6) ----------------
#
# 定位: **辅助工具, 不承担检测职责**。检测归抽取过程的 fail-closed; 本工具只
# 负责「帮人更快选对参数」。所以它坏了只影响方便, 不影响正确性 —— 不必追求
# 100% 可靠 (A17 之前 --propose 崩溃就属于「坏了但不影响正确性」)。
#
# 它存在的理由是 A20 的痛点: 手写完 profile 要**跑一遍抽取**才知道 role 写没写
# 写错。这里在提案阶段就把那个检查做掉。


#: 标题里的需求编号前缀 (PA601 写成 "4.2.1 SR-PA601-D54A-0300 结构要求")。
#: 章节选择器要的是标题语义, 编号只是这份文档的实例编号 —— 换产品就变。
_REQ_ID_RE = re.compile(r"^\s*(?:SR[-_][A-Za-z0-9_-]+)\s+")


def split_heading(heading: str) -> tuple[str, str]:
    """``"4.2.1 SR-PA601-D54A-0300 结构要求"`` -> ``("4.2.1", "结构要求")``。

    两级剥离: 先去章节编号, 再去需求编号。前者是编号体系的产物, 后者是本文档
    的实例编号 —— 两者都会随换文档而变, 而 ``section_keywords`` 要的恰恰是
    跨文档稳定的标题语义。
    """
    h = (heading or "").strip()
    num = ""
    if " " in h:
        head, rest = h.split(" ", 1)
        if re.fullmatch(r"\d+(?:\.\d+)*", head):
            num, h = head, rest.strip()
    h = _REQ_ID_RE.sub("", h).strip()
    return num, h


def section_tree(blocks: list[Block], scope: str = "") -> list[dict]:
    """实际章节树 (编号 + 标题 + 层级 + 表数), 确定性, 直接读 blocks。

    两个容易漏掉的点, 都在这里处理了:

    1. **去重**: blocks 是**分段**的, 一个章节常有标题块 + 表格块 + 正文块,
       逐块列会把同一章节重复三遍。
    2. **父章节**: ``4.3 功能/性能要求`` 这类章节往往只有子章节的块, 自己没有
       块, 只出现在子块的 ``parent_headings`` 里。而章节选择器要的恰恰是父
       章节 (PA601 档案的 ``section_keywords`` 就是它) —— 只遍历块会把最该
       选的那个章节漏掉。
    """
    out: dict[str, dict] = {}

    def row_for(sp: str, heading: str, level: int, from_parent: bool) -> dict:
        return out.setdefault(
            sp or f"?{heading}",
            {
                "section_path": sp,
                "heading": heading,
                "level": level,
                "tables": 0,
                "blocks": 0,
                "from_parent": from_parent,
            },
        )

    for b in blocks:
        # 先补父章节: 它们可能没有自己的块, 但必须在树里。
        depth = 1
        for ph in b.parent_headings:
            num, title = split_heading(ph)
            if not title:
                continue
            # heading 存原文 (含编号), 语义由 split_heading 现算 —— 树要能对照文档。
            row_for(num, ph, depth, True)
            depth += 1

        sp = b.section_path or ""
        if scope and not (sp == scope or sp.startswith(scope + ".")):
            continue
        row = row_for(sp, b.heading, b.level, False)
        row["blocks"] += 1
        row["tables"] += len([t for t in b.tables if t])
        if not row["heading"]:
            row["heading"] = b.heading

    def _sort_key(r: dict) -> tuple:
        parts = [int(x) for x in r["section_path"].split(".") if x.isdigit()]
        return (parts or [0], r["level"], r["heading"])

    return sorted(out.values(), key=_sort_key)


def candidate_keywords(tree: list[dict]) -> list[dict]:
    """候选 ``section_keywords``: 去掉编号后的标题片段, 带出处章节与表数。

    与既有档案一致 (``doc_profiles.yaml`` 里写的是 ``功能/性能要求`` 这种标题
    片段), 所以这里给的是同一种东西。不做筛选 —— 选哪个是人的判断, 工具只负责
    把料摆齐; 带出处与表数是为了让人不必回文档里逐条查。

    排序: 有表格的在前 (章节选择器多半要选装表的那种), 其次按标题。
    """
    merged: dict[str, dict] = {}
    for row in tree:
        _num, frag = split_heading(row["heading"])
        if not frag:
            continue
        item = merged.setdefault(frag, {"keyword": frag, "sections": [], "tables": 0})
        if row["section_path"]:
            item["sections"].append(row["section_path"])
        item["tables"] += row["tables"]
    items = list(merged.values())
    for it in items:
        it["sections"] = sorted(it["sections"])
    return sorted(items, key=lambda i: (-i["tables"], i["keyword"]))


def role_vocabulary(methods: MethodBook) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(方法库引用到的角色, 方法库自带角色)。

    自带角色 (``any`` / ``any_except_other``) 是匹配指令而非角色名, 提案里不能
    建议使用; 其余的必须由档案 ``section_priors`` 声明 —— 这就是 A20 那条报错
    的全部由来。
    """
    referenced = tuple(
        sorted(
            {
                m.role
                for m in methods.methods
                if m.role and m.role not in (ROLE_ANY, ROLE_ANY_EXCEPT)
            }
        )
    )
    return referenced, (ROLE_ANY, ROLE_ANY_EXCEPT)


def precheck_roles(
    candidate_roles: dict[str, str], methods: MethodBook
) -> dict:
    """与 ``test_methods.yaml`` 的 role 一致性预检 —— 提案阶段就把 A20 那条报错做掉。

    返回三段:
    - ``uncovered``: 方法库引用了、但候选档案没声明的角色。**这些正是抽取时会
      抛 ``ValueError: methods[x].applies_to.role 非法`` 的那些**, 提前点名。
    - ``unused``: 候选档案声明了、但没有方法引用的角色 (无害, 只是提一句)。
    - ``ok``: 是否自洽 —— 提案阶段就能判定, 不必等跑抽取。
    """
    referenced, builtin = role_vocabulary(methods)
    declared = set(candidate_roles.values())
    uncovered = [r for r in referenced if r not in declared]
    unused = [r for r in sorted(declared) if r not in referenced]
    return {
        "referenced_by_methods": list(referenced),
        "builtin_instruction_roles": list(builtin),
        "declared_in_proposal": sorted(declared),
        "uncovered": uncovered,
        "unused": unused,
        "ok": not uncovered,
        "note": (
            "uncovered 非空 -> 照此提案并入 doc_profiles.yaml 后, 抽取会在"
            "加载期抛 ValueError(方案 §11.6 A20); 请为这些角色补 section_priors, "
            "或确认该文档确实没有对应章节并改方法库"
        ),
    }


def _profile_prompt(tree: list[dict], vocabulary: tuple[str, ...]) -> str:
    lines = "\n".join(
        f"  {r['section_path'] or '?'} {r['heading']} (level={r['level']}, 表 {r['tables']} 张)"
        for r in tree
    )
    return f"""你是电源/通信规格书解析专家。下面是一份新规格书的实际章节树。

章节树:
{lines}

请为每个**含表格的章节**建议一个角色, 角色只能从下列词表里选:
{", ".join(vocabulary)}

角色含义:
  input_domain        —— 输入侧/环境/供电来向(输入电压范围、输入电流、输入功率)
  output_spec         —— 输出侧规格(输出电压/电流/功率、精度、纹波、调整率)
  protection_response —— 保护动作与其触发门限/恢复条件
  signal_io           —— 信号/接口/引脚定义(遥测、PWM、使能、告警)
  other               —— 以上都不是

回答要求:
1. `priors`: 数组, 每项形如 {{"section": "4.3.1", "role": "output_spec", "reason": "一句话理由"}}
2. `keywords`: 候选 section_keywords 数组(标题片段, 供章节选择用)
3. `exclude_words`: 候选剔除词数组(像"不适用""无要求"的段落)

只输出 JSON, 不要解释文字。示例:
{{"priors": [{{"section": "4.3.1", "role": "output_spec", "reason": "输出电压/电流精度表"}}], "keywords": ["功能/性能要求"], "exclude_words": ["不适用"]}}
"""


def build_profile_proposal(
    blocks: list[Block],
    agg: dict,
    reg: SchemaRegistry,
    methods: MethodBook,
    use_llm: bool,
    scope: str = "",
) -> dict:
    """候选档案画像: 章节树 + 候选关键词 + 候选 section_priors + role 预检。"""
    tree = section_tree(blocks, scope)
    keywords = candidate_keywords(tree)
    referenced, builtin = role_vocabulary(methods)
    # 角色词表来自方法库引用 + 既有档案的约定角色; 提案只能在词表内选。
    vocabulary = tuple(referenced) or ("input_domain", "output_spec", "protection_response",
                                       "signal_io", "other")

    priors: dict[str, dict] = {}
    proposal_meta: dict = {}
    if use_llm:
        try:
            from aterag.models import LLMClient

            client = LLMClient(get_settings())
            raw = asyncio.run(
                client.chat(
                    [{"role": "user", "content": _profile_prompt(tree, vocabulary)}],
                    temperature=0.0,
                    max_tokens=1200,
                )
            )
            obj = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            for item in obj.get("priors") or []:
                sec = str(item.get("section", ""))
                role = str(item.get("role", ""))
                if not sec or role not in vocabulary:
                    continue
                priors[sec] = {"role": role, "reason": str(item.get("reason", ""))}
            llm_keywords = [str(k) for k in (obj.get("keywords") or [])]
            if llm_keywords:
                # LLM 提的关键词可能不在实测标题里 —— 那样章节选择会永远选不中。
                # 只保留确实出现过的, 其余单列, 不静默丢弃也不静默接受。
                known = {k["keyword"] for k in keywords}
                rejected = [k for k in llm_keywords if k not in known]
                proposal_meta["llm_keywords_rejected"] = rejected
                keywords = [
                    {"keyword": k, "sections": [], "tables": -1} for k in llm_keywords if k in known
                ] + [k for k in keywords if k["keyword"] not in set(llm_keywords)]
            proposal_meta["exclude_words"] = [
                str(w) for w in (obj.get("exclude_words") or [])
            ]
            proposal_meta["llm"] = {
                "status": "待人工审核 (LLM 建议, 未经确认不得直接并入档案)",
                "tried_priors": len(obj.get("priors") or []),
                "accepted_priors": len(priors),
            }
        except Exception as e:  # noqa: BLE001 提案失败不影响确定性部分
            proposal_meta["llm"] = {"error": f"LLM 画像失败: {e}"}
    else:
        proposal_meta["note"] = (
            "未加 --llm: 只给出确定性部分(章节树/候选关键词/role 预检), "
            "section_priors 的 role 需人工指定"
        )

    candidate_roles = {sec: p["role"] for sec, p in priors.items()}
    precheck = precheck_roles(candidate_roles, methods)

    schemas_by_section: dict[str, list[str]] = {}
    for sig, e in agg.items():
        for sec in e["sections"]:
            schemas_by_section.setdefault(sec, []).append(sig)

    return {
        "version": 1,
        "note": "由 table_schema_report.py --propose-profile 生成",
        "_proposal": {
            "status": "待人工审核",
            "generated_by": "scripts/table_schema_report.py --propose-profile",
            "scope": scope,
            **proposal_meta,
        },
        "section_tree": tree,
        "candidate_section_keywords": keywords,
        "table_signatures": [
            {
                "signature": sig,
                "schema": e["schema"],
                "entity": e["entity"],
                "tables": e["count"],
                "rows": e["rows"],
                "sections": sorted(s for s in e["sections"] if s != "?"),
            }
            for sig, e in agg.items()
        ],
        "section_priors": priors,
        "role_precheck": precheck,
        "usage_note": (
            "本文件是提案, 非生效配置。生效配置是 config/doc_profiles.yaml; "
            "并入前请逐条确认 role 与理由, 并先跑 role_precheck。"
        ),
    }


def _emit_profile_proposal(args, blocks, agg, reg, source) -> int:
    """档案画像提案出口 (方案 §11.6)。返回退出码: 预检不通过返回 2。

    退出码 2 与"有缺口"的 1 区分开: 前者是**角色配不平** (并进去必然在加载期
    抛错), 后者是**表还没映射**。混成一个码, 门禁脚本就分不清该找谁。
    """
    methods = MethodBook.load()
    doc = build_profile_proposal(blocks, agg, reg, methods, args.llm, args.scope)
    out = Path(args.out or (Path(PROPOSAL_DIR) / "profile.proposed.yaml"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# 由 scripts/table_schema_report.py --propose-profile 生成 —— 提案, 非生效配置\n"
        "# 生效配置是 config/doc_profiles.yaml; 本文件须经人工审核后手动合并\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    pc = doc["role_precheck"]
    print(f"\n档案画像提案已写入: {out}")
    print(
        f"  章节 {len(doc['section_tree'])} 个 | 候选关键词 {len(doc['candidate_section_keywords'])} 条 | "
        f"表头签名 {len(doc['table_signatures'])} 个 | section_priors {len(doc['section_priors'])} 条"
    )
    print(
        f"  role 预检: 方法库引用 {len(pc['referenced_by_methods'])} 个角色, "
        f"提案声明 {len(pc['declared_in_proposal'])} 个"
    )
    if pc["uncovered"]:
        print(f"  {FAIL} 未覆盖角色 (并入后抽取会在加载期抛错): {', '.join(pc['uncovered'])}")
        print(f"       {pc['note']}")
    else:
        print(f"  {PASS} role 自洽: 方法库引用的角色全部有对应 section_priors")
    if pc["unused"]:
        print(f"  {IGNORED} 无方法引用的角色 (无害): {', '.join(pc['unused'])}")
    print("下一步: 人工审核 role 与理由 -> 合并进 config/doc_profiles.yaml -> 重跑抽取验收")
    if args.llm:
        print("注意: LLM 建议仅为草案, 未经确认不会影响运行时行为")
    return 2 if not pc["ok"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="表结构覆盖报告与映射提案")
    ap.add_argument("--doc", help="规格书 md 路径 (默认 PA601)")
    ap.add_argument("--blocks", help="已解析的 blocks.jsonl 路径 (比读 md 快)")
    ap.add_argument("--propose", action="store_true", help="为缺口表头生成候选 schema 提案")
    ap.add_argument(
        "--propose-profile",
        action="store_true",
        help="生成候选档案画像 (章节树/候选关键词/section_priors/role 预检)",
    )
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

    if args.propose_profile:
        return _emit_profile_proposal(args, blocks, agg, reg, source)

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
