"""工作台 HTTP 接口 (P1b) —— 前端向导调用的管理面。

安全定位
--------
这是**唯一**对 ATEStudio 开放写能力的通道, 因此:

* 鉴权用共享密钥 (环境变量), 缺失即启动失败 —— 不提供"未鉴权调试模式",
  那类后门在生产环境活下来的概率远高于被关掉的概率。
* 写操作全部经由 workbench.gate 的白名单闸, 端点自身不碰文件。
* 读接口无副作用且是纯函数式的(每次重跑抽取), 被重放也不会产生状态漂移。

与板卡 MCP 的关系: MCP 只读且面向产测消费; 本接口面向评审与签字, 两者
权限面完全分离 —— 产线侧不持有任何能改配置的凭据。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from aterag.workbench.gate import WriteNotAllowed, WriteRejected
from aterag.workbench.service import Workbench

#: 共享密钥的环境变量名。刻意不设默认值 —— 缺失就起不来。
TOKEN_ENV = "ATERAG_WORKBENCH_TOKEN"
REPO_ENV = "ATERAG_REPO_ROOT"


class ApproveRequest(BaseModel):
    """批准请求。req_id 必须显式给出, 不从请求体之外推断。"""

    model_id: str = Field(..., min_length=1, max_length=100)
    req_id: str = Field(..., min_length=1, max_length=200)
    actor: str = Field(..., min_length=1, max_length=100)
    reason: str = Field(default="", max_length=2000)


class SnapshotQuery(BaseModel):
    model_id: str
    doc_version: str = "B"
    detail: bool = True


def _require_token(expected: str) -> None:
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"服务端未配置 {TOKEN_ENV}, 拒绝提供写能力",
        )


def auth(x_workbench_token: Annotated[str | None, Header()] = None) -> None:
    """鉴权依赖 (模块级, 不用闭包)。

    刻意不写成 create_app 内部的闭包: 闭包依赖配合 Annotated 默认值时,
    FastAPI 可能静默不注册该依赖 —— 实测 dependant.dependencies 为空,
    端点变成无鉴权开放, 而带不带令牌都返回 200。安全边界不能建立在
    "应该会解析成功"上, 所以依赖放在模块级, 引用点用显式 AuthDep。
    """
    expected = os.environ.get(TOKEN_ENV, "")
    _require_token(expected)
    if not x_workbench_token or x_workbench_token != expected:
        # 统一 401, 不区分"没传"与"传错", 避免泄漏密钥长度/格式信息。
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="工作台令牌无效")


#: 显式依赖对象, 在各端点以默认值引用。
AuthDep = Depends(auth)


def create_app(repo_root: Path | None = None) -> FastAPI:
    """构造 app。repo_root 为空时取环境变量, 再空则用当前工作目录。"""
    root = Path(repo_root or os.environ.get(REPO_ENV, "") or Path(__file__).resolve().parents[3])
    app = FastAPI(title="ATERag Workbench", version="1.0")
    bench = Workbench(root)
    app.state.workbench = bench

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "repo_root": str(root),
            "token_configured": bool(os.environ.get(TOKEN_ENV, "")),
            "writable": sorted(bench.policy().allowed_rel),
        }

    @app.get("/workbench/snapshot")
    def snapshot(
        model_id: str,
        doc_version: str = "B",
        detail: bool = True,
        _: None = AuthDep,
    ) -> dict[str, Any]:
        """抽取快照。detail=false 只回统计与任务, 供首屏渲染。"""
        try:
            snap = bench.snapshot(model_id, doc_version, detail=detail)
        except (FileNotFoundError, ValueError) as e:
            # 配置缺失/不自洽 -> 400 并把原因带回, 不吞成 500。
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
        return snap.to_dict(detail=detail)

    @app.get("/workbench/tasks")
    def tasks(
        model_id: str,
        doc_version: str = "B",
        _: None = AuthDep,
    ) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "tasks": [t.to_dict() for t in bench.tasks(model_id, doc_version)],
        }

    @app.post("/workbench/approve")
    def approve(
        body: ApproveRequest,
        _: None = AuthDep,
    ) -> dict[str, Any]:
        """批准一条注记(签字)。写盘 + 单文件 git commit。"""
        try:
            receipt = bench.approve(body.model_id, body.req_id, body.actor, reason=body.reason)
        except WriteNotAllowed as e:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
        except WriteRejected as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
        except RuntimeError as e:
            # git 失败: 500 且带原因 —— 审计不能假装成功。
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
            ) from e
        return receipt.to_dict()

    return app


app = None  # 由部署入口显式构造, 避免 import 即读环境变量

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8099)
