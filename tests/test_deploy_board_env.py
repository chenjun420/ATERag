"""board_env 的 fail-fast 断言 —— 缺 EMBED_DIM 必须在**写板卡 .env 之前**就失败。

为什么要有这层断言: 缺 ``EMBED_DIM`` 不报错, 而是回落到**服务端原生维度**
(豆包 2048)。库里的列是 ``vector(1024)``(ADR-013 定死全系统 1024 维 halfvec),
而 pgvector 的 HNSW 索引上限 2000 维 —— 于是失败点被推迟到灌库那一刻:

    ProgramLimitExceeded: column cannot have more than 2000 dimensions

那时板卡 ``.env`` 已经被写坏、代码已经部署完, 回滚成本高得多。放到 ``board_env``
里, 失败发生在上传之前, 板卡还是原样。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_deploy_mcp_board", ROOT / "scripts" / "deploy_mcp_board.py"
)
assert _spec and _spec.loader
deploy = importlib.util.module_from_spec(_spec)
sys.modules["_deploy_mcp_board"] = deploy
_spec.loader.exec_module(deploy)

BASE_ENV = """\
POSTGRES_DSN=postgresql://u:p@192.168.5.25:5432/power_specs
EMBED_BASE=https://ark.cn-beijing.volces.com/api/plan/v3
EMBED_MODEL=doubao-embedding-vision
EMBED_API_KEY=ark-test
MCP_HOST=127.0.0.1
MCP_PORT=8080
"""


def _write_env(tmp: Path, body: str) -> str:
    p = tmp / ".env"
    p.write_text(body, encoding="utf-8")
    return str(p)


class TestDuplicateKeysUseLastNonEmpty:
    """同键多行时取最后一个非空值 —— ``.env`` 里追加而非替换是常事。"""

    def test_trailing_empty_value_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        body = BASE_ENV + "EMBED_API_KEY=\nEMBED_DIM=1024\n"
        out = deploy.board_env(_write_env(tmp_path, body))
        assert "ark-test" in out

    def test_last_non_empty_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        body = BASE_ENV + "EMBED_MODEL=second-model\nEMBED_DIM=1024\n"
        out = deploy.board_env(_write_env(tmp_path, body))
        assert "second-model" in out


class TestEmbedKeysRequired:
    def test_minimal_valid_env_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = BASE_ENV + "EMBED_DIM=1024\n"
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        out = deploy.board_env(_write_env(tmp_path, body))
        assert "EMBED_DIM=1024" in out

    @pytest.mark.parametrize("missing", ["EMBED_BASE", "EMBED_MODEL", "EMBED_API_KEY"])
    def test_missing_embed_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        body = "".join(
            ln
            for ln in (BASE_ENV + "EMBED_DIM=1024\n").splitlines(keepends=True)
            if not ln.startswith(missing + "=")
        )
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        with pytest.raises(RuntimeError, match=missing):
            deploy.board_env(_write_env(tmp_path, body))

    def test_empty_embed_key_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """整行只有空值时必须拒 —— 嵌入服务会在第一次调用时才报 401/403,
        而不是启动时。"""
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        body = "".join(
            ln
            for ln in (BASE_ENV + "EMBED_DIM=1024\n").splitlines(keepends=True)
            if not ln.startswith("EMBED_API_KEY=")
        )
        with pytest.raises(RuntimeError, match="EMBED_API_KEY"):
            deploy.board_env(_write_env(tmp_path, body))


class TestEmbedDimIsEnforced:
    def test_missing_dim_raises_with_pgvector_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 EMBED_DIM 是本轮实测踩到的那条路径, 错误信息必须点出真实后果。"""
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        with pytest.raises(RuntimeError) as exc:
            deploy.board_env(_write_env(tmp_path, BASE_ENV))
        msg = str(exc.value)
        assert "EMBED_DIM" in msg
        assert "2000" in msg, f"错误信息没提 pgvector 上限: {msg}"
        assert "2048" in msg, f"错误信息没提豆包原生维度: {msg}"

    def test_wrong_dim_raises_and_points_to_adr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        with pytest.raises(RuntimeError) as exc:
            deploy.board_env(_write_env(tmp_path, BASE_ENV + "EMBED_DIM=2560\n"))
        msg = str(exc.value)
        assert "2560" in msg and "1024" in msg
        assert "ADR-013" in msg

    def test_commented_dim_is_uncommented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``.env.example`` 里曾是 ``#EMBED_DIM=2560`` —— 注释形态照样能被读成值,
        于是「这个键存在」在文件里看不见。本函数负责去注释, 让它显式。"""
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        out = deploy.board_env(_write_env(tmp_path, BASE_ENV + "#EMBED_DIM=1024\n"))
        assert "\nEMBED_DIM=1024" in out
        assert "#EMBED_DIM" not in out


class TestEndpointRewritesStillWork:
    def test_endpoints_are_rewritten_to_loopback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        out = deploy.board_env(_write_env(tmp_path, BASE_ENV + "EMBED_DIM=1024\n"))
        assert "@127.0.0.1:5432" in out
        assert "192.168.5.25" not in out, "存储端点必须改回环, 否则依赖网卡地址"
        # 嵌入端点是公网服务, 不能一起改回环
        assert "ark.cn-beijing.volces.com" in out

    def test_mcp_port_is_forced(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deploy, "EMBED_DIM", 1024)
        out = deploy.board_env(_write_env(tmp_path, BASE_ENV + "EMBED_DIM=1024\n"))
        assert f"MCP_PORT={deploy.PORT}" in out
        assert "MCP_HOST=0.0.0.0" in out
