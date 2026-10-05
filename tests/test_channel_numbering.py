"""输出通道编号: 按表格行序映射到需求, 与轨名解耦。

背景: PA601 输出特性表的「项目」列被 markdown 转换拍平, 「额定输出电压」与
「-54V」挤在同一列 —— 语义上「额定输出电压」是条目名、「-54V」是输出通道。
规格书已按"拆分列 + 复制项目名填充"修正为 9 列, 但抽取侧仍需把通道单独编号:
产测与客户沟通说的是"输出1通道/输出2通道", 不是"-54V/3.45V"。

编号必须按**表格内行序**而非轨名大小或字母序 —— 换产品若 12V 排在 -54V 之前,
它就是输出1通道。这样才与产品无关。

三个判据缺一不可, 少一个都会造出错误的通道:
  1. 复合列: 表头出现重复列名, 才声明了"条目名由子列组成";
  2. 子列是轨名写法: 绝缘试验电压(4000Vdc)同样形似电压, 但它不是输出通道;
  3. 无轨行不编号: 整机级要求(SR-1204 输出功率 / SR-1210 整机效率)适用于
     所有输出轨, 编号它会让"第N通道"凭空多出一路不存在的通道。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aterag.ingest.markdown_parser import parse_markdown  # noqa: E402

_HEADER = (
    "| 编号 | 项目 | 项目 | 单位 | 最小值 | 典型值 | 最大值 | 备注 | 等级 |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)
_SEPARATOR = "\n---\n\n"


def _entities(md: str, model_id: str = "SYNTH-X"):  # noqa: ANN201
    import os

    for k, v in (
        ("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x"),
        ("QDRANT_URL", "http://127.0.0.1:6333"),
        ("LLM_BASE", "http://x/v1"),
        ("LLM_MODEL", "x"),
        ("EMBED_BASE", "http://x"),
        ("EMBED_MODEL", "x"),
    ):
        os.environ.setdefault(k, v)
    from aterag.ingest.entity_extract import extract_from_blocks

    blocks = parse_markdown(md)
    return extract_from_blocks(blocks, model_id=model_id)


def _reqs(md: str, model_id: str = "SYNTH-X"):  # noqa: ANN202
    return [e for e in _entities(md, model_id) if e.etype == "Requirement"]


def _section(title: str, rows: str, header: str = _HEADER) -> str:
    return f"# 规格书\n\n## 4 技术要求\n\n### 4.3 输出特性\n\n**表 输出特性表**\n\n{header}{rows}{_SEPARATOR}"


def _row(req_id: str, item: str, sub: str, unit: str, mn: str, mx: str, notes: str = "-") -> str:
    return f"| {req_id} | {item} | {sub} | {unit} | {mn} | - | {mx} | {notes} | 强制 |\n"


def test_channel_number_follows_row_order_not_rail_name() -> None:
    """通道号按行序, 与轨名大小无关 (负压轨在前即 CH1)。"""
    md = _section(
        "输出特性",
        _row("SR-1", "额定输出电压", "-54V", "V", "-54", "-")
        + _row("SR-1", "额定输出电压", "3.45V", "V", "+3.45", "-"),
    )
    got = {e.props["rail"]: e.props.get("channel_no") for e in _reqs(md) if e.props.get("rail")}
    assert got == {"-54V": "1", "3.45V": "2"}, got


def test_channel_number_follows_table_order_when_first_rail_is_positive() -> None:
    """首行是正压轨时它就是输出1通道 —— 不按电压大小排序。

    这条是跨产品通用性的核心: 若按轨名排序或按绝对值排序, 12V 会排到 -54V
    之后, 产测与客户说的"输出1通道"就对不上了。
    """
    md = _section(
        "输出特性",
        _row("SR-1", "额定输出电压", "12V", "V", "12", "-")
        + _row("SR-1", "额定输出电压", "-54V", "V", "-54", "-"),
    )
    got = {e.props["rail"]: e.props.get("channel_no") for e in _reqs(md) if e.props.get("rail")}
    assert got == {"12V": "1", "-54V": "2"}, got


def test_three_channels_numbered_in_row_order() -> None:
    """三路输出按行序编 1/2/3。"""
    md = _section(
        "输出特性",
        _row("SR-1", "输出电流", "-54V", "A", "0", "11.1")
        + _row("SR-1", "输出电流", "12V", "A", "0", "25")
        + _row("SR-1", "输出电流", "3.45V", "A", "0", "0.1"),
    )
    got = [e.props.get("channel_no") for e in _reqs(md) if e.props.get("rail")]
    assert got == ["1", "2", "3"], got


def test_rail_less_row_gets_no_channel_number() -> None:
    """整机级要求(第3列与项目名相同)不占通道号。

    SR-1204 输出功率 / SR-1210 整机效率 适用于所有输出轨 —— 编号它会让
    "输出3通道"这种不存在的路数凭空出现。
    """
    md = _section(
        "输出特性",
        _row("SR-1", "输出功率", "输出功率", "W", "0", "600", "90~176Vac: 400W")
        + _row("SR-2", "输出电流", "-54V", "A", "0", "11.1")
        + _row("SR-2", "输出电流", "3.45V", "A", "0", "0.1"),
    )
    rows = _reqs(md)
    whole = next(e.props for e in rows if e.props["req_id"] == "SR-1")
    assert whole.get("channel_no") == "", whole.get("channel_no")
    assert whole["rail"] == ""
    # 有轨行照常编号, 且从 1 起 —— 整机行既不占号也不跳号
    chans = sorted({e.props["channel_no"] for e in rows if e.props.get("rail")})
    assert chans == ["1", "2"], chans


def test_non_subcolumn_table_gets_no_channel_numbers() -> None:
    """表头无重复列名时不编号 —— 该表未声明"条目名由子列组成"。"""
    plain = "| 编号 | 项目 | 单位 | 最小值 | 最大值 | 备注 | 等级 |\n|---|---|---|---|---|---|---|\n"
    md = _section(
        "输出特性",
        _row("SR-1", "输出电流", "-54V", "A", "0", "11.1"),
        header=plain,
    )
    rows = _reqs(md)
    assert all(e.props.get("channel_no") in ("", None) for e in rows), [
        e.props.get("channel_no") for e in rows
    ]


def test_withstand_voltage_is_not_a_channel() -> None:
    """绝缘试验电压(带 Vdc 后缀)不得被当成输出通道。

    安规表22 表头也有重复列名(两个「等级」), 中间列是 4000Vdc/2500Vdc/500Vdc。
    只判"复合列"会凭空造出 CH1/CH2/CH3 三路不存在的输出通道。
    """
    safety = "| 编号 | 项目 | 等级 | 标准（或测试条件） | 等级 |\n|---|---|---|---|---|\n"
    md = (
        "# 规格书\n\n## 4 技术要求\n\n### 4.4 安规要求\n\n"
        "#### 4.4.6 安规测试要求\n\n**表 安规测试要求表**\n\n"
        f"{safety}"
        "| SR-2500 | 绝缘电压（输入对输出） | 4000Vdc | 应能承受4000V直流电压1分钟 | 强制 |\n"
        "| SR-2501 | 绝缘电压（输入对地） | 2500Vdc | 应能承受2500V直流电压1分钟 | 强制 |\n"
        "| SR-2502 | 绝缘电压（输出对地） | 500Vdc | 应能承受500Vdc电压1分钟 | 强制 |\n"
        + _SEPARATOR
    )
    rows = _reqs(md)
    assert rows, "应抽出绝缘电压需求"
    assert all(e.props.get("channel_no") in ("", None) for e in rows), [
        (e.props.get("req_id"), e.props.get("channel_no")) for e in rows
    ]


def test_channel_number_is_not_part_of_eid() -> None:
    """通道号不进 eid —— eid 稳定性优先。

    规格书增删输出通道会让 eid 变化, 已审注记随之全部失效需人工重审,
    代价过高。通道号是可重算的展示属性, eid 是引用键, 两者分开。
    """
    md = _section(
        "输出特性",
        _row("SR-1", "额定输出电压", "-54V", "V", "-54", "-")
        + _row("SR-1", "额定输出电压", "3.45V", "V", "+3.45", "-"),
    )
    eids = {e.eid for e in _reqs(md)}
    assert not any("CH" in e or "channel" in e for e in eids), eids


def test_same_rail_in_two_rows_keeps_same_channel() -> None:
    """同轨多行(长期/短期工作制)共用同一通道号。"""
    md = _section(
        "输出特性",
        _row("SR-1", "输出电流", "-54V", "A", "0", "11.1", "长期工作")
        + _row("SR-1", "输出电流", "-54V", "A", "-", "-", "短期工作")
        + _row("SR-1", "输出电流", "3.45V", "A", "0", "0.1", "长期工作"),
    )
    chans = [e.props.get("channel_no") for e in _reqs(md) if e.props.get("rail")]
    assert chans == ["1", "1", "2"], chans
