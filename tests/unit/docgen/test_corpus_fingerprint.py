r"""语料指纹:抽取计数不许悄悄变化。

方案有 22,919 行,抽取链有六层(表格识别 → 表达式归一化 → 量纲闭合 → 标准 →
勘误 → 符号表)。任何一层的正则改动都可能让某个计数悄悄挪动 —— 而**数字本身
就是交付物**:W1 判据 1 报的是「129 条闭合 / 400 未达标」,若哪天抽取器悄悄
多认出 20 条公式,报告里的数字就跟着变,而没人知道是哪一层变了。

所以这里把关键计数**钉死**。变了不一定是 bug(方案文件更新、故意扩充规则都会
变),但**必须有人显式确认** —— 失败信息里写清是哪一层、变了多少。

复现全部计数::

    uv run pytest tests/unit/docgen/test_corpus_fingerprint.py -q -s

已知的合法变更来源:补 `quantity_rules` 规则(闭合数会涨)、U.5 符号表扩充
(闭合数会涨)、方案文件更新(公式 ID 数会变)。改完请同步更新本文件的期望值,
并在 commit message 里写清为什么。

## 期望值变更史

| 值 | 变更 | 原因 |
|---|---|---|
| 545 -> **573** | 2026-10 | 附录 U 的公式登记表(公式 ID\|名称\|上游\|规则\|测试)**没有表达式列**, 早先一版把这种行整行丢弃, 于是附录 U 对抽取零贡献。改为按字段合并 + 一格多 ID 展开后, 多认出 29 个 ID |
| 129 / 369 | 未变 | 合并只补 `name_zh`/`upstream`, 不改表达式, 故可解析与闭合数不受影响 —— 这本身是「合并没有污染表达式」的一个旁证 |
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from aterag.docgen.errata import parse_errata
from aterag.docgen.expr_norm import normalize_equation
from aterag.docgen.quantity_rules import build_dictionary
from aterag.docgen.spec_parse import parse_formula_rows, read_spec
from aterag.docgen.standards import parse_standards
from aterag.docgen.symbols import parse_symbol_table

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"

#: 公式 ID 里的域字母:``F_J.3.1_INDUCTOR_RIPPLE`` -> ``J``。
#: 量纲解析键是 **(符号, 命名空间)**, 所以必须先取出命名空间, 否则规则表
#: 会跨域误命中 —— 而跨域误命中正是 U.5 自己警告过的歧义。
_DOMAIN_RE = re.compile(r"F_([A-Z])(?=[._])")

#: ``^`` 紧跟字母 —— 即「会被误读成按位异或」的幂写法(Steinmetz/Weibull)。
_CARET_EXP_RE = re.compile(r"\^\s*[A-Za-z\u03b1-\u03c9_]")


def _require_spec() -> list[str]:
    if not SPEC.is_file():
        pytest.skip(f"方案文件不在预期位置: {SPEC}")
    return read_spec(SPEC)


@pytest.fixture(scope="module")
def corpus() -> dict[str, object]:
    """一次读完, 供本模块所有断言复用 —— 读 22,919 行不便宜。"""
    lines = _require_spec()
    rows = parse_formula_rows(lines)

    # 每个 ID 只取**第一段**: 一个单元格里常有多条公式(反引号分隔),
    # 逐条计数会把「公式条数」与「公式 ID 数」混为一谈。
    first: dict[str, object] = {}
    for row in rows.rows:
        first.setdefault(row.formula_id, row)

    dictionary = build_dictionary(parse_symbol_table(lines)[0])
    parseable: list[str] = []
    closed: list[str] = []
    unclosed_blockers: Counter[str] = Counter()
    for fid, row in first.items():
        normalized = normalize_equation(row.expression)
        if not normalized.ok:
            continue
        parseable.append(fid)
        match = _DOMAIN_RE.match(fid)
        ns = match.group(1) if match else None
        unresolved = [
            v for v in normalized.variables if dictionary.resolve(v, ns).dimension is None
        ]
        if unresolved:
            unclosed_blockers[unresolved[0]] += 1
        else:
            closed.append(fid)

    return {
        "rows": rows,
        "first": first,
        "parseable": parseable,
        "closed": closed,
        "blockers": unclosed_blockers,
        "standards": parse_standards(lines),
        "errata": parse_errata(lines),
        "symbols": parse_symbol_table(lines)[0],
    }


@pytest.mark.slow
class TestCorpusFingerprint:
    """每一项对应抽取链的一层。"""

    def test_formula_id_count(self, corpus: dict[str, object]) -> None:
        """表格识别层:互不重复的公式 ID 数。"""
        first = corpus["first"]
        assert isinstance(first, dict)
        assert len(first) == 573, (
            f"公式 ID 数从 574 变成 {len(first)}: 表格识别层变了 "
            f"(表头识别或 ID 列判定), 不是公式内容变了"
        )

    def test_parseable_count(self, corpus: dict[str, object]) -> None:
        """表达式归一化层:第一段能被引擎语法解析的公式数。"""
        parseable = corpus["parseable"]
        assert len(parseable) == 369, (
            f"可解析数从 369 变成 {len(parseable)}: 归一化层变了 "
            f"(`^` 折叠/限定词并入/隐含乘法任一改动都会动这里)"
        )

    def test_dimension_closed_count(self, corpus: dict[str, object]) -> None:
        """量纲闭合层 —— **W1 判据 1 报的就是这个数**。

        变动通常来自 `quantity_rules` 规则增删(合法)或符号表扩充(合法),
        但也可能来自某条规则被改错。这里是最后一道发现「规则与方案打架」的
        机会, 所以数字必须有人确认, 不能自动接受。
        """
        closed = corpus["closed"]
        assert len(closed) == 129, (
            f"量纲闭合数从 129 变成 {len(closed)}: "
            f"若是补规则所致请更新本测试; 若非, 查 quantity_rules"
        )

    def test_standards_count(self, corpus: dict[str, object]) -> None:
        """标准索引层(判据 4 要求 ≥60, 当前 177)。"""
        standards = corpus["standards"]
        records = getattr(standards, "records", ())
        assert len(records) == 177, f"标准数从 177 变成 {len(records)}"

    def test_errata_count(self, corpus: dict[str, object]) -> None:
        """勘误层(判据 3 要求 7/7)。"""
        errata = corpus["errata"]
        errata_items = getattr(errata, "errata", ())
        assert len(errata_items) == 7, f"勘误数从 7 变成 {len(errata_items)}"

    def test_symbol_table_size(self, corpus: dict[str, object]) -> None:
        """U.5 符号表层 —— 符号覆盖是判据 1 的**主因瓶颈**。"""
        symbols = corpus["symbols"]
        entries = getattr(symbols, "entries", ())
        assert len(entries) == 149, f"U.5 符号数从 149 变成 {len(entries)}"

    def test_no_silent_multiplier_regression(self, corpus: dict[str, object]) -> None:
        """``on``/``off`` 不得作为变量重新出现。

        这两个是 MOSFET 状态标注而非物理量。作为变量出现说明
        ``_fold_state_qualifier`` 回归了 —— 而症状极隐蔽:公式只是多出一个
        查不到的变量, 报「变量 on 未定义」, 与真实原因毫无关系。
        """
        blockers = corpus["blockers"]
        assert isinstance(blockers, Counter)
        leaked = {s: c for s, c in blockers.items() if s in ("on", "off")}
        assert not leaked, f"状态限定词泄漏成变量: {leaked}"

    def test_no_bitxor_regression(self, corpus: dict[str, object]) -> None:
        """``^`` 不得回退成按位异或。

        Python 的 ``^`` 是 BitXor, 而 ``ast`` **照样通过** —— 字母上标于是被
        ``_VarCollector`` 当成独立物理量变量, 量纲按**乘法**算而不是按幂算。
        这类失效不会让任何解析测试变红, 只会在给上标补了量纲规则之后产出
        **错误的** ``dimension_ok=true``。所以只能直接查 AST 节点类型。

        语料里确有 ``^`` + 字母的公式(Steinmetz ``f^α``、Weibull ``exp(λt/η)^β``),
        所以这个断言不是空转 —— 若哪天 ``^`` 折叠被删掉, 它会立刻红。
        """
        import ast

        from aterag.docgen.expr_norm import _clean, _split_relation, _translate

        first = corpus["first"]
        assert isinstance(first, dict)
        checked = 0
        offenders: list[str] = []
        for fid, row in first.items():
            if not row.expression or not _CARET_EXP_RE.search(row.expression):
                continue
            # ``_translate`` 只处理**单侧**, 整条 ``A = B`` 交给它必然 SyntaxError。
            # 所以先按关系符切分, 再逐侧查 —— 否则断言会因「解析失败」而空转。
            # 注意取的是 ``(lhs, rel, rhs)`` 的 **0 与 2**, 关系符本身不能翻。
            lhs, _rel, rhs = _split_relation(_clean(row.expression))
            for side in (lhs, rhs):
                if not side or not _CARET_EXP_RE.search(side):
                    continue
                try:
                    tree = ast.parse(_translate(side), mode="eval")
                except SyntaxError:
                    # 该侧因**其它**原因不合法, 与 ``^`` 无关, 不在本断言范围。
                    continue
                checked += 1
                if any(isinstance(node, ast.BitXor) for node in ast.walk(tree)):
                    offenders.append(f"{fid}: {side}")

        assert checked, "一条含 `^字母` 的可解析侧都没有 —— 断言空转, 请复核"
        assert not offenders, f"`^` 回退成按位异或: {offenders}"
