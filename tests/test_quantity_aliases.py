"""标题别名表与 ``_model_facts`` 解耦 (方案 §11.7 衔接层) 的不变量测试。

被替换的旧实现: ``_model_facts`` 里三个中文子串
(``额定输出电压`` / ``输出电流`` / ``整机效率``)。它在规格书换措辞时静默取空,
而下游看到的是「这个型号没有该事实」—— 措辞问题伪装成数据缺失。

这里钉住三件事:
1. **等价性**: PA601-D54A 实际实体上, 新旧取数结果一致 (不静默改变已上线行为);
2. **比旧实现更严**: 实测标题里有一整族邻近词(``输出电压`` / ``输出电压范围``
   / ``输出电压上升时间`` / ``输出功率``), 子串匹配会把它们认成额定电压 ——
   取到错值比取不到更坏, 所以必须是整串相等;
3. **别名表写错在加载期报错**: 别名跨事实互斥, 不是靠遍历顺序定输赢。
"""

from __future__ import annotations

import pytest
import yaml

from aterag.extract.api import load_blocks
from aterag.extract.quantity_aliases import (
    MATCH_MODES,
    VALUE_FIELDS,
    QuantityAliasBook,
    QuantityFact,
)
from aterag.ingest.entity_extract import extract_from_blocks

ALIASES = "config/quantity_aliases.yaml"


@pytest.fixture(scope="module")
def book():
    return QuantityAliasBook.load(ALIASES)


@pytest.fixture(scope="module")
def pa601_requirements():
    """真实抽取产物 —— 不拿手写样本代替, 手写样本证明不了真实措辞。"""
    blocks = load_blocks("PA601-D54A", "rag_storage/blocks")
    return [e for e in extract_from_blocks(blocks, "PA601-D54A") if e.etype == "Requirement"]


def _legacy_facts(ents: list[dict]) -> dict:
    """旧实现的逐字复刻 —— 等价性对照的基准。"""
    volts: dict[str, float] = {}
    currs: dict[str, float] = {}
    eff = None
    for e in ents:
        title = e.get("title", "")
        rail = e.get("rail", "") or "main"
        if "额定输出电压" in title:
            raw = e.get("typ")
            if raw is None:
                raw = e.get("min") or e.get("max")
            try:
                volts.setdefault(rail, abs(float(raw)))
            except (TypeError, ValueError):
                continue
        elif "输出电流" in title:
            try:
                currs[rail] = float(e["max"])
            except (KeyError, TypeError, ValueError):
                continue
        elif "整机效率" in title and e.get("min") is not None:
            eff = e["min"]
    facts = {f"voltage_{r}": v for r, v in volts.items()}
    if currs:
        main = max(currs, key=lambda r: currs[r])
        facts["current"] = currs[main]
        facts["main_rail"] = main
        if main in volts:
            facts["voltage"] = volts[main]
    if eff is not None:
        facts["efficiency_min"] = eff
    return facts


def _new_facts(ents: list[dict], book: QuantityAliasBook) -> dict:
    """新实现 (与 ``_model_facts`` 同逻辑) —— 放在测试里是为了能喂假实体。"""
    volts: dict[str, float] = {}
    currs: dict[str, float] = {}
    facts: dict = {}
    for e in ents:
        spec = book.match_fact(e.get("title", ""))
        if spec is None:
            continue
        val = spec.value_of(e)
        if val is None:
            continue
        rail = e.get("rail", "") or "main"
        if spec.name == "voltage":
            volts.setdefault(rail, val)
        elif spec.name == "current":
            currs[rail] = val
        else:
            facts[spec.name] = val
    facts.update({f"voltage_{r}": v for r, v in volts.items()})
    if currs:
        main = max(currs, key=lambda r: currs[r])
        facts["current"] = currs[main]
        facts["main_rail"] = main
        if main in volts:
            facts["voltage"] = volts[main]
    return facts


def _as_dicts(entities) -> list[dict]:
    return [e.props for e in entities]


class TestBookContract:
    def test_repo_book_loads_and_is_valid(self, book):
        assert set(book.facts) == {"voltage", "current", "efficiency_min"}

    def test_match_and_value_vocabularies_are_closed(self):
        """两个取值空间封闭 —— 写错只会静默取不到值。"""
        assert MATCH_MODES == {"exact", "prefix", "contains"}
        assert VALUE_FIELDS == {"typ", "min", "max"}

    def test_default_match_is_exact_not_contains(self, book):
        """默认必须是整串相等: 子串会认走邻近事实(见 TestStricterThanSubstring)。"""
        for name, f in book.facts.items():
            assert f.match == "exact", f"facts[{name}] 的 match 应显式为 exact, 实际 {f.match}"

    def test_titles_summary_is_human_readable(self, book):
        """报错与健康检查要能让人照着改 yaml -> 清单得是人话。"""
        s = book.titles_summary()
        assert "额定输出电压" in s and "输出电流" in s


class TestEquivalenceWithLegacy:
    def test_real_pa601_facts_unchanged(self, book, pa601_requirements):
        """真实数据上新旧实现取数一致 —— 不静默改变已上线行为。"""
        ents = _as_dicts(pa601_requirements)
        assert _new_facts(ents, book) == _legacy_facts(ents)

    def test_real_pa601_main_rail_is_the_biggest_current_rail(self, book, pa601_requirements):
        """主轨 = 额定输出电流最大者 (不写死轨名)。"""
        f = _new_facts(_as_dicts(pa601_requirements), book)
        assert f["main_rail"] == "-54V"
        assert f["current"] == pytest.approx(11.1)
        assert f["voltage"] == pytest.approx(54.0), "负号轨名的电压取绝对值"


class TestStricterThanSubstring:
    """这一组是新实现比旧实现更严的地方: 旧实现靠子串, 错配无处不在。"""

    def test_neighbouring_titles_are_not_matched(self, book):
        """PA601 实测存在的一整族邻近词, 一个都不能被当成额定输出电压。"""
        neighbours = [
            "输出电压",
            "输出电压范围",
            "输出电压可调节范围",
            "输出电压整定范围",
            "输出电压上升时间",
            "输出功率",
            "输入输出电压",
            "标称输入电压范围",
            "绝缘电压（输入对输出）",
            "腐蚀感知电压",
        ]
        for t in neighbours:
            f = book.match_fact(t)
            assert f is None or f.name != "voltage", f"{t!r} 被认成额定输出电压 -> 会取到错的数"

    def test_input_side_titles_are_not_matched(self, book):
        """输入侧电压/电流不能被当成输出事实 —— 那会让推导方向反掉。"""
        for t in ("输入电压", "输入电流", "输入工作电压范围", "输入冲击电流"):
            f = book.match_fact(t)
            assert f is None or not f.name.startswith(("voltage", "current")), (
                f"{t!r} 被认成输出事实 {f.name}"
            )

    def test_real_neighbour_titles_exist_in_data(self, pa601_requirements):
        """哨兵: 上面那组邻近词必须真的在数据里, 否则测试是空转。"""
        titles = {e.props.get("title", "") for e in pa601_requirements}
        assert "输出电压" in titles, "PA601 里应存在裸'输出电压'这一行"
        assert "输出电压上升时间" in titles
        assert "输入电流" in titles

    def test_alternative_wording_still_recognized(self, book):
        """解耦的收益: 换个说法也能认, 不必改代码。"""
        assert book.match_fact("标称输出电压").name == "voltage"
        assert book.match_fact("额定输出电流").name == "current"


class TestValueExtraction:
    def test_zero_is_a_value_not_a_missing_field(self):
        """0.0 是合法值: ``raw or fallback`` 会把它当"没有"继续往后找。"""
        f = QuantityFact(name="voltage", titles=("v",), value_from=("min", "max"))
        assert f.value_of({"min": 0.0, "max": 54.0}) == 0.0

    def test_falls_through_in_declared_order(self):
        f = QuantityFact(name="voltage", titles=("v",), value_from=("typ", "min", "max"))
        assert f.value_of({"min": 1.0, "max": 9.0}) == 1.0
        assert f.value_of({"max": 9.0}) == 9.0

    def test_abs_value_applied(self):
        f = QuantityFact(name="voltage", titles=("v",), value_from=("min",), abs_value=True)
        assert f.value_of({"min": -54.0}) == 54.0

    def test_unparsable_value_returns_none_not_guess(self):
        """取不到就是取不到, 不猜 —— 反幻觉。"""
        f = QuantityFact(name="voltage", titles=("v",), value_from=("typ", "min", "max"))
        assert f.value_of({"typ": "待定", "min": None, "max": None}) is None

    def test_missing_value_returns_none(self):
        f = QuantityFact(name="current", titles=("c",), value_from=("max",))
        assert f.value_of({"max": None}) is None


class TestLoadTimeValidation:
    def _write(self, tmp_path, mutate) -> str:
        doc = yaml.safe_load(open(ALIASES, encoding="utf-8").read())
        mutate(doc)
        p = tmp_path / "aliases.yaml"
        p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        return str(p)

    def test_alias_shared_by_two_facts_raises(self, tmp_path):
        """一条标题认两个事实时结果只取决于遍历顺序 —— 必须在加载期报错。"""

        def mutate(doc):
            doc["facts"]["current"]["title"].append("额定输出电压")

        with pytest.raises(ValueError) as ei:
            QuantityAliasBook.load(self._write(tmp_path, mutate))
        assert "额定输出电压" in str(ei.value)
        assert "voltage" in str(ei.value) and "current" in str(ei.value)

    def test_missing_file_raises(self, tmp_path):
        """缺表即无标题知识 -> 取数全空, 必须报错而不是静默。"""
        with pytest.raises(FileNotFoundError):
            QuantityAliasBook.load(tmp_path / "nope.yaml")

    def test_empty_facts_raise(self, tmp_path):
        with pytest.raises(ValueError) as ei:
            QuantityAliasBook.load(self._write(tmp_path, lambda d: d.update(facts={})))
        assert "未定义任何事实" in str(ei.value)

    def test_unknown_match_mode_raises(self, tmp_path):
        with pytest.raises(ValueError) as ei:
            QuantityAliasBook.load(
                self._write(
                    tmp_path,
                    lambda d: d["facts"]["voltage"].update(match="regex"),
                )
            )
        assert "match 非法" in str(ei.value)

    def test_unknown_value_field_raises(self, tmp_path):
        with pytest.raises(ValueError) as ei:
            QuantityAliasBook.load(
                self._write(
                    tmp_path,
                    lambda d: d["facts"]["voltage"].update(value_from=["avg"]),
                )
            )
        assert "value_from 含未知字段" in str(ei.value)

    def test_fact_without_titles_raises(self, tmp_path):
        with pytest.raises(ValueError) as ei:
            QuantityAliasBook.load(
                self._write(
                    tmp_path,
                    lambda d: d["facts"]["voltage"].update(title=[]),
                )
            )
        assert "没有 title" in str(ei.value)

    def test_all_problems_reported_at_once(self, tmp_path):
        def mutate(doc):
            doc["facts"]["voltage"].update(match="regex", value_from=["avg"])
            doc["facts"]["current"]["title"].append("整机效率")

        with pytest.raises(ValueError) as ei:
            QuantityAliasBook.load(self._write(tmp_path, mutate))
        msg = str(ei.value)
        for token in ("regex", "avg", "整机效率"):
            assert token in msg, f"{token} 未出现在汇总报错里: {msg}"


class TestNoHardcodedTitlesInCode:
    def test_server_has_no_title_substring_match(self):
        """取数侧不得再有标题子串判定 —— 那是这次要拆掉的东西。

        扫的是**去掉注释与文档串后的代码**: 字面量出现在注释里(说明这条轨
        是什么)是对的, 出现在判断里才是硬编码。
        """
        import ast
        import inspect

        from aterag.mcp_server import server

        fn = server._model_facts
        tree = ast.parse(inspect.getsource(fn))
        # 文档串本身可以提这些词(说明每条轨是什么), 但它不算"硬编码标题"。
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(getattr(body[0], "value", None), ast.Constant)
                ):
                    doc_nodes.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in doc_nodes:
                    continue
                for literal in ("额定输出电压", "输出电流", "整机效率"):
                    assert literal not in node.value, (
                        f"_model_facts 的代码里仍有硬编码标题 {literal!r} (行 {node.lineno})"
                    )
        # 也不许再有 title 上的 in / substring 判定
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Subscript)
                and isinstance(node.ops[0], ast.In)
            ):
                raise AssertionError(f"_model_facts 里仍有 title 子串判定 (行 {node.lineno})")

    def test_server_reads_alias_book_from_settings(self):
        """路径来自配置, 不写死相对路径 —— 板卡部署目录与开发机不同。"""
        from aterag.mcp_server import server

        assert (
            "quantity_aliases_path" in server._alias_book.__code__.co_consts
            or any("quantity_aliases_path" in str(c) for c in server._alias_book.__code__.co_consts)
            or "quantity_aliases_path"
            in (server._alias_book.__doc__ or "") + str(server._alias_book.__code__.co_names)
        )


class TestBookIsWiredIntoConfigLoad:
    def test_configs_load_includes_alias_book(self):
        """别名表属于抽取侧配置 -> 走 A20 的加载期校验, health 才覆盖得到。"""
        from aterag.config import get_settings
        from aterag.extract.configs import load_extraction_configs

        cfgs = load_extraction_configs(get_settings())
        assert cfgs.quantity_aliases is not None
        assert cfgs.quantity_aliases.facts

    def test_every_config_path_is_declared_in_settings(self):
        """路径有 settings 字段且可被环境变量改写 —— 板卡部署目录与开发机不同。"""
        from aterag.config import Settings

        assert Settings().quantity_aliases_path == "config/quantity_aliases.yaml"
        assert Settings(quantity_aliases_path="/opt/aterag/qa.yaml").quantity_aliases_path == (
            "/opt/aterag/qa.yaml"
        )
