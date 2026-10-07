"""业界方法补齐层 (A6) —— 规格书只写单边时, 按 test_methods.yaml 补出缺的那一侧。

定位
----
规格书 4.3 有大量条目只给了一侧条件: 输出特性章节给了响应限值, 却不会逐条
重述"额定输入下测量"; 输入特性章节给了激励域限值, 却不写"输出应正常"。
产测要能跑就必须把这个常识前提显式化, 否则用例缺激励设定值。

不做什么 (与 extract 其余模块的分工)
------------------------------------
* 不补"限值"。判据数值只能来自规格书或人工注记 —— 凭空造数值等于伪造质量标准。
  方法条目里 verdict=curve 的表示判据在曲线图上无法机读, 只产待审项不产子句。
* 不覆盖已有条件。已抽出的子句不动, 补齐只追加缺失侧。
* 不让提案自动生效。产出的子句一律 status=draft(见 assemble/dict 契约),
  未人审不得进 approved 出口 —— 与人工注记的 draft 门禁同构。

匹配顺序 (先命中先用, 全在 config 里, 代码不含任何文档措辞)
----------------------------------------------------------
kinds > title_pattern > standard_cites > role
role: any 是兜底, 保证"缺哪侧补哪侧"总有回应。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aterag.extract.models import (
    CONF_PROPOSED,
    SRC_METHOD,
    STATUS_APPROVED,
    STATUS_DRAFT,
    ConditionClause,
    ReviewItem,
    TestCondition,
)

DEFAULT_METHODS_PATH = Path("config/test_methods.yaml")

#: 补齐产物的来源标记 —— 与 notes/limits/annotation/title 并列, 区分"哪来的"。
ORIGIN_SPEC = "spec"

#: applies_to.role 的特殊取值 —— 它们不是 doc_profiles 的角色名, 而是匹配指令。
ROLE_ANY = "any"  # 适用于所有需求 (兜底)
ROLE_ANY_EXCEPT = "any_except_other"  # 适用于 role_exclude 之外的任何需求

#: 方法条目里 supplies 的合法取值。
SUPPLIES_SIDES = ("input", "output")

#: 描述模板允许的占位符。
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

#: verdict 的合法取值: numeric=可机读子句; curve=判据在曲线, 需人工数字化。
VERDICTS = ("numeric", "curve")


@dataclass(frozen=True, slots=True)
class MethodCondition:
    """方法声明的一条待补条件。

    kind 必须落在 condition_patterns.yaml 的封闭词表内 —— 由 MethodBook.validate
    交叉校验, 否则下游按 kind 聚合会断裂。
    """

    kind: str
    value: dict[str, Any] | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class TestMethod:
    """一条业界测试方法。

    Attributes:
        id: 方法标识, 写进子句的 method_ref, 供追溯到依据。
        basis: 标准出处/行业实践依据 (必填, 供人审核对)。
        verdict: numeric | curve
        applies_to: 匹配条件 kinds/title_pattern/standard_cites/role
        supplies: 补哪一侧 (input | output)
        conditions: 待补条件清单
    """

    id: str
    basis: str
    verdict: str
    supplies: str
    conditions: tuple[MethodCondition, ...]
    kinds: tuple[str, ...] = ()
    title_pattern: str = ""
    standard_cites: tuple[str, ...] = ()
    role: str = ROLE_ANY
    role_exclude: str = ""
    measurement_hints: dict[str, Any] = field(default_factory=dict)
    #: role 是否在 YAML 里被显式声明 (决定它算兜底还是具体选择器)
    _role_declared: bool = False

    def matches(self, cond: TestCondition) -> bool:
        """按 kinds > title_pattern > standard_cites > role 的顺序匹配。

        返回 True 只表示"这条方法适用于该需求", 不表示"应该现在补" ——
        补哪一侧由 ``supplies`` 与实际缺侧共同决定。

        关键: 只要声明了任一具体选择器, 就不再回落到"适用于所有需求"。
        否则一条只写了 title_pattern 的方法会因 role 默认值而变成全局兜底 ——
        表现为每条需求都匹配到它, 把最精确的方法换成最不相关的那条。
        """
        if self.kinds and self.kinds & _present_kinds(cond):
            return True
        if self.title_pattern and re.search(self.title_pattern, cond.title or ""):
            return True
        if self.standard_cites:
            hay = f"{cond.title} {cond.notes}"
            if any(cite in hay for cite in self.standard_cites):
                return True
        if self._has_specific_selector:
            # 声明了具体选择器但都没命中 -> 退到 role 判定。
            # role 本身就是一种具体选择器: 显式写了 role: signal_io 的方法
            # 对非 signal_io 需求不该生效, 但对 signal_io 需求必须生效。
            # 唯一不算数的是 role 的默认值 any —— 默认值不等于显式声明兜底,
            # 只有 YAML 里真的写了 role: any 才算有意兜底 (_role_declared)。
            if not self._role_declared:
                return False
            if self.role == ROLE_ANY:
                return True
            if self.role == ROLE_ANY_EXCEPT:
                return cond.role != self.role_exclude
            return self.role == cond.role
        if self.role == ROLE_ANY_EXCEPT:
            # 排除型兜底: 适用于该角色之外的任何需求。存在的意义是"某些角色
            # 正确处置是显式不提取, 不能被兜底方法补出一条无意义的条件"。
            return cond.role != self.role_exclude
        if self.role:
            return self.role == cond.role
        if self.role == ROLE_ANY_EXCEPT:
            # 排除型兜底: 适用于该角色之外的任何需求。存在的意义是"某些角色
            # 正确处置是显式不提取, 不能被兜底方法补出一条无意义的条件"。
            return cond.role != self.role_exclude
        if self.role:
            return self.role == cond.role
        return False

    def specificity(self, cond: TestCondition) -> int:
        """匹配精确度打分, 越大越优先 (4=最精确 … 0=兜底)。

        pick() 用它取代"文件里第一条命中": 兜底方法通常写在靠后位置, 但若某条
        专用方法恰好在其后, "先命中先赢"会让泛用默认激励顶替掉专用测法 ——
        例如纹波条目的 20MHz 带宽条件被"额定输入+额定负载"覆盖, 测量值就不可比了。
        """
        if self.kinds and self.kinds & _present_kinds(cond):
            return 4
        if self.title_pattern and re.search(self.title_pattern, cond.title or ""):
            return 3
        if self.standard_cites and any(
            cite in f"{cond.title} {cond.notes}" for cite in self.standard_cites
        ):
            return 2
        if self._role_declared and self.role not in {ROLE_ANY, ROLE_ANY_EXCEPT}:
            return 1
        return 0

    @property
    def _has_specific_selector(self) -> bool:
        """是否声明了具体选择器 (非兜底)。

        判定依据是"配置里真的写了什么", 而不是 dataclass 的默认值 ——
        所以要在 load() 里依据原始 YAML 是否有 role 键来记录。
        """
        return self._role_declared or bool(self.kinds or self.title_pattern or self.standard_cites)


@dataclass(frozen=True, slots=True)
class MethodBook:
    """业界方法库 (缝⑤) + 需求描述模板。"""

    methods: tuple[TestMethod, ...] = ()
    templates: tuple[DescriptionTemplate, ...] = ()
    path: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_METHODS_PATH) -> MethodBook:
        p = Path(path)
        if not p.exists():
            # fail-closed: 方法库缺失时不静默降级为空库, 否则补齐层会悄悄失效,
            # 表现为"单边条件一直没被补"却查不出原因。
            raise FileNotFoundError(f"业界方法库不存在: {p}")
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out: list[TestMethod] = []
        for m in doc.get("methods") or []:
            conds = tuple(
                MethodCondition(
                    kind=str(c.get("kind", "")),
                    value=dict(c["value"]) if c.get("value") else None,
                    note=str(c.get("note", "")),
                )
                for c in (m.get("conditions") or [])
            )
            ap = m.get("applies_to") or {}
            out.append(
                TestMethod(
                    id=str(m.get("id", "")),
                    basis=str(m.get("basis", "")),
                    verdict=str(m.get("verdict", "")),
                    supplies=str(m.get("supplies", "")),
                    conditions=conds,
                    kinds=tuple(str(k) for k in (ap.get("kinds") or ())),
                    title_pattern=str(ap.get("title_pattern", "")),
                    standard_cites=tuple(str(c) for c in (ap.get("standard_cites") or ())),
                    role=str(ap.get("role", ROLE_ANY)),
                    role_exclude=str(ap.get("role_exclude", "")),
                    _role_declared="role" in ap,
                    measurement_hints=dict(m.get("measurement_hints") or {}),
                )
            )
        templates = tuple(
            DescriptionTemplate(
                id=str(t.get("id", "")),
                text=str(t.get("text", "")),
                parts=dict(t.get("parts") or {}),
                omit_when_empty=tuple(str(k) for k in (t.get("omit_when_empty") or ())),
                when=str(t.get("when", "any")),
            )
            for t in (doc.get("description_templates") or [])
        )
        return cls(methods=tuple(out), templates=templates, path=str(p))

    def validate(
        self,
        known_kinds: frozenset[str],
        known_roles: frozenset[str] = frozenset(),
    ) -> None:
        """交叉校验: 引用的 kind 必须在封闭词表内, role 必须是合法角色名。

        这是两仓对接的第一道闸 —— 方法库引用了词表外的 kind, 下游 ATEStudio
        就无法为它翻译出执行动作, 而这类断裂在运行期只会表现为"用例缺步骤"。

        known_roles 由调用方从 doc_profiles.yaml 的 section_priors 传入 (不在此处
        硬编码角色表 —— 角色是文档档案的知识, 换文档就该换档案, 不是换代码)。
        """
        bad: list[str] = []
        for m in self.methods:
            if not m.id:
                bad.append("存在无 id 的方法条目")
            if not m.basis:
                bad.append(f"methods[{m.id}] 缺 basis (无依据的方法不可用于补齐)")
            if m.supplies not in SUPPLIES_SIDES:
                bad.append(f"methods[{m.id}].supplies 非法: {m.supplies}")
            if m.verdict not in VERDICTS:
                bad.append(f"methods[{m.id}].verdict 非法: {m.verdict}")
            # role 是匹配指令, 写错不会报错、只会永远匹配不上 -> 显式校验。
            if m.role == ROLE_ANY_EXCEPT:
                if not m.role_exclude:
                    bad.append(f"methods[{m.id}] 用了 {ROLE_ANY_EXCEPT} 但未声明 role_exclude")
            elif m.role != ROLE_ANY and m.role not in known_roles:
                bad.append(
                    f"methods[{m.id}].applies_to.role 非法: {m.role} "
                    f"(应为 {ROLE_ANY} / {ROLE_ANY_EXCEPT} 或档案里的章节先验角色)"
                )
            if (
                m._role_declared
                and m.role == ROLE_ANY
                and (m.kinds or m.title_pattern or m.standard_cites)
            ):
                # 同时声明了具体选择器和兜底 role: 具体选择器会先命中,
                # 未命中时又靠 any 兜住 -> 实际是兜底, 但作者的意图多半是精确匹配。
                bad.append(
                    f"methods[{m.id}] 同时声明了具体选择器与 role: {ROLE_ANY}, "
                    "意图不明确 (命中靠选择器, 未命中靠 any 兜底) -> 请显式二选一"
                )
            for k in m.kinds:
                if k not in known_kinds:
                    bad.append(f"methods[{m.id}].applies_to.kinds 引用词表外的 kind: {k}")
            for c in m.conditions:
                if c.kind not in known_kinds:
                    bad.append(f"methods[{m.id}].conditions 引用词表外的 kind: {c.kind}")
        if bad:
            raise ValueError("业界方法库与条件词表不自洽: " + "; ".join(bad))

    def validate_templates(self) -> None:
        """校验描述模板占位符齐全 —— 否则渲染时才会抛 KeyError, 很难定位。

        两类占位符: 正文用基础键 (title/inputs/…); parts 片段是"先渲染再插入",
        所以它们内部只能用基础键, 自身名字 (如 method_note) 只允许出现在正文里。
        """
        allowed = {
            "title",
            "rail",
            "notes",
            "inputs",
            "outputs",
            "limits",
            "methods",
        }
        bad: list[str] = []
        for t in self.templates:
            body_keys = set(_PLACEHOLDER_RE.findall(t.text))
            for name, p in t.parts.items():
                part_keys = set(_PLACEHOLDER_RE.findall(p))
                unknown = part_keys - allowed
                if unknown:
                    bad.append(
                        f"description_templates[{t.id}].parts[{name}] 未知占位符: {sorted(unknown)}"
                    )
                # 片段必须真正被正文引用, 否则是死配置
                if name not in body_keys:
                    bad.append(f"description_templates[{t.id}].parts[{name}] 未在正文中使用")
            unknown_body = body_keys - allowed - set(t.parts)
            if unknown_body:
                bad.append(f"description_templates[{t.id}] 正文未知占位符: {sorted(unknown_body)}")
            if not t.text.strip():
                bad.append(f"description_templates[{t.id}] 的 text 为空")
        if not self.templates:
            bad.append("test_methods.yaml 未定义任何 description_templates")
        if bad:
            raise ValueError("需求描述模板不自洽: " + "; ".join(bad))

    def pick(self, cond: TestCondition, side: str) -> TestMethod | None:
        """取最精确的"适用于该需求且补该侧"的方法。

        精确度排序 (高→低): kinds 命中 > title_pattern 命中 > role 命中 > 兜底。
        之所以不按配置文件顺序取第一条: 兜底方法(role 匹配)通常写在文件靠后处,
        但若某条精确方法恰好写在其后, "先匹配先赢"就会让泛用的默认激励顶替掉
        针对本条目的专用方法(如纹波测量的 20MHz 带宽条件被"额定输入+额定负载"覆盖)。
        精确度必须显式表达, 不能依赖作者记得把精确的写在前面。
        """
        best: TestMethod | None = None
        best_rank = -1
        for m in self.methods:
            if m.supplies != side or not m.matches(cond):
                continue
            rank = m.specificity(cond)
            if rank > best_rank:
                best, best_rank = m, rank
        return best

    def methods_for_side(self, side: str) -> tuple[TestMethod, ...]:
        """声明补某侧的方法 (按文件顺序), 供 CLI --list 展示。"""
        return tuple(m for m in self.methods if m.supplies == side)


@dataclass(frozen=True, slots=True)
class SupplementResult:
    """一次补齐的产出。"""

    #: 实际补上子句的需求
    supplemented: list[TestCondition] = field(default_factory=list)
    #: 曲线判据等需人工数字化的待审项
    needs_review: list[ReviewItem] = field(default_factory=list)
    #: 各方法的命中次数 (验证覆盖率用)
    hits: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        return {
            "supplemented": len(self.supplemented),
            "needs_manual": len(self.needs_review),
            "methods_used": len(self.hits),
        }


def _present_kinds(cond: TestCondition) -> set[str]:
    return {c.kind for c in (*cond.input_conditions, *cond.output_conditions)}


def _missing_sides(cond: TestCondition) -> list[str]:
    """该需求缺哪一侧。双边齐全返回空。"""
    out: list[str] = []
    if not cond.input_conditions:
        out.append("input")
    if not cond.output_conditions:
        out.append("output")
    return out


def _clause_from(m: TestMethod, mc: MethodCondition) -> ConditionClause:
    """方法条件 -> 提案子句。

    confidence 一律 proposed / status 一律 draft: 业界做法是"默认做法",
    不是"规格书说的", 两者在追溯上不可混同, 未人审不得生效。
    text 用方法原文的 note, 不做改写, 保证可回溯到 config。
    """
    return ConditionClause(
        kind=mc.kind,
        text=mc.note or mc.kind,
        role=m.supplies,
        value=dict(mc.value) if mc.value else None,
        source=SRC_METHOD,
        confidence=CONF_PROPOSED,
        status=STATUS_DRAFT,
        method_ref=m.id,
    )


def _review_for_curve(cond: TestCondition, m: TestMethod) -> ReviewItem:
    return ReviewItem(
        kind="needs_manual_digitization",
        section_path=cond.section_path,
        heading=cond.heading,
        detail=f"{cond.req_id} {cond.title}".strip()[:200],
        hint=f"{m.id}: 判据为曲线需人工数字化 | 依据 {m.basis[:80]}",
        method_ref=m.id,
    )


def supplement_conditions(
    conditions: Sequence[TestCondition],
    book: MethodBook,
) -> SupplementResult:
    """对单边条件按业界方法补齐缺侧 (原地追加, 返回补过的需求)。

    已双边齐全的需求不进入本流程; 已有子句不改动 —— 本层只做"补", 不做"改"。
    """
    result = SupplementResult()
    for cond in conditions:
        missing = _missing_sides(cond)
        if not missing:
            continue
        for side in missing:
            m = book.pick(cond, side)
            if m is None:
                continue
            # verdict 只管"判据能不能机读", 不管"条件能不能补":
            # 曲线判据的方法照样能给出可机读的测试前提(参考电压/负载/上电时刻),
            # 丢掉它们等于把本来可执行的条件退回人工。只有当曲线方法连条件都没写
            # (判据与条件均未声明) 时才是纯待审项。
            if m.verdict == "curve" and not m.conditions:
                result.needs_review.append(_review_for_curve(cond, m))
                continue
            if m.verdict == "curve":
                result.needs_review.append(_review_for_curve(cond, m))
            if not m.conditions:
                continue
            target = cond.input_conditions if side == "input" else cond.output_conditions
            existing = {c.kind for c in target}
            added = False
            for mc in m.conditions:
                if mc.kind in existing:
                    continue  # 该侧已有同类条件, 不重复追加
                target.append(_clause_from(m, mc))
                added = True
            if added:
                if f"method:{m.id}" not in cond.flags:
                    cond.flags.append(f"method:{m.id}")
                if "supplemented" not in cond.flags:
                    cond.flags.append("supplemented")
                # 装配阶段打的 ``no_<side>_condition`` 在此刻已经**不成立** ——
                # flag 描述的是最终状态, 留着等于让审计读到自相矛盾的标记
                # (「无输出条件」与实际挂着输出子句同时出现)。本层只增不改
                # **子句**, 但摘掉自己刚推翻的标记是它的义务。
                stale = f"no_{side}_condition"
                if stale in cond.flags:
                    cond.flags.remove(stale)
                result.supplemented.append(cond)
                result.hits[m.id] = result.hits.get(m.id, 0) + 1
    return result


@dataclass(frozen=True, slots=True)
class DescriptionTemplate:
    """需求描述模板 —— 输入/输出条件合成的呈现规则 (声明在 config, 代码只套用)。

    为什么模板也要外置: 描述里"输入条件/输出条件/判据"这些标签措辞属于文档
    呈现知识, 与剔除词/章节关键字同一性质。写进代码就等于把某份文档的呈现方式
    焊死在本实现里, 换产品线时要改代码 —— 违反禁止硬编码。
    """

    id: str
    text: str
    parts: Mapping[str, str] = field(default_factory=dict)
    omit_when_empty: tuple[str, ...] = ()
    when: str = "any"

    def applies(self, cond: TestCondition) -> bool:
        if self.when in {"", "any"}:
            return True
        return self.when == cond.role

    def render(self, cond: TestCondition) -> str:
        """渲染该需求的条件描述。空片段按 omit_when_empty 省略。"""
        clauses_in = tuple(cond.input_conditions)
        clauses_out = tuple(cond.output_conditions)
        methods = sorted({c.method_ref for c in (*clauses_in, *clauses_out) if c.method_ref})
        ctx: dict[str, str] = {
            "title": cond.title or "",
            "rail": cond.rail or "",
            "notes": cond.notes or "",
            "inputs": _join_clauses(clauses_in),
            "outputs": _join_clauses(clauses_out),
            "limits": _render_limits(cond),
            "methods": ", ".join(methods),
        }
        for key, tmpl in self.parts.items():
            ctx[key] = tmpl.format(**ctx)
        out = self.text.format(**ctx)
        # 省略规则: 若某片段渲染为空, 删掉它在正文中所在的整行 ——
        # 否则会产出"判据: "这种空标签行, 读的人会误以为该维度缺数据。
        # 必须在 _tidy (去行尾空格) 之前做, 否则 "标签: " 的尾随空格已消失,
        # 空标签行就无法被识别了。
        for key in self.omit_when_empty:
            # 片段渲染后仍带标签文字(如 "补齐依据: "), 不能直接判空 ——
            # 要看它内含的占位符对应的真实值是否为空。取该片段里引用的
            # 基础键, 任一有值则保留整行。
            part = self.parts.get(key, "")
            keys = _PLACEHOLDER_RE.findall(part)
            has_value = any(ctx.get(k, "").strip() for k in keys)
            if keys and not has_value:
                out = _drop_label_only_lines(out)
        return _tidy(out)


def _drop_label_only_lines(text: str) -> str:
    """删掉"标签: "冒号后无内容的行 (空维度/空依据)。"""
    kept = []
    for line in text.splitlines():
        if re.match(r"^\s*[^:\n]{1,40}?:\s*$", line):
            continue  # 纯标签行 -> 该维度无内容, 省略
        kept.append(line)
    return "\n".join(kept)


def _tidy(text: str) -> str:
    """去掉纯空白行与行尾空格, 保留缩进结构。"""
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())


def _join_clauses(clauses: Sequence[ConditionClause]) -> str:
    """条件子句列表 -> 单行文本, 标出人审状态(草案可见, 不与已定混同)。"""
    if not clauses:
        return ""
    parts = []
    for c in clauses:
        mark = "" if c.status == STATUS_APPROVED else "(待审)"
        val = c.value or {}
        detail = ""
        if val:
            detail = " " + " ".join(f"{k}={v}" for k, v in val.items() if v is not None)
        parts.append(f"{c.kind}{detail}{mark}: {c.text}" if c.text else f"{c.kind}{detail}{mark}")
    return "; ".join(parts)


def _render_limits(cond: TestCondition) -> str:
    """限值 -> 紧凑文本, 只列实际存在的键, 缺项不臆造。"""
    lim = cond.limits or {}
    keys = [k for k in ("min", "typ", "max") if lim.get(k) is not None]
    if not keys:
        return ""
    unit = str(lim.get("unit", "") or "")
    parts = [f"{k}={lim[k]}" for k in keys]
    return (" ".join(parts) + (f" {unit}" if unit else "")).strip()


def render_descriptions(
    conditions: Sequence[TestCondition],
    templates: Sequence[DescriptionTemplate],
) -> int:
    """把输入/输出条件合成进 TestCondition.description (原地写回)。

    返回实际写入的需求数。选择第一条 applies 的模板; 模板表为空则不改动
    (缺模板是配置问题, 交由 validate_configs 报出, 不在这里静默兜底)。
    """
    n = 0
    for cond in conditions:
        for t in templates:
            if t.applies(cond):
                cond.description = t.render(cond)
                n += 1
                break
    return n


def has_draft(conditions: Sequence[TestCondition]) -> bool:
    """是否存在未批准的补齐提案 (CI --check 门禁用)。"""
    return any(
        c.status == STATUS_DRAFT
        for cond in conditions
        for c in (*cond.input_conditions, *cond.output_conditions)
    )


def approved_only(conditions: Sequence[TestCondition]) -> list[TestCondition]:
    """只保留全部子句已批准的需求 —— 导出的安全子集。

    draft 需求整体不导出: 部分子句已批准、部分未批准时导出会让下游拿到
    语义不完整的条件集, 比不导出更难排查。
    """
    out: list[TestCondition] = []
    for cond in conditions:
        clauses = (*cond.input_conditions, *cond.output_conditions)
        if all(c.status == STATUS_APPROVED for c in clauses):
            out.append(cond)
    return out
