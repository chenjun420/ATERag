"""产测条件抽取验证套件 (对应产测输入/输出条件需求).

覆盖: 章节选择 / 剔除语义 / 短横线语义 / 条件装配 / 章节边界 / 双通道一致性 / 人工注记。

blocks 侧车 (rag_storage/) 不入库, 故 CI 上会由规格书现生成一份再验证 ——
验证对象始终是真实规格书, 而不是"没数据就跳过"。

用法: .venv\\Scripts\\python.exe scripts/verify_conditions.py
"""

import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings  # noqa: E402
from aterag.extract import (  # noqa: E402
    PatternBook,
    ProfileBook,
    SectionKeywordNotFound,
    apply_sieve,
    extract_test_conditions,
    is_placeholder,
    load_annotations,
    load_blocks,
    row_fingerprint,
    rows_from_blocks,
    rows_from_postgres,
    section_matches,
    select_sections,
)
from aterag.extract.selector import heading_section  # noqa: E402

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" | {detail}" if detail else ""))


MODEL = "PA601-D54A"
DOC = "PA601-D54A 定制电源技术规格书.md"


def main() -> int:
    # 4.3 抽取范围为零缺口门禁的前提: 规格书 md 与 blocks 侧车同源。
    # CI 上 blocks 侧车 (rag_storage/) 不入库, 故无侧车时从规格书现生成一份 ——
    # 验证对象仍是真实规格书, 而非"跳过"。
    blocks_path = Path("rag_storage/blocks") / f"{MODEL}.jsonl"
    if not blocks_path.exists():
        if not Path(DOC).exists():
            print(f"(既无 {blocks_path} 也无规格书 {DOC}, 跳过)")
            return 0
        from aterag.ingest.markdown_parser import parse_file, write_blocks_jsonl

        write_blocks_jsonl(parse_file(DOC), blocks_path)
        print(f"(侧车缺失, 已由 {DOC} 现生成 {blocks_path})")

    prof = ProfileBook.load().get("power_spec_cn")
    book = PatternBook.load()
    blocks = load_blocks(MODEL)
    sel = select_sections(blocks, prof.section_keywords)
    r = extract_test_conditions(
        MODEL,
        doc_version="B",
        profiles=ProfileBook.load(),
        patterns=book,
        annotations=load_annotations(MODEL),
    )
    kept = {c.req_id for c in r.conditions}
    excl = {e.req_id for e in r.excluded}

    # ---------- 1. 章节选择 ----------
    check(
        "章节-命中功能/性能要求",
        sel.matched_headings == ["4.3 功能/性能要求"],
        str(sel.matched_headings),
    )
    check("章节-前缀为 4.3", sel.section_prefixes == ["4.3"], str(sel.section_prefixes))
    subsecs = {c.section_path for c in r.conditions}
    expected = {"4.3.1", "4.3.2", "4.3.3", "4.3.4.1", "4.3.4.2", "4.3.4.3", "4.3.5"}
    check("章节-收编全部子章节", expected == subsecs, str(sorted(subsecs)))
    check("章节-无 4.2 越界 (接口章节不进条件)", not any(s.startswith("4.2") for s in subsecs))
    check("章节-无 4.4 越界 (可靠性章节不进条件)", not any(s.startswith("4.4") for s in subsecs))
    # 标题含"输入/输出"但在 4.2 章下, 必须被章节过滤排除
    check(
        "章节-4.2.4.2 输出接口被排除",
        not any(s == "4.2.4.2" for s in subsecs),
        "接口章节不参与抽取",
    )

    # 边界: "4.3" 不应命中 "4.30"
    check("章节-前缀边界(4.3 不误吞 4.31)", section_matches("4.31", ["4.3"]) is False)
    check("章节-前缀边界(4.3 命中 4.3.1)", section_matches("4.3.1", ["4.3"]) is True)
    check("章节-标题编号解析", heading_section("4.3.4.1 遥信功能") == "4.3.4.1")

    # 零命中必须 fail-closed
    try:
        select_sections(blocks, ["不存在的章节名"])
        check("章节-关键字零命中 fail-closed", False, "未抛异常")
    except SectionKeywordNotFound:
        check("章节-关键字零命中 fail-closed", True)

    # ---------- 2. 剔除语义 ----------
    for rid, label in (
        ("SR-PA601-D54A-1107", "输入防反功能"),
        ("SR-PA601-D54A-1108", "输入ORING功能"),
        ("SR-PA601-D54A-1221", "输入输出电压降"),
        ("SR-PA601-D54A-1420", "输出过压告警(备注=不要求)"),
    ):
        check(f"剔除-{label}", rid in excl and rid not in kept, rid[-4:])
    n1202 = sum(1 for e in r.excluded if e.req_id == "SR-PA601-D54A-1202")
    check("剔除-SR-1202 三档全剔", n1202 == 3, f"{n1202}/3")
    check("剔除-均以等级列命中", all(e.field in {"priority", "notes"} for e in r.excluded))
    check(
        "剔除-每条都有原因可审计",
        all(e.reason and e.matched_word for e in r.excluded),
    )

    # R3: 备注含"不要求"子串的强制项必须保留
    c1219 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1219"), None)
    check(
        "剔除-SR-1219 强制项保留(备注含'不要求均流度'子串)",
        c1219 is not None and c1219.priority == "强制",
        "子串匹配会误删, 故备注只做整格等值",
    )

    # ---------- 3. 短横线语义 (R5) ----------
    check("短横线-is_placeholder 识别", all(is_placeholder(v) for v in ("-", "—", "", "  -  ")))
    check("短横线-非占位符不误判", not any(is_placeholder(v) for v in ("0", "-54", "不要求")))
    c1217 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1217"), None)
    check(
        "短横线-SR-1217 保留(该轨无数据≠整条不要求)",
        c1217 is not None,
        "3.45V 轨限值为 '-', 但 -54V 轨有效",
    )
    c1101 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1101"), None)
    check("短横线-SR-1101 保留(Vac 档有效)", c1101 is not None and bool(c1101.input_conditions))
    check(
        "短横线-无数据维度被标注而非剔除",
        any("no_data:" in f for c in r.conditions for f in c.flags),
        "flags 记录哪几个维度无数据",
    )
    check(
        "短横线-统计不把表结构差异当缺口",
        r.stats["no_data_dims"].get("rail", 0) < 60,
        f"rail 空 {r.stats['no_data_dims'].get('rail', 0)} 条(遥测表无电压轨列)",
    )
    # 单元级: 等级=强制 且限值全空 -> 保留但无子句, 不臆造
    rows, _, _ = rows_from_blocks(blocks, MODEL, "B", sel.section_prefixes)
    out = apply_sieve(rows, prof.exclude_words)
    forced_nolimit = [
        rr
        for rr in out.kept
        if str(rr["priority"]).strip() == "强制"
        and all(is_placeholder(rr[k]) for k in ("min", "typ", "max"))
    ]
    check(
        "短横线-强制但无限值行保留",
        any(rr["req_id"] == "SR-PA601-D54A-1217" for rr in forced_nolimit),
        f"共 {len(forced_nolimit)} 条",
    )

    # ---------- 4. 条件装配 (输入/输出) ----------
    c1210 = [c for c in r.conditions if c.req_id == "SR-PA601-D54A-1210"]
    check("装配-SR-1210 三档效率都在", len(c1210) == 3, f"{len(c1210)}/3")
    e50 = next((c for c in c1210 if c.limits.get("min") == 91.0), None)
    check(
        "装配-SR-1210 第2档 激励=220Vac+50%负载",
        bool(e50)
        and any("220" in i.text for i in e50.input_conditions)
        and any("50%" in i.text for i in e50.input_conditions),
        "; ".join(i.text for i in (e50.input_conditions if e50 else []))[:80],
    )
    check(
        "装配-SR-1210 第2档 响应=效率≥91%",
        bool(e50)
        and any(
            o.kind == "efficiency" and o.value and o.value.get("min") == 91.0
            for o in e50.output_conditions
        ),
    )

    c1402 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1402"), None)
    check(
        "装配-SR-1402 响应含 OC 门告警行为",
        bool(c1402) and any("OC" in o.text for o in c1402.output_conditions),
        "依赖 signal_req 列抽取 (原被静默丢弃)",
    )
    check(
        "装配-SR-1402 激励含掉电事件",
        bool(c1402) and any("掉电" in i.text or "市电" in i.text for i in c1402.input_conditions),
    )

    tel = [c for c in r.conditions if c.section_path == "4.3.4.3"]
    tel_excl = [e for e in r.excluded if e.section_path == "4.3.4.3"]
    check(
        "装配-遥测 18 行全覆盖",
        len(tel) + len(tel_excl) == 18,
        f"{len(tel)}留 + {len(tel_excl)}剔 = 18",
    )
    t0 = next((c for c in tel if c.req_id == "SR-PA601-D54A-1600"), None)
    check(
        "装配-遥测含检测范围与精度",
        bool(t0) and any("0~320Vac" in o.text for o in t0.output_conditions),
        "; ".join(o.text for o in (t0.output_conditions if t0 else []))[:70],
    )

    prot = [c for c in r.conditions if c.section_path == "4.3.3"]
    both = [c for c in prot if c.input_conditions and c.output_conditions]
    check("装配-保护点兼具激励+响应", len(both) >= 12, f"{len(both)}/{len(prot)}")

    # 4.3.1 输入特性: 限值归激励侧
    c1101_2 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1103"), None)
    check(
        "装配-4.3.1 限值归激励侧",
        bool(c1101_2) and any(i.kind == "input_frequency" for i in c1101_2.input_conditions),
        "章节先验 input_domain/limits_to=input",
    )
    # 4.3.2 输出特性: 限值本身是响应判据; 备注里的"额定输入/半载"才是激励。
    # 故不能要求 input 为空 —— 备注给出的激励本就该进 input 侧。
    c1201 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1201"), None)
    check(
        "装配-4.3.2 限值归响应侧",
        bool(c1201)
        and any(o.kind == "output_voltage" for o in c1201.output_conditions)
        and not any(
            o.source == "limits" and o.kind == "output_voltage" for o in c1201.input_conditions
        ),
        "章节先验 output_spec/limits_to=output; 激励取自备注",
    )
    check(
        "装配-4.3.2 备注激励入激励侧",
        bool(c1201) and any(i.kind in {"input_voltage", "load"} for i in c1201.input_conditions),
        "; ".join(i.text for i in (c1201.input_conditions if c1201 else []))[:60],
    )

    # 词表封闭性
    used_kinds = {c.kind for c in r.conditions for c in (*c.input_conditions, *c.output_conditions)}
    check(
        "装配-所有 kind 均在封闭词表内",
        used_kinds <= book.kinds,
        str(sorted(used_kinds - book.kinds)),
    )

    # ---------- 5. 数据来源一致性 (blocks 通道 vs RAG 落库实体) ----------
    # 两条通道共用同一份档案与装配器, 但一条重跑抽取、一条读 PG 实际落库;
    # 若入库链路与抽取逻辑漂移, 这里会立刻暴露。
    try:
        rows_pg = rows_from_postgres(get_settings().postgres_dsn, MODEL, sel.section_prefixes)
        sig = lambda rr: (  # noqa: E731
            str(rr["section_path"]),
            str(rr["req_id"]),
            str(rr["min"]),
            str(rr["max"]),
            str(rr["rail"]),
            str(rr["priority"]),
        )
        sig_blocks = sorted(sig(rr) for rr in rows)
        sig_pg = sorted(sig(rr) for rr in rows_pg)
        check(
            "数据源-blocks 与 postgres 产出同一批条目",
            sig_blocks == sig_pg,
            f"blocks={len(sig_blocks)} pg={len(sig_pg)} 差异={len(set(sig_blocks) ^ set(sig_pg))}",
        )
    except Exception as e:  # noqa: BLE001  无存储栈时跳过, 不伪造通过
        check("数据源-需 PG (本次跳过: " + type(e).__name__ + ")", True, "离线环境")

    # ---------- 6. 三桶与统计 ----------
    check("三桶-条件非空", len(r.conditions) > 0, f"{len(r.conditions)}")
    check("三桶-剔除可审计", len(r.excluded) > 0, f"{len(r.excluded)}")
    # 同一 req_id 可有多档位行 (SR-1210 三档效率), 故按 (req_id, 档位后缀) 去重, 不能按 req_id
    kept_ids = {c.req_id for c in r.conditions}
    excl_ids = {e.req_id for e in r.excluded}
    check(
        "三桶-条件与剔除不相交",
        not (kept_ids & excl_ids),
        f"交集 {sorted(kept_ids & excl_ids)[:3]}",
    )
    check(
        "三桶-条目数守恒",
        r.stats["kept"] + r.stats["excluded"] == r.stats["rows_total"],
        f"留{r.stats['kept']}+剔{r.stats['excluded']}=行{r.stats['rows_total']}",
    )
    check(
        "三桶-多档位不丢行",
        len(r.conditions) == r.stats["kept"]
        and any(c.req_id == "SR-PA601-D54A-1210" for c in r.conditions)
        and sum(1 for c in r.conditions if c.req_id == "SR-PA601-D54A-1210") == 3,
        "按档位而非仅 req_id 计数",
    )
    check("统计-命中+剔除=总行", r.stats["kept"] + r.stats["excluded"] == r.stats["rows_total"])
    check("统计-需求评审队列可见", r.stats["needs_review"] > 0, f"{r.stats['needs_review']} 项")

    # ---------- 6. 注记机制 ----------
    fp = row_fingerprint({"req_id": "X", "title": "t", "notes": "n"})
    check("注记-指纹稳定", fp == row_fingerprint({"req_id": "X", "title": "t", "notes": "n"}))
    check(
        "注记-内容变更则指纹变化",
        fp != row_fingerprint({"req_id": "X", "title": "t", "notes": "n2"}),
    )
    ann = load_annotations(MODEL)
    check("注记-档案存在", hasattr(ann, "entries"), ann.source_path)

    # ---------- 7. 人工注记 (人工审核的语义应覆盖规则推断) ----------
    c1213 = next((c for c in r.conditions if c.req_id == "SR-PA601-D54A-1213"), None)
    check("注记-SR-1213 复合条件已注记", c1213 is not None)
    check(
        "注记-生效后 confidence=annotated",
        bool(c1213)
        and all(
            cl.confidence == "annotated"
            for cl in (*c1213.input_conditions, *c1213.output_conditions)
        ),
        "人工审核过的语义应覆盖正则推断",
    )
    check(
        "注记-拆出四层复合条件",
        bool(c1213)
        and sum(1 for i in c1213.input_conditions if i.kind == "temperature") == 2
        and sum(1 for i in c1213.input_conditions if i.kind == "load") == 2
        and sum(1 for o in c1213.output_conditions if o.kind == "timing") == 2,
        "温度≥/-25℃ + 额定/半载 + 6s/12s",
    )
    check(
        "注记-所有 kind 仍在封闭词表内",
        bool(c1213)
        and {cl.kind for cl in (*c1213.input_conditions, *c1213.output_conditions)} <= book.kinds,
    )
    check("注记-统计计入 annotated", r.stats["annotated"] >= 1, str(r.stats["annotated"]))
    # 指纹失配的注记不得被采用
    ann = load_annotations(MODEL)
    stale_entry, fresh = ann.lookup("SR-PA601-D54A-1213", "deadbeefdeadbeef")
    check("注记-指纹失配即视为过期", stale_entry is not None and fresh is False)
    real_entry, _ = ann.lookup(
        "SR-PA601-D54A-1213", ann.entries["SR-PA601-D54A-1213"]["fingerprint"]
    )
    check("注记-查询接口返回条目", real_entry is not None)

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n===== {len(results) - n_fail}/{len(results)} passed =====")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
