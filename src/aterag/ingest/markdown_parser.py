"""Markdown 章节树解析器.

输出 blocks.jsonl 兼容格式 (规格书 §4.2.3):
  chunk_id / heading / level / parent_headings / section_path / text / tables
表格行 (GFM) 原样保留并附带结构化信息, 供实体抽取使用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_SECTION_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\s")


@dataclass
class Block:
    chunk_id: str
    heading: str  # 最近标题, 如 "4.3.3 保护功能"
    level: int  # 标题层级 (0=文档头)
    parent_headings: list[str] = field(default_factory=list)
    section_path: str = ""  # 章节编号路径, 如 "4.3.3"
    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)  # GFM 表格: 行->单元格
    is_table_block: bool = False

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "heading": self.heading,
            "level": self.level,
            "parent_headings": self.parent_headings,
            "section_path": self.section_path,
            "text": self.text,
            "tables": self.tables,
            "is_table_block": self.is_table_block,
        }


def parse_markdown(text: str) -> list[Block]:
    """把 Markdown 文本切成语义块: 标题边界对齐, 表格整块保留。"""
    lines = text.splitlines()
    blocks: list[Block] = []

    # 标题栈: (level, 标题文本, 编号路径)
    stack: list[tuple[int, str, str]] = []

    current_lines: list[str] = []
    current_tables: list[list[list[str]]] = []
    in_table = False
    table_buf: list[list[str]] = []

    def flush() -> None:
        nonlocal current_lines, current_tables, in_table, table_buf
        if in_table and table_buf:
            current_tables.append(table_buf)
            table_buf = []
            in_table = False
        body = "\n".join(current_lines).strip()
        if body or current_tables:
            head = stack[-1] if stack else ("", "", "")[0:1] + ("", "")[0:1] + ("",)
            heading = head[1] if stack else ""
            level = head[0] if stack else 0
            parents = [h[1] for h in stack[:-1]]
            section = head[2] if stack else ""
            blocks.append(
                Block(
                    chunk_id=f"chunk_{len(blocks):04d}",
                    heading=heading,
                    level=level,
                    parent_headings=parents,
                    section_path=section,
                    text=body,
                    tables=current_tables,
                    is_table_block=bool(current_tables and not body.strip("#")),
                )
            )
        current_lines = []
        current_tables = []

    for raw in lines:
        line = raw.rstrip("\n")
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            sec_m = _SECTION_NUM_RE.match(title)
            sec = sec_m.group(1) if sec_m else ""
            # 维护标题栈
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title, sec))
            continue

        tm = _TABLE_ROW_RE.match(line)
        if tm and not _TABLE_SEP_RE.match(line):
            if not in_table:
                # 表格开始前, 若已在收集正文, 保持正文继续 (表格附着于当前块)
                in_table = True
                table_buf = []
            cells = [c.strip() for c in tm.group(1).split("|")]
            table_buf.append(cells)
            continue
        if _TABLE_ROW_RE.match(line) and _TABLE_SEP_RE.match(line) and in_table:
            continue  # 跳过分隔行 |---|---|
        if in_table:
            # 表格结束
            if table_buf:
                current_tables.append(table_buf)
            table_buf = []
            in_table = False
        current_lines.append(line)

    flush()
    return blocks


def parse_file(path: str | Path) -> list[Block]:
    return parse_markdown(Path(path).read_text(encoding="utf-8"))


def write_blocks_jsonl(blocks: list[Block], out_path: str | Path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps(b.to_dict(), ensure_ascii=False) + "\n")
