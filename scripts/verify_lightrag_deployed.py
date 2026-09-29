"""板卡 LightRAG 落库完整性验证.

覆盖: 表齐备 / 每文档 status=processed / workspace 已归一化 / 实体与关系统计 /
向量层与图谱层非空 / 无失败残留。

注意: Qdrant 的 lightrag_vectors collection 是历史遗留 —— LightRAG 现用 PGVectorStorage
(lightrag_vdb_* 表), 业务向量用 aterag_chunks collection。故不计入判定。

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\verify_lightrag_deployed.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.pipeline import lrag_workspace
from aterag.registry import Registry

REQUIRED_TABLES = [
    "lightrag_doc_status", "lightrag_doc_chunks", "lightrag_doc_full",
    "lightrag_full_entities", "lightrag_full_relations",
    "lightrag_entity_chunks", "lightrag_relation_chunks",
    "lightrag_vdb_chunks_qwen3_7_text_embedding_1024d",
    "lightrag_vdb_entity_qwen3_7_text_embedding_1024d",
    "lightrag_vdb_relation_qwen3_7_text_embedding_1024d",
]
# 已知 workspace 的 (实体数下限, 关系数下限); 新型号走 DEFAULT_MIN —
# 不能硬编码 workspace 清单, 否则每导入一个新型号这个校验就失效一次。
KNOWN_MIN = {"pa601_d54a": (400, 300), "_domain_power": (100, 50), "pn1000_48a": (3, 3)}
DEFAULT_MIN = (1, 1)


def expected_workspaces(s) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """返回 (型号 workspace 下限, 域 workspace 下限)。

    - 型号 workspace 必须有数据: ``ingest_spec`` 总会走 ainsert_custom_kg + ainsert。
    - 域 workspace 只有在存在叙述层 (.md) 时才会建 LightRAG 图谱; 纯规则 YAML 的域
      (如 common 仅 1 条 K-CMN-001) 只写业务表, 不建图谱, 因此不强制。

    workspace 名必须走 ``reg.domain_workspace(domain)`` 而非直接读 ``entry.workspace``:
    common 域有特判, 数据实际落在 ``_common``, 注册表里的 ``_domain_common`` 字段从不生效。
    """
    reg = Registry.load(s)
    models: dict[str, tuple[int, int]] = {}
    for model_id in reg.products:
        ws = lrag_workspace(model_id)
        models[ws] = KNOWN_MIN.get(ws, DEFAULT_MIN)
    domains: dict[str, tuple[int, int]] = {}
    for domain, entry in reg.domains.items():
        if entry.kb_status == "populated":
            ws = lrag_workspace(reg.domain_workspace(domain))
            domains[ws] = KNOWN_MIN.get(ws, DEFAULT_MIN)
    return models, domains


def main() -> int:
    s = get_settings()
    host = s.postgres_dsn.split("@")[1].split(":")[0]
    print(f"=== LightRAG 落库完整性 @ {host} ===")
    checks: list[tuple[str, bool, str]] = []

    def chk(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:36s} {detail}")

    with urllib.request.urlopen(f"{s.qdrant_url}/collections", timeout=15) as r:
        cols = {c["name"]: c for c in json.loads(r.read())["result"]["collections"]}
    for name in ("aterag_chunks",):
        if name in cols:
            with urllib.request.urlopen(f"{s.qdrant_url}/collections/{name}", timeout=15) as r:
                info = json.loads(r.read())["result"]
            chk(f"Qdrant {name} 业务向量", (info.get("points_count") or 0) > 0,
                f"points={info.get('points_count')} status={info.get('status')}")

    with psycopg.connect(s.postgres_dsn) as conn, conn.cursor() as cur:
        models, domains = expected_workspaces(s)
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename LIKE 'lightrag%'")
        present = {r[0] for r in cur.fetchall()}
        missing = [t for t in REQUIRED_TABLES if t not in present]
        chk("LightRAG 表齐备", not missing, f"{len(present)} 张, 缺 {missing or '无'}")

        cur.execute("SELECT DISTINCT workspace FROM public.lightrag_doc_status")
        wss = sorted(r[0] for r in cur.fetchall())
        chk("workspace 全部已归一化(全小写)",
            all(w == lrag_workspace(w) for w in wss), str(wss))

        # 真实失败判定: 只看非 dup-* 的文档。LightRAG 每次 ainsert 相同内容都会写一条
        # dup-* / status=failed / "File name already exists" 簿记记录, 属正常幂等行为。
        # 真正的失败长这样: 大写 workspace 那次 id=doc-* 且 status=failed (图谱写入报错)。
        cur.execute("SELECT id, workspace, error_msg FROM public.lightrag_doc_status "
                    "WHERE status <> 'processed' AND id NOT LIKE 'dup-%%'")
        bad = cur.fetchall()
        chk("无真实入库失败", not bad,
            "无" if not bad else f"{[(r[0][:12], r[1]) for r in bad]}")
        cur.execute("SELECT count(*) FROM public.lightrag_doc_status WHERE id LIKE 'dup-%%'")
        n_dup = cur.fetchone()[0]
        print(f"    (幂等簿记记录 dup-*: {n_dup} 条, 正常)")

        cur.execute("SELECT count(*) FROM public.lightrag_doc_status WHERE status = 'processed'")
        n_ok = cur.fetchone()[0]
        chk("文档均已处理", n_ok > 0, f"processed {n_ok} 个文档")

        # 型号 workspace 必须有数据; 域 workspace 仅在存在叙述层 (.md) 走 LightRAG 时
        # 才有实体/关系, 纯规则 YAML 的域 (common 仅 1 条 K-CMN-001) 全 0 属预期。
        for ws, (min_e, min_r) in models.items():
            cur.execute("SELECT COALESCE(sum(count), 0) FROM public.lightrag_full_entities "
                        "WHERE workspace = %s", (ws,))
            n_e = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(sum(count), 0) FROM public.lightrag_full_relations "
                        "WHERE workspace = %s", (ws,))
            n_r = cur.fetchone()[0]
            chk(f"{ws} 合并实体数", n_e >= min_e, f"{n_e} (>= {min_e})")
            chk(f"{ws} 合并关系数", n_r >= min_r, f"{n_r} (>= {min_r})")

        # 域: 有数据才校验下限, 无数据则确认其为"纯规则域"并记为信息项
        narrative_domains: set[str] = set()
        for ws, (min_e, min_r) in domains.items():
            cur.execute("SELECT COALESCE(sum(count), 0) FROM public.lightrag_full_entities "
                        "WHERE workspace = %s", (ws,))
            n_e = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(sum(count), 0) FROM public.lightrag_full_relations "
                        "WHERE workspace = %s", (ws,))
            n_r = cur.fetchone()[0]
            if n_e or n_r:
                narrative_domains.add(ws)
                chk(f"{ws} 合并实体数(叙述域)", n_e >= min_e, f"{n_e} (>= {min_e})")
                chk(f"{ws} 合并关系数(叙述域)", n_r >= min_r, f"{n_r} (>= {min_r})")
            else:
                print(f"    (域 {ws} 无叙述层文档, 未建 LightRAG 图谱 — 纯规则域, 预期内)")

        for t in ("lightrag_entity_chunks", "lightrag_relation_chunks",
                  "lightrag_vdb_chunks_qwen3_7_text_embedding_1024d",
                  "lightrag_vdb_entity_qwen3_7_text_embedding_1024d",
                  "lightrag_vdb_relation_qwen3_7_text_embedding_1024d"):
            cur.execute(f"SELECT count(*) FROM public.{t}")
            n = cur.fetchone()[0]
            chk(f"向量/图谱层 {t.replace('lightrag_', '')}", n > 0, f"{n} 行")

        # AGE 图谱: 每个 LightRAG workspace 一个
        cur.execute("SELECT name FROM ag_catalog.ag_graph WHERE name LIKE '%chunk_entity_relation' "
                    "ORDER BY name")
        graphs = [r[0] for r in cur.fetchall()]
        # 图谱判据: 型号 workspace + 有叙述层的域 workspace 必须有图谱;
        # 纯规则域 (无 .md) 不建图谱, 属预期。多余的历史图谱只提示不判失败。
        expect_graphs = {f"{ws}_chunk_entity_relation" for ws in set(models) | narrative_domains}
        chk("应有图谱的 workspace 均有 AGE 图谱", expect_graphs <= set(graphs),
            f"缺 {sorted(expect_graphs - set(graphs)) or '无'}")
        extra = sorted(set(graphs) - expect_graphs)
        if extra:
            print(f"    (注册表外的历史图谱: {extra}, 需人工确认后清理)")

    ok = all(o for _, o, _ in checks)
    npass = sum(1 for _, o, _ in checks if o)
    print(f"LIGHTING_VERIFY {'PASS' if ok else 'FAIL'} {npass}/{len(checks)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
