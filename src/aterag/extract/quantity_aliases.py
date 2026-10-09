"""型号事实的标题别名表 (方案 §11.7 衔接层)。

取数侧 (``mcp_server._model_facts``) 过去在代码里写死三个中文子串
(``额定输出电压`` / ``输出电流`` / ``整机效率``) 来认实体标题。规格书换个
措辞就静默取不到值, 而下游看到的是「这个型号没有该事实」—— 措辞问题伪装
成数据缺失, 是最难查的一类故障。

本模块把那份「标题写法」的知识搬进配置, 并补上代码里原本没有的两件事:

1. **默认整串相等, 不是子串。** 实测标题里有一整族邻近词(``输出电压`` /
   ``输出电压范围`` / ``输出电压上升时间`` / ``输出功率``), 子串匹配会把
   「上升时间」认成额定电压 —— 取到错值比取不到更坏, 所以收紧是修正不是收紧。
2. **别名跨事实互斥, 加载期报错。** 一条标题若同时是两个事实的别名, 取哪个
   都可能是错的; 过去靠 ``elif`` 顺序决定, 那只是先来后到, 不是判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_ALIASES_PATH = Path("config/quantity_aliases.yaml")

#: 允许的匹配档位。exact 是默认也是唯一安全档; prefix/contains 需要在 yaml 里
#: 显式声明 —— 声明本身就是「我确认过这个档位不会误伤」的记录。
MATCH_MODES = frozenset({"exact", "prefix", "contains"})

#: 允许作为数值来源的实体字段。写别的名字等于取不到值, 而"取不到"在下游
#: 表现为"该型号没这个事实", 所以取值空间也封闭。
VALUE_FIELDS = frozenset({"typ", "min", "max"})


@dataclass(frozen=True, slots=True)
class QuantityFact:
    """一个型号事实: 用哪些标题认它, 以及从实体哪个字段取它的值。"""

    name: str
    titles: tuple[str, ...]
    match: str = "exact"
    value_from: tuple[str, ...] = ("typ", "min", "max")
    abs_value: bool = False

    def matches(self, title: str) -> bool:
        t = title.strip()
        if self.match == "exact":
            return t in self.titles
        if self.match == "prefix":
            return any(t.startswith(p) for p in self.titles)
        return any(p in t for p in self.titles)

    def value_of(self, props: dict) -> float | None:
        """按 ``value_from`` 顺序取第一个**非 None** 的字段值。

        这里必须是 ``is not None`` 而不是真值判断: 0.0 是合法值, 而
        ``raw or fallback`` 会把 0.0 当成"没有"继续往后找 —— 0V 轨道的额定
        电压会被静默换成 max。原来的代码就是这个写法。
        """
        for f in self.value_from:
            raw = props.get(f)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                return None
            return abs(v) if self.abs_value else v
        return None


@dataclass(frozen=True, slots=True)
class QuantityAliasBook:
    facts: dict[str, QuantityFact] = field(default_factory=dict)
    path: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_ALIASES_PATH) -> QuantityAliasBook:
        p = Path(path)
        if not p.exists():
            # fail-closed: 缺表即无标题知识 -> 取数全空。与场景规则同一条纪律。
            raise FileNotFoundError(f"型号事实别名表不存在: {p}")
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        facts: dict[str, QuantityFact] = {}
        for name, spec in (doc.get("facts") or {}).items():
            titles = tuple(str(t) for t in (spec.get("title") or []))
            facts[str(name)] = QuantityFact(
                name=str(name),
                titles=titles,
                match=str(spec.get("match", "exact")),
                value_from=tuple(str(f) for f in (spec.get("value_from") or ["typ"])),
                abs_value=bool(spec.get("abs_value", False)),
            )
        book = cls(facts=facts, path=str(p))
        book.validate()
        return book

    def validate(self) -> None:
        """加载期自检 (fail-closed): 别名表写错会静默取不到值, 所以当场报。"""
        bad: list[str] = []
        if not self.facts:
            bad.append("未定义任何事实 (取数会全部落空, 而下游看到的是'型号没这个事实')")
        owner: dict[str, str] = {}
        for name, f in self.facts.items():
            if not f.titles:
                bad.append(f"facts[{name}] 没有 title")
            if f.match not in MATCH_MODES:
                bad.append(f"facts[{name}].match 非法: {f.match} (允许: {sorted(MATCH_MODES)})")
            for v in f.value_from:
                if v not in VALUE_FIELDS:
                    bad.append(
                        f"facts[{name}].value_from 含未知字段: {v} (允许: {sorted(VALUE_FIELDS)})"
                    )
            for t in f.titles:
                if not t.strip():
                    bad.append(f"facts[{name}] 有空标题")
                elif t in owner:
                    # 互斥: 同一条标题认两个事实时, 结果只取决于字典/遍历顺序。
                    bad.append(
                        f"标题 {t!r} 同时是 facts[{owner[t]}] 与 facts[{name}] 的别名 -> "
                        "取哪个都可能是错的, 请改用可区分的写法"
                    )
                else:
                    owner[t] = name
        if bad:
            raise ValueError("型号事实别名表不自洽: " + "; ".join(bad))

    def match_fact(self, title: str) -> QuantityFact | None:
        """标题 -> 事实。互斥已在加载期保证, 所以命中即唯一, 不靠遍历顺序。"""
        for f in self.facts.values():
            if f.matches(title):
                return f
        return None

    def titles_summary(self) -> str:
        """人可读的标题清单, 供报错与健康检查用 (让人能照着改 yaml)。"""
        return "; ".join(f"{n}: {'/'.join(f.titles)}" for n, f in sorted(self.facts.items()))
