"""条件场景拆分验证 (A7 / 缝⑥)。

固化四类契约 —— 其中两类是会导致产线静默丢数据的, 必须在 CI 里挡住:
  1. 档位与推导规则自洽, 且公式用受限求值(不放开 eval)
  2. 黄金回归: 110Vac 满载 -54V 轨 = 7.401A (需求方已确认口径)
  3. 标识与序号唯一: (req_id, seq) 必须能唯一派生 case_code, 否则幂等导入
     会把多行并成一条用例, 条件集悄悄少一半
  4. 顺序稳定: 同一输入两次展开结果完全一致(case_code 不漂移)

用法: .venv\\Scripts\\python.exe scripts/verify_scenarios.py
"""

from __future__ import annotations

import sys
from collections import Counter

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.extract import extract_test_conditions, load_annotations  # noqa: E402
from aterag.extract.scenarios import (  # noqa: E402
    ScenarioRules,
    _safe_eval,
    expand_scenarios,
)

MODEL = "PA601-D54A"
PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"✅ {name}" + (f" | {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"❌ {name} | {detail}")


def main() -> int:
    rules = ScenarioRules.load()
    try:
        rules.validate()
        check(
            "规则-自洽",
            True,
            f"{len(rules.dimensions)} 维度 / {len(rules.derivations)} 推导 / tier 解析式已配置",
        )
    except ValueError as e:
        check("规则-自洽", False, str(e))
        print(f"\n===== {PASSED}/{PASSED + FAILED} passed =====")
        return 1

    # ---------- 2. 受限求值 ----------
    try:
        _safe_eval("(P - P_aux) / U", {"P": 400.0, "P_aux": 0.345, "U": 54.0})
        check("受限求值-正常公式可算", True, "(P-P_aux)/U = 7.401")
    except ValueError as e:
        check("受限求值-正常公式可算", False, str(e))
    for bad, why in (
        ("__import__('os').system('ls')", "函数调用"),
        ("open('x')", "函数调用"),
        ("P ** 2", "幂运算不在白名单"),
        ("1 if P else 2", "条件表达式"),
        ("P; import os", "多语句"),
    ):
        try:
            _safe_eval(bad, {"P": 1.0})
            check(f"受限求值-拒绝{why}", False, f"竟然通过了: {bad}")
        except (ValueError, SyntaxError):
            check(f"受限求值-拒绝{why}", True, bad[:28])

    # ---------- 3. 档位来自规格书原文 ----------
    r = extract_test_conditions(MODEL, doc_version="B", annotations=load_annotations(MODEL))
    res = expand_scenarios(r.conditions, rules)
    check(
        "档位-从规格书原文解析",
        len(res.tiers) >= 2 and all(t.source_text for t in res.tiers),
        "; ".join(f"{t.min_vac:g}~{t.max_vac:g}Vac->{t.power_w:g}W" for t in res.tiers),
    )
    check(
        "档位-按电压升序",
        all(a.min_vac <= b.min_vac for a, b in zip(res.tiers, res.tiers[1:], strict=False)),
        "顺序稳定则 case_code 不漂移",
    )

    # ---------- 4. 黄金回归 ----------
    der = next((d for d in rules.derivations if d.regression_golden), None)
    check(
        "黄金回归-已在配置中声明", der is not None, f"{len(der.regression_golden) if der else 0} 条"
    )
    if der:
        # 按 key 取维度取值, 不能用 list(bindings.values())[0] —— 展开顺序变了
        # (现在还有温度/负载/掉电等维度), "第一个绑定值"未必是输入电压档, 那样
        # 会拿别的维度的取值去比档位, 黄金回归就失去防漂移的作用。
        for g in der.regression_golden:
            got = None
            for s in res.scenarios:
                if (
                    s.rail == g["expect_rail"]
                    and s.derived
                    and s.bindings.get("ac_input_tier") == g["tier"]
                ):
                    got = list(s.derived.values())[0]
                    break
            check(
                f"黄金回归-{g['tier']} {g['expect_rail']}",
                got is not None and abs(got - g["expect_current"]) < 0.001,
                f"期望 {g['expect_current']}A, 实得 {got}A",
            )

    # ---------- 5. 唯一性 (防静默丢数据) ----------
    ids = [s.scenario_id for s in res.scenarios]
    check(
        "唯一性-场景标识不重复",
        len(ids) == len(set(ids)),
        f"{len(ids)} 个场景 / {len(set(ids))} 个唯一",
    )
    per_req: dict[str, list[int]] = {}
    for s in res.scenarios:
        per_req.setdefault(s.req_id, []).append(s.seq)
    dup_seq = {k: v for k, v in per_req.items() if len(v) != len(set(v))}
    check(
        "唯一性-同需求内序号不重复",
        not dup_seq,
        f"{len(per_req)} 个需求, 序号冲突 {len(dup_seq)} 个",
    )
    codes = [f"{s.req_id}-S{s.seq:03d}" for s in res.scenarios]
    check(
        "唯一性-case_code 可唯一派生",
        len(codes) == len(set(codes)),
        f"{len(codes)} 个 case_code / {len(set(codes))} 个唯一",
    )

    # ---------- 6. 覆盖与顺序稳定 ----------
    # 覆盖率的分母是"需求行数"而非"需求编号数": PA601 有 95 行但只有 74 个
    # 编号(多轨/多工作制拆成多行)。用编号数当分母会误报缺失 —— 实测 95 行
    # 全部有场景, 按编号去重后自然是 74。
    covered_rows = {(s.req_id, s.rail) for s in res.scenarios}
    all_rows = {(c.req_id, c.rail) for c in r.conditions}
    check(
        "覆盖-每条需求行都有场景",
        all_rows <= covered_rows,
        f"{len(covered_rows & all_rows)}/{len(all_rows)} 行覆盖 "
        f"({len(r.conditions)} 行 / {len({c.req_id for c in r.conditions})} 个编号)",
    )
    res2 = expand_scenarios(r.conditions, rules)
    check(
        "稳定-两次展开标识一致",
        [a.scenario_id for a in res.scenarios] == [b.scenario_id for b in res2.scenarios],
        "顺序稳定",
    )
    check(
        "稳定-两次展开序号一致",
        [a.seq for a in res.scenarios] == [b.seq for b in res2.scenarios],
        "序号稳定",
    )
    check(
        "稳定-两次展开推导值一致",
        [a.derived for a in res.scenarios] == [b.derived for b in res2.scenarios],
        "推导确定性",
    )

    # ---------- 7. 显式排除桶 ----------
    check(
        "排除-不可行组合有桶",
        hasattr(res, "excluded"),
        f"本次排除 {len(res.excluded)} 条",
    )
    bad_exc = [e for e in res.excluded if not e.reason]
    check("排除-每条都带理由", not bad_exc, f"缺理由 {len(bad_exc)} 条")

    # ---------- 8. 统计 ----------
    src = Counter(s.source for s in res.scenarios)
    print(f"\n    场景来源: {dict(src)}")
    print(f"    每需求场景数: {dict(Counter(Counter(s.req_id for s in res.scenarios).values()))}")
    check(
        "统计-场景总数 > 需求数",
        len(res.scenarios) > len(r.conditions),
        f"{len(res.scenarios)} > {len(r.conditions)}",
    )

    print(f"\n===== {PASSED}/{PASSED + FAILED} passed =====")
    if FAILED:
        print(f"SCENARIOS FAIL ({FAILED} 项)")
        return 1
    print("SCENARIOS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
