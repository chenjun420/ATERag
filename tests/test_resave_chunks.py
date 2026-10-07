"""resave_chunks.py 的不变量测试 (方案 §4.6 向量回填)。

这一节要挡的是「回填看起来跑了, 其实没补上」—— 那是 §4.6 的全部意义所在。
向量那一路的检索条件是 ``embedding IS NOT NULL``, 所以型号层向量为空时它被
**整体排除**, 型号层只能靠 BM25 单路支撑, 而这不会报任何错。

早先脚本的三个缺陷 (A3):
1. ``DOC`` 硬编码相对路径 —— 只在开发者那台机器成立, 板卡上必然 FileNotFoundError,
   而报错只是文件名, 没人猜得到要传参;
2. 第 65 行硬编码查 ``SR-PA601-D54A-1308`` —— 换型号就查不到, 且**查不到时静默
   什么都不打**, 让人以为校验通过了;
3. 脚本整体用 ``sys.argv`` + ``asyncio.run`` 在模块级, 没法测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from resave_chunks import build_parser, main, resolve_doc  # noqa: E402


def _code_only(path: Path) -> str:
    """去掉**行注释与文档串**后的源码。

    这些断言钉的是「代码里不再有硬编码」, 而那个需求编号**必须**出现在注释里
    (说明旧实现的缺陷与板卡上跑不起来的来由) —— 所以只能扫代码, 扫全文会误伤。
    """
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    drop: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            drop |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in drop or line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


class TestDocResolution:
    def test_absolute_path_found(self, tmp_path: Path) -> None:
        doc = tmp_path / "spec.md"
        doc.write_text("# x", encoding="utf-8")
        assert resolve_doc(str(doc)) == doc

    def test_relative_path_found_from_cwd(self, tmp_path: Path, monkeypatch) -> None:
        doc = tmp_path / "spec.md"
        doc.write_text("# x", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert resolve_doc("./spec.md").exists()

    def test_missing_doc_says_how_to_fix_it(self) -> None:
        """报错必须说清「请用 -d 指定」并给出板卡路径示例。

        只抛 FileNotFoundError 的话, 调用方拿到的只是一个文件名, 猜不到要传参 ——
        而板卡上规格书根本不在开发机的位置。
        """
        with pytest.raises(SystemExit) as ei:
            resolve_doc("definitely_not_here.md")
        msg = str(ei.value)
        assert "-d" in msg, msg
        assert "/opt/aterag/specs" in msg, "要给板卡上的路径示例, 那是真正要用的"
        assert "definitely_not_here.md" in msg, "要报出试过哪些路径"


class TestCliContract:
    def test_doc_is_required(self, capsys) -> None:
        """``-d`` 必填 —— 与其让 FileNotFoundError 说话, 不如直接要这个参数。"""
        rc = main([])
        assert rc == 2
        out = capsys.readouterr().out
        assert "-d" in out
        assert "/opt/aterag/specs" in out

    def test_doc_flag_is_accepted(self) -> None:
        args = build_parser().parse_args(["-m", "X", "-d", "/opt/aterag/specs/X.md"])
        assert args.model == "X"
        assert args.doc == "/opt/aterag/specs/X.md"

    def test_no_delete_flag_exists(self) -> None:
        """保留一个显式开关: 默认删, 但「不删」也该能说出口。"""
        args = build_parser().parse_args(["-d", "x.md", "--no-delete"])
        assert args.no_delete is True

    def test_code_has_no_hardcoded_requirement_id(self) -> None:
        """旧实现硬编码 ``SR-PA601-D54A-1308`` —— 换型号就查不到, 且静默无输出。

        抽样必须按 workspace 查, 与型号无关。
        """
        assert "SR-PA601-D54A-1308" not in _code_only(ROOT / "scripts/resave_chunks.py"), (
            "仍有硬编码的 PA601 专属需求编号"
        )

    def test_code_has_no_hardcoded_doc_name(self) -> None:
        """旧默认 ``"PA601-D54A 定制电源技术规格书.md"`` 只在本机成立。"""
        assert '"PA601-D54A 定制电源技术规格书.md"' not in _code_only(
            ROOT / "scripts/resave_chunks.py"
        ), "仍有硬编码的默认文档名"


class TestCompletenessVerdict:
    """回填的**验收判据**本身: 向量不完整要报出来, 而不是打印一个数字就算过。

    旧实现只 ``print`` 了 ``rows``/``with_embedding`` 就结束 —— 人要自己比两个数。
    数字摆在那里而没人被告知「不完整」, 等于没有验收。
    """

    def test_reports_incomplete_when_vectors_short(self, monkeypatch, capsys) -> None:

        import resave_chunks as rc

        async def fake_resave(model, doc, *, delete_first=True):
            return {"model": model, "chunks": 10, "deleted": 10, "saved": 10,
                    "vectors": 4, "rows": 10, "with_embedding": 4}

        monkeypatch.setattr(rc, "resave", fake_resave)
        rc.main(["-d", "x.md"])
        out = capsys.readouterr().out
        assert "向量仍不完整" in out, f"向量缺了却没报出来: {out}"

    def test_reports_complete_when_all_embedded(self, monkeypatch, capsys) -> None:
        import resave_chunks as rc

        async def fake_resave(model, doc, *, delete_first=True):
            return {"model": model, "chunks": 10, "deleted": 10, "saved": 10,
                    "vectors": 10, "rows": 10, "with_embedding": 10}

        monkeypatch.setattr(rc, "resave", fake_resave)
        # main() 里跑 asyncio.run(resave(...)); 上面 patch 的是 rc.resave
        rc.main(["-d", "x.md"])
        out = capsys.readouterr().out
        assert "向量回填完整" in out, out
        assert "仍不完整" not in out

    def test_uses_doc_flag_not_default(self, monkeypatch) -> None:
        """``-d`` 传下去, 不能被默认值悄悄替换。"""
        import resave_chunks as rc

        seen: dict = {}

        async def fake_resave(model, doc, *, delete_first=True):
            seen.update(model=model, doc=doc)
            return {"model": model, "chunks": 1, "deleted": 1, "saved": 1,
                    "vectors": 1, "rows": 1, "with_embedding": 1}

        monkeypatch.setattr(rc, "resave", fake_resave)
        rc.main(["-m", "PA601-D54A", "-d", "/opt/aterag/specs/PA601-D54A.md"])
        assert seen["doc"] == "/opt/aterag/specs/PA601-D54A.md"
        assert seen["model"] == "PA601-D54A"
