"""注册表 schema 名声明 + RLS 上下文设置 的测试。

两个坑各有测试:
1. ``Registry.save()`` 曾静默丢掉 ``main_rail`` —— 保存一次就把声明抹了,
   之后未标注的行全部不再归主轨, 而注册表文件看起来完好无损。
2. ``app.current_model`` 设成 ``'"pw_sr5400"'``(字面含双引号)时, 与 RLS 策略里
   比的 ``'pw_sr5400'`` 不相等, 症状是 INSERT 报「违背行级安全策略」/ SELECT
   返回 0 行, 都不指向真正的配置问题。
"""

from __future__ import annotations

import pytest

from aterag.config import Settings
from aterag.registry import Registry
from aterag.storage.rls import set_current_model_sql, set_current_schema_sql


@pytest.fixture
def registry_file(tmp_path):
    p = tmp_path / "registry.yaml"
    # **带注释**: 真实注册表里 main_rail/schema 上方写着「为什么不自动推断」
    # 「为什么必须声明」这类依据。没有注释的 fixture 测不出「save 抹注释」——
    # 那正是本文件里 TestSavePreservesComments 的全部意义。
    p.write_text(
        "domains:\n"
        "  power:\n"
        "    workspace: _domain_power\n"
        "products:\n"
        "  PA601-D54A:\n"
        "    domain: power\n"
        "    doc_version: B\n"
        "    # 主轨: 不自动推断, 猜错会把判据挂到不存在的输出路上\n"
        "    main_rail: '-54V'\n"
        "    # PG schema 名: 板卡实际部署的那个, 型号键推导出的不是一回事\n"
        "    schema: pw_sr5400\n"
        "  PN1000-48A:\n"
        "    domain: power\n"
        "    schema: pw_pn1000\n",
        encoding="utf-8",
    )
    return p


def _reg(registry_file):
    return Registry.load(Settings(registry_path=str(registry_file)))


def test_schema_name_reads_declared(registry_file):
    assert _reg(registry_file).schema_name("PA601-D54A") == "pw_sr5400"


def test_schema_name_raises_when_undeclared(registry_file):
    """未声明必须报错 —— 现算 `model_schema_name('PA601-D54A')` 会得到
    `pw_pa601_d54a`, 在同一型号名下建第二份空 schema(红线 4 的双源)。"""
    reg = _reg(registry_file)
    reg.products["PN2000-24A"] = reg.products["PN1000-48A"]
    reg.products["PN2000-24A"].schema = ""
    with pytest.raises(KeyError, match="未声明 PG schema"):
        reg.schema_name("PN2000-24A")


def test_schema_name_raises_for_unknown_model(registry_file):
    with pytest.raises(KeyError, match="型号未注册"):
        _reg(registry_file).schema_name("NOPE-1")


def test_save_round_trips_main_rail(registry_file):
    """**回归**: save() 曾只写 domain/doc_*/doc_profile, 保存一次就把
    main_rail 与 schema 抹掉 —— 声明没了而文件看起来正常。"""
    reg = _reg(registry_file)
    reg.save()
    again = _reg(registry_file)
    assert again.products["PA601-D54A"].main_rail == "-54V"
    assert again.products["PA601-D54A"].schema == "pw_sr5400"
    assert again.products["PN1000-48A"].schema == "pw_pn1000"


def test_save_omits_empty_fields(registry_file):
    """空值不写进 yaml —— 让「未声明」在文件里看得见, 而不是写成空串。"""
    reg = _reg(registry_file)
    reg.products["PN1000-48A"].main_rail = ""
    reg.save()
    text = registry_file.read_text(encoding="utf-8")
    assert "main_rail" not in text.split("PN1000-48A")[1]


class TestReentryMustNotWipeDeclarations:
    """**重入**已存在型号时, 未显式传入的字段必须保持原值。

    为什么单独成类
    --------------
    ``test_save_round_trips_main_rail`` 只调 ``save()``, 覆盖不到这条路径 ——
    而抹掉声明的不是 ``save()`` 本身, 是 :meth:`Registry.register_product`
    用默认值**整体替换** ``ProductEntry``。

    实际触发者: ``ingest_spec`` 调 ``register_product(model_id, domain,
    doc_number="", doc_version=...)``, 不传 ``main_rail``/``schema``/
    ``doc_profile``。于是**每跑一次导入, 注册表里的这三项声明就被抹一次**,
    而文件语法正确、字段看着齐全, 只是空了 —— 静默且不可逆。

    后果都能各自独立地坏掉: 主轨一没, 未标注轨的参数行全部不再归轨;
    schema 一没, ``schema_name()`` 转为抛错, 数据落到别处去了。
    """

    def test_reentry_preserves_main_rail_and_schema(self, registry_file):
        reg = _reg(registry_file)
        reg.register_product("PA601-D54A", "power", doc_number="", doc_version="B")
        again = _reg(registry_file)
        assert again.products["PA601-D54A"].main_rail == "-54V"
        assert again.products["PA601-D54A"].schema == "pw_sr5400"

    def test_reentry_preserves_doc_profile(self, registry_file):
        """PN2000 的档案绑定也受同一批字段拖累 —— 它决定抽取走哪份档案。"""
        reg = _reg(registry_file)
        reg.register_product("PN2000-24A", "power", doc_profile="power_spec_cn_pn2000")
        _reg(registry_file).register_product("PN2000-24A", "power", doc_version="A")
        assert _reg(registry_file).products["PN2000-24A"].doc_profile == "power_spec_cn_pn2000"

    def test_repeated_reentry_is_stable(self, registry_file):
        """幂等: 连跑三次结果与跑一次相同(第二三次不能再退化成空)。"""
        reg = _reg(registry_file)
        for _ in range(3):
            reg.register_product("PA601-D54A", "power", doc_version="B")
        p = _reg(registry_file).products["PA601-D54A"]
        assert (p.main_rail, p.schema) == ("-54V", "pw_sr5400")

    def test_explicit_value_still_overrides(self, registry_file):
        """保留原值不等于「无法改」—— 显式传入的字段照常覆盖。

        这条钉住方向: 修法是「没传的才保留」, 不是「一律不覆盖」。后者会把
        改主轨的正常操作也堵死。
        """
        reg = _reg(registry_file)
        reg.register_product("PA601-D54A", "power", main_rail="3.45V")
        assert _reg(registry_file).products["PA601-D54A"].main_rail == "3.45V"

    def test_explicit_empty_clears(self, registry_file):
        """想清空声明就显式传空串 —— 那是明确的意图, 不该被「保留原值」拦下。"""
        reg = _reg(registry_file)
        reg.register_product("PA601-D54A", "power", main_rail="")
        assert _reg(registry_file).products["PA601-D54A"].main_rail == ""


class TestSavePreservesComments:
    """``save()`` 不得抹掉注册表里的注释。

    注释不是装饰: ``main_rail`` 上面写着「为什么不自动推断」, ``schema`` 上面
    写着「为什么必须声明而不是现算」。整体 ``safe_dump`` 覆盖会把它们全清掉,
    文件仍然语法正确、字段齐全, 读代码的人却再也看不到依据。
    """

    def test_comments_survive_save(self, registry_file):
        before = registry_file.read_text(encoding="utf-8")
        comments = [ln for ln in before.split("\n") if ln.strip().startswith("#")]
        assert comments, "fixture 里本来就没有注释, 这条测不出东西"
        _reg(registry_file).save()
        after = registry_file.read_text(encoding="utf-8")
        assert [ln for ln in after.split("\n") if ln.strip().startswith("#")] == comments

    def test_comments_survive_reentry(self, registry_file):
        """重入(真实触发路径)也要保住注释, 不只是直接 save。"""
        before = [
            ln
            for ln in registry_file.read_text(encoding="utf-8").split("\n")
            if ln.strip().startswith("#")
        ]
        _reg(registry_file).register_product("PA601-D54A", "power", doc_version="B")
        after = [
            ln
            for ln in registry_file.read_text(encoding="utf-8").split("\n")
            if ln.strip().startswith("#")
        ]
        assert after == before

    def test_value_change_is_still_written(self, registry_file):
        """保注释不能变成「什么都不写」—— 值确实变了必须落盘。"""
        reg = _reg(registry_file)
        reg.set_domain_populated("power")
        text = registry_file.read_text(encoding="utf-8")
        assert "kb_status: populated" in text

    def test_file_stays_reloadable(self, registry_file):
        """重建后的文件必须还能被 load 读回 —— 写坏文件比丢注释严重。"""
        reg = _reg(registry_file)
        reg.register_product("PA601-D54A", "power", doc_version="B")
        reg.set_domain_populated("power")
        again = _reg(registry_file)
        assert again.domains["power"].kb_status == "populated"
        assert again.products["PA601-D54A"].domain == "power"

    def test_no_duplicate_keys(self, registry_file):
        """**同一个块里不许出现重复键。**

        重复键是 YAML 里最阴的一类坏: 后者覆盖前者, 值相同时无害、值不同时静默
        丢数据。所以它能通过上面那条「读回来和期望一致」的测试 —— 文件本身已经
        坏了, 而检查是绿的。

        产生过的真实形态: 补字段时把行插到「已发出内容的末尾」而不是「本块末尾」,
        于是 ``workspace`` 出现两次、``kb_status`` 挤在中间、注释跑到文件最前。

        这条断言刻意检查**文本**而不是解析结果 —— 解析结果看不见重复键。
        """
        reg = _reg(registry_file)
        reg.register_product("PA601-D54A", "power", doc_version="B")
        reg.set_domain_populated("power")

        block: list[str] = []  # 当前 entry 下已出现的字段名
        for raw in registry_file.read_text(encoding="utf-8").split("\n"):
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent in (0, 2):
                block = []  # 新段 / 新条目 -> 块重置
                continue
            field = raw.strip().split(":", 1)[0]
            assert field not in block, (
                f"块内重复字段 {field!r} —— YAML 里后者覆盖前者, 值不同时静默丢数据: {raw!r}"
            )
            block.append(field)

    def test_fields_land_in_the_right_block(self, registry_file):
        """新字段必须落在**自己那个块**里, 不能跨段。

        ``kb_status`` 属于 ``domains``, 落到 ``products`` 段下面时 YAML 靠缩进
        判定归属, 于是它变成某个 product 的字段 —— 而那个 product 的
        ``schema`` 声明就此不见。
        """
        reg = _reg(registry_file)
        reg.set_domain_populated("power")
        lines = [
            ln
            for ln in registry_file.read_text(encoding="utf-8").split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        sec = None
        for ln in lines:
            indent = len(ln) - len(ln.lstrip(" "))
            if indent == 0:
                sec = ln.strip().rstrip(":")
            elif indent == 2:
                continue  # 条目名本身不参与字段归属判定
            else:
                field = ln.strip().split(":", 1)[0]
                owner = "domains" if field in ("workspace", "kb_status") else "products"
                assert sec == owner, f"字段 {field!r} 出现在 {sec!r} 段下, 但它属于 {owner!r} 段"


# --------------------------------------------------------------------------
# RLS 上下文
# --------------------------------------------------------------------------


def test_set_current_schema_sql_has_no_stray_quotes():
    """值里不能带双引号。

    `quote_literal(quote_ident(s))` 得到的是文字含双引号的 `'"pw_sr5400"'`,
    与策略 `ctx_model() = 'pw_sr5400'` 不相等 —— 表开了 FORCE RLS 时
    所有行都看不见, 且不指向任何配置错误。
    """
    sql = set_current_schema_sql("pw_sr5400")
    assert sql == "SET LOCAL app.current_model = 'pw_sr5400'"


def test_set_current_schema_sql_rejects_bad_identifier():
    from aterag.storage.schema import SchemaError

    with pytest.raises(SchemaError):
        set_current_schema_sql("pw_sr5400'; DROP TABLE x --")


def test_set_current_model_sql_differs_from_declared_schema():
    """型号键推导 ≠ 声明的 schema 名时, 两者设出的上下文不同 —— 落库必须用后者。

    这条不是「顺便记一下」: `PA601-D54A` 推导得 `pw_pa601_d54a`, 板卡上部署的是
    `pw_sr5400`。用推导值设上下文, 所有行都会被 RLS 静默挡掉。
    """
    assert "'pw_pa601_d54a'" in set_current_model_sql("PA601-D54A")
    assert "'pw_sr5400'" in set_current_schema_sql("pw_sr5400")
