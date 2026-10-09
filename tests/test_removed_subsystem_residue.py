# -*- coding: utf-8 -*-
"""已移除子系统的残留检查: 死引用 / 死配置 / 死脚本。

为什么要有这条门禁
------------------
某个子系统被移除后, 残留不会自己消失, 而且**每种残留都静默**:

* 编排器引用已删除的脚本 -> ``subprocess`` 抛 FileNotFoundError, 被宽泛捕获后
  报成「该套件失败」, 看不出真相是编排器指向不存在的文件。
* 部署脚本 import 已移除的包 -> 安装时才发现装不上, 而那时板卡已经改了一半。
* 文档教配已删除的环境变量 -> 用户照着配, 静默无效, 直到某个功能不工作。
* 诊断脚本查已删除的表 -> 排查时抱着一个「表不存在」的错误结论。

所以门禁扫的是**引用**, 而不是「有没有这个词」: ADR 与交付报告里出现这些名字
是正确的 (那是存档), 而 ``ci.yml`` / ``deploy/`` / ``src/`` / ``scripts/`` /
配置与文档里出现就是残留。

豁免范围 (显式列出, 不靠通配猜)
--------------------------------
* ``docs/adr/``        —— 决策存档, 按 README 的边界说明记录的是「当时」
* ``DELIVERY-REPORT.md`` / ``docs/knowledge-gap-*.md`` —— 交付与评估记录
* ``tests/test_retrieval_hybrid.py`` —— 防复归护栏, 断言本身要按名字检查
* 两份方案 md          —— 一次性引导源, 交付后不再依赖 (见 seed provenance)
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

#: 已移除的子系统。``chunk_entity_relation`` 是一并清掉的 AGE 图 schema 名。
REMOVED = ("lightrag", "qdrant", "chunk_entity_relation")
PATTERN = re.compile("|".join(REMOVED), re.IGNORECASE)

#: 按目录/文件的豁免表: **路径前缀 -> 理由**。
#: 新增豁免必须写理由 —— 无理由的豁免等于把检查关掉。
EXEMPT: dict[str, str] = {
    "docs/adr/": "决策存档: ADR-014 记录的就是移除该子系统的裁决",
    "docs/knowledge-gap-": "知识缺口评估记录, 记录当时的状态",
    "docs/规格书上传与处理.md": "该文档明确声明「已整层退场」并指向替代方案, 属有效告知",
    "DELIVERY-REPORT.md": "交付验收记录, 含历史数字",
    "tests/test_retrieval_hybrid.py": "防复归护栏: 断言本身按名字检查已移除的库",
    "tests/test_board_semantica_status.py": "门禁自身: 按名字检查已移除的子系统",
    "tests/test_removed_subsystem_residue.py": "门禁自身: 判据里含被检查的名字",
    "产测智能体系统-规格书驱动RAG与多Agent协同方案.md": "方案原文, 一次性引导源",
    "定制电源产品转产工装研发系统_开发指导方案_V6.0.md": "方案原文, 一次性引导源",
}

SCANNED_SUFFIXES = {".py", ".yml", ".yaml", ".toml", ".sh", ".md", ".json"}


def _exempt(rel: str) -> str | None:
    for prefix, why in EXEMPT.items():
        if rel.startswith(prefix):
            return why
    return None


def _walk() -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SCANNED_SUFFIXES:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "node_modules/", ".git/", "__pycache__/")):
            continue
        if _exempt(rel):
            continue
        out.append(p)
    return out


def _hits(path: Path) -> list[tuple[int, str]]:
    try:
        lines = io.open(path, encoding="utf-8").read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [(i, ln.strip()) for i, ln in enumerate(lines, 1) if PATTERN.search(ln)]


def test_no_removed_subsystem_references() -> None:
    """除显式豁免外, 仓库里不得再出现已移除子系统的名字。

    失败时的每一条都要人判断, 不能顺手加豁免了事: 出现的位置几乎都意味着
    「有个东西还指向已经不存在的依赖」—— 那正是本门禁要抓的。
    """
    offenders: list[str] = []
    for p in _walk():
        for lineno, text in _hits(p):
            rel = p.relative_to(ROOT).as_posix()
            offenders.append(f"{rel}:{lineno}  {text[:100]}")
    assert not offenders, (
        f"发现 {len(offenders)} 处已移除子系统({', '.join(REMOVED)})的残留:\n  "
        + "\n  ".join(offenders[:40])
        + ("\n  ..." if len(offenders) > 40 else "")
    )


class TestExemptionTableIsHonest:
    """豁免表本身不许变成「关掉检查的后门」。"""

    def test_every_exemption_has_a_reason(self) -> None:
        for prefix, why in EXEMPT.items():
            assert why.strip(), f"豁免 {prefix} 没写理由"
            assert len(why.strip()) >= 8, f"豁免 {prefix} 的理由太简略: {why!r}"

    def test_every_exemption_actually_still_mentions_it(self) -> None:
        """豁免理由里说的那个文件必须真的还在, 否则豁免变成僵尸。

        文件被删/改名后豁免会静默留着 —— 而检查范围已经不包括它了, 于是
        看起来「有豁免所以检查过了」, 实际那个文件早没人扫了。
        """
        stale = [
            prefix
            for prefix in EXEMPT
            if not any(
                p.relative_to(ROOT).as_posix().startswith(prefix)
                for p in ROOT.rglob("*")
                if p.is_file()
            )
        ]
        assert not stale, f"豁免指向的文件已不存在(僵尸豁免): {stale}"

    def test_scan_actually_covers_the_risky_dirs(self) -> None:
        """扫描范围必须含 CI / 部署 / 活代码 / 配置。

        少扫一个目录就等于那个目录里的死引用没人管 —— 而 CI 配置与部署脚本
        恰恰是「照着执行会真失败」的两处。
        """
        rels = {p.relative_to(ROOT).as_posix() for p in _walk()}
        for must in (
            ".github/workflows/ci.yml",
            "pyproject.toml",
            "deploy/native/06-install-aterag.sh",
            "deploy/docker-compose.yml",
            "scripts/verify_all.py",
            "src/aterag/retrieval/hybrid.py",
        ):
            assert must in rels, f"扫描范围漏了 {must}"


class TestVerifierSelfCheck:
    """门禁自己得先证明它能抓到东西。"""

    def test_detector_fires_on_a_planted_reference(self, tmp_path: Path) -> None:
        """往临时文件里塞一处残留, 判定函数必须报出来。

        没有这条, 「扫描范围没覆盖到」和「代码里真的干净」在测试里长得一样 ——
        而这两种情况下门禁给出的是同一个 PASS。
        """
        fake = tmp_path / "sample.py"
        fake.write_text("dsn = 'lightrag://x'\n", encoding="utf-8")
        assert _hits(fake), "判定函数对植入的残留无反应 —— 门禁是空转的"

    def test_detector_is_case_insensitive(self, tmp_path: Path) -> None:
        fake = tmp_path / "s.py"
        fake.write_text("X = 'Qdrant_Client'\n", encoding="utf-8")
        assert _hits(fake), "大小写不同的残留漏掉了"

    def test_detector_ignores_clean_files(self, tmp_path: Path) -> None:
        fake = tmp_path / "s.py"
        fake.write_text("x = 'pgvector'\n", encoding="utf-8")
        assert not _hits(fake), "干净文件被误报"


class TestOrchestratorHasNoDeadReference:
    """编排器引用不存在的脚本必须报错, 而不是被宽泛捕获成「套件失败」。"""

    def test_verify_all_suite_scripts_all_exist(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("va", ROOT / "scripts" / "verify_all.py")
        va = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(va)
        try:
            va.assert_suite_scripts_exist()
        except FileNotFoundError as e:
            pytest.fail(f"verify_all 引用了不存在的脚本: {e}")

    def test_self_check_actually_raises_on_a_dead_reference(self) -> None:
        """自检本身要能抛 —— 不然它只是个永远返回空的函数。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location("va2", ROOT / "scripts" / "verify_all.py")
        va = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(va)
        bogus = [("假的套件", ["scripts/definitely_not_here.py"], re.compile("X"))]
        with pytest.raises(FileNotFoundError):
            va.assert_suite_scripts_exist(bogus)


class TestNoStaleDeploymentInstructions:
    """部署文档不许指向不存在的脚本。"""

    @pytest.mark.parametrize(
        "doc",
        ["deploy/native/README-部署与接入说明.md", "docs/使用说明.md"],
        ids=["deploy_readme", "usage_doc"],
    )
    def test_referenced_scripts_exist(self, doc: str) -> None:
        text = (ROOT / doc).read_text(encoding="utf-8")
        # 只查反引号里形如 scripts/xxx.py 的引用
        for ref in set(re.findall(r"scripts/[A-Za-z0-9_]+\.py", text)):
            assert (ROOT / ref).exists(), f"{doc} 指向不存在的脚本 {ref} —— 照着做会卡住"

    def test_usage_doc_lists_no_removed_env_vars(self) -> None:
        """.env 变量表不许教配已删除的变量(配了静默无效)。"""
        text = (ROOT / "docs" / "使用说明.md").read_text(encoding="utf-8")
        table = re.search(r"必需的环境变量.*?\n\n", text, re.S)
        assert table, "找不到环境变量表"
        assert not PATTERN.search(table.group(0)), "环境变量表里仍有已移除子系统的变量"

    def test_env_example_and_docs_agree(self) -> None:
        """.env.example 是唯一真相, 文档不许声明 example 里没有的必填项。

        **只查必需变量表**, 不查全文: 文档正文里有 ``ATERAG_POSTGRES_DSN`` 这种
        「反例说明」(「环境变量没有前缀, 是 POSTGRES_DSN 而不是 ATERAG_...」),
        全文匹配会把它当成一个没人声明过的配置项而误报。
        """
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        declared = {m.split("=", 1)[0] for m in re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M)}
        doc = (ROOT / "docs" / "使用说明.md").read_text(encoding="utf-8")
        table = re.search(r"必需的环境变量.*?\n\n", doc, re.S)
        assert table, "找不到环境变量表"
        for var in set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", table.group(0))):
            assert var in declared, (
                f"使用说明的必需变量表列了 {var}, 但 .env.example 里没有 —— "
                f"要么补进 example, 要么从文档删掉"
            )
