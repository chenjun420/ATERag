"""跨型号可复现性: 同一份代码换 PN1000-48A 跑, 不能因为只认 PA601 而退化。

## 为什么这条测试存在

主轨与 eid 标签这两件事都极易退化成「为 PA601 写死」:

* 主轨若按「带轨行里出现最多者」自动推断, 换个主轨电压的型号就会挂错;
* eid 标签若在代码里枚举 kind/短语, 换个规则集(或同一模板的不同表格措辞)就失效。

所以这条测试要断言的是**机制**, 不是 PA601 的具体结果。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="module")
def ee():
    spec = importlib.util.spec_from_file_location(
        "_ee2", ROOT / "src/aterag/ingest/entity_extract.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ee2"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSameCodeWorksOnAnotherModel:
    @pytest.fixture(scope="class")
    @staticmethod
    def pn1000():
        """在**仓库根**下跑抽取: 主轨声明读的是相对路径 ``data/registry.yaml``。"""
        f = ROOT / "rag_storage/blocks/PN1000-48A.jsonl"
        if not f.exists():
            pytest.skip("PN1000-48A 种子不在(裁剪仓库里)")
        old = Path.cwd()
        import os

        os.chdir(ROOT)
        try:
            from aterag.extract.api import load_blocks
            from aterag.ingest.entity_extract import extract_from_blocks

            reqs = [
                e
                for e in extract_from_blocks(
                    load_blocks("PN1000-48A"), "PN1000-48A", doc_version="A"
                )
                if e.etype == "Requirement"
            ]
            return reqs
        finally:
            os.chdir(old)

    @pytest.fixture(scope="class")
    @staticmethod
    def pn1000_reqs(pn1000):
        return pn1000

    def test_it_produces_entities(self, pn1000_reqs) -> None:
        assert pn1000_reqs, "换型号抽不出实体 —— 改造把 PA601 写死了?"

    def test_no_criterion_value_in_its_eids_either(self, pn1000_reqs) -> None:
        """同一套代码在另一个型号上也得守住「判据数值不进 id」。"""
        import re

        bad = [
            e.eid
            for e in pn1000_reqs
            if re.search(r"@(min|typ|max|notes|requirement_text)=", e.eid)
        ]
        assert not bad, f"PN1000-48A 上判据数值进了 id: {bad[:5]}"

    def test_its_main_rail_is_its_own_not_pa601s(self, pn1000_reqs, ee) -> None:
        """PN1000-48A 主轨必须是 -48V, 不能沿用 PA601 的 -54V。

        这是「主轨是型号事实」的硬断言: 若代码里出现 ``-54V`` 字面量, 这里会红。
        """
        import os

        old = Path.cwd()
        os.chdir(ROOT)
        try:
            assert ee.main_rail_declared("PN1000-48A") == "-48V"
            assert ee.main_rail_declared("PA601-D54A") == "-54V"
        finally:
            os.chdir(old)

    def test_unannotated_output_rows_land_on_its_own_main_rail(self, pn1000_reqs) -> None:
        """PN1000-48A 上若有未标注轨的输出行, 应挂到 -48V。"""
        got = {str(e.props.get("rail") or "") for e in pn1000_reqs}
        assert "-54V" not in got, "PA601 的主轨串到了另一个型号"
        if got - {""}:
            assert got <= {"-48V"}, f"出现了非本型号主轨的轨: {got}"

    def test_no_model_specific_literal_in_the_rail_module(self) -> None:
        """**代码**里不许出现具体型号/具体轨电压 —— 那是写死的信号。

        判据用 AST 而不是文本扫描: docstring 里出现型号是**期望**的(要解释依据,
        如「PA601-D54A 主轨 -54V」), 而文本扫描分不清代码与注释, 只能靠「行首 #」
        猜 —— 上面就因此误报过一次(多行 docstring 的第二行不以 # 开头)。
        AST 只看字符串字面量与比较操作数, docstring 是 Expr 而非 Str 参与比较。
        """
        import ast

        tree = ast.parse(
            (ROOT / "src/aterag/ingest/entity_extract.py").read_text(encoding="utf-8")
        )
        # docstring 集合: 模块/类/函数体第一条 Expr(Str)。文档里引用型号是**期望**
        # 的(要解释依据), 判据只看这些之外的位置。
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))

        offenders: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                offenders.extend(
                    node.value
                    for lit in ("-54V", "3.45V", "PA601", "PN1000")
                    if lit in node.value
                )
        assert not offenders, (
            f"代码里出现具体型号/轨电压字面量: {sorted(set(offenders))[:3]} —— "
            f"主轨必须来自 data/registry.yaml 声明"
        )

    def test_main_rail_is_not_computed_from_the_document(self, ee) -> None:
        """主轨只能来自声明, 不能从文档推断。

        推断路径(「带轨行最多者」/「额定电流最大者」)在 PA601 上恰好也对,
        所以只有断言「不声明就不给」才拦得住那种实现。
        """
        assert ee.main_rail_declared("NO-SUCH-MODEL") == ""
        assert ee.resolve_main_rail({"unit": "W", "notes": ""}, "") == ""


@pytest.fixture
def clean_settings_cache():
    """每个用例前后清 ``get_settings`` 的 lru_cache。

    **必须清**: ``get_settings`` 带 ``lru_cache``, 测试里 ``monkeypatch.setenv``
    改了 ``REGISTRY_PATH`` 之后缓存里仍留着指向 tmp 文件的 Settings。monkeypatch
    会在用例结束时还原环境变量, 但**不会**动 lru_cache —— 于是后面的测试读到的是
    一个已被删除的临时路径。实测表现为整批 8 个无关测试
    (``test_local`` 5 个 + ``test_extract`` 1 个)报 ``FileNotFoundError: registry``,
    而它们单独跑全绿。
    """
    from aterag.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestRegistryDrivesEverything:
    """主轨的来源是 registry, 改配置就改行为 —— 不需要改代码。"""

    def test_resolution_reads_registry_each_call(
    self, ee, tmp_path, monkeypatch, clean_settings_cache
) -> None:
        """改 registry 里的 main_rail, 解析结果随之改变(不缓存旧值)。

        走 ``REGISTRY_PATH`` 环境变量而不是 chdir: ``Registry._resolve_path``
        的兜底是「``data/<basename>``」, chdir 到 tmp 后会命中兜底路径从而读到
        别的文件 —— 那样测的就不是「配置驱动」而是「相对路径碰巧对」。
        """
        reg = tmp_path / "registry.yaml"
        reg.write_text(
            "domains:\n  power:\n    workspace: _domain_power\n"
            "products:\n  FAKE-1:\n    domain: power\n"
            "    doc_version: 'A'\n    main_rail: '-48V'\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REGISTRY_PATH", str(reg))
        from aterag.config import get_settings

        # get_settings 是 lru_cache 的, 必须清缓存, 否则读到的是上一次的 settings
        get_settings.cache_clear()
        assert ee.main_rail_declared("FAKE-1") == "-48V"
        reg.write_text(
            "domains:\n  power:\n    workspace: _domain_power\n"
            "products:\n  FAKE-1:\n    domain: power\n"
            "    doc_version: 'A'\n    main_rail: '+24V'\n",
            encoding="utf-8",
        )
        get_settings.cache_clear()
        assert ee.main_rail_declared("FAKE-1") == "+24V"

    def test_declaration_changes_require_no_code_edit(self) -> None:
        """主轨是**数据**: 注册表里加一行就生效, 不需要改 entity_extract。

        这条是「同模板不同型号可重复」的落点 —— 新型号进来只需填声明。
        """
        import dataclasses

        from aterag.registry import ProductEntry

        entry = ProductEntry(domain="power", main_rail="-12V")
        assert entry.main_rail == "-12V"
        assert dataclasses.replace(entry, main_rail="").main_rail == ""

    def test_missing_registry_file_is_not_fatal(
    self, ee, tmp_path, monkeypatch, clean_settings_cache
) -> None:
        """registry 读不到时返回空串(不挂主轨)而不是抛错。

        主轨是**增强**: 缺声明时行保持无轨(不猜), 不该让一份配置缺失阻断整个型号
        的实体抽取。
        """
        monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "nope.yaml"))
        from aterag.config import get_settings

        get_settings.cache_clear()
        assert ee.main_rail_declared("ANY") == ""

    def test_registry_entry_is_the_only_source(self) -> None:
        """主轨存在 ``ProductEntry`` 上 —— 字段有类型有位置, 不是散落的裸 dict。

        绕过注册表自己 yaml.safe_load 就等于承认第二份 main_rail 可以存在。
        """
        import dataclasses

        from aterag.registry import ProductEntry

        names = {f.name for f in dataclasses.fields(ProductEntry)}
        assert "main_rail" in names, "ProductEntry 上没有 main_rail 字段"


class TestVariantTagOrderIsModelIndependent:
    def test_same_text_yields_same_tags_on_any_model(self, ee) -> None:
        """标签抽取不依赖 model_id —— 它只看备注文本。

        如果实现里偷偷按型号挑规则集, 那么同一句话在两个型号上会得到不同 eid,
        而 eid 是要跨型号对照着读的。
        """
        book = ee._pattern_book("PA601-D54A")
        text = "额定220Vac输入，50%最大输出负载，负载突变速率≤0.1A/uS"
        assert ee._semantic_tags({"notes": text}, book) == ee._semantic_tags(
            {"notes": text}, book
        )
        assert ee._semantic_tags({"notes": text}, book), "真实句子应至少命中一条"
