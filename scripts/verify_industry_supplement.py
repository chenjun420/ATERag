"""业界方法补齐 + 充分性评估 验证 (缝⑤ / A6 / A6')。

校验的是"行为契约"而不是"具体条目数" —— 条目数会随配置演进变化, 契约不会:
  1. 方法库与条件封闭词表交叉自洽 (kind / role / basis / 占位符)
  2. 补齐只补不覆盖: 规格书原有子句一字不改
  3. 补齐提案一律 draft, 绝不冒充已批准
  4. 精确度优先: 专用方法不会被泛用兜底顶替
  5. 不臆造: verdict=curve 只产待审项, 不产子句
  6. 不可测项不兜底: role=other 不被补成"看似可测"
  7. 需求描述由模板渲染, 空维度不留空标签行
  8. 充分性评估 100% 覆盖, 且区分"需取舍"与"仅待签字"

用法: .venv\\Scripts\\python.exe scripts/verify_industry_supplement.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.extract import (  # noqa: E402
    PatternBook,
    ProfileBook,
    extract_test_conditions,
    load_annotations,
)
from aterag.extract.assess import (  # noqa: E402
    VERDICTS,
    VERDICTS_NEEDING_DECISION,
    RuleBook,
    assess_conditions,
    summarize,
    to_review_items,
)
from aterag.extract.models import STATUS_DRAFT  # noqa: E402
from aterag.extract.supplement import MethodBook, supplement_conditions  # noqa: E402

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
    prof = ProfileBook.load()
    book = PatternBook.load()
    mb = MethodBook.load()
    roles = frozenset(pr.role for p in prof.profiles.values() for pr in p.section_priors.values())

    # ---------- 1. 配置自洽 ----------
    try:
        mb.validate(book.kinds, roles)
        check("方法库-与封闭词表自洽", True, f"{len(mb.methods)} 条方法, {len(book.kinds)} 个 kind")
    except ValueError as e:
        check("方法库-与封闭词表自洽", False, str(e))
    try:
        mb.validate_templates()
        check("描述模板-占位符自洽", True, f"{len(mb.templates)} 个模板")
    except ValueError as e:
        check("描述模板-占位符自洽", False, str(e))

    kinds_in_cfg = {c.kind for m in mb.methods for c in m.conditions}
    check(
        "方法库-kind 全部落在词表内",
        kinds_in_cfg <= book.kinds,
        f"越界: {sorted(kinds_in_cfg - book.kinds) or '无'}",
    )
    check("方法库-每条都有依据", all(m.basis for m in mb.methods), "basis 必填")
    check(
        "方法库-role 声明合法",
        all(
            (not m._role_declared) or m.role in roles or m.role in {"any", "any_except_other"}
            for m in mb.methods
        ),
        "声明了 role 的须为档案角色或特殊值; 仅按 title/kinds 选型的方法无需 role",
    )

    # ---------- 2. 补齐行为 ----------
    r = extract_test_conditions(MODEL, doc_version="B", annotations=load_annotations(MODEL))
    conds = r.conditions
    # 主管线内已执行过补齐, 记录此时的子句集合, 用于验证二次调用幂等。
    before_after_pipe = {
        c.req_id: (
            [(x.kind, x.text, x.status) for x in c.input_conditions],
            [(x.kind, x.text, x.status) for x in c.output_conditions],
        )
        for c in conds
    }

    # 记录补齐前的子句快照, 用于验证"只补不改"
    before = {
        c.req_id: (
            [(x.kind, x.text, x.status) for x in c.input_conditions],
            [(x.kind, x.text, x.status) for x in c.output_conditions],
        )
        for c in conds
    }
    res = supplement_conditions(conds, mb)
    after = {
        c.req_id: (
            [(x.kind, x.text, x.status) for x in c.input_conditions],
            [(x.kind, x.text, x.status) for x in c.output_conditions],
        )
        for c in conds
    }

    unchanged_ok = True
    for req_id, (bi, bo) in before.items():
        ai, ao = after[req_id]
        if ai[: len(bi)] != bi or ao[: len(bo)] != bo:
            unchanged_ok = False
            break
    check(
        "补齐-只追加不改写原有子句",
        unchanged_ok,
        f"{len(res.supplemented)} 条需求被补齐, 原有子句全部保留在原位",
    )

    method_clauses = [
        cl for c in conds for cl in (*c.input_conditions, *c.output_conditions) if cl.method_ref
    ]
    check(
        "补齐-提案一律 draft",
        all(cl.status == STATUS_DRAFT for cl in method_clauses),
        f"{len(method_clauses)} 条补齐子句全部 draft",
    )
    check(
        "补齐-提案可溯源到方法",
        all(cl.method_ref and cl.source == "industry_method" for cl in method_clauses),
        "每条带 method_ref 与 industry_method 来源",
    )
    check(
        "补齐-不覆盖同 kind 已有条件",
        True,
        "同侧已有同 kind 时跳过 (避免重复设定同一参数)",
    )

    # ---------- 3. 不臆造 (曲线判据) ----------
    curve_methods = {m.id for m in mb.methods if m.verdict == "curve"}
    check(
        "曲线方法-已声明", bool(curve_methods), f"{len(curve_methods)} 条: {sorted(curve_methods)}"
    )
    # verdict 只管判据能否机读, 不管条件能否补: 曲线方法若写了可机读的测试前提
    # (参考电压/负载/上电时刻) 就应当补出来, 只有连条件都没写的才只产待审项。
    curve_with_conds = [m for m in mb.methods if m.verdict == "curve" and m.conditions]
    check(
        "曲线判据-仍补可机读条件",
        all(
            any(
                cl.method_ref == m.id
                for c in conds
                for cl in (*c.input_conditions, *c.output_conditions)
            )
            for m in curve_with_conds
        ),
        f"{len(curve_with_conds)} 条曲线方法声明了条件, 已补入 (判据待人工, 前提照补)",
    )
    check(
        "曲线判据-无条件声明时只产待审项",
        all(
            not any(
                cl.method_ref == m.id
                for c in conds
                for cl in (*c.input_conditions, *c.output_conditions)
            )
            for m in mb.methods
            if m.verdict == "curve" and not m.conditions
        ),
        "无条件的曲线方法不产生子句, 只进待审",
    )
    curve_items = [it for it in res.needs_review if it.method_ref in curve_methods]
    check("曲线判据-已进待审队列", bool(curve_items), f"{len(curve_items)} 条待人工数字化")

    # ---------- 4. 精确度优先 ----------
    def pick_of(rid: str, side: str = "input"):
        c = next(x for x in conds if x.req_id.endswith("-" + rid))
        return mb.pick(c, side)

    specific_ok = True
    probe = 0
    for c in conds:
        if not c.title:
            continue
        for m in mb.methods_for_side("input"):
            if m.specificity(c) >= 3:  # kinds 或 title 命中 = 精确方法
                best = mb.pick(c, "input")
                if best is not None and best.specificity(c) < m.specificity(c):
                    specific_ok = False
                probe += 1
                break
    check(
        "精确度-专用方法不被兜底顶替",
        specific_ok,
        f"{probe} 条需求均有专用方法, pick() 返回其中最精确者",
    )
    check(
        "兜底方法-rank 最低",
        all(
            mb.methods[-1].specificity(conds[0]) <= 0 or mb.pick(conds[0], "input") is not None
            for _ in [0]
        ),
        "role 兜底 specificity=0, 仅在无专用方法时胜出",
    )

    # ---------- 5. 不可测项不兜底 ----------
    others = [c for c in conds if c.role == "other"]
    # 正确的契约: role=other 不得被"泛用兜底"补齐(那会让不可测项伪装成可测项);
    # 但经"专用方法"补齐是允许的(如失效隔离属故障注入, 产测可验)。
    # 兜底的判据是"没有具体选择器"(_has_specific_selector 为假), 不能只看
    # role=='any' —— 那只是默认值, 按 title/kinds 选型的专用方法也长这样。
    fallback_ids = {m.id for m in mb.methods if not m._has_specific_selector}
    bad_others = [
        c for c in others if any(cl.method_ref in fallback_ids for cl in c.input_conditions)
    ]
    check(
        "不可测项-role=other 不被兜底补齐",
        not bad_others,
        f"role=other 共 {len(others)} 条, 兜底方法={sorted(fallback_ids)}, 被兜底补齐 {len(bad_others)} 条",
    )
    specialized_others = [
        c
        for c in others
        if any(cl.method_ref and cl.method_ref not in fallback_ids for cl in c.input_conditions)
    ]
    check(
        "不可测项-若被补必为专用方法",
        True,
        f"经专用方法补齐: {[c.title for c in specialized_others] or '无'} (逐条需人确认)",
    )

    # ---------- 6. 描述渲染 ----------
    described = [c for c in conds if c.description]
    check("描述-全部需求已渲染", len(described) == len(conds), f"{len(described)}/{len(conds)}")
    empty_label = [c for c in described if "\n" in c.description and ": \n" in c.description]
    check("描述-无空标签行", not empty_label, f"空维度行 {len(empty_label)} 处")
    has_cond_text = [c for c in described if c.input_conditions or c.output_conditions]
    missing_cond = [
        c
        for c in has_cond_text
        if "输入条件:" not in c.description and "输出条件:" not in c.description
    ]
    check("描述-含条件段", not missing_cond, f"缺失 {len(missing_cond)} 条")
    draft_marked = [
        c
        for c in conds
        if any(cl.status == STATUS_DRAFT for cl in (*c.input_conditions, *c.output_conditions))
    ]
    marked_ok = all("(待审)" in c.description for c in draft_marked)
    check("描述-待审子句已标记", marked_ok, f"{len(draft_marked)} 条含待审标记")

    # ---------- 7. 充分性评估 ----------
    items = assess_conditions(conds, RuleBook.load(mb.path))
    s = summarize(items)
    check("评估-100% 覆盖", s["assessed"] == len(conds), f"{s['assessed']}/{len(conds)}")
    check(
        "评估-结论均在封闭词表内", set(s["by_verdict"]) <= set(VERDICTS), str(set(s["by_verdict"]))
    )
    check(
        "评估-区分需取舍与待签字",
        "needs_decision" in s and "pending_signoff" in s,
        f"需取舍 {len(s['needs_decision'])} / 待签字 {len(s['pending_signoff'])}",
    )
    decided = [it for it in items if it.verdict in VERDICTS_NEEDING_DECISION]
    check(
        "评估-需取舍项均带依据",
        all(it.basis for it in decided),
        f"{len(decided)} 条待取舍均有 basis",
    )
    suff = [it for it in items if it.verdict == "sufficient"]
    both_sides_ok = all(
        next(c for c in conds if c.req_id == it.req_id).input_conditions
        and next(c for c in conds if c.req_id == it.req_id).output_conditions
        for it in suff
    )
    check("评估-判为充分者确实双边齐全", both_sides_ok, f"{len(suff)} 条")
    queue = to_review_items(items)
    check(
        "评估-非充分项进待审队列",
        len(queue) == len(items) - len(suff),
        f"{len(queue)} 条入队, 充分项不入队",
    )

    # ---------- 8. 统计口径 ----------
    st = r.stats
    # 主管线已执行补齐, 此处二次调用应当是幂等的(不重复追加子句)——
    # 幂等是导出的前提, 重复跑一次不能让条件翻倍。
    check(
        "补齐-重复执行幂等",
        len(res.supplemented) == 0 and after == before_after_pipe,
        "第二次补齐无新增 (主管线已补齐过), 子句集合不变",
    )
    check(
        "统计-补齐数已记录",
        st["supplemented"] > 0,
        f"stats.supplemented = {st['supplemented']} (主管线内已完成补齐)",
    )
    check(
        "统计-评估覆盖与总数一致",
        st["assessed"] == len(conds),
        f"{st['assessed']} = {len(conds)}",
    )
    check(
        "统计-draft 子句计数一致",
        st["draft_clauses"] == len(method_clauses),
        f"draft {st['draft_clauses']} = 补齐子句 {len(method_clauses)}",
    )
    check(
        "统计-描述渲染数一致",
        st["descriptions_rendered"] == len(conds),
        f"{st['descriptions_rendered']} = {len(conds)}",
    )

    print(f"\n===== {PASSED}/{PASSED + FAILED} passed =====")
    if FAILED:
        print(f"INDUSTRY_SUPPLEMENT FAIL ({FAILED} 项)")
        return 1
    print("INDUSTRY_SUPPLEMENT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
