"""门禁与生成器的**标准号提取口径**必须一致 —— 防漂。

## 为什么这条测试存在

「引用到的标准号」要拿去和库里的 ``std::`` 实体比对, 所以需要从
``"GB/Z 14429-2005 §442-01-01"`` 里**提取出** ``GB/Z 14429-2005``。这件事有两处
实现:

- ``scripts/build_seed_data.py`` 的 ``_STD_ID_RE`` —— 决定 ``defined_by`` 边建不建得出来
- ``scripts/knowledge_gate.py`` 的 ``STANDARD_ID_HEAD`` —— 决定门禁报不报「引用不存在」

两处口径不同就会得出**相反**的结论, 而**两个都不报错**: 生成器默默少建一条边,
门禁默默放过一条悬空引用。这正是本项目反复出现的那类缺陷(注释写着意图、
实现是另一回事)。

实测已踩过一次: 门禁原本只有形态检查用的 ``STANDARD_NUMBER``
(``^[A-Z]{2,4}(/([A-Z]|T|Z))?\\s+\\d``, 只锚到第一位数字), 拿它当提取器用会把
``GB/Z 14429-2005`` 截成 ``GB/Z 1``, 于是报出 22 条「库里不存在的标准」——
其中包括刚注入的、真实存在的 ``GB/Z 14429-2005``。报错的门禁比不报更糟。

## 这条测试断言什么

对**真实种子**里每个非空 ``authority_ref``, 两个提取器给出的 head 必须一致。
用真实数据而不是手挑样本: 手挑样本会漏掉前缀书写方式的意外形态
(``IEC/IEC``、``GB/Z`` 里的 ``Z``、带 ``+A1:2012`` 的版本后缀)。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data/seed/power_domain_seed.json"
sys.path.insert(0, str(ROOT / "src"))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 必须先注册进 ``sys.modules``: ``@dataclass`` 装饰器会做
    # ``sys.modules.get(cls.__module__).__dict__``, 模块没登记就是
    # ``'NoneType' object has no attribute '__dict__'``。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load(ROOT / "scripts/knowledge_gate.py", "_kg")


@pytest.fixture(scope="module")
def builder():
    return _load(ROOT / "scripts/build_seed_data.py", "_bsd")


@pytest.fixture(scope="module")
def refs() -> list[str]:
    if not SEED.exists():
        pytest.skip("种子文件不在(离线包/裁剪仓库里)")
    doc = json.loads(SEED.read_text(encoding="utf-8"))
    out = [
        str(r.get("authority_ref"))
        for r in doc["records"]
        if isinstance(r, dict) and r.get("authority_ref")
    ]
    assert out, "种子没有任何 authority_ref —— 样本空了, 这条测试就变成空转"
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class TestStdIdHeadAgrees:
    def test_same_head_on_every_real_authority_ref(self, gate, builder, refs) -> None:
        """两个提取器在真实数据的每一个引用上必须给出同一个 head。"""
        disagree = []
        for ref in refs:
            a = gate.STANDARD_ID_HEAD.match(ref)
            b = builder._STD_ID_RE.match(ref)
            ha = _norm(a.group(0)) if a else None
            hb = _norm(b.group(0)) if b else None
            if ha != hb:
                disagree.append((ref, ha, hb))
        assert not disagree, (
            f"{len(disagree)}/{len(refs)} 处 authority_ref 两个提取器结论不一致"
            f"(门禁 / 生成器): {disagree[:5]}"
        )

    def test_generator_extractor_finds_at_least_one_head(self, builder, refs) -> None:
        """抽得出 head 才有「建边/漏建」这回事 —— 顺带防样本空转。"""
        heads = set()
        for ref in refs:
            m = builder._STD_ID_RE.match(ref)
            if m:
                heads.add(_norm(m.group(0)))
        assert heads, "一个 head 都抽不出来 —— 这条测试已经空转"

    def test_unresolved_heads_are_reported_by_the_gate(self, gate, builder, refs) -> None:
        """抽得出来但库里没有的 head, 门禁**必须**报出来。

        两处口径一致还不够 —— 门禁还得真的执行到。分母取
        ``_STD_ID_RE`` 抽出的 head 集合, 这样门禁漏报(比如只报 32 条不报 61 条)
        也会被抓住。
        """
        from collections import defaultdict

        if not SEED.exists():
            pytest.skip("种子文件不在(离线包/裁剪仓库里)")
        doc = json.loads(SEED.read_text(encoding="utf-8"))
        records = doc["records"]
        std_ids = {
            str(r["id"]).removeprefix("std::").strip()
            for r in records
            if isinstance(r, dict) and r.get("entity_type") == "standard"
        }
        want: dict[str, set[str]] = defaultdict(set)
        for r in records:
            if not isinstance(r, dict) or not r.get("authority_ref"):
                continue
            m = builder._STD_ID_RE.match(str(r["authority_ref"]))
            if not m:
                continue
            head = _norm(m.group(0))
            if head not in std_ids:
                want[head].add(str(r.get("id")))

        report = gate.GateReport()
        gate.check_authority_refsolvable(records, report)
        got = {
            str(f.where)
            for f in report.findings
            if f.check == "authority_ref_resolvable"
        }
        assert got == set(want), (
            f"门禁漏报 {sorted(set(want) - got)[:6]} / 多报 {sorted(got - set(want))[:6]}"
        )


class TestExtractorIsNotTheShapeCheck:
    def test_shape_regex_would_truncate(self, gate) -> None:
        """形态正则**不能**当提取器用 —— 这条测试把踩过的坑钉住。

        ``STANDARD_NUMBER`` 只锚到第一位数字, 拿它提取会得到 ``GB/Z 1``。若哪天
        有人把 :data:`STANDARD_ID_HEAD` 删掉改回形态正则, 这条会先红。
        """
        ref = "GB/Z 14429-2005"
        assert gate.STANDARD_NUMBER.search(ref).group(0) == "GB/Z 1", (
            "形态正则的行为变了 —— 若它已能完整提取, STANDARD_ID_HEAD 就成了第二套口径"
        )
        assert gate.STANDARD_ID_HEAD.match(ref).group(0) == ref

    def test_clause_suffix_is_not_part_of_the_head(self, gate) -> None:
        """``"GB/T 2900.70-2008 §442-01-01"`` 的 head 只到标准号, 不含条款。"""
        m = gate.STANDARD_ID_HEAD.match("GB/T 2900.70-2008 §442-01-01")
        assert m is not None
        assert _norm(m.group(0)) == "GB/T 2900.70-2008"

    def test_covers_prefixes_present_in_the_seed(self, gate) -> None:
        """前缀集合要覆盖种子里真实出现的那些 —— 少一个就漏一批检查。"""
        for ref in (
            "DL/T 5-2019",
            "GJB/Z 299C-2003",
            "EN 50156-2016",
            "IEEE 1588-2019",
            "ISO 13849-1-2023",
            "UL 62368-1-2013",
            "CISPR 22-2008",
            "GB/Z 14429-2005",
            "YD/T 4523-2023",
        ):
            assert gate.STANDARD_ID_HEAD.match(ref) is not None, (
                f"提取器认不出 {ref!r} —— 种子里有这条标准, 它的引用会被漏检"
            )
