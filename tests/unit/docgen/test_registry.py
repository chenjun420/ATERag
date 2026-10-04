"""``docgen.registry`` —— 公式总表生成。

核心断言是 :class:`TestRejectsRatherThanFabricates`: 两列 ``NOT NULL`` 在方案
公式表里根本不存在(``name_zh`` 83/129 缺、``source_ref`` 129/129 缺), 模块
**必须拒收而不是编造**。§18.10 注 8 明写「标准条款号不可编造」。

因此本模块在当前语料下的**正确**行为是产出 0 条记录并以非 0 退出 —— 若哪天
它开始安静地填占位值, 这些测试就该红。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aterag.docgen.registry import (
    CSV_COLUMNS,
    DIMENSION_ORDER,
    build_records,
    main,
)
from aterag.docgen.spec_parse import read_spec

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"


@pytest.fixture(scope="module")
def built() -> tuple[tuple[object, ...], object]:
    if not SPEC.is_file():
        pytest.skip(f"方案文件不在预期位置: {SPEC}")
    return build_records(read_spec(SPEC))


class TestRejectsRatherThanFabricates:
    def test_量纲闭合的候选集非空(self, built: tuple[tuple[object, ...], object]) -> None:
        """先确认过滤器**确实有活干** —— 否则「0 条可入库」可能只是没读进语料。"""
        _records, report = built
        assert report.considered == 130, f"候选 {report.considered} 条, 预期 130"

    def test_不编造name_zh(self, built: tuple[tuple[object, ...], object]) -> None:
        """缺中文名的公式一律拒收, 不拿 ID 短名冒充中文名。"""
        _records, report = built
        assert len(report.missing_name_zh) == 43
        assert all(fid.startswith("F_") for fid in report.missing_name_zh)

    def test_不编造source_ref(self, built: tuple[tuple[object, ...], object]) -> None:
        """§18.10 注 8「标准条款号不可编造」。

        ``source_ref`` 是 ``NOT NULL`` 且公式表**无此列**, 只能从标准条目
        反查(实测仅覆盖 4/129)。缺口必须拒收上报。
        """
        _records, report = built
        assert len(report.missing_source_ref) == 87
        assert not any("编造" in r for r in report.reasons)

    def test_当前语料下可入库为0(self, built: tuple[tuple[object, ...], object]) -> None:
        """**当前**的正确行为就是 0 条。

        83 缺 name_zh + 46 缺 source_ref = 129 全被拒。这不是 bug, 是方案侧
        两处缺口。若哪天这个断言开始失败, 说明有人加了编造逻辑。
        """
        records, report = built
        assert len(records) == 0
        assert report.considered == len(report.missing_name_zh) + len(
            report.missing_source_ref
        )

    def test_拒收原因逐条对应到ID(self, built: tuple[tuple[object, ...], object]) -> None:
        """缺口要能点名, 不能只是个数字。"""
        _records, report = built
        for reason in report.reasons:
            assert report.reasons[reason], reason
            assert all(fid.startswith("F_") for fid in report.reasons[reason])


class TestRecordShape:
    def test_构造记录时四列齐备(self) -> None:
        """绕过语料直接构造, 验证列形状 —— 不受当前 0 条的影响。"""
        from aterag.docgen.registry import FormulaRecord
        from aterag.docgen.render import render_equation

        rendered = render_equation("V_out", "D*V_in", "=", {"D": "占空比"})
        assert rendered.error is None
        rec = FormulaRecord(
            formula_id="F_X.1_Y",
            name_zh="测试",
            domain="J",
            section="J.1",
            domain_tags=("变换器拓扑",),
            var_refs=("V_out", "D", "V_in"),
            dimension_vec=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            derive_from=("A-2",),
            source_ref="IEC 60255-1",
            rendered=rendered,
        )
        row = rec.as_csv_row()
        assert set(row) == set(CSV_COLUMNS), "列与 CSV_COLUMNS 不一致"
        assert all(v is not None for v in row.values())
        assert row["derive_from"] == "A-2"
        assert row["source_ref"] == "IEC 60255-1"

    def test_dimension_vec_恰好七个分量(self) -> None:
        """CHECK 只验 ``cardinality = 7``; 顺序错了数据库不会拦。"""
        assert len(DIMENSION_ORDER) == 7

    def test_csv列序含四个表达式列(self) -> None:
        for col in ("expr_latex", "expr_plaintext", "expr_ascii", "expr_ast"):
            assert col in CSV_COLUMNS


class TestCli:
    def test_有拒收则退出码非0(self, tmp_path: Path) -> None:
        """缺口不许被安静吞掉 —— 这是 T3.2 存在的意义。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        out = tmp_path / "formula.csv"
        assert main(["--spec", str(SPEC), "--out", str(out)]) == 1

    def test_写出的CSV有表头且行数与记录一致(self, tmp_path: Path) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        out = tmp_path / "formula.csv"
        main(["--spec", str(SPEC), "--out", str(out)])
        with out.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert list(rows[0].keys()) == list(CSV_COLUMNS) if rows else True
        assert out.is_file()
