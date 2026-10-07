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
    p.write_text(
        "domains:\n"
        "  power:\n"
        "    workspace: _domain_power\n"
        "products:\n"
        "  PA601-D54A:\n"
        "    domain: power\n"
        "    doc_version: B\n"
        "    main_rail: '-54V'\n"
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
