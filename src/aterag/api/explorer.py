"""ATERag 的 HTTP 门面: 自身 REST + Semantica Explorer UI。

为什么是「一个进程两个 app」
---------------------------
Semantica Explorer 的前端用**绝对路径**加载资源(实测 ``index.html`` 引
``/assets/index-*.css`` 与 ``/favicon.svg``), 且它自己注册了一个 SPA catch-all
``@app.get("/{full_path:path}")``。所以它必须占住根路径 —— 挂在 ``/explorer``
子路径下会让每个资源 URL 都 404, 而 SPA 前端的 router base 也在根上。

于是这里用一个顶层 ASGI app 做分流, **顺序是关键**:

1. 先 ``include_router`` ATERag 自己的 ``/aterag/*``;
2. 再 ``Mount("/", explorer_app)``。

Starlette 按注册顺序匹配, 所以 ``/aterag/*`` 命中第 1 条, 其余全部落到
Explorer。若顺序反过来, ``Mount("/")`` 会吞掉一切。

**本轮不做的全量 REST 化**
-------------------------
仓库已有 17 个 MCP tool。把它们原样再暴露一份 REST 并不增加能力, 反而多出
一个需要独立鉴权面的只读入口 —— 而 ``workbench/api.py:12`` 写明的设计意图是
「MCP 只读且面向产测消费; Workbench 面向评审与签字, 两者权限面完全分离」。
所以这里只提供 Explorer 接入真正需要的端点(健康与图谱概览), 全量 REST 化
等出现第二个非 MCP 消费方时再做。

**图的构建方式**
--------------
:mod:`aterag.kg.graph` 在**启动时**从种子 JSON + PG 现读现建, 不落盘(理由见
该模块 docstring 与 :mod:`aterag.kg.materialize`)。代价是每次重启重建
(种子侧实测 869 条记录 / 594 实体, 秒级), 收益是不存在第二份副本。
(早先写的「1257 节点 / 306 边」含 PG 型号数据且已过期, 别照抄。)

**与 MCP 分析面不是同一张图**
------------------------------
本模块走 ``build_graph(dsn)``, 读**种子 + PG 型号数据**; 而
:func:`aterag.mcp_server.server._kg_graph` 只读**种子**。所以 Explorer 与 MCP
``analyze_graph`` / ``trace_dependency`` 报出的节点/边规模本就不同, 对不上时先
确认查的是哪个面, 别当数据不一致的 bug 去查。
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse

from aterag.config import Settings
from aterag.kg import graph as kg_graph

logger = logging.getLogger(__name__)

#: ATERag 自身 REST 的前缀。刻意不叫 ``/api`` —— 那个前缀归 Explorer
#: (它的 12 个 router 全在 ``/api/`` 下, 且 SPA catch-all 会对 ``api/``
#: 前缀的未匹配路径直接 404)。
API_PREFIX = "/aterag"

#: 请求头名, 与 Explorer 的 ``X-API-Key`` 一致 —— 一个凭据覆盖整个 HTTP 面,
#: 免得出现「ATERag 要一个 token、Explorer 要另一个 key」的双凭据局面。
API_KEY_HEADER = "X-API-Key"


def require_key(
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    """``/aterag/*`` 的鉴权。与 Explorer 的 ``require_auth`` 同一把钥匙、同一种
    比对方式(``hmac.compare_digest``, 常数时间)。

    **未配置 key 时 503 而不是放行** —— 与上游一致。放行等于给只读面开一个匿名
    入口, 而本项目已有的纪律(``workbench/api.py:28``)是不提供未鉴权调试模式。
    """
    expected = Settings().explorer_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "HTTP 门面未配置鉴权。设置 EXPLORER_API_KEY 后用 X-API-Key 头访问; "
                "不设置则保持 fail-closed(与 Semantica Explorer 的行为一致)。"
            ),
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"凭据无效或缺失。请以 {API_KEY_HEADER} 头发送。",
        )


def _router() -> APIRouter:
    r = APIRouter(prefix=API_PREFIX, tags=["aterag"])

    @r.get("/live")
    async def live() -> dict[str, Any]:
        """存活探针。**不鉴权**, 但只回答「进程活着吗」。

        刻意与 ``/health`` 分开: 存活探针要给负载均衡器/容器编排用, 它们不会
        持有 API key; 而健康报告里有路径与行数, 属于信息, 该鉴权。
        """
        return {"status": "ok"}

    @r.get("/health", dependencies=[Depends(require_key)])
    async def health() -> dict[str, Any]:
        """两个数据源各自的状态 + 鉴权是否已配置。

        刻意不合成一个 ok/false: 「种子没找到」与「PG 连不上」是完全不同的
        故障, 合成一个布尔就丢了定位信息 —— 而这正是本项目反复吃过的亏
        (配置与注释不一致、且不报错)。
        """
        s = Settings()
        return {
            "status": "ok",
            "auth_configured": bool(s.explorer_api_key),
            "loaders": kg_graph.loaders_status(s.postgres_dsn),
        }

    @r.get("/graph/summary", dependencies=[Depends(require_key)])
    async def graph_summary() -> dict[str, Any]:
        """图谱规模与类型分布 —— 「知识到底进来多少了」的第一手依据。

        同时给出**原始关系数**与**实际建边数**: 两者不等(外部本体引用不建边、
        自环丢弃), 只报一个数会让人以为图丢了数据。
        """
        s = Settings()
        ents, rels = kg_graph.collect_records(s.postgres_dsn)
        by_type: dict[str, int] = {}
        for e in ents:
            t = str(e.get("entity_type") or "entity")
            by_type[t] = by_type.get(t, 0) + 1
        by_rel: dict[str, int] = {}
        for x in rels:
            t = str(x.get("relationship_type") or "related_to")
            by_rel[t] = by_rel.get(t, 0) + 1
        _graph, kept = kg_graph.build_graph(s.postgres_dsn, model_ids=None)
        return {
            "entities": len(ents),
            "relations_raw": len(rels),
            "relations_built": kept["edges"],
            "entity_types": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "relation_types": dict(sorted(by_rel.items(), key=lambda kv: -kv[1])),
        }

    return r


def _dsn() -> str | None:
    try:
        return Settings().postgres_dsn
    except Exception as exc:  # noqa: BLE001
        # 配置缺失不该让整个门面起不来: /aterag/health 要能报「PG 没配上」,
        # 而不是「服务启动失败」。理由同上 —— 失败信息必须指向真实原因。
        logger.warning("读不到 postgres_dsn: %s", exc)
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    """组装顶层 app。``settings`` 形参留着是为了将来注入非默认配置(测试)。"""
    from semantica.explorer.app import create_app as create_explorer_app

    s = settings or Settings()

    # Explorer 的鉴权读 ``os.environ["SEMANTICA_API_KEY"]``(每次调用现读,
    # 见 explorer/dependencies.py:24), 所以在**建 app 之前**把 ATERag 的配置
    # 写进环境。写进环境而不是改上游代码: 上游的 ``require_auth`` 是我们要
    # 复用的鉴权实现, 自己再实现一份必然漂移。
    #
    # 不设则不写 —— 上游随即对 ``/api/*`` 返回 503 并在日志说明原因, 即
    # fail-closed。我们**不**去设 SEMANTICA_ALLOW_ANONYMOUS。
    if s.explorer_api_key:
        os.environ["SEMANTICA_API_KEY"] = s.explorer_api_key
        logger.info("Explorer API 鉴权已启用 (X-API-Key)")
    else:
        logger.warning(
            "未配置 EXPLORER_API_KEY —— Explorer 的 /api/* 将返回 503 "
            "(fail-closed)。要启用请在 .env 里设置。"
        )

    session, stats = kg_graph.build_session(s.postgres_dsn)
    logger.info("Explorer 图谱就绪: %s", stats)

    explorer = create_explorer_app(session=session)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """把 Explorer 自己的 lifespan 跑起来。

        **这不是可选的润色, 是不做就 503**: Starlette 的 ``Mount`` **不传播
        lifespan 事件** —— 子 app 的 startup 不会被调用, 而 Explorer 的
        ``app.state.session`` / ``ws_manager`` / ``markdown_resources`` 与
        ``install_mutation_bridge`` 全部装在它自己的 lifespan 里(实测:
        缺了它, ``/api/graph/*`` 返回 503 "GraphSession not initialized.",
        而 ``/api/health`` 照样 200 —— 看起来服务是好的, 只是没有数据)。

        所以顶层 app 显式进入子 app 的 lifespan 上下文。顺带得到的好处是
        WebSocket 的 ``event_loop`` 绑定在**真正在跑的那个 loop** 上, 而不是
        一个从没被 enter 过的上下文。
        """
        async with explorer.router.lifespan_context(explorer):
            yield

    app = FastAPI(
        title="ATERag",
        description=(
            "ATERag 知识门面。`/aterag/*` 是 ATERag 自身的健康与图谱概览; "
            "其余路径是 Semantica Explorer UI(第一方前端 + 它的 REST)。"
            "除 `/aterag/live` 外都需要 `X-API-Key` 头。"
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    # 顺序不能反: Mount("/") 会匹配一切。见模块 docstring。
    app.include_router(_router())
    app.mount("/", explorer)

    @app.exception_handler(Exception)
    async def _unhandled(_req, exc: Exception) -> JSONResponse:  # pragma: no cover
        # 顶层 app 挂载了子 app, 子 app 的异常默认会冒泡到这里; 不加处理
        # 的话浏览器收到的是纯文本 traceback, 定位信息全丢。
        logger.exception("未处理异常", exc_info=exc)
        return JSONResponse({"error": f"{type(exc).__name__}"}, status_code=500)

    # 顶层自己也存一份, 供本模块的测试与将来的谱系查询端点用; 权威仍是
    # explorer.state.session(由上面的 lifespan 设置)。
    app.state.explorer = explorer
    app.state.session = session
    app.state.graph_stats = stats
    return app


def main() -> None:  # pragma: no cover - 进程入口
    import uvicorn

    s = Settings()
    uvicorn.run(create_app(s), host=s.explorer_host, port=s.explorer_port)


if __name__ == "__main__":  # pragma: no cover
    main()
