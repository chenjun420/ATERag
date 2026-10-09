"""方法签字书的机制测试。

三条不变量:
1. 未签方法 -> draft/proposed (未人审不得生效)
2. 已签且指纹一致 -> approved/annotated
3. 方法内容改动 -> 指纹不一致 -> 签字自动失效 (回 draft)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

import yaml

from aterag.extract.models import (
    CONF_ANNOTATED,
    CONF_PROPOSED,
    STATUS_APPROVED,
    STATUS_DRAFT,
    TestCondition,
)
from aterag.extract.supplement import (
    MethodBook,
    load_signoffs,
    method_fingerprint,
    supplement_conditions,
)


def _write_methods(tmp: Path, *, note: str = "测试条件") -> Path:
    p = tmp / "test_methods.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "methods": [
                    {
                        "id": "m_test",
                        "basis": "测试依据",
                        "verdict": "numeric",
                        "applies": "always",
                        "applies_to": {"title_pattern": "温度系数"},
                        "supplies": "input",
                        "conditions": [{"kind": "measurement_setup", "value": {"note": note}}],
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return p


def _sign(tmp: Path, methods_path: Path, fp: str, by: str = "张三") -> Path:
    sign_path = tmp / "method_signoffs.yaml"
    sign_path.write_text(
        yaml.safe_dump(
            {
                "methods": {
                    "m_test": {
                        "fingerprint": fp,
                        "approved_by": by,
                        "approved_at": "2026-10-07",
                    }
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return sign_path


def _run(methods_path: Path, sign_path: Path | None) -> TestCondition:
    book = MethodBook.load(methods_path, signoffs_path=sign_path)
    cond = TestCondition(
        req_id="SR-X", title="温度系数", section_path="4.3.2", flags=["no_input_condition"]
    )
    supplement_conditions([cond], book)
    return cond


class TestMethodSignoff:
    def test_unsigned_yields_draft(self, tmp_path: Path) -> None:
        mp = _write_methods(tmp_path)
        cond = _run(mp, None)
        clause = cond.input_conditions[0]
        assert clause.status == STATUS_DRAFT
        assert clause.confidence == CONF_PROPOSED

    def test_signed_matching_fingerprint_yields_approved(self, tmp_path: Path) -> None:
        mp = _write_methods(tmp_path)
        raw = yaml.safe_load(mp.read_text(encoding="utf-8"))["methods"][0]
        fp = method_fingerprint(raw)
        sp = _sign(tmp_path, mp, fp)
        cond = _run(mp, sp)
        clause = cond.input_conditions[0]
        assert clause.status == STATUS_APPROVED
        assert clause.confidence == CONF_ANNOTATED

    def test_changed_method_invalidates_signoff(self, tmp_path: Path) -> None:
        mp = _write_methods(tmp_path)
        raw = yaml.safe_load(mp.read_text(encoding="utf-8"))["methods"][0]
        fp = method_fingerprint(old_raw := raw)
        sp = _sign(tmp_path, mp, fp)
        # 方法内容改动 (note 变) -> 指纹不同 -> 签字失效
        mp2 = _write_methods(tmp_path, note="改过的条件")
        assert method_fingerprint(
            yaml.safe_load(mp2.read_text(encoding="utf-8"))["methods"][0]
        ) != method_fingerprint(old_raw)
        cond = _run(mp2, sp)
        assert cond.input_conditions[0].status == STATUS_DRAFT

    def test_fingerprint_is_content_based_not_file_based(self, tmp_path: Path) -> None:
        # 同一方法 dict 的键序不同, 指纹必须一致 (sort_keys 归一化)
        a = {"id": "m", "basis": "b", "verdict": "numeric"}
        b = {"verdict": "numeric", "basis": "b", "id": "m"}
        assert method_fingerprint(a) == method_fingerprint(b)

    def test_missing_signoff_file_means_unsigned(self, tmp_path: Path) -> None:
        assert load_signoffs(tmp_path / "nope.yaml") == {}
