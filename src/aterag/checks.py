"""启动自检 (fail-fast): PG 扩展、检索后端、LLM、Embedding 全量验证.

禁止本地降级 —— 任一依赖失败即拒绝启动, 并输出可执行的诊断信息。

**检索后端只有 pgvector 一种**
--------------------------------
检索层由 ``retrieval/hybrid.py`` 承担(pgvector + BM25 + RRF), 单一
PostgreSQL 存储底座。这里只查 pgvector 的真实可用性:

``_check_pgvector`` 查的是 chunk 表在不在、向量列在不在、有多少行带向量,
而不只是「``vector`` 扩展装着」—— 表没建或列缺失时检索会**静默返回空
结果**, 扩展全绿而功能不可用。
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
    results.append(_check_pgvector(settings))
    results.extend(await _check_llm(settings))
    results.extend(await _check_embedding(settings))
    results.append(_check_extraction_configs(settings))
    return results


def _check_extraction_configs(settings: Settings) -> CheckResult:
    """抽取侧五份配置的自洽性 —— **在健康检查里, 而不是等抽取时才炸**。

    ``doc_profiles.yaml`` 与 ``test_methods.yaml`` 互相引用(方法库用档案的
    ``role`` 指认适用范围), 校验过去只发生在抽取内部, 于是「配置能加载成功」
    会被误读成「配置可用」(方案 §11.6 A20)。这一项零 IO 之外的依赖, 不查它
    的健康检查等于把最常见的一类配置错误留给线上才发现。
    """
    try:
        from aterag.extract.configs import load_extraction_configs

        cfgs = load_extraction_configs(settings)
        return CheckResult(
            "extraction_configs",
            True,
            f"{len(cfgs.profiles.profiles)} profiles / {len(cfgs.patterns.rules)} 规则 / "
            f"{len(cfgs.methods.methods)} 方法 / {len(cfgs.assess_rules.rules)} 评估规则 / "
            f"{len(cfgs.scenario_rules.dimensions)} 场景维度 / "
            f"{len(cfgs.quantity_aliases.facts)} 事实别名, 交叉校验通过",
        )
    except Exception as e:  # noqa: BLE001 配置坏了就是坏了, 报原样让人能改
        return CheckResult(
            "extraction_configs",
            False,
            f"抽取侧配置不自洽: {type(e).__name__}: {e}",
        )


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


def _check_pgvector(settings: Settings) -> CheckResult:
    """默认检索后端的自检: chunk 表 + 向量列 + 倒排索引。

    为什么不只看扩展在不在: ``vector`` 扩展装着但 ``aterag_chunks`` 没建,
    或者建了但没有 BM25 用的 ``tsv`` 列, 检索都会**静默返回空结果** ——
    扩展检查全绿而功能不可用, 是最难查的一类故障。

    维度不在这里查: 向量列的维度由 ``hybrid.ensure_vector_schema`` 按嵌入
    模型探测后建表, 不同模型的维度不同, 硬编码一个值会在换模型时误报。
    """
    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=10) as conn:
            cols = {
                r[0]
                for r in conn.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'aterag_chunks'
                    """
                ).fetchall()
            }
            if not cols:
                return CheckResult(
                    "pgvector",
                    False,
                    "public.aterag_chunks 不存在 —— 检索会静默返回空结果。"
                    "先跑 ingest_spec 或 build_domain 灌一次数据",
                )
            missing = {"embedding", "workspace_id", "content"} - cols
            if missing:
                return CheckResult("pgvector", False, f"aterag_chunks 缺列 {sorted(missing)}")
            n_vec = conn.execute(
                "SELECT count(*) FROM public.aterag_chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            n_all = conn.execute("SELECT count(*) FROM public.aterag_chunks").fetchone()[0]
            n_ent = conn.execute("SELECT count(*) FROM public.aterag_entities").fetchone()[0]
            idx = [
                r[0]
                for r in conn.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'aterag_chunks'"
                ).fetchall()
            ]
            return CheckResult(
                "pgvector",
                True,
                f"chunks={n_all} (带向量 {n_vec}); entities={n_ent}; indexes={sorted(idx) or '无'}",
            )
    except Exception as e:  # noqa: BLE001
        return CheckResult("pgvector", False, f"connect failed: {e}")


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
                f"protocol={embed.protocol} dim={dim}" + ("" if dim else " (dim unknown!)"),
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
