"""推导表达式沙箱的边界 (方案 §4.5 补工装夹具规则时抓到的假阳性)。

原 :data:`aterag.inference.engine._DENY_RE` 是无边界的 ``search``, 于是
``can_inject_open``(故障注入「开路」判据的输入名)因为**含子串** ``open`` 被判成
「调用 open」, 整条规则直接不可用 —— 而报错是 ``expr contains forbidden
tokens``, 人完全猜不到是自己的变量名撞上了关键字。

这类假阳性比没有机制更糟: 它逼着人改掉正常的变量名, 或者干脆删掉规则。
所以这里两条都要钉住 —— 真禁用拦得住, 正常名不误伤。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aterag.inference.engine import _DENY_RE  # noqa: E402


class TestForbiddenTokensStillBlocked:
    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os').system('x')",
            "eval('1+1')",
            "exec('a=1')",
            "open('/etc/passwd').read()",
            "compile('x','','eval')",
            "globals()",
            "getattr(x, 'y')",
            "setattr(x, 'y', 1)",
            "import os",
            "x.__class__",
        ],
    )
    def test_real_escapes_are_blocked(self, expr: str) -> None:
        assert _DENY_RE.search(expr), f"应被拦下却放过了: {expr}"

    def test_dunder_is_still_blocked(self) -> None:
        """``__`` 单独成条 —— 属性逃逸全靠它。"""
        assert _DENY_RE.search("a.__init__")
        assert _DENY_RE.search("x.__class__.__bases__")


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "expr",
        [
            # 实施 §4.5 时真实踩到的那个: 输入名含 open
            "min(can_inject_open, can_inject_short_inter_channel)",
            "can_inject_open",
            # 同类子串
            "max(reopen_count, eval_count)",
            "compass_heading * 2",
            "recursive_depth",
            "executable_mode",
            "open_gap_mm",
            # 正常数学表达式
            "sum(t * t for t in components) ** 0.5",
            "min(spec_tolerance, process_variation) / 10",
            "-(-throughput * test_time // available_time)",
            "1 if default_state == 'normally_closed' else 0",
        ],
    )
    def test_normal_expressions_pass(self, expr: str) -> None:
        assert not _DENY_RE.search(expr), f"被误伤: {expr!r}"

    def test_boundary_is_the_whole_point(self) -> None:
        """同一个词, 独立出现被拦、作为标识符一部分被放行。

        这两条写在同一个测试里, 是为了说清区别只在**边界**而不在词本身 ——
        否则下一个人看到「open 竟然放过了」就会去「修」它。
        """
        assert _DENY_RE.search("open('/x')")
        assert not _DENY_RE.search("can_inject_open")
        assert not _DENY_RE.search("open_gap")


class TestEngineActuallyEvaluatesSafely:
    def test_empty_builtins_mean_dangerous_calls_unreachable(self) -> None:
        """双重防护的另一半: ``eval`` 的 globals 里 ``__builtins__`` 被置空。

        正则只是额外一道 —— 真拦得住的是这里。所以必须验证 ``eval`` 的第二个参数
        真的带着空 ``__builtins__``, 否则有人「优化」掉它就静默失去防线。
        """
        import inspect

        from aterag.inference import engine

        src = inspect.getsource(engine.InferenceEngine._derive_one) if hasattr(
            engine.InferenceEngine, "_derive_one"
        ) else ""
        evals = [
            line.strip()
            for line in inspect.getsource(engine).splitlines()
            if "eval(" in line and "compile" not in line
        ]
        assert evals, "找不到 eval 调用点"
        assert any('"__builtins__": {}' in line or "'__builtins__': {}" in line for line in evals), (
            f"eval 未清空 __builtins__: {evals}"
        )
        assert src is not None
