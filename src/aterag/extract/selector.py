"""缝① 章节选择: 标题链关键字匹配 -> 章节号前缀集 -> 收编全部子章节.

两个必须处理的真实坑:
  1. 源文档标题层级异常: `## 4.3 功能/性能要求` 是二级标题 (与 `## 4 技术要求` 同级),
     markdown_parser 弹栈后 4.3.x 的 parent_headings 仍含该标题, 但祖父级会丢。
     故匹配只看"自身标题 + 父标题链", 不依赖完整祖先链。
  2. 章节号前缀必须带边界: "4.3" 命中 4.3.1 但不能命中 4.30/4.31。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from aterag.extract.models import SectionKeywordNotFound, Selection
from aterag.ingest.markdown_parser import Block

# 标题里的章节号, 如 "4.3.1 输入特性" -> "4.3.1"
_HEADING_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\s")


def heading_section(heading: str) -> str:
    m = _HEADING_NUM_RE.match(heading or "")
    return m.group(1) if m else ""


def section_matches(section_path: str, prefixes: Sequence[str]) -> bool:
    """章节号是否落在任一前缀之下 (带点边界, 避免 4.3 误吞 4.31)。"""
    sp = section_path or ""
    return any(sp == p or sp.startswith(p + ".") for p in prefixes)


def match_keywords(heading: str, keywords: Sequence[str]) -> bool:
    return bool(heading) and any(kw and kw in heading for kw in keywords)


def select_sections(
    blocks: Iterable[Block],
    keywords: Sequence[str],
    *,
    require_hit: bool = True,
) -> Selection:
    """按标题关键字选出章节及其全部子章节。

    raises SectionKeywordNotFound: 一个章节都没命中 (配置错误或文档结构变化)。
        与"命中但条目全被剔除"(合法空结果)严格区分 —— fail-closed。
    """
    blocks = list(blocks)
    keywords = [k for k in keywords if k]
    if not keywords:
        raise SectionKeywordNotFound("section_keywords 为空: 无从选择章节")

    prefixes: set[str] = set()
    matched: list[str] = []
    for b in blocks:
        for h in (b.heading, *b.parent_headings):
            if not match_keywords(h, keywords):
                continue
            if h not in matched:
                matched.append(h)
            # 优先用命中标题自身的章节号; 标题无编号时退回该块自身的章节号
            sec = heading_section(h) or (b.section_path or "")
            if sec:
                prefixes.add(sec)

    if not prefixes:
        raise SectionKeywordNotFound(
            f"章节关键字 {keywords} 未命中任何章节标题 —— "
            f"检查关键字拼写, 或规格书章节结构已变化 (当前共 {len(blocks)} 个块)"
        )
    if not require_hit:
        matched = matched or []

    ordered = sorted(prefixes, key=lambda s: [int(x) for x in s.split(".")])
    selected = [b for b in blocks if section_matches(b.section_path, ordered)]
    return Selection(
        keywords=keywords,
        matched_headings=matched,
        section_prefixes=ordered,
        selected_chunk_ids=[b.chunk_id for b in selected],
        blocks_total=len(blocks),
        blocks_selected=len(selected),
    )
