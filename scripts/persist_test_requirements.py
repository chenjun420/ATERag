"""把抽取出的产测条件落库到 ``<schema>.test_requirement``。

用法::

    .venv\\Scripts\\python.exe scripts/persist_test_requirements.py -m PA601-D54A
    .venv\\Scripts\\python.exe scripts/persist_test_requirements.py -m PA601-D54A --dry-run

**默认 dry-run=False 但需显式 ``--apply``**: 落库是写操作, 而抽取管线本身可重复运行。
把「看结果」与「写库」分成两个动作, 是为了让「抽取变了什么」可以先看再决定落不落。

``--dry-run`` 只打印统计与抽样, 不连库。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="产测条件落库")
    ap.add_argument("-m", "--model", required=True, help="型号, 如 PA601-D54A")
    ap.add_argument("-v", "--doc-version", default="", help="文档版本, 默认取注册表")
    ap.add_argument("--apply", action="store_true", help="真正写库(默认只打印)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条(调试用)")
    ap.add_argument("--show", type=int, default=3, help="打印前 N 条样例")
    args = ap.parse_args()

    from aterag.config import get_settings
    from aterag.extract import extract_test_conditions, load_annotations
    from aterag.ingest.persist_requirements import rows_from_result, upsert_sql
    from aterag.registry import Registry
    from aterag.storage.rls import set_current_schema_sql
    from aterag.storage.schema import quote_ident

    reg = Registry.load(get_settings())
    entry = reg.products.get(args.model)
    if entry is None:
        print(f"型号未注册: {args.model}(在册: {sorted(reg.products)})", file=sys.stderr)
        return 2
    version = args.doc_version or entry.doc_version

    result = extract_test_conditions(
        args.model, doc_version=version, annotations=load_annotations(args.model)
    )
    rows = rows_from_result(result)
    if args.limit:
        rows = rows[: args.limit]

    print(f"型号 {args.model}(v{version or '?'})  模板 {result.template}")
    print(f"  条件 {len(result.conditions)} -> 待落库 {len(rows)} 行")
    print(f"  coverage_status: {dict(Counter(r.coverage_status for r in rows))}")
    print(f"  signal_type    : {dict(Counter(r.signal_type for r in rows))}")
    n_draft = sum(1 for r in rows if r.condition_vector["draft"])
    n_inst = sum(1 for r in rows if r.instrument_need["required"])
    print(f"  含 draft 子句 {n_draft} 行; 有仪器需求 {n_inst} 行")

    for r in rows[: max(args.show, 0)]:
        vec = r.condition_vector
        print(f"\n  --- {r.sr_id} {r.measurand} [{r.coverage_status}/{r.signal_type}]")
        print(f"      spec={r.spec}")
        for c in vec["approved"]:
            print(f"      [已批] {c['kind']}: {c['text'][:70]}")
        for c in vec["draft"]:
            print(f"      [待审] {c['kind']}: {str(c.get('proposal_note') or c['text'])[:70]}")
        print(f"      仪器={r.instrument_need['required']}")
        print(f"      工装={r.fixture_need['required']}")

    if not args.apply:
        print("\n(dry-run: 未写库。加 --apply 真正写入)")
        return 0

    dsn = os.environ.get("POSTGRES_DSN") or get_settings().postgres_dsn
    if not dsn:
        print("未配置 POSTGRES_DSN", file=sys.stderr)
        return 2
    import psycopg

    schema = reg.schema_name(args.model)
    sql = upsert_sql().format(schema=quote_ident(schema))
    cols = _cols()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # RLS 上下文必须先设: 表开了 FORCE ROW LEVEL SECURITY, 没设
            # app.current_model 时 INSERT 直接被拒, 症状是「违背行级安全策略」。
            cur.execute(set_current_schema_sql(schema))
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.test_requirement",))
            if not cur.fetchone()[0]:
                print(
                    f"表不存在: {schema}.test_requirement"
                    f"(先跑 schema 部署: scripts/storage_admin.py 或 alembic)",
                    file=sys.stderr,
                )
                return 2
            for r in rows:
                cur.execute(sql, {c: _adapt(getattr(r, c)) for c in cols})
            cur.execute(f"SELECT count(*) FROM {quote_ident(schema)}.test_requirement")
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT coverage_status, count(*) "
                f"FROM {quote_ident(schema)}.test_requirement GROUP BY 1 ORDER BY 1"
            )
            dist = dict(cur.fetchall())
        conn.commit()
    print(f"\n已写入 {schema}.test_requirement, 该表现有 {total} 行")
    print(f"库内分布: {dist}")
    return 0


def _cols() -> tuple[str, ...]:
    from aterag.ingest.persist_requirements import _COLUMNS

    return _COLUMNS


def _adapt(value: object) -> object:
    """dict/list 包成 ``Jsonb``; 其余原样。

    psycopg 不会自动把 dict 适配成 jsonb —— 直接传会抛
    ``cannot adapt type 'dict' using placeholder``。**按值类型判断而不是按列名**:
    列名清单会漂移(加一列 JSONB 忘了加进 :data:`_COLUMNS` 类型判断), 而值类型不会。
    """
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    if isinstance(value, (dict, list)):
        return Jsonb(value)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
