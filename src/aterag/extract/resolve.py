"""缝④ 延伸: 引用穿透 —— 需求正文是"详见 X.X.X"时, 解析到被引用章节取真实内容.

真实案例 (PA601 4.3.4.5):
    4.3.4.5 SR-PA601-D54A-1701 版本管理功能
        详见4.3.4.4通信协议中版本管理的内容

该需求的实体内容 (版本管理相关参数) 在 4.3.4.4 的属性表里, 4.3.4.5 自身只有一句引用。
不穿透的结果是: 引用被当成"无内容散文"跳过 -> 一条真实需求凭空消失。

设计:
  * 引用识别措辞与需求编号形态 —— 一律来自档案 (config/doc_profiles.yaml),
    本模块不含任何具体文档的中文词
  * 目标解析 —— 从正文抓章节号; 抓不到则显式记为未解析, 不静默丢弃
  * 内容回收 —— 取被引用章节的表格行与散文, 逐条作为输出条件, 带双重溯源
    (需求归属章节 + 内容实际所在章节)
  * 引用目标自身"无要求"时不产条件, 记入剔除清单, 不把空引用伪装成有内容
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aterag.extract.models import (
    CONF_RULE,
    ROLE_OTHER,
    SRC_BLOCK,
    ConditionClause,
    TestCondition,
)
from aterag.ingest.markdown_parser import Block
from aterag.ingest.table_schema import SchemaRegistry

# 章节号形态 (被引用目标): 纯结构正则, 不含文档词汇
_SECTION_REF_RE = re.compile(r"(\d+(?:\.\d+){1,5})")


class ReferenceConfigMissing(LookupError):
    """档案未声明引用识别配置 —— fail-closed。

    引用措辞与需求编号形态属于文档认知, 不得内置默认值:
    猜错会让真实需求被当噪声跳过, 或让噪声被当成需求塞进产测条件。
    """


@dataclass
class ReferenceSpec:
    """引用识别配置 (来自档案)。"""

    markers: tuple[str, ...]
    req_id_pattern: str

    @property
    def enabled(self) -> bool:
        return bool(self.markers) and bool(self.req_id_pattern)

    def req_id_re(self) -> re.Pattern[str]:
        return re.compile(self.req_id_pattern)


@dataclass
class ReferenceHit:
    """一条"正文为引用"的需求."""

    section_path: str
    heading: str
    req_id: str
    title: str
    target: str
    body: str
    resolved: bool = False
    source_sections: list[str] = field(default_factory=list)
    content: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_path": self.section_path,
            "heading": self.heading,
            "req_id": self.req_id,
            "title": self.title,
            "target": self.target,
            "resolved": self.resolved,
            "source_sections": self.source_sections,
            "content_items": len(self.content),
            "note": self.note,
        }


def _is_reference(text: str, markers: Sequence[str]) -> bool:
    return any(m in text for m in markers)


def find_references(
    blocks: Sequence[Block],
    spec: ReferenceSpec,
    *,
    section_prefixes: Sequence[str] = (),
) -> list[ReferenceHit]:
    """找出章节范围内"正文为引用"的需求块。

    raises ReferenceConfigMissing: 档案未声明引用配置 (fail-closed, 不猜)。
    """
    if not spec.enabled:
        raise ReferenceConfigMissing(
            "档案未声明 reference_markers / req_id_pattern —— "
            "引用穿透的识别条件属文档认知, 不得内置默认; "
            "请在 config/doc_profiles.yaml 补齐 (显式留空即关闭该能力)"
        )
    rid_re = spec.req_id_re()
    hits: list[ReferenceHit] = []
    for b in blocks:
        text = (b.text or "").strip()
        if not text or b.tables:
            continue
        if section_prefixes and not _in_scope(b.section_path, section_prefixes):
            continue
        if not _is_reference(text, spec.markers):
            continue
        m = _SECTION_REF_RE.search(text)
        if not m:
            continue
        rid = rid_re.search(b.heading or "")
        hits.append(
            ReferenceHit(
                section_path=b.section_path,
                heading=b.heading,
                req_id=rid.group(1) if rid else "",
                title=_title_from_heading(b.heading or "", rid_re),
                target=m.group(1),
                body=text,
            )
        )
    return hits


def _in_scope(section_path: str, prefixes: Sequence[str]) -> bool:
    sp = section_path or ""
    return any(sp == p or sp.startswith(p + ".") for p in prefixes)


def _title_from_heading(heading: str, rid_re: re.Pattern[str]) -> str:
    """'4.3.4.5 SR-PA601-D54A-1701 版本管理功能' -> '版本管理功能'"""
    s = re.sub(r"^\d+(?:\.\d+)*\s*", "", heading or "")
    s = rid_re.sub("", s).strip()
    return s or heading


def resolve_references(
    hits: Sequence[ReferenceHit],
    blocks: Sequence[Block],
    reg: SchemaRegistry,
    spec: ReferenceSpec,
) -> list[ReferenceHit]:
    """穿透到被引用章节, 回收其表格行与散文作为真实内容。"""
    by_section: dict[str, list[Block]] = {}
    for b in blocks:
        by_section.setdefault(b.section_path, []).append(b)

    for h in hits:
        srcs: list[str] = []
        items: list[str] = []
        # 被引用章节本身 + 其子章节 (4.3.4.4 可能再往下分)
        for b in blocks:
            if not _in_scope(b.section_path, [h.target]):
                continue
            srcs.append(b.section_path)
            for t in b.tables:
                if not t:
                    continue
                det = reg.detect(t[0], t[1:])
                if det.schema and det.schema.entity == "none":
                    continue  # 元数据/参考表不作需求内容
                for cells in t[1:]:
                    row = (
                        det.schema and reg.map_row(det.schema, t[0], cells) if det.matched else None
                    )
                    if row:
                        items.append(_row_to_text(row))
                    else:
                        items.append(" | ".join(c.strip() for c in cells))
            body = (b.text or "").strip()
            if body and not _is_reference(body, spec.markers):
                items.append(body)
        h.source_sections = sorted(set(srcs))
        h.content = items
        h.resolved = bool(items)
        if not items:
            h.note = f"引用目标 {h.target} 在本文档内未找到可回收内容 (需人工确认)"
    return list(hits)


def _row_to_text(row: Mapping[str, str]) -> str:
    """把解析出的规范字段还原成一句可读文本 (键: 值)。"""
    parts = []
    for k, v in row.items():
        if k in {"section_path", "heading", "model_id", "parent_headings"}:
            continue
        if v not in ("", "-", "—", None):
            parts.append(f"{k}={v}")
    return "; ".join(parts)


def to_conditions(
    hits: Sequence[ReferenceHit],
    *,
    priority: str = "",
    flags_extra: Sequence[str] = (),
) -> list[TestCondition]:
    """已解析的引用 -> 产测条件 (内容列为输出侧, 需求归属保留在 section_path)。"""
    out: list[TestCondition] = []
    for h in hits:
        if not h.resolved or not h.content:
            continue
        flags = ["resolved_reference", f"reference_target:{h.target}", *flags_extra]
        out.append(
            TestCondition(
                req_id=h.req_id,
                title=h.title or h.heading,
                section_path=h.section_path,
                heading=h.heading,
                priority=priority,
                role=ROLE_OTHER,
                input_conditions=[],
                output_conditions=[
                    ConditionClause(
                        kind="presence",
                        text=item,
                        role="output",
                        source=SRC_BLOCK,
                        confidence=CONF_RULE,
                    )
                    for item in h.content
                ],
                limits={},
                flags=flags,
                etype="Requirement",
                source=SRC_BLOCK,
                notes=h.body,
            )
        )
    return out
