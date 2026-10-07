"""从已落库的产测需求推导**工装与仪器能力需求**, 并列出缺口清单。

用法::

    .venv\\Scripts\\python.exe scripts\\derive_fixture_demand.py -m PA601-D54A
    .venv\\Scripts\\python.exe scripts\\derive_fixture_demand.py -m PA601-D54A --json

## 为什么不直接往 ``fixture`` / ``fixture_channel_map`` 写行

那两张表要的是**接线决策**: 哪个物理通道接哪个点位、工装形态选哪个、通道数多少。
``fixture_channel_map`` 的 ``ck_exactly_one_target`` 要求每通道恰好映射一个点位,
点位来自 ``yx_point`` / ``yc_point`` / ``yk_command`` / ``yt_parameter`` /
``protection_setting``。本脚本会实测这些表并把缺口列出来 —— **有缺口时写工装行等于
伪造产品数据**, 所以本脚本只读不写。

需求 ≠ 设计: 推导结果回答「必须能做什么」, 不回答「买什么 / 怎么接线」。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

#: 通道绑定依赖的点位表。**这五张表任一为空, 通道绑定就无法进行**。
POINT_TABLES = ("yx_point", "yc_point", "yk_command", "yt_parameter",
                "protection_setting")

#: 工装侧要落行需要的表, 以及它们缺数据时的后果。
FIXTURE_TABLES = {
    "fixture": "工装本体(形态/通道数/接触电阻/联锁)",
    "fixture_channel_map": "通道到点位的绑定",
    "fixture_checkpoint": "点检与防错动作",
    "test_station": "工位(工装+仪器绑定、节拍、MES)",
    "instrument_ledger": "在册仪器台账 —— 选型的唯一依据",
}

ROWS_SQL = (
    "SELECT sr_id, variant_key, measurand, signal_type, coverage_status, "
    "spec, condition_vector, instrument_need, source_ref "
    "FROM {schema}.test_requirement ORDER BY sr_id, variant_key"
)


def _load(dsn: str, schema: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    import psycopg
    from psycopg.rows import dict_row

    from aterag.storage.rls import set_current_schema_sql

    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
        # FORCE RLS: 不设上下文就 SELECT 不到行(返 0 行, 不报错)。
        cur.execute(set_current_schema_sql(schema))
        cur.execute(ROWS_SQL.format(schema=schema))
        rows = [dict(r) for r in cur.fetchall()]
        counts: dict[str, int] = {}
        for t in (*POINT_TABLES, *FIXTURE_TABLES):
            # dict_row 下 fetchone() 返回 dict, 必须按列名取 —— 按序号会 KeyError。
            cur.execute(
                "SELECT to_regclass(%s) AS reg", (f"{schema}.{t}",)
            )
            row = cur.fetchone()
            if not row or row["reg"] is None:
                counts[t] = -1  # 表不存在
                continue
            cur.execute(f"SELECT count(*) AS n FROM {schema}.{t}")  # noqa: S608
            got = cur.fetchone()
            counts[t] = int(got["n"]) if got else 0
    return rows, counts


def _blockers(counts: dict[str, int]) -> list[str]:
    """把「推得出来但落不下去」的地方逐条说清 —— 不给一句「数据不足」。"""
    out: list[str] = []
    empty_points = [t for t in POINT_TABLES if counts.get(t, 0) <= 0]
    if empty_points:
        out.append(
            f"通道绑定无法进行: 点位表 {empty_points} 全为空。"
            "fixture_channel_map 的 ck_exactly_one_target 要求每通道恰好映射一个点位, "
            "没有点位就绑不了 —— 需先从规格书信号表结构化出点位。"
        )
    if counts.get("fixture", 0) <= 0:
        out.append(
            "工装本体未登记: fixture 0 行。形态(load_board/relay_matrix/…), "
            "通道数、接触电阻、联锁类型均为**产品侧决策**, 不能从判据推出。"
        )
    if counts.get("instrument_ledger", 0) <= 0:
        out.append(
            "在册仪器台账为空: 推导只给「需要能做什么」的能力类别, "
            "无法做选型(CSP)。台账补齐前不要报具体型号。"
        )
    if counts.get("test_station", 0) <= 0:
        out.append(
            "测试工位未登记: 工艺步骤、并行批数、节拍(oee_target)、MES 接口均待定。"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="工装/仪器能力需求推导")
    ap.add_argument("-m", "--model", required=True, help="型号, 如 PA601-D54A")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    from aterag.config import get_settings
    from aterag.fixture import (
        derive_capability_demand,
        derive_fixture_type_demand,
        derive_instrument_ranges,
        derive_rail_channel_demand,
    )
    from aterag.registry import Registry

    reg = Registry.load(get_settings())
    if args.model not in reg.products:
        print(f"型号未注册: {args.model}(在册: {sorted(reg.products)})",
              file=sys.stderr)
        return 2
    dsn = os.environ.get("POSTGRES_DSN") or get_settings().postgres_dsn
    if not dsn:
        print("未配置 POSTGRES_DSN", file=sys.stderr)
        return 2
    schema = reg.schema_name(args.model)

    rows, counts = _load(dsn, schema)
    if not rows:
        print(f"{schema}.test_requirement 为空 —— 先跑 "
              "scripts/persist_test_requirements.py", file=sys.stderr)
        return 2

    demand = derive_capability_demand(rows)
    rails = derive_rail_channel_demand(rows)
    ranges = derive_instrument_ranges(rows)
    ftypes = derive_fixture_type_demand(rows)
    blockers = _blockers(counts)

    payload = {
        "model": args.model,
        "schema": schema,
        "requirements": len(rows),
        "capability_demand": [d.to_dict() for d in demand],
        "rail_channel_demand": [r.to_dict() for r in rails],
        "instrument_ranges": [r.to_dict() for r in ranges],
        "fixture_types": list(ftypes),
        "table_counts": counts,
        "blockers": blockers,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    L: list[str] = []
    L.append("=" * 88)
    L.append(f"工装/仪器能力需求 —— {args.model} ({schema})")
    L.append("=" * 88)
    L.append(f"依据: 已落库产测需求 {len(rows)} 行")
    L.append("")
    L.append("--- 仪器/工装能力需求 (kind + spec 单位两路证据并集) ---")
    L.append(f"{'能力':26s} {'硬':>4s} {'提示':>4s}  依据 sr_id(前 4)")
    for d in demand:
        src = sorted(d.demanded_by)[:4]
        more = f" +{len(d.demanded_by) - len(src)}" if len(d.demanded_by) > 4 else ""
        mark = "  (仅提案)" if d.provisional_only else ""
        L.append(f"{d.capability:26s} {len(d.approved_by):4d} {len(d.provisional_by):4d}"
                 f"  {', '.join(x[-4:] for x in src)}{more}{mark}")
    L.append("")
    L.append("  硬 = 依据已批准/规则来源; 提示 = 依据含未人审业界提案。")
    L.append("  「仅提案」项在提案签字前不该进采购清单。")
    L.append("")
    L.append("--- 每轨测量通道需求 (只数电压/电流采样, 不含负载与保护通路) ---")
    for r in rails:
        L.append(f"  轨 {r.rail:12s} 采样通道 {r.sense_channels}  "
                 f"需负载切换={r.needs_switching} 需故障注入={r.needs_fault_injection}"
                 f"  依据 {len(r.demanded_by)} 条判据")
    L.append("")
    L.append("--- 仪器量程边界 (选型依据; 只有能力类别, 无型号) ---")
    for r in ranges:
        L.append(f"  {r.capability:22s} {r.unit:8s} "
                 f"{r.low if r.low is not None else '-'} ~ {r.high if r.high is not None else '-'}"
                 f"   依据 {len(r.demanded_by)} 条")
    L.append("")
    L.append("--- 需要的工装形态 ---")
    L.append(f"  {', '.join(ftypes) if ftypes else '(无)'}")
    L.append("  形态是产品侧决策, 这里只指出「必须有对应形态」, 不指定型号/通道数。")
    L.append("")
    L.append("--- 表现状 ---")
    for t, n in counts.items():
        mark = "0 行" if n == 0 else ("表不存在" if n < 0 else f"{n} 行")
        L.append(f"  {t:22s} {mark}")
    L.append("")
    L.append("=" * 88)
    L.append("缺口: 推得出来但还落不下去的地方")
    L.append("=" * 88)
    if blockers:
        for b in blockers:
            L.append(f"  - {b}")
    else:
        L.append("  (无)")
    L.append("")
    L.append("本脚本只读不写: 上述缺口需产品侧决策后, 才谈得上建工装行。")

    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
