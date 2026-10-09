# -*- coding: utf-8 -*-
"""board_semantica_status 的「服务路径接入判定」不变量。

这些断言钉的不是「有几处 import」这个会变的数字, 而是**判定方法本身不许退化**。

判据一旦做浅, 结论会反向: 浅层 glob (``src\\aterag\\*.py``) 漏掉函数内 import
与嵌套子目录, 得出「生产代码零 import」—— 那个结论会让人**误删一个正在被
服务路径使用的依赖**。``pip show`` 的返回码同样不可信 (Windows venv 上报
「未安装」, 而 import 成功)。所以下面分别钉: 扫描范围、匹配形态、包状态判据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from board_semantica_status import (  # noqa: E402
    REPO,
    SRC,
    _package_installed,
    service_path_consumes_semantica,
)


class TestScanScope:
    """扫描范围: 必须覆盖整个包树, 而不是顶层。"""

    def test_scans_the_whole_package_tree_not_just_the_top_level(self):
        """必须 rglob 全树。

        真实的 import 分布在 ``api/`` / ``conflicts/`` / ``inference/`` /
        ``kg/`` / ``provenance/`` 五个子包里, 顶层 ``src/aterag/*.py`` 一处都没有 ——
        顶层 glob 会得出「零 import」那个反向结论。
        """
        assert SRC.is_dir(), f"扫描根不存在: {SRC}"
        nested = [p for p in SRC.rglob("*.py") if p.parent != SRC]
        assert len(nested) > 20, (
            f"子目录只有 {len(nested)} 个 .py —— rglob 没生效, 扫描范围退化成顶层 glob 了"
        )

    def test_actually_finds_the_nested_imports(self):
        """实测: 现在确有 import, 且每一处都在子目录里。

        这条会随代码演进而变 (有人删掉 import 就红), 但那正是它该做的 ——
        「服务路径不再用 semantica」是个需要有人拍板的事实, 不该悄悄发生。
        真要变时, 改这里并写清为什么。
        """
        connected, hits = service_path_consumes_semantica()
        assert connected, "src/aterag 下无任何 semantica import —— 若确为有意移除, 请改本断言"
        assert hits, "connected=True 但没有命中位置, 判定逻辑自相矛盾"
        # 每条都应形如 相对路径:行号, 否则报错时定位不到
        for h in hits:
            assert ":" in h, f"命中位置缺行号, 报错时无法定位: {h!r}"
            path_part = h.rsplit(":", 1)[0]
            assert (REPO / path_part).exists(), f"命中位置指向不存在的文件: {h!r}"

    def test_module_level_and_function_level_imports_are_both_caught(self):
        """``importorskip`` 风格(模块级)与函数内 import 都必须被抓到。

        本项目两种都有: ``provenance/seed_loader.py`` 是模块级
        ``from semantica.provenance.manager import ...``, 而
        ``kg/analytics.py`` 是函数内 ``from semantica.kg import GraphValidator``。
        只匹配行首无缩进的形态会漏掉后者 —— 而后者恰好是 semantica 的主要用法
        (重依赖, 按需导入)。
        """
        _, hits = service_path_consumes_semantica()
        files = {h.rsplit(":", 1)[0] for h in hits}
        assert "src/aterag/kg/analytics.py" in files, "函数内 import 没被抓到(要求行首零缩进?)"
        assert "src/aterag/provenance/seed_loader.py" in files, "模块级 import 没被抓到"


class TestPackageState:
    """包状态判定 —— 上面第 2、3 次错误出在这里。"""

    def test_reports_installed_with_version_when_importable(self):
        """semantica 是 pyproject 主依赖, 装不上时整条推理链都跑不起来。

        断言「能 import」而不是断言某个版本号: 版本会升级, 「装得上」才是
        这条链路真正依赖的事实。
        """
        state = _package_installed()
        assert state.startswith("OK"), (
            f"semantica 不可用: {state} —— pyproject 声明它是主依赖, "
            f"装不上则 kg/provenance/conflicts/inference/explorer 全部无法导入"
        )

    def test_failure_is_reported_distinctly_from_absent(self):
        """失败与「没装」必须能区分开。

        装残了(文件在但导入炸)与压根没装, 处置完全不同: 前者要修环境, 后者要
        改依赖声明。第一版用 ``pip show`` 返回码把两者都报成「未安装」。
        """
        state = _package_installed()
        assert not state.startswith("未安装"), "沿用了 pip show 的判定口径, 换成实际 import"
        if not state.startswith("OK"):
            assert state.startswith("IMPORT-FAILED"), f"非 OK 状态必须带失败原因: {state!r}"
            assert "(" in state and ":" in state, f"失败信息缺异常类型/消息: {state!r}"


class TestNoUnverifiableClaims:
    """脚本输出不许出现它无法核实的断言。"""

    def test_no_claim_about_a_removed_subsystem(self):
        """可执行代码里不许再提 LightRAG —— 已按 ADR-014 全量移除 (186719a)。

        原脚本末尾打印「与 LightRAG 的 ``{workspace}_chunk_entity_relation``
        图谱彼此独立」。那句话在指一个本项目已经不存在的东西, 会让人照着去找
        一个找不到的图谱, 并据此以为还有第二套图谱要维护。

        **用 AST 判而不是按行首匹配**: 注释里解释「为什么删掉了这句话」是必要
        的, 而 docstring 是续行拼成的 —— 按 ``#`` 前缀过滤会漏掉它。第一版
        就是这么写的, 结果这条断言在正确实现上误报 (它把 docstring 里的
        引用当成了可执行代码)。AST 只看字符串常量, 注释根本不进 AST,
        判据与「会不会被打印」严格对齐。
        """
        import ast

        path = ROOT / "scripts" / "board_semantica_status.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_doc = ast.get_docstring(tree) or ""
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc_nodes.add(ast.get_docstring(node, clean=False))
        doc_nodes.discard(None)

        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            s = node.value
            if "chunk_entity_relation" not in s and "LightRAG" not in s:
                continue
            # 文档字符串里可以解释「为什么不再提它」, 其余任何字符串都不行
            if s in doc_nodes or (module_doc and s in module_doc):
                continue
            offenders.append(s)
        assert not offenders, f"可执行字符串里仍在宣称已移除的子系统: {offenders}"

    @pytest.mark.parametrize(
        "token",
        ["SEMANTICA_ENABLED", "svc-path imports"],
        ids=["enabled_printed", "import_count_printed"],
    )
    def test_integration_claims_are_actually_computed(self, token):
        """这两行必须来自实算, 不能是硬编码字符串。

        docstring 曾经承诺「运行时接入判定」而 main() 里没有任何判定 —— 承诺的
        东西不实现比不写更坏。所以钉住: 判定结论由函数算出, 打印只是搬运。
        """
        src = (ROOT / "scripts" / "board_semantica_status.py").read_text(encoding="utf-8")
        assert token in src, f"输出里应包含 {token}"
        assert "service_path_consumes_semantica()" in src, "判定函数没被调用"
        assert "_package_installed()" in src, "包状态没被实算"
