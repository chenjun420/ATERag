"""满载基准值 ``value_at_full_load`` 事实的**生产方**。

**为什么以前没有**
-----------------
``seed.json`` 的 ``facts`` 里只有 4 条 ``load_ratio`` / ``load_alias``, 没有
``value_at_full_load``。于是 Datalog 只能推出 ``value_at_load(Quantity, Load,
?, Ratio)`` —— 前提缺失, 第四元组永远是空的。测试里能算出半载 299.7W, 是因为
**测试自己手工喂了那条事实**; 生产链路上没有喂事实的地方。

**两种口径同时成立, 不是二选一**
---------------------------------
这是本模块存在的理由。PA601 的「满载输出功率」有两个都站得住的数:

    规格上限  600.0 W   SR-1204 的 max, 规格书给的判据上限 —— 合格判定用它
    推导值    599.4 W   K-ELEC-001 由 54V x 11.1A 算出 —— 某工作点的计算结果

早先我把它们当成「必须选一个」的冲突, 那是个错误的框: 它们是**不同来源、
不同用途**的量。产线两个都要 —— 用规格上限判合格, 用推导值算某工况的实际值。
所以两者作为**不同谓词项**同时产出:

    value_at_full_load("output_power", 600.0)          <- spec_limit 口径
    value_at_full_load("output_power_derived", 599.4)  <- derived 口径

分开成两个 Quantity 而不是给同一 Quantity 塞两个值, 是因为 ``value_at_load``
规则以 ``Quantity`` 为键 —— 同名会让一次查询返回两行却说不清哪个是哪个。
加了后缀, 口径在名字里就写明了, 消费方按名字取, 不靠猜。

**来源必须可追**
----------------
每条事实带 ``basis`` 指向它的权威来源(哪条需求 / 哪条规则的哪个测试用例)。
没有来源的满载值会让「半载 300W」这个数失去复核路径 —— 而那正是这套推理
存在的理由(见 ``reasoning/__init__.py``: 乘法那一步本身必须可审计)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _seed_dir() -> Path:
    # parents[3] = 仓库根(本文件在 <repo>/src/aterag/reasoning/)。少算一层指向
    # src/, 声明文件读不到 -> 推导口径静默产不出事实。
    return Path(__file__).resolve().parents[3] / "data" / "seed"


#: 口径后缀。规格上限用原名, 推导值加 ``_derived``。
DERIVED_SUFFIX = "_derived"


@dataclass(frozen=True)
class FullLoadFact:
    """一条 ``value_at_full_load`` 事实及其来源。"""

    quantity: str
    value: float
    basis: str
    authority_kind: str
    unit: str = ""
    #: 产生这条事实的需求号或规则号。**用字段而不是从 basis 文本里切词** ——
    #: 实测按空格切词匹配不上, 因为 req_id 后面紧跟着中文标题
    #: (``SR-PA601-D54A-1204「输出功率」的 max=600.0W`` 是**一个** token),
    #: 于是归名静默失效, 两种口径对不上号。
    source_id: str = ""

    @property
    def fact_str(self) -> str:
        """Datalog 事实文本。

        **必须带引号**: ``DatalogReasoner.add_fact`` 把首字母大写的裸标识符
        当变量 —— 实测 ``Power(PA601, 599.4)`` 直接抛 ``Facts must be
        constants only``, 而 ``power("PA601", 599.4)`` 才收。
        """
        return f'value_at_full_load("{self.quantity}", {self.value})'

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "value": self.value,
            "basis": self.basis,
            "authority_kind": self.authority_kind,
            "unit": self.unit,
            "source_id": self.source_id,
        }


def from_spec_limits(requirements: list[dict[str, Any]]) -> list[FullLoadFact]:
    """从型号需求的 ``max`` 产出规格上限口径。

    ``requirements`` 取自 ``aterag_kg.pg_source`` 读出的 Requirement 实体
    (属性在 ``metadata`` 下)。只取有 ``max`` 的那些 —— 没有上限就没有「满载
    值」可言, 凭空补一个 0 会让半载也变成 0, 而那看起来像「这一档是 0」。
    """
    out: list[FullLoadFact] = []
    for e in requirements:
        md = e.get("metadata") or e.get("properties") or {}
        if str(md.get("etype") or e.get("entity_type") or "") != "Requirement":
            continue
        raw_max = md.get("max")
        if raw_max is None:
            continue
        try:
            val = float(raw_max)
        except (TypeError, ValueError):
            continue
        title = str(md.get("title") or "").strip()
        req_id = str(md.get("req_id") or e.get("id") or "")
        unit = str(md.get("unit") or "")
        # 同一 title 可能有多个 rail/工况 变体; max 相同则去重, 不同则都留
        # (多档位本身就是事实 —— PA601 输出功率就是按输入电压分档的)
        out.append(
            FullLoadFact(
                quantity=_quantity_name(title),
                value=val,
                basis=(
                    f"规格书 {req_id}「{title}」的 max={raw_max}{unit}"
                    f"(eid={e.get('id')})" + (f"; 备注 {md['notes']}" if md.get("notes") else "")
                ),
                authority_kind=str(md.get("authority_kind") or "spec"),
                unit=unit,
                source_id=req_id,
            )
        )
    return _dedup(out)


def from_rule_tests(rules: list[dict[str, Any]]) -> list[FullLoadFact]:
    """**只**产出被声明为「满载点」的规则自测基准。

    为什么不能把 130 条规则的 ``test.expect`` 全收进来: 那是规则的**自校验
    输入**, 不是产品的满载值。实测反例 —— K-ELEC-003(欧姆定律 电流=U/R) 的
    ``test.expect.current`` 是 **2.0**, 而 PA601 的满载输出电流是 **11.1A**
    (SR-1203, -54V 轨)。把 2.0 当满载值会让「半载输出电流 = 1.0A」这个
    错数看起来有推导过程, 比没有更危险。

    所以只认 ``data/seed/requirement_concept_edges.yaml`` 里
    ``derived_full_load`` 段**显式声明**过的规则, 每条自带依据。
    """
    import yaml

    path = _seed_dir() / "requirement_concept_edges.yaml"
    declared: dict[str, dict[str, str]] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for raw in data.get("derived_full_load") or []:
            rid = str(raw.get("rule") or "").strip()
            if rid and str(raw.get("basis") or "").strip():
                declared[rid] = {
                    "basis": str(raw["basis"]).strip(),
                    "quantity": str(raw.get("quantity") or "").strip(),
                }
    if not declared:
        return []

    by_id = {str(r.get("id") or "").strip(): r for r in rules}
    out: list[FullLoadFact] = []
    for rid, meta in declared.items():
        r = by_id.get(rid)
        if r is None:
            continue
        expect = dict((r.get("test") or {}).get("expect") or {})
        given = dict((r.get("test") or {}).get("given") or {})
        derive = r.get("derive") or {}
        # 取声明规则 derive.output 对应的那个期望值; 取不到就整条跳过(不猜)
        output = str(derive.get("output") or "")
        val = expect.get(output)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        out.append(
            FullLoadFact(
                quantity=_quantity_name(meta["quantity"] or output),
                value=float(val),
                basis=f"规则 {rid} 的 test.expect.{output}={val}(given={given or '无'}); {meta['basis']}",
                authority_kind="derived",
                unit=str((r.get("variables") or {}).get(output) or ""),
                source_id=rid,
            )
        )
    return _dedup(out)


def build_all(
    rules: list[dict[str, Any]],
    requirements: list[dict[str, Any]] | None = None,
) -> tuple[list[FullLoadFact], dict[str, Any]]:
    """两种口径一起产出, 并**归到同一个概念 id 上**。

    为什么必须归名
    --------------
    两侧的名字本来对不上: 规格侧来自需求标题(中文「输出功率」), 推导侧来自
    ``derive.output``(符号名 ``power``)。同名才查得出「半载输出功率」,
    否则一次查询只能看到一侧, 那就还是二选一。

    归名的依据是**已声明的映射**(:mod:`aterag.kg.req_concept_edges`)——
    ``SR-PA601-D54A-1204 -> YC_POUT`` 与 ``K-ELEC-001 -> YC_POUT`` 两条都已在
    ``data/seed/requirement_concept_edges.yaml`` 里声明过, 且各带 basis。
    概念 id 本身就同时出现在图里, 所以 ``value_at_load("YC_POUT", half_load,
    ...)`` 查出来的量**在图上可追**。

    归不到名的(未声明映射的那些)保留原名产出, 并在 stats 里计数 —— 不静默
    丢弃, 否则「为什么这条规则没进 Datalog」会查不到。
    """
    from aterag.kg.req_concept_edges import load_declarations

    # 声明里 req_id 既可能是需求号也可能是规则号, 正好两边都能查
    to_concept = {
        d.req_id: d.concept for d in load_declarations() if d.authority_kind in {"spec", "derived"}
    }

    def _rename(fs: list[FullLoadFact]) -> list[FullLoadFact]:
        """把事实的 quantity 换成它声明对应的概念 id。

        按 ``source_id`` 字段查, **不是**按 basis 文本切词 —— 后者匹配不上:
        ``SR-PA601-D54A-1204「输出功率」的 max=600.0W`` 是**一个** token,
        于是归名静默失效, 两种口径对不上号(实测 spec 侧归名 0 条)。
        """
        out: list[FullLoadFact] = []
        for f in fs:
            hit = to_concept.get(f.source_id)
            if hit is None or hit == f.quantity:
                out.append(f)
                continue
            out.append(
                FullLoadFact(
                    quantity=hit,
                    value=f.value,
                    basis=f.basis,
                    authority_kind=f.authority_kind,
                    unit=f.unit,
                    source_id=f.source_id,
                )
            )
        return out

    concepts = set(to_concept.values())
    facts = _rename(from_rule_tests(rules))
    stats: dict[str, Any] = {
        "derived": len(facts),
        "derived_renamed_to_concept": sum(1 for f in facts if f.quantity in concepts),
        "spec_limit": 0,
        "spec_renamed_to_concept": 0,
        "requirements_seen": 0,
    }
    if requirements:
        spec = _rename(from_spec_limits(requirements))
        stats["spec_limit"] = len(spec)
        stats["spec_renamed_to_concept"] = sum(1 for f in spec if f.quantity in concepts)
        stats["requirements_seen"] = sum(
            1
            for e in requirements
            if str((e.get("metadata") or {}).get("etype") or e.get("entity_type") or "")
            == "Requirement"
        )
        facts.extend(spec)
    return facts, stats


def _quantity_name(raw: str) -> str:
    """把中文标题/符号名规整成 Datalog 常量友好的名字。

    空格与括号会让 ``_parse_fact_string`` 拆错参数, 这里压成下划线。
    中文保留 —— 引擎接受引号内的任意字符(实测), 压成拼音反而丢了可读性。
    """
    s = "".join(ch if (ch.isalnum() or ch in "_") else "_" for ch in raw.strip())
    return "_".join(x for x in s.split("_") if x) or "quantity"


def _dedup(facts: list[FullLoadFact]) -> list[FullLoadFact]:
    """同 (quantity, value, authority_kind) 只留一条 —— 但**值不同就都留**。

    值不同不是重复: PA601 输出功率按输入电压分档(90~176Vac: 400W /
    176~286Vac: 600W), 两个档位都是事实, 去重成一条会把档位信息抹掉。
    """
    seen: set[tuple[str, float, str]] = set()
    out: list[FullLoadFact] = []
    for f in facts:
        key = (f.quantity, f.value, f.authority_kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


__all__ = [
    "DERIVED_SUFFIX",
    "FullLoadFact",
    "build_all",
    "from_rule_tests",
    "from_spec_limits",
]
