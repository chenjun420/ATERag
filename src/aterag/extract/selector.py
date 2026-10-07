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


def _char_jaccard(a: str, b: str) -> float:
    """字符集 Jaccard 相似度 —— 用来在报错里标「你大概想填这个」。

    **刻意用最笨的度量**: 不引依赖、不做分词、不改匹配语义(匹配仍然是
    :func:`match_keywords` 的子串判定)。它只出现在**报错文本**里, 用来把
    候选摆到人眼前让人自己判断 —— 落配置仍然要人签字(红线 9)。

    中文标题按字符比就够: 「功能/性能要求」与「性能指标」的公共字是「能/功/要/求」
    这类, 而「性能指标」四个字与「性能/要求」也共享「性/能」—— 正是要标出来的
    那种近似。分词反而会因为引入同义词表而给出「不像」的错觉。
    """
    sa, sb = set(a or ""), set(b or "")
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


#: 近似命中的相似度下限。低于它就不标 —— 标一堆不相关的候选比不标更糟,
#: 人会开始无视箭头。
_NEAR_MISS_FLOOR = 0.34


def _near_misses(keywords: Sequence[str], headings: Sequence[str]) -> dict[str, list[str]]:
    """关键字 -> 疑似应该填的标题(按相似度降序)。

    返回的是**候选**, 不是判定: 报告里写「建议改 section_keywords」而人照着改,
    那是人在签字, 不是程序替人决定。
    """
    out: dict[str, list[str]] = {}
    for kw in keywords:
        scored = sorted(
            ((_char_jaccard(kw, h), h) for h in headings if h),
            key=lambda x: (-x[0], x[1]),
        )
        hits = [h for score, h in scored if score >= _NEAR_MISS_FLOOR]
        if hits:
            out[kw] = hits[:3]
    return out


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
        # 报错必须**可操作**(红线 12): 只给「共 N 个块」等于让人猜该往
        # section_keywords 填什么 —— 而那是唯一需要的信息。把实际标题树
        # 全列出来, 近似命中的标出来。
        titles: list[tuple[str, str]] = []
        seen_titles: set[str] = set()
        for b in blocks:
            for h in (b.heading, *b.parent_headings):
                if h and h not in seen_titles:
                    seen_titles.add(h)
                    titles.append((h, b.section_path or ""))
        near = _near_misses(keywords, [h for h, _ in titles])
        lines = [
            f"章节关键字 {list(keywords)} 未命中任何章节标题 —— "
            f"规格书章节结构已变或关键字拼写错误。",
            "",
            f"  本文档实际章节 (共 {len(blocks)} 个块 / {len(titles)} 个标题):",
        ]
        for h, sec in titles[:40]:
            hint = ""
            for kw, cands in near.items():
                if h in cands:
                    hint = f"  <- 近似命中 {kw!r}, 建议改 section_keywords"
                    break
            lines.append(f"    {sec or '(无章节号)':<12} {h}{hint}")
        if len(titles) > 40:
            lines.append(f"    ... 另有 {len(titles) - 40} 个标题未列出")
        lines += [
            "",
            "  处置: 改 config/doc_profiles.yaml 该 profile 的 section_keywords, "
            "或用 profile_name 指定另一份档案。",
            "        落配置必须人工签字 —— 本提示只是候选, 不自动改任何东西。",
        ]
        raise SectionKeywordNotFound("\n".join(lines))
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
