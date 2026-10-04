"""从方案 md 抽取「电源产品通用知识」-> Semantica / LightRAG 初始数据(一次性引导)。

    python scripts/build_seed_data.py
    python scripts/build_seed_data.py --spec <方案.md> --out data/seed

## 定位: 一次性引导, 不是常设管线

方案 md 是**引导源**。交付后不再依赖它 —— 后续补充知识直接编辑
``data/seed/power_domain_seed.json``, 不必回到方案, 也不必重跑本脚本。

本脚本保留只为**可复现**: 万一初始数据要从头重建, 能从同一份输入得到同一份
输出。它是自包含的(不 import 任何项目模块), 因为它服务于**架构之外**的引导 ——
架构内的知识维护是直接编辑 JSON。

## 输出形态

Semantica ``SeedDataManager.create_foundation_graph()`` 读 ``entities`` /
``relationships``; 实体取 ``id`` / ``name`` / ``type`` / ``text`` / ``properties``,
关系取 ``source`` / ``target`` / ``type`` / ``properties``。

**一份文件装全部**, 不按类型切碎 —— 关系要跨类型连边(拓扑连公式、公式连公理、
公式连勘误), 切开就断了。LightRAG 侧读同一份 JSON 的 ``text`` 字段做图谱抽取,
所以 ``text`` 必须是**自然语言陈述**, 而不是符号串。

## 审计形态

逐属性挂出处, 对齐 ``semantica.provenance.schemas.PropertySource`` /
``SourceReference``: 每个属性值带自己的 ``sources``, 而不是整个实体挂一个 ——
同一实体的不同字段可能来源不同(符号的量纲来自 U.5, 中文名来自术语表)。

``confidence`` 只给**查证过**的来源。方案原文未查证 -> ``None``, 不给 1.0:
「来自方案」不是「已验证」。给 1.0 会让所有未验证知识在审计里显得与已验证等价。

## SHACL 兼容

``properties.source_kind`` 区分 ``section``(方案章节号)与 ``standard``(真标准号)。
两者混同会让下游把章节号当认证依据(§18.10 注 8), 所以它是**受约束字段**,
不是自由文本。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: 归一化基准。满载自身比例恒为 1.0, 任何 xx%载 都相对它折算。
#: 「50%载」的 50% 是**相对满载**的, 不是绝对输出功率的百分数 —— 这条弄反了
#: 所有代入都会错, 而算式照样成立。
LOAD_CONDITIONS: tuple[dict[str, Any], ...] = (
    {"zh": "满载", "en": "full load", "ratio": 1.0, "kind": "load",
     "note": "归一化基准。任何 xx%载 都相对它折算。"},
    {"zh": "半载", "en": "half load", "ratio": 0.5, "kind": "load",
     "note": "= 50%载 = 满载 × 50%。"},
    {"zh": "空载", "en": "no load", "ratio": 0.0, "kind": "load",
     "note": "输出为 0。「空载」不等于「轻载」: 空载下常降频, 损耗模型不适用同一套参数。"},
    {"zh": "最小载", "en": "minimum load", "ratio": None, "kind": "load",
     "note": "比例待确认: 最小稳定负载由具体拓扑决定, 不是常数。收录但不给比例 —— "
             "编一个数会让规则在错误工况上运行且不报错。"},
    {"zh": "额定", "en": "rated", "ratio": None, "kind": "rating",
     "note": "保证工作点/保证极限。与「标称」不同, 不可互换。不给比例。"},
    {"zh": "标称", "en": "nominal", "ratio": None, "kind": "nominal",
     "note": "常规取整的代表值, 无保证含义。不给比例。"},
    {"zh": "最大额定", "en": "absolute maximum rating", "ratio": None, "kind": "rating",
     "note": "超过即可能损坏的绝对上限, 与「额定工作值」不是一回事。"},
)

_BOOTSTRAP = "V6.0 开发指导方案"


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _clean(text: str | None) -> str | None:
    """去 markdown 装饰: 反引号、星号、尾随的 ✅ 与标点。"""
    if text is None:
        return None
    out = text.replace("`", "").replace("**", "").strip()
    out = out.strip(" ✅✓*")
    return out or None


def source_ref(
    ref: str | None, *, kind: str, line_no: int | None = None, confidence: float | None = None
) -> dict[str, Any]:
    """构造 ``semantica.provenance.schemas.SourceReference`` 形态的出处。

    ``kind`` 取 ``section`` / ``standard``, 是**受约束字段**: 它决定下游能不能
    把这个出处当认证依据。
    """
    return {
        "document": _BOOTSTRAP if kind == "section" else (ref or ""),
        "section": ref,
        "line": line_no,
        "confidence": confidence,
        "metadata": {"source_kind": kind},
    }


def _props(
    values: dict[str, Any], ref: str | None, kind: str, line_no: int | None
) -> dict[str, Any]:
    """组装 properties + 逐属性 provenance。

    只给**有值**的属性挂 provenance —— 给空值挂一条出处是在声称「这个空值也有
    来源」, 那会让冲突检测把「未提供」误判成「两处来源不一致」。
    """
    out = {k: v for k, v in values.items() if v is not None}
    out["source_ref"] = ref
    out["source_kind"] = kind
    out["provenance"] = {
        k: {
            "property_name": k,
            "value": v,
            "sources": [source_ref(ref, kind=kind, line_no=line_no)],
        }
        for k, v in out.items()
        if k not in ("source_ref", "source_kind", "provenance")
    }
    return out


def build_section_index(lines: list[str]) -> dict[int, str]:
    """行号 -> 最近的章节号(``12.3.4`` 形态)。"""
    index: dict[int, str] = {}
    pattern = re.compile(r"^#{2,4}\s*(\d+(?:\.\d+)*)\s+\S")
    for i, line in enumerate(lines, 1):
        m = pattern.match(line)
        if m:
            index[i] = m.group(1)
    return index


def _section_of(line_no: int, index: dict[int, str]) -> str | None:
    for probe in range(line_no, 0, -1):
        if probe in index:
            return index[probe]
    return None


# ---------------------------------------------------------------------------
# 各类知识
# ---------------------------------------------------------------------------


def extract_concepts(lines: list[str], sections: dict[int, str]) -> list[dict[str, Any]]:
    """概念字典 -> ``power_concept``。

    ``| CONCEPT_ID | 中文 | english | 别名 | qudt:X |``

    概念字典在方案里**出现两次**(实测第 3163 与第 15140 行起)。按 ID 去重并留
    **第一次**出现的位置 —— 留后一份会让 ``source_ref`` 指向复制处。
    """
    out: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"^\|\s*(?P<id>[A-Z][A-Z0-9_]{2,})\s*\|")
    for i, line in enumerate(lines, 1):
        m = pattern.match(line.strip())
        if m is None:
            continue
        cells = _cells(line)
        if len(cells) < 3:
            continue
        cid = m.group("id")
        if cid in out:
            continue
        zh, en = _clean(cells[1]), _clean(cells[2])
        qudt = None
        alias = None
        for cell in cells[3:]:
            if cell.startswith("qudt:"):
                qudt = cell.split(":", 1)[1].strip()
            elif _clean(cell) and not cell.startswith("qudt"):
                alias = _clean(cell)
        sec = _section_of(i, sections)
        out[cid] = {
            "id": cid,
            "name": zh or en or cid,
            "type": "power_concept",
            "text": f"{cid}: {zh or ''} {en or ''}".strip(": "),
            "properties": _props(
                {"zh": zh, "en": en, "aliases": alias, "qudt_ref": qudt},
                f"V6.0§{sec}" if sec else None,
                "section",
                i,
            ),
        }
    return list(out.values())


_U5_HEADER = re.compile(r"^\|\s*符号\s*\|\s*含义\s*\|")


def extract_symbols(lines: list[str]) -> list[dict[str, Any]]:
    """U.5 符号表 -> ``symbol``。

    ``| `V_in`, `V_out` | 电压 | `[V]` | `E` |`` —— 一格可含**多个符号**,
    逐个拆成独立节点(否则 ``V_in`` 与 ``V_out`` 会挤成一个节点, 关系连不上)。

    量纲未定的(方案写「见各条」或格子数不等)也收, 但 ``dimension=None``:
    收进来是为了让关系边完整, 而不是因为它可参与计算。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    in_table = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if _U5_HEADER.match(stripped):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            if stripped.startswith("#"):
                in_table = False
            continue
        cells = _cells(stripped)
        if len(cells) < 3 or set(cells[0]) <= {"-", ":"}:
            continue
        meaning, dim_text = _clean(cells[1]), _clean(cells[2])
        ns = _clean(cells[3]) if len(cells) > 3 else None
        for sym in re.split(r"[,，]", cells[0]):
            name = _clean(sym)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(
                {
                    "id": f"sym::{name}",
                    "name": name,
                    "type": "symbol",
                    "text": f"{name}: {meaning}" if meaning else name,
                    "properties": _props(
                        {
                            "zh": meaning,
                            "dimension": dim_text,
                            "namespaces": [ns] if ns else None,
                            "dimension_declared": bool(dim_text and dim_text not in ("见各条",)),
                        },
                        "V6.0§U.5",
                        "section",
                        i,
                    ),
                }
            )
    return out


#: 公式表行: | `F_J.2.1_BUCK` | `V_out = D × V_in` | `[V]` | A-2, A-4, T3 |
#:
#: ID 必须**独占一格**(整格就是 ID), 不能用「行里出现 F_ 就算」—— 后者会把
#: 「某规则的公式列写着 F_J.2」这类引用行也当成公式, 实测那样只抽到 11 条。
_FORMULA_ROW = re.compile(r"^\|\s*`(?P<fid>F_[A-Z]\.[\d.]+(?:_[A-Z0-9_]+)?)`\s*\|")


def extract_formulas(lines: list[str], sections: dict[int, str]) -> list[dict[str, Any]]:
    """公式表 -> ``formula``。

    ``| `F_J.2.1_BUCK` | `V_out = D × V_in` | `[V]` | A-2, A-4, T3 |``

    **保留原始表达式文本**, 不做归一化 —— 归一化是为量纲齐次性服务的, 而那不是
    本层的职责; 概念层要的是「这条式子说了什么」, LightRAG 抽实体时看到的也应是
    工程师写的式子, 而不是被改写过的形式。

    ``declared_dimension`` 存方案自己写的量纲列(``[V]`` 之类)。**照抄, 不校验**
    —— 校验是另一层的职责; 这里只负责「方案说了什么」, 标明它只是方案的声明。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        m = _FORMULA_ROW.match(stripped)
        if m is None:
            continue
        fid = m.group("fid")
        cells = _cells(stripped)
        if len(cells) < 2:
            continue
        # 第一个含等号或运算符的格是表达式; 再往后是量纲与公理列
        expr = dim = upstream = None
        for c in cells[1:]:
            text = _clean(c)
            if text is None:
                continue
            if expr is None and re.search(r"[=≈≤≥<>·×/]", text):
                expr = text
                continue
            if dim is None and (text.startswith("[") or "无量纲" in text):
                dim = text
                continue
            if upstream is None and re.fullmatch(r"[AFGRPETUW][\w.]*(\s*[,，]\s*[AFGRPETUW][\w.]*)*", text):
                upstream = [t for t in re.split(r"[,，]", text)]
                continue
        if fid in seen:
            continue
        seen.add(fid)
        # **章节号取自公式 ID 本身**(``F_J.2.1_BUCK`` -> ``J.2.1``), 不用「最近的
        # markdown 标题」: 公式总表物理上位于附录 §18.10, 按标题定位会让 570 条
        # 公式的出处全指向 §18.10, 追不回「这条式子原本在方案哪一节」。
        own = re.match(r"^F_([A-Z]\.[\d.]+)", fid)
        sec = own.group(1) if own else None
        out.append(
            {
                "id": fid,
                "name": fid,
                "type": "formula",
                "text": f"{fid}: {expr}" if expr else fid,
                "properties": _props(
                    {
                        "section": sec,
                        "expr": expr,
                        "declared_dimension": dim,
                        "upstream": upstream,
                        "domain": fid[2],
                    },
                    f"V6.0§{sec}" if sec else None,
                    "section",
                    i,
                ),
            }
        )
    return out


def extract_standards(lines: list[str]) -> list[dict[str, Any]]:
    """标准表 -> ``standard``。

    **只收有编号的**。``PMBus`` / ``CAN FD`` 这类以名称标识的规范确实是真标准,
    但标识里没有可核对的编号; 放进初始数据后下游会把 ``std::PMBus`` 当成可引用
    的标准号, 而 §18.10 注 8 禁止编造标准条款号。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    header = re.compile(r"^\|\s*标准号\s*\|")
    in_table = False
    num_re = re.compile(r"^[A-Z]{1,5}(?:/T)?\s?\d")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if header.match(stripped):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            if stripped.startswith("#"):
                in_table = False
            continue
        cells = _cells(stripped)
        if len(cells) < 2 or set(cells[0]) <= {"-", ":"}:
            continue
        sid = _clean(cells[0])
        if not sid or sid in seen or not num_re.match(sid):
            continue
        seen.add(sid)
        # 列序是「标准号 | 版本 | 名称 | 适用范围 | 关联规则/公式」。
        # 版本格可能是「无」/「年」这类占位, 那不是版本号。
        version = _clean(cells[1]) if len(cells) > 1 else None
        if version in ("无", "年", "-", "—"):
            version = None
        title = _clean(cells[2]) if len(cells) > 2 else None
        scope = _clean(cells[3]) if len(cells) > 3 else None
        bindings = _clean(cells[4]) if len(cells) > 4 else None
        full = f"{sid}-{version}" if version else sid
        out.append(
            {
                "id": f"std::{full}",
                "name": title or full,
                "type": "standard",
                "text": f"{full} {title or ''}".strip(),
                "properties": _props(
                    {
                        "standard_id": full,
                        "version": version,
                        "title": title,
                        "scope": scope,
                        "bindings": bindings,
                        "citation_status": "UNVERIFIED",
                    },
                    full,
                    "standard",
                    i,
                ),
            }
        )
    return out


#: 勘误块: ``> **勘误 E-1**：…`` / ``> **勘误 E-4 说明**：…``
#:
#: 标签后允许有修饰词(实测 E-4 写作「E-4 说明」, 其余写作「E-1」)。早先一版
#: 写成 ``勘误\s*(E-\d+)\*\*`` —— 要求 ``**`` 紧跟编号, 于是 E-4 整条漏掉,
#: 而「6/7 条勘误」看起来仍是个合理数字, 不核对总数就发现不了。
_ERRATA = re.compile(r"^>\s*\*\*勘误\s*(E-\d+)(?:\s*[^:：*]*)?\*\*\s*[::]?\s*(.+)$")


def extract_errata(lines: list[str]) -> list[dict[str, Any]]:
    """勘误 -> ``erratum``。**正文必须进来** —— 只有标签是悬空引用。

    勘误是全套数据里最要命的一类标注: 它说的是「这条式子有个常见错法」。目标
    公式不在库里时, 这条警告就随公式一起消失, 而下游看到的仍是绿灯。
    """
    out = []
    for i, line in enumerate(lines, 1):
        m = _ERRATA.match(line.strip())
        if m is None:
            continue
        tag, text = m.group(1), m.group(2).strip()
        out.append(
            {
                "id": f"err::{tag}",
                "name": tag,
                "type": "erratum",
                "text": text,
                "properties": _props({"statement": text}, "V6.0§勘误", "section", i),
            }
        )
    return out


_AXIOM_ROW = re.compile(
    r"^\|\s*(?P<axiom_id>A-\d+)\s+(?P<axiom>[^|]+?)\s*\|"
    r"\s*(?P<theorem>T\d+[^|]*?)\s*\|"
    r"\s*(?P<formula_refs>[^|]*)\|\s*(?P<rules>[^|]*)\|\s*(?P<tests>[^|]*)\|"
)


def _split_refs(cell: str) -> list[str]:
    """``R5 通例`` / ``R1, R5`` 这类格 -> 干净的记号列表。

    只取记号本身, 丢掉中文说明 —— 留着会让关系的 target 变成 ``R5 通例`` 这种
    永远匹配不上的串。
    """
    out: list[str] = []
    for part in re.split(r"[,;、]", cell):
        token = (_clean(part) or "").split(" ")[0]
        if re.fullmatch(r"[AFGRPETUW]\.?[\w.]*", token):
            out.append(token)
    return out


def extract_axioms(lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """公理-定理索引 -> ``axiom`` + ``theorem`` 实体与关系。

    ``| A-1 KCL | T1 Tellegen | `F_E.1` … | R5 … | G.26 … |``

    **按 ID 形态匹配, 不按表位置** —— 早先一版按「第几张表」定位, 抓到术语表上,
    产出 192 条内容为「监督电流」「误报」的假公理。垃圾进图比缺图更难查:
    Semantica 会拿「误报是一条公理」去推理, 而且不报错。

    公理数是方案给定的(A-1 起连续), 不是「表里有几行」; 同一公理有多条定理,
    按 ``axiom_id`` 归并, 定理单独建节点。
    """
    axioms: dict[str, dict[str, Any]] = {}
    theorems: dict[str, dict[str, Any]] = {}
    rels: list[dict[str, Any]] = []
    for i, line in enumerate(lines, 1):
        m = _AXIOM_ROW.match(line.strip())
        if m is None:
            continue
        aid, label = m.group("axiom_id"), m.group("axiom").strip()
        tid, tname = m.group("theorem").split()[0], m.group("theorem").strip()
        node = axioms.setdefault(
            aid,
            {
                "id": aid,
                "name": label,
                "type": "axiom",
                "text": label,
                "properties": _props(
                    {"label": label, "theorems": [], "rules": [], "tests": [], "formula_refs": []},
                    "V6.0§附录U",
                    "section",
                    i,
                ),
            },
        )
        props = node["properties"]
        for key, cell in (
            ("theorems", [tid]),
            ("rules", _split_refs(m.group("rules"))),
            ("tests", _split_refs(m.group("tests"))),
            ("formula_refs", _split_refs(m.group("formula_refs"))),
        ):
            for token in cell:
                if token not in props[key]:
                    props[key].append(token)
        theorems.setdefault(
            tid,
            {
                "id": f"thm::{tid}",
                "name": tname,
                "type": "theorem",
                "text": tname,
                "properties": _props({"theorem_id": tid}, "V6.0§附录U", "section", i),
            },
        )
        rels.append({"source": aid, "target": f"thm::{tid}", "type": "has_theorem", "properties": {}})
    return list(axioms.values()) + list(theorems.values()), rels


def extract_load_conditions() -> list[dict[str, Any]]:
    """工况限定词 -> ``load_condition``。比例见 :data:`LOAD_CONDITIONS`。"""
    return [
        {
            "id": f"load::{item['zh']}",
            "name": item["zh"],
            "type": "load_condition",
            "text": f"{item['zh']} {item['en']}: ratio={item['ratio']}",
            "properties": _props(
                {
                    "en": item["en"],
                    "ratio": item["ratio"],
                    "kind": item["kind"],
                    "note": item["note"],
                },
                "V6.0§J.5",
                "section",
                None,
            ),
        }
        for item in LOAD_CONDITIONS
    ]


def build_relationships(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从实体属性推出关系边。

    只连**同一次抽取里真实存在**的节点 —— 指向不存在节点的边会让 Semantica 的
    foundation graph 校验失败, 而悬空边比缺边更难排查。
    """
    ids = {e["id"] for e in entities}
    rels: list[dict[str, Any]] = []

    def add(src: str, dst: str, rtype: str) -> None:
        if src in ids and dst in ids:
            rels.append({"source": src, "target": dst, "type": rtype, "properties": {}})

    for e in entities:
        props = e.get("properties") or {}
        eid = e["id"]
        if e["type"] == "power_concept" and props.get("qudt_ref"):
            # 指向**外部本体**, 不是本图节点 —— 用 IRI 形式, 不伪造本地 id
            rels.append(
                {
                    "source": eid,
                    "target": f"qudt:{props['qudt_ref']}",
                    "type": "has_unit_kind",
                    "properties": {"external": True, "ontology": "QUDT"},
                }
            )
        for ref in props.get("formula_refs") or ():
            add(eid, ref, "applies_to_formula")
        for aid in props.get("axiom_refs") or ():
            add(eid, aid, "derived_from_axiom")
    return rels


def to_seed_records(
    entities: list[dict[str, Any]], relationships: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """转成 Semantica ``SeedDataManager`` 认识的**扁平记录数组**。

    契约(读 ``semantica.seed.seed_manager.create_foundation_graph`` 的源码确认):
    实体与关系在**同一个数组**里, 靠键区分 ——
    ``id``(+``entity_type``) 是实体; ``source_id`` + ``target_id`` 是关系。
    早先一版输出 ``{"entities": [...], "relationships": [...]}`` 两个并列键,
    结果 ``create_foundation_graph()`` 载入 1096 个实体、**0 条关系**而
    ``validate_quality`` 仍报 ``valid: True`` —— 静默丢关系, 不报错。

    两个必须显式处理的点:

    - ``confidence`` **缺省是 1.0**。不显式给就等于宣称「这条知识已验证」。
      引导来的方案原文**未查证**, 所以显式给 ``None`` —— 让审计看见「未验证」,
      而不是让它显示成与已验证等价。
    - ``_record_to_entity`` 把**除已知键外的全部字段**塞进 ``metadata``。
      所以属性必须**平铺在记录顶层**, 放进嵌套的 ``properties`` 会变成
      ``metadata["properties"]["zh"]``, 多一层且与其它来源的形状不一致。
    """
    records: list[dict[str, Any]] = []
    for e in entities:
        props = e.get("properties") or {}
        record: dict[str, Any] = {
            "id": e["id"],
            "name": e["name"],
            "entity_type": e["type"],
            "text": e.get("text") or e["name"],
            # 引导数据未查证: 显式 None, 不让它落到缺省 1.0
            "confidence": props.get("confidence"),
        }
        for key, value in props.items():
            if key in ("confidence",):
                continue
            record[key] = value
        records.append(record)
    for r in relationships:
        records.append(
            {
                "source_id": r["source"],
                "target_id": r["target"],
                "relationship_type": r["type"],
                "confidence": None,
                **{k: v for k, v in (r.get("properties") or {}).items()},
            }
        )
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description="抽取电源产品通用知识 -> 初始数据(一次性引导)")
    ap.add_argument(
        "--spec",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md",
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "seed")
    args = ap.parse_args()
    if not args.spec.is_file():
        print(f"方案文件不在: {args.spec}", file=sys.stderr)
        return 2

    lines = args.spec.read_text(encoding="utf-8").splitlines()
    sections = build_section_index(lines)

    axiom_entities, axiom_rels = extract_axioms(lines)
    entities: list[dict[str, Any]] = []
    entities += extract_concepts(lines, sections)
    entities += extract_symbols(lines)
    entities += extract_formulas(lines, sections)
    entities += extract_standards(lines)
    entities += extract_errata(lines)
    entities += axiom_entities
    entities += extract_load_conditions()

    rels = build_relationships(entities) + axiom_rels

    deduped: dict[str, dict[str, Any]] = {}
    for e in entities:
        deduped.setdefault(e["id"], e)

    entities = list(deduped.values())
    payload = {
        "schema_version": 1,
        "provenance": {
            "bootstrap_source": args.spec.name,
            "note": "方案 md 是**一次性引导源**, 交付后不再依赖。后续补充知识直接编辑本文件。",
        },
        "records": to_seed_records(entities, rels),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "power_domain_seed.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    counts: dict[str, int] = {}
    for e in entities:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    n_rel = sum(1 for r in payload["records"] if "source_id" in r)
    print(f"已生成 {path}")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:5}  {k}")
    print(f"  {n_rel:5}  relationships")
    print(f"  {len(payload['records']):5}  records (Semantica 读的就是这个数组)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
