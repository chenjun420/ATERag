"""模板漂移门禁 (方案 §4.0③)。

**定位: 辅助工具, 不是防线。** §4.0 的核心结论是「检测归抽取过程, 且必须
fail-closed」—— 抽取撞上模板失配时会直接报错 (红线 12), 不依赖本脚本。

那这个脚本还负责什么? 两件抽取过程**做不到**的事:

1. **事后追责**: 历史产物是用哪套参数算出来的。抽取时落的
   ``ExtractionResult.template`` 就是为此存在 (§4.0②)。
2. **相对基线漂移**: 「和上次比变了没有」。抽取过程只能告诉你「现在这样对不对」,
   不知道「相对上次变了」—— 而模板变更的典型后果恰恰是**静默失效**: 抽取跑完、
   结果看着正常、其实抽错了。这类失效抽取过程自己发现不了, 因为它没有「上次」
   可比。

所以本脚本的判据是**回归对账**, 不是正确性判定。§4.0 那句「辅助工具坏了只影响
方便, 不影响正确性」在这里成立的前提是: 抽取过程已经能独立 fail-closed。

三段检查 (对应 §4.0 的方案):

============  ==============================================  ==========
检查          判据                                           红了说明
============  ==============================================  ==========
基线对比      当前 fingerprint vs 基线                       参数被改过
影响面        按 section_path 前缀反查受影响的需求           **改了什么内容**
覆盖率回归    条件数 / 未解析队列 / 待审队列 三项            **结果变了多少**
============  ==============================================  ==========

只报「变了」没有用 —— 人无法判断影响面, 于是只能忽略。后两段是给「变了」配的
可操作信息。

多型号
------
基线按 ``(template_id, model_id)`` 存, 不全局一份 (§4.0④)。改 PA601 的档案不会
让另两个型号一起红。

用法:
    .venv\\Scripts\\python.exe scripts/template_drift.py              # 对比基线
    .venv\\Scripts\\python.exe scripts/template_drift.py --update     # 接受变更并写基线
    .venv\\Scripts\\python.exe scripts/template_drift.py --model PN2000-24A

退出码: 0 未漂移 / 1 有漂移 / 2 基线缺失或不可读
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from aterag.config import get_settings  # noqa: E402
from aterag.extract.api import (  # noqa: E402
    ProfileBook,
    extract_test_conditions,
    model_profile_name,
)
from aterag.extract.assembler import PatternBook  # noqa: E402
from aterag.extract.configs import role_vocabulary, template_identity  # noqa: E402
from aterag.ingest.table_schema import load_registry  # noqa: E402

BASELINE_PATH = "config/template_baseline.yaml"
PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

#: 覆盖率回归的允许波动。绝对数而非百分比: 条件数本来就只有几十条, 百分比在
#: 小基数上会显得很宽容 (5% of 20 = 1 条, 1 条可能就是整个 4.4 章节)。
#:
#: **待审队列被拆成两个指标, 因为塞在一起的两类东西方向相反** (2026-10-09):
#:
#: ============================ ================================== ============
#: 指标                          含义                              变多意味着
#: ============================ ================================== ============
#: ``needs_review_extract``     抽不出来的东西                     **抽取退化**
#: ``needs_review_assess``      评估层判为待签字/不足/不必要       评审工作量涨
#: ============================ ================================== ============
#:
#: 实测 PA601-D54A: 47 = 抽取层 **2** + 评估层 **45**。抽取层几乎归零
#: (``needs_manual_digitization`` 2 条, 都在 4.3.1), 涨的全是评估层 ——
#: 那是 :mod:`aterag.extract.assess` 引入签字通道后的**功能增加**, 不是退化。
#:
#: 一个计数 + 一个容差同时管这两类, 判据必然失真, 而且是**双向**失真:
#: 评估层正当涨 45 条, 抽取层即使退化 5 条也照样 PASS (被容差吃掉);
#: 反过来抽取层退化 45 条也会被评估层的正当增长解释掉。两种情况都报「无漂移」。
#:
#: 拆开之后各自的方向就清楚了: 抽取层涨 = 真退化, 零容忍起步; 评估层涨 =
#: 评审工作量, 允许但要按章节看清楚是哪一章涨的。
TOLERANCE = {
    "conditions_total": 0,  # 条件数**不许**变: 变了就是漏抽或多抽, 没有解释
    "unresolved_text": 5,  # 未解析文本: 允许波动 (抽取器在演进), 但要看清
    "needs_review_extract": 2,  # 抽不出来的: 涨 2 条就要解释 (实测基线值 2)
    "needs_review_assess": 8,  # 评估层待签字: 工作量, 允许涨但按章节报
}

#: **章节级分布容差**: 任一章节的待审项变化超过这个数就报, 无论总数是否超容差。
#:
#: 为什么总数容差不够: PA601 的待审项集中在 ``4.3.2`` (20/47)。总数 +5 可能
#: 是「五个章节各 +1」(分散, 无害) 也可能是「4.3.2 单独 +5」(集中, 那一章的
#: 抽取或评估规则出问题了)。两种在总数上长得一模一样, 而第二种要查。
#:
#: 按章节报出来之后, 集中变化会自己显形 —— 不用人去猜这 +5 落在哪。
SECTION_TOLERANCE = 3

#: 待审项的 kind 前缀 -> 归到哪个指标。**前缀而非全量枚举**: 评估层 verdict
#: 由 :mod:`aterag.extract.assess` 自己定义 (``VERDICT_*``), 脚本侧再抄一份
#: 就会在对方新增 verdict 时静默漏归类 —— 而漏归类的后果是新 verdict 被算进
#: 抽取层, 把评审工作量报成抽取退化。
_ASSESS_PREFIX = "assess:"

#: 基线文件的头部说明。**每次 --update 都重写它** —— 所以它必须由代码生成,
#: 写成手工维护的注释会在第一次 --update 时被冲掉, 而那段说明恰恰是防止
#: 「直接 --update 把门禁的牙拔了」的唯一东西。
BASELINE_HEADER = """\
# 模板基线 —— 由 scripts/template_drift.py --update 生成
#
# 按 (template_id, model_id) 存 (§4.0④): 改一个型号的档案不该让别的型号跟着变红。
#
# **这个文件是「回归对账」用的, 不是防线。** 抽取过程本身对模板失配是 fail-closed
# 的 (红线 12): 章节选不中会抛 SectionKeywordNotFound, 表头不认识会抛
# TableSchemaUnmapped。这里管的是抽取过程**管不到**的那一类 —— 参数变了、抽取照样
# 跑完、结果看着正常但其实判错了 (role / limits_to 判错时条件数往往不变)。
#
# 更新流程: 改配置 -> 跑 scripts/template_drift.py 看清楚红了什么 -> 确认变更
# 有意 -> 递增 doc_profiles.yaml 里的 template_version -> 跑 --update 采新基线。
# 不要跳过「看清楚红了什么」直接 --update, 那等于把门禁的牙拔了。
#
# 缺条目 ≠ 无漂移: 没基线的型号不参与漂移判定, 脚本会单独报出来。
#
# needs_review_by_section 是待审项的章节分布, 与总数**独立**判定: 总数在容差内
# 而某一章单独超容差, 照样报红 —— 那是「一章的规则坏了, 被其它章节的噪声摊平」
# 的情形, 摊平之后总数看着正常。
"""


def _baseline_path(args) -> Path:
    return Path(args.baseline)


def load_baseline(path: Path) -> dict:
    """读基线。缺失时返回空结构 —— 由调用方决定是「新增型号」还是「配置坏了」。"""
    if not path.exists():
        return {"version": 1, "note": "由 scripts/template_drift.py --update 生成", "entries": {}}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    doc.setdefault("entries", {})
    return doc


def measure(model_id: str, *, blocks_dir: str | None = None) -> dict:
    """跑一次抽取, 取模板身份与三项覆盖率指标。

    ``template_drift`` 关心的是**产物统计**, 不是条件内容 —— 所以这里不逐条比对
    条件, 只比数量与队列长度。逐条比对会因排序/措辞产生大量噪声 diff, 而噪声会
    让人习惯性忽略红字, 那等于没有门禁。
    """
    settings = get_settings()
    prof_book = ProfileBook.load(settings.doc_profiles_path)
    profile_name = model_profile_name(model_id)
    profile = prof_book.get(profile_name)
    patterns = PatternBook.load(settings.condition_patterns_path)
    # 用**当前**配置算指纹, 不读基线里的旧值 —— 基线是被比方, 不是证据。
    ident = template_identity(
        profile,
        known_kinds=patterns.kinds,
        known_roles=role_vocabulary(prof_book),
        schema_fields=sorted(load_registry(settings.table_schemas_path).known_fields),
    )
    kwargs = {"blocks_dir": blocks_dir} if blocks_dir else {}
    result = extract_test_conditions(model_id, profiles=prof_book, patterns=patterns, **kwargs)
    stats = result.stats

    # 待审项按语义分两层, 各自再按章节分布 —— 见 TOLERANCE 的说明。
    by_layer: dict[str, collections.Counter] = {
        "extract": collections.Counter(),
        "assess": collections.Counter(),
    }
    for it in result.needs_review:
        layer = "assess" if it.kind.startswith(_ASSESS_PREFIX) else "extract"
        by_layer[layer][it.section_path or "(未标章节)"] += 1

    return {
        "model_id": model_id,
        "profile": profile.name,
        **ident,
        "metrics": {
            "conditions_total": int(stats.get("conditions_total", 0)),
            "unresolved_text": int(stats.get("unresolved_text", 0)),
            "needs_review_extract": sum(by_layer["extract"].values()),
            "needs_review_assess": sum(by_layer["assess"].values()),
            "scenarios": int(stats.get("scenarios", 0)),
        },
        # 章节分布: 让「+5 落在哪一章」不必人去猜。总数容差看不出集中还是分散,
        # 而这两者的处置完全不同 —— 集中要查那一章的规则, 分散不用。
        "needs_review_by_section": {
            layer: dict(sorted(c.items())) for layer, c in by_layer.items()
        },
        # 影响面: section_priors 的键就是「模板管到哪些章节号」。人工改了章节号,
        # 这些键就是受影响的范围 —— 比让人自己回忆改了哪一处可靠。
        "governed_sections": sorted(profile.section_priors),
    }


def diff_metrics(now: dict, base: dict) -> tuple[list[str], list[str]]:
    """覆盖率指标对比基线 (总数口径)。

    返回 ``(超容差, 未超容差但确实变了)`` 两段 —— 都报出来。

    为什么要分开: 只报超容差的话, 「变了 3 条待审」和「没变」在输出上一模一样,
    而人无法区分这两种情况。真正的代价不是「红」, 是「学会了忽略输出」——
    门禁一旦让人习惯性扫一眼, 后面真出事那次也一样被扫掉。
    """
    over: list[str] = []
    within: list[str] = []
    for key, tol in TOLERANCE.items():
        cur = now.get(key, 0)
        old = base.get(key, 0)
        if cur == old:
            continue
        arrow = "增加" if cur > old else "减少"
        text = f"{key}: {old} -> {cur} ({arrow} {abs(cur - old)}, 容差 {tol})"
        (over if abs(cur - old) > tol else within).append(text)
    return over, within


def diff_by_section(now: dict, base: dict, *, tolerance: int) -> list[str]:
    """待审项的**章节级**变化, 超 ``tolerance`` 的章节逐个报出来。

    与 :func:`diff_metrics` 互补而不是替代: 总数说「变了多少」, 这里说
    「变在哪一章」。总数在容差内而单章超容差的情况必须报出来 —— 那正是
    「一个章节的规则坏了, 但被其它章节的噪声摊平」的情形, 而摊平后总数看着正常。

    两侧的章节集合取并集: 只在当前出现的章节 (= 新增待审) 和只在基线出现的
    章节 (= 待审消失) 都要报, 只比交集会把两者都当成「没变」。
    """
    out: list[str] = []
    for layer in sorted(set(now) | set(base)):
        cur_map = now.get(layer) or {}
        old_map = base.get(layer) or {}
        for sec in sorted(set(cur_map) | set(old_map)):
            cur = int(cur_map.get(sec, 0))
            old = int(old_map.get(sec, 0))
            if abs(cur - old) <= tolerance:
                continue
            arrow = "增加" if cur > old else "减少"
            out.append(
                f"{layer}/{sec}: {old} -> {cur} ({arrow} {abs(cur - old)}, 章节容差 {tolerance})"
            )
    return out


def check(model_id: str, baseline: dict, *, blocks_dir: str | None = None) -> dict:
    """对某个型号做三段检查。返回结果字典, 不直接打印 —— 便于测试。"""
    now = measure(model_id, blocks_dir=blocks_dir)
    entries = baseline.get("entries") or {}
    base = entries.get(model_id)
    problems: list[str] = []
    if base is None:
        # 新增型号不是漂移, 是「还没有基线」—— 单独一类, 不混进漂移里报红。
        return {
            "model_id": model_id,
            "status": "no_baseline",
            "current": now,
            "fingerprint_changed": None,
            "metric_diffs": [],
            "message": "该型号没有基线条目 —— 跑 --update 接受当前状态后才会开始比对",
        }
    fp_changed = now["fingerprint"] != base.get("fingerprint")
    over, within = diff_metrics(now["metrics"], base.get("metrics") or {})
    # 章节级: 与总数**独立**判定, 不挂在 over 的计算结果后面 —— 挂在后面的话
    # 总数在容差内时章节差异会被一起吞掉, 而单章集中退化正是最该报的那种。
    section_over = diff_by_section(
        now.get("needs_review_by_section") or {},
        base.get("needs_review_by_section") or {},
        tolerance=SECTION_TOLERANCE,
    )
    if fp_changed:
        problems.append("模板指纹变化 (参数被改过)")
    if over:
        problems.append("覆盖率回归")
    if section_over:
        problems.append("单章待审项集中变化")
    return {
        "model_id": model_id,
        "status": "drift" if problems else "ok",
        "current": now,
        "baseline": base,
        "fingerprint_changed": fp_changed,
        "metric_diffs": over,
        "metric_diffs_within_tolerance": within,
        "section_diffs": section_over,
        "governed_sections_changed": sorted(
            set(now["governed_sections"]) ^ set(base.get("governed_sections") or [])
        ),
        "message": "; ".join(problems),
    }


def render(result: dict) -> None:
    m = result["model_id"]
    now = result["current"]
    print(
        f"\n=== {m} (profile={now['profile']}, template={now['template_id']} v{now['template_version']}) ==="
    )
    print(f"  当前指纹 {now['fingerprint']}  指标 {json.dumps(now['metrics'], ensure_ascii=False)}")
    if result["status"] == "no_baseline":
        print(f"  {WARN} {result['message']}")
        return
    base = result["baseline"]
    if result["fingerprint_changed"]:
        print(f"  {FAIL} 模板指纹 {base.get('fingerprint')} -> {now['fingerprint']}")
        print(f"       模板版本 {base.get('template_version')} -> {now['template_version']}")
        gov = result.get("governed_sections_changed") or []
        if gov:
            print(f"       {FAIL} 模板管辖章节号有增删 (影响面): {', '.join(gov)}")
            print("            ↑ 这些章节号的变化会让 role / limits_to 判错, 且抽取不会报错")
        else:
            print(f"       模板管辖章节: {', '.join(now['governed_sections']) or '(无先验)'}")
        print("       下一步: 确认变更是有意的 -> template_version 递增 -> 跑 --update")
    else:
        print(f"  {PASS} 模板指纹未变 ({now['fingerprint']})")
    for d in result["metric_diffs"]:
        print(f"  {FAIL} 覆盖率回归 {d}")
    for d in result.get("section_diffs") or []:
        print(f"  {FAIL} 单章集中变化 {d}")
    for d in result.get("metric_diffs_within_tolerance") or []:
        print(f"  {WARN} 有变化但未超容差 {d}")
    if result["status"] == "ok":
        print(f"  {PASS} 无漂移")
        # 待审项分布即使没超容差也打出来: 「无漂移」不等于「没变化」, 而待审
        # 队列的章节分布是人工评审下一步该从哪看起的唯一线索。
        sec = now.get("needs_review_by_section") or {}
        for layer in sorted(sec):
            dist = ", ".join(f"{k}={v}" for k, v in (sec[layer] or {}).items())
            print(f"  {WARN} 待审分布[{layer}]: {dist or '(空)'}")


def update(baseline: dict, results: list[dict], *, note: str = "") -> dict:
    """把当前状态写进基线。

    只覆盖本次**实际测量过**的型号 —— 不碰没跑的型号的条目。没测过的型号若被
    顺手刷成当前值, 等于把它的基线清空, 而清空的基线永远不会红。
    """
    entries = dict(baseline.get("entries") or {})
    for r in results:
        now = r["current"]
        entries[now["model_id"]] = {
            "profile": now["profile"],
            "template_id": now["template_id"],
            "template_version": now["template_version"],
            "fingerprint": now["fingerprint"],
            "metrics": now["metrics"],
            # ``.get`` 而非 ``[]``: update() 是**搬运**, 不该因调用方给的字典缺
            # 一个可选字段就 KeyError。缺了就写空分布 —— 后续 check() 会把当前
            # 每一章报成「新增待审」(fail-closed 方向), 而不是崩在写入这一步。
            # 真实路径上 measure() 一定带这个键, 这里只是不把「搬运」写成「断言」。
            "needs_review_by_section": now.get("needs_review_by_section") or {},
            "governed_sections": now["governed_sections"],
        }
    return {
        "version": 1,
        "note": "由 scripts/template_drift.py --update 生成; 人工审核后接受",
        **({"update_note": note} if note else {}),
        "entries": entries,
    }


def registered_models() -> list[str]:
    """注册表里在册的型号 —— 基线该覆盖谁由注册表说了算, 不由脚本里写死。

    走 :class:`Registry` 而不是自己读 YAML: 定位注册表的兜底规则 (配置路径 ->
    ``data/<basename>`` -> 报错) 只在那一处实现, 自己读一遍就等于复制一份,
    而复制的那份会以「注册表为空」的形式静默失败。
    """
    from aterag.registry import Registry

    return sorted(Registry.load(get_settings()).products)


def build_parser() -> argparse.ArgumentParser:
    """单独成函数而不是内联在 ``main`` 里: 参数解析不该与执行逻辑缠在一起,
    否则「直接调 main() 做一次检查」就必须先伪造 sys.argv。"""
    ap = argparse.ArgumentParser(description="模板漂移门禁 (辅助工具: 抽取过程本身已 fail-closed)")
    ap.add_argument(
        "--baseline", default=BASELINE_PATH, help=f"基线文件路径 (默认 {BASELINE_PATH})"
    )
    ap.add_argument("--model", action="append", help="只查该型号 (可重复); 默认查注册表在册型号")
    ap.add_argument("--blocks-dir", help="blocks 目录 (默认 rag_storage/blocks)")
    ap.add_argument("--update", action="store_true", help="接受当前状态并写基线")
    ap.add_argument("--note", help="随 --update 一起记录的变更说明 (给审计看)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    path = _baseline_path(args)
    baseline = load_baseline(path)
    models = args.model or registered_models()
    if not models:
        print("没有可检查的型号: 注册表为空或 --model 未指定")
        return 2

    if args.update:
        results = []
        for m in models:
            try:
                res = check(m, {"entries": {}}, blocks_dir=args.blocks_dir)
            except Exception as e:  # noqa: BLE001 更新时也要报清是哪个型号
                print(f"[{FAIL}] {m}: 无法测量 ({type(e).__name__}: {e})")
                return 2
            results.append(res)
        new = update(baseline, results, note=args.note or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            BASELINE_HEADER + yaml.safe_dump(new, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        print(f"基线已写入: {path}  ({len(results)} 个型号)")
        for r in results:
            now = r["current"]
            print(
                f"  {now['model_id']:14s} {now['template_id']} v{now['template_version']} "
                f"fp={now['fingerprint']}  {json.dumps(now['metrics'], ensure_ascii=False)}"
            )
        return 0

    counts = {"drift": 0, "no_baseline": 0, "unmeasurable": 0, "ok": 0}
    for m in models:
        try:
            res = check(m, baseline, blocks_dir=args.blocks_dir)
        except Exception as e:  # noqa: BLE001 测量失败不能当成「无漂移」
            print(f"[{FAIL}] {m}: 无法测量 ({type(e).__name__}: {e})")
            counts["unmeasurable"] += 1
            continue
        render(res)
        counts[res["status"]] += 1

    print("\n" + "=" * 64)
    if counts["unmeasurable"]:
        # 测量失败不能当成「无漂移」—— 那会把「查不了」报成「没问题」。
        print(
            f"TEMPLATE_DRIFT ERROR ({counts['unmeasurable']} 个型号无法测量; 测量失败不等于无漂移)"
        )
        return 2
    if counts["drift"]:
        print(f"TEMPLATE_DRIFT FAIL ({counts['drift']}/{len(models)} 个型号有漂移)")
        if counts["no_baseline"]:
            print(f"  {WARN} {counts['no_baseline']} 个型号没有基线 (未参与漂移判定)")
        return 1
    if counts["no_baseline"]:
        print(
            f"TEMPLATE_DRIFT PASS ({len(models) - counts['no_baseline']}/{len(models)} 个型号无漂移; "
            f"{counts['no_baseline']} 个无基线未判定)"
        )
        return 0
    print(f"TEMPLATE_DRIFT PASS ({len(models)} 个型号无漂移)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
