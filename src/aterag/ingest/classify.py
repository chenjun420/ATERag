"""产品类型自动识别.

规则优先 (标题/章节关键词), 远程 LLM 兜底; 识别结果写入 registry。
"""
from __future__ import annotations

import re

from aterag.models import LLMClient
from aterag.registry import Registry

# 已知产品类型关键词 (顺序即优先级)
_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("power", ["电源", "整流器", "power supply", "psu", "rectifier"]),
    ("rf", ["射频", "功放", "rru", "aau", "radio", "transceiver"]),
    ("board", ["单板", "主板", "电路板", "pcb board", "main board"]),
    ("battery", ["电池", "battery", "bms"]),
]


def classify_by_rules(text: str) -> str | None:
    head = text[:3000].lower()
    scores: dict[str, int] = {}
    for type_name, kws in _TYPE_KEYWORDS:
        s = sum(head.count(kw) for kw in kws)
        if s:
            scores[type_name] = s
    if not scores:
        return None
    return max(scores, key=scores.get)  # type: ignore[arg-type]


async def classify_product_type(text: str, registry: Registry, llm: LLMClient | None) -> str:
    """识别产品类型: 规则优先, LLM 兜底, 双路径都失败则报错。"""
    by_rules = classify_by_rules(text)
    if by_rules:
        return by_rules
    if llm is None:
        raise ValueError("规则无法识别产品类型, 且 LLM 不可用; 请在 registry 中手工注册")
    prompt = (
        "判断以下技术文档属于哪个产品类型, 只回答一个英文小写词:\n"
        "可选: power(电源/整流器), rf(射频/无线), board(单板), battery(电池), other\n\n"
        f"文档开头:\n{text[:1500]}"
    )
    answer = (await llm.chat([{"role": "user", "content": prompt}], max_tokens=500)).strip().lower()
    m = re.search(r"(power|rf|board|battery|other)", answer)
    if not m:
        raise ValueError(f"LLM 分类结果不可解析: {answer[:60]!r}")
    return m.group(1)
