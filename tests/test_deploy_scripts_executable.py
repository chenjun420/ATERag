# -*- coding: utf-8 -*-
"""部署脚本的门禁: shell 续行/heredoc 必须可执行。

为什么值得有
------------
``06-install-aterag.sh`` 的「装 Python 依赖」这一步长期损坏 —— ``\\`` 续行
后面跟着空行, shell 于是把一条命令拆成两条, 第一条缺 <PACKAGE> 参数直接
rc=2。**板卡上服务照常在跑**, 因为 venv 是别的途径装的; 所以「板卡能用」
完全不能证明部署脚本是对的。

也就是说: 这类损坏的唯一症状是「照文档重装一遍会失败」, 而那往往发生在
换机器或重建环境时 —— 那时已经没人记得这段脚本上次跑没跑过。

所以门禁只查两件能机械判定的事:
  1. 续行符后是否接空行/注释
  2. heredoc 里的代码能否编译(Python 段)
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

SHELL_FILES = sorted(p for p in (ROOT / "deploy").rglob("*.sh") if p.is_file()) + [
    ROOT / "deploy" / "docker-compose.yml"
]


def _shell_files() -> list[Path]:
    return [p for p in SHELL_FILES if p.exists() and p.suffix in (".sh", ".yml")]


class TestShellLineContinuations:
    """``\\`` 续行断裂的文本检查。

    **这一条不能删, 因为 ``bash -n`` 抓不到它。** 实测: 在真命令行
    ``uv pip install ... \\`` 与 ``-e APP`` 之间插一个空行, ``bash -n`` 仍然
    rc=0 —— 因为 ``cmd \\`` + 空行 + ``arg`` 在语法上是**两条合法命令**,
    只是语义变成了「先跑一条缺参数的命令」。板卡上它的表现是
    ``rc=2 缺 <PACKAGE> 参数``, 指向一条看起来没问题的部署脚本。

    所以 ``test_install_script_is_syntactically_valid`` 是**补充**而非替代:
    前者查「是不是写坏了」, 那条查「语法能不能过」。
    """

    def test_no_blank_line_after_a_continuation(self) -> None:
        """``\\`` 后面必须是同一命令的下一部分。

        空行会终止命令: ``uv pip install --python X \\`` + 空行 + ``-e APP``
        变成两条命令, 第一条缺参数 rc=2。而这类损坏**不影响已装好的环境**,
        所以本地与板卡都察觉不到, 直到照文档重装。
        """
        offenders: list[str] = []
        for sh in _shell_files():
            lines = io.open(sh, encoding="utf-8").read().split("\n")
            for i, ln in enumerate(lines[:-1]):
                if not ln.rstrip().endswith("\\"):
                    continue
                nxt = lines[i + 1]
                if not nxt.strip() or nxt.strip().startswith("#"):
                    rel = sh.relative_to(ROOT).as_posix()
                    offenders.append(
                        f"{rel}:{i + 1} 续行后是{'空行' if not nxt.strip() else '注释'}"
                    )
        assert not offenders, (
            "续行符后接空行/注释 —— shell 会把一条命令拆成两条:\n  " + "\n  ".join(offenders)
        )

    def test_install_script_is_syntactically_valid(self) -> None:
        """``bash -n`` 过一遍。语法错的上传后会在板卡上才炸。

        两个必须做对的细节, 都是踩出来的:

        * **内容走 stdin 而不是路径**: 本机 ``bash`` 是 WSL2(挂载在 ``/mnt/x``),
          脚本路径是 Windows 形式; 路径换算在 Git bash(/x) 与 WSL(/mnt/x) 之间
          各不相同, 硬编码或猜都会得到 ``No such file or directory`` —— 那个
          报错看着像「脚本不存在」, 实际是判据坏了。
        * **传 bytes 而不是 str**: ``text=True`` 会用 locale 编码把 str 编回去,
          脚本含中文注释, 在非 UTF-8 locale 下被编坏, 于是 bash 报
          ``line N: syntax error: unexpected end of file`` —— 一个**内容完好**
          的脚本被判成语法错。用 bytes 直传就没有这层编解码。

        验不了就 skip 并说清原因 —— 不能让「跑不了」长得像「通过了」。
        """
        sh = ROOT / "deploy" / "native" / "06-install-aterag.sh"
        if not sh.exists():
            pytest.skip("安装脚本不在(尚未进入板卡部署阶段)")
        bash = _find_bash()
        if bash is None:
            pytest.skip("本机无 bash, 跳过语法检查")
        proc = subprocess.run(
            [bash, "-n", "-s"],
            input=sh.read_bytes(),
            capture_output=True,
            timeout=120,
        )
        err = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 0, (
            f"bash -n 失败 (rc={proc.returncode}):\n{err.strip()[:500]}\n"
            f"—— 脚本上传到板卡后才会在安装阶段炸, 而那时板卡已改了一半"
        )


class TestHeredocBlocksCompile:
    """heredoc 里的代码是**真代码**, 只是藏在一个 shell 字符串里 —— 常规 lint
    扫不到它, 坏了也只在板卡上炸。

    **只对 Python 段做编译检查。** 本目录的 heredoc 里混着 SQL
    (``04-init-postgres.sh`` / ``07-probe-caps.sh``), 拿 Python 去编译它们
    必然报错 —— 那不是缺陷, 是判据用错了语言。语言按**内容**判: 含
    ``import`` / ``def`` / ``print(`` 等 Python 特征才当 Python。
    """

    def test_python_heredocs_compile(self) -> None:
        offenders: list[str] = []
        checked = 0
        for sh in _shell_files():
            text = io.open(sh, encoding="utf-8").read()
            for tag in set(re.findall(r"<<'(\w+)'", text)):
                m = re.search(r"<<'" + tag + r"'([^\n]*)\n(.*?)\n" + tag + r"\b", text, re.S)
                if not m:
                    continue
                body = m.group(2)
                if not _looks_like_python(body):
                    continue
                checked += 1
                try:
                    compile(body, f"{sh.name}:{tag}", "exec")
                except SyntaxError as e:
                    rel = sh.relative_to(ROOT).as_posix()
                    offenders.append(f"{rel} {tag}: {e.msg} @ line {e.lineno}")
        assert not offenders, "heredoc 里的 Python 编译失败:\n  " + "\n  ".join(offenders)
        assert checked, "一个 Python heredoc 都没识别到 —— 判据可能过严, 成了空转"


def _looks_like_python(body: str) -> bool:
    """按内容判断 heredoc 段是不是 Python(而不是只认 tag 名)。"""
    return bool(re.search(r"^\s*(import |from \w+ import |def |print\()", body, re.M))


def _find_bash() -> str | None:
    for cand in ("bash", "/bin/bash", r"C:\Program Files\Git\bin\bash.exe"):
        try:
            proc = subprocess.run([cand, "--version"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return cand
    return None


class TestDeployManifestPathsResolve:
    """``FILES`` 里的本地路径必须相对**仓库根**存在, 而不是相对 CWD。

    原来写的是 ``("registry.yaml", "registry.yaml")``, 而文件在 ``data/`` 下 ——
    只有从仓库根跑才对。换个 CWD(IDE、CI 步骤、定时任务)就在**上传到一半**时
    才抛 FileNotFoundError, 板卡停在「一半新一半旧」, 而报错指向一个本地
    路径, 与板卡毫无关系。
    """

    def test_every_entry_exists_relative_to_repo_root(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import deploy_mcp_board as dmb  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法导入 deploy_mcp_board: {type(e).__name__}: {e}")
        try:
            dmb.assert_local_files_exist()
        except FileNotFoundError as e:
            pytest.fail(str(e))

    def test_self_check_actually_raises(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import deploy_mcp_board as dmb  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法导入 deploy_mcp_board: {type(e).__name__}: {e}")
        with pytest.raises(FileNotFoundError):
            dmb.assert_local_files_exist([("data/definitely-absent.yaml", "x")])

    def test_shipped_scripts_exist(self) -> None:
        """板卡侧被远程调用的脚本必须真的存在。

        ``verify_all.py`` 曾引用一个已删除的脚本, 而 ``subprocess`` 的
        FileNotFoundError 被宽泛捕获后报成「该套件失败」—— 看不出真相是
        编排器指向一个不存在的文件。
        """
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import verify_all as va  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法导入 verify_all: {type(e).__name__}: {e}")
        try:
            va.assert_suite_scripts_exist()
        except FileNotFoundError as e:
            pytest.fail(str(e))
