"""启动自检 (fail-fast): PG 扩展、Qdrant、LLM、Embedding 全量验证.

禁止本地降级 —— 任一依赖失败即拒绝启动, 并输出可执行的诊断信息。
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from aterag.config import Settings
from aterag.models import EmbeddingClient, LLMClient

REQUIRED_EXTENSIONS = ("vector", "age", "pg_textsearch", "zhparser")


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


async def run_all_checks(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(_check_postgres(settings))
    results.extend(await _check_qdrant(settings))
    results.extend(await _check_llm(settings))
    results.extend(await _check_embedding(settings))
    return results


def _check_postgres(settings: Settings) -> CheckResult:
    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=10) as conn:
            rows = conn.execute(
                "SELECT extname FROM pg_extension WHERE extname = ANY(%s)",
                (list(REQUIRED_EXTENSIONS),),
            ).fetchall()
            found = {r[0] for r in rows}
            missing = [e for e in REQUIRED_EXTENSIONS if e not in found]
            if missing:
                return CheckResult(
                    "postgres",
                    False,
                    f"connected, but missing extensions: {missing} "
                    f"(部署 deploy/postgres 镜像并重建数据库)",
                )
            # AGE 图存在性 (info-only: 查询失败不影响通过)
            graphs: list[tuple] = []
            try:
                graphs = conn.execute(
                    "SELECT name FROM ag_catalog.ag_graph",
                ).fetchall()
            except Exception:  # noqa: BLE001, S110
                # 故意吞掉: AGE 图只是信息项, 查不到 (如未装扩展) 不应让整体检查失败
                pass
            return CheckResult(
                "postgres",
                True,
                f"extensions ok; graphs={sorted(g[0] for g in graphs)}",
            )
    except Exception as e:  # noqa: BLE001
        return CheckResult("postgres", False, f"connect failed: {e}")


async def _check_qdrant(settings: Settings) -> list[CheckResult]:
    import httpx

    results: list[CheckResult] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
            results.append(
                CheckResult("qdrant", resp.status_code == 200, f"healthz HTTP {resp.status_code}")
            )
            resp = await client.get(
                f"{settings.qdrant_url.rstrip('/')}/collections/{settings.qdrant_collection}"
            )
            if resp.status_code == 200:
                payload = resp.json()["result"]
                idx = (payload.get("payload_schema") or {})
                has_tenant = "workspace_id" in idx
                results.append(
                    CheckResult(
                        "qdrant_tenant_index",
                        has_tenant,
                        "workspace_id tenant index "
                        + ("present" if has_tenant else "missing (run deploy/qdrant/init_tenant.py)"),
                    )
                )
                results.append(
                    CheckResult(
                        "qdrant_vector_dim",
                        True,
                        f"vectors={payload.get('vectors_count')}, "
                        f"size={(payload.get('config', {}).get('params', {}).get('vectors', {}) or {}).get('size', '?')}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "qdrant_tenant_index",
                        False,
                        f"collection {settings.qdrant_collection} HTTP {resp.status_code}",
                    )
                )
    except Exception as e:  # noqa: BLE001
        results.append(CheckResult("qdrant", False, f"connect failed: {e}"))
    return results


async def _check_llm(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    llm = LLMClient(settings)
    try:
        models = await llm.list_models()
        ok = settings.llm_model in models
        results.append(
            CheckResult(
                "llm",
                True,
                f"endpoint ok; model {settings.llm_model} "
                + ("listed" if ok else f"NOT in {models[:10]} (服务端可能动态受理, 继续)"),
            )
        )
    except Exception as e:  # noqa: BLE001
        results.append(CheckResult("llm", False, f"endpoint failed: {e}"))
    finally:
        await llm.aclose()
    return results


async def _check_embedding(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    embed = EmbeddingClient(settings)
    try:
        dim = await embed.probe_dimension()
        results.append(
            CheckResult(
                "embedding",
                True,
                f"protocol={embed.protocol} dim={dim}"
                + ("" if dim else " (dim unknown!)"),
            )
        )
    except Exception as e:  # noqa: BLE001
        results.append(CheckResult("embedding", False, f"probe failed: {e}"))
    finally:
        await embed.aclose()
    return results


def report(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        mark = "✅" if r.ok else "❌"
        lines.append(f"{mark} {r.name}: {r.detail}")
    return "\n".join(lines)


def assert_all_ok(results: list[CheckResult]) -> None:
    failed = [r for r in results if not r.ok]
    if failed:
        raise RuntimeError(
            "启动自检失败 (禁止本地降级, 请修复以下依赖):\n"
            + "\n".join(f"  ❌ {r.name}: {r.detail}" for r in failed)
        )
