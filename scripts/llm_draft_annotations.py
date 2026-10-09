"""LLM 起草通道: 为「规则切不出的复合条件」生成注记草案 (方案 §4.2)。

这是「什么适合 LLM」在**离线**的落点。运行期抽取路径零 LLM —— 这条不破。

为什么不放在运行时
----------------
LLM 起草的价值在「人写不出来或懒得写」的那些条款 (SR-1213 的响应: 电压+电流+
时间+温度四个条件嵌套在一个单元格里)。而这类条款恰恰最需要人签字 —— 判据错了
下游会照着判。所以 LLM 只做初稿, 人签字才生效, 且草案落盘后**与运行时解耦**:
运行时不读 proposals/ 目录, 只读审核后的 ``data/annotations/*.conditions.yaml``。

单一来源 (红线 4)
----------------
提案不进 ``conditions.yaml``, 必须经 ``review_annotation.py --accept-proposals``
显式合并。若让抽取同时读两个文件, 就有了双源: 一条注记在两处都能改, 而运行时
用哪份取决于读了哪个 —— 副本漂移的起点。

原文可溯是硬要求
----------------
每个子句的 ``text`` 必须能在规格书原文里检索到, ``value`` 必须与原文一致。
这不是「最好有」而是**红线**: 编造的判据会一路流到产线, 而产线上没人会去核对
它对不对 —— 那正是判据存在的意义所在。无法溯源的子句直接丢弃并记入
``rejected``, 不进草案。

用法:
    .venv\\Scripts\\python.exe scripts/llm_draft_annotations.py -m PA601-D54A
    .venv\\Scripts\\python.exe scripts/llm_draft_annotations.py -m PA601-D54A --limit 5
    .venv\\Scripts\\python.exe scripts/llm_draft_annotations.py -m PA601-D54A --dry-run

退出码: 0 产出草案(或确认无需草案) / 1 有草案需要评审
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from aterag.config import get_settings  # noqa: E402
from aterag.extract import (  # noqa: E402
    PatternBook,
    ProfileBook,
    apply_sieve,
    load_blocks,
    row_fingerprint,
    rows_from_blocks,
    select_sections,
)
from aterag.extract.assembler import assemble  # noqa: E402

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

#: 子句溯源时忽略的标点与空白。原文单元格里常有 ``±`` ``,`` ``、`` 这类修饰,
#: LLM 抄写时可能去掉, 但语义没变 —— 要求逐字符相等会误杀真提案。
_TRACE_STRIP_RE = re.compile(r"[\s，,、;；:：()（）\[\]【】]+")

#: 判定「原文里确实有这句话」的最小长度。太短的片段 (``>=``、``5V``) 在任何技术
#: 文档里都能找到, 溯源通过等于没检查 —— 所以短片段直接判为不可溯。
MIN_TRACE_LEN = 6


def traceable(text: str, doc: str) -> bool:
    """子句是否能在规格书原文里找到。

    规范化两侧 (去标点空白) 后做子串包含。**不做**模糊匹配: 相似不等于支撑,
    而「看着像」正是编造最隐蔽的形态。
    """
    t = _TRACE_STRIP_RE.sub("", text or "")
    if len(t) < MIN_TRACE_LEN:
        return False
    return t in _TRACE_STRIP_RE.sub("", doc or "")


def value_in_doc(value: Any, doc: str) -> bool:
    """``value`` 里的数值必须与原文一致。

    逐个 token 查: value 是 ``{"typ": 54}`` 这类字典, 逐值核对比整体序列化更
    严格 —— 序列化里塞一句解释就能整体「通过」。
    """
    doc_flat = _TRACE_STRIP_RE.sub("", doc or "")
    vals = value.values() if isinstance(value, dict) else [value]
    for v in vals:
        if isinstance(v, list | tuple | dict):
            return False  # 嵌套结构没法逐 token 核 —— 拒绝而不是放过
        s = _TRACE_STRIP_RE.sub("", str(v))
        if not s:
            continue
        if s not in doc_flat:
            return False
    return True


def rows_needing_annotation(model_id: str, profile_name: str = "") -> list[dict]:
    """找出**规则切不出条件**的行 —— 那才是 LLM 该出手的地方。

    已经有规则结果的不进草案: 那类条款人写得比 LLM 快, 而且规则结果已经生效,
    用 LLM 版本覆盖它反而是无谓的风险。
    """
    settings = get_settings()
    prof = ProfileBook.load(settings.doc_profiles_path).get(profile_name or None)
    blocks = load_blocks(model_id)
    sel = select_sections(blocks, prof.section_keywords)
    rows, _, _ = rows_from_blocks(blocks, model_id, "", sel.section_prefixes)
    book = PatternBook.load(settings.condition_patterns_path)

    out: list[dict] = []
    kept = apply_sieve(rows, prof.exclude_words).kept
    for row in kept:
        prior = prof.prior_for(str(row.get("section_path", "")))
        asm = assemble(row, role=prior.role, limits_to=prior.limits_to, book=book, annotations=None)
        if asm.inputs or asm.outputs:
            continue  # 规则已经切出来了 —— 不需要人/LLM 写
        out.append(
            {
                "req_id": str(row.get("req_id", "")),
                "fingerprint": row_fingerprint(row),
                "section_path": str(row.get("section_path", "")),
                # 条目名优先 title: requirement_text/subject 是遥测表的列, 参数表
                # 里它们恒为 None —— 早先只读 requirement_text, 于是三条候选的
                # 条目名全是 "None", LLM 拿到的是没有主语的任务。
                "title": str(row.get("title", "")),
                "requirement_text": str(row.get("requirement_text", "")),
                "subject": str(row.get("subject", "")),
                "notes": str(row.get("notes", "")),
                "rail": str(row.get("rail", "")),
                "min": row.get("min"),
                "typ": row.get("typ"),
                "max": row.get("max"),
                "unit": str(row.get("unit", "")),
                "priority": str(row.get("priority", "")),
            }
        )
    return out


def _doc_text(model_id: str) -> str:
    """规格书原文 —— 溯源的唯一依据。读不到就返回空串 (此时全部不可溯)。"""
    for cand in (f"{model_id} 定制电源技术规格书.md", f"{model_id}.md"):
        p = Path(cand)
        if p.exists():
            return p.read_text(encoding="utf-8")
    specs = sorted(Path("specs").glob(f"{model_id}*.md")) if Path("specs").is_dir() else []
    return specs[0].read_text(encoding="utf-8") if specs else ""


def draft_prompt(row: dict, kinds: tuple[str, ...] | list[str]) -> str:
    kinds_list = ", ".join(sorted(kinds))
    # 条目名要落到一个实际字符串上: title / requirement_text / subject 任一有值
    # 都算。全空时明说, 而不是发一个 "None" 给模型 —— 那会让它自由发挥。
    name = row.get("title") or row.get("requirement_text") or row.get("subject") or "(无条目名)"
    return f"""你是电源产测规格书解析专家。下面这条需求**规则切不出产测条件**,
需要人写出激励与响应。请给出该条的输入条件(激励)与输出条件(响应)。

需求编号: {row["req_id"]}
章节: {row["section_path"]}
条目名: {name}
限值: min={row["min"]} typ={row["typ"]} max={row["max"]} 单位={row["unit"]}
原文备注: {row["notes"][:400]}

要求:
1. `kind` 只能从下列封闭词表里选, 不得自造:
   {kinds_list}
2. `text` 必须是**原文里出现过的话**, 不得改写或润色 —— 判据要能被产线照着执行,
   编造的描述比没有描述更危险。
3. `value` 只填原文明确给出的数值 (如 {{"typ": 54}}), 没有就给 {{}}。
4. 每个子句都要给 `reason`: 为什么这条是这个 kind。

只输出 JSON, 不要任何解释文字。示例:
{{"input": [{{"kind": "input_voltage", "text": "在输入 88Vac 下", "value": {{}}, "reason": "标称输入电压范围的下限"}}],
  "output": [{{"kind": "output_voltage", "text": "输出电压应为 54V±2%", "value": {{"typ": 54}}, "reason": "额定输出电压"}}],
  "overall_reason": "一条同时给激励与响应, 规则只切出前半"}}
"""


def _parse_llm_json(raw: str) -> dict:
    """从 LLM 回复里取出 JSON 对象 (容忍前后废话与 ```json 包裹)。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"回复里没有 JSON 对象: {raw[:200]}")
    return json.loads(text[start : end + 1])


def validate_draft(
    req_id: str, draft: dict, kinds: tuple[str, ...] | list[str], doc: str
) -> tuple[dict | None, list[str]]:
    """校验一份草案; 不可溯源的子句**丢弃**, 整条为空则返回 None。

    返回 ``(可接受的草案或 None, 被拒理由列表)``。丢弃而不整条拒绝是因为一个
    条目常有多个子句, 丢掉编造的那个不影响其余 —— 但**一个都不剩**就没意义了。
    """
    rejected: list[str] = []
    clean: dict[str, Any] = {"input": [], "output": []}
    for role in ("input", "output"):
        for i, spec in enumerate(draft.get(role) or [], 1):
            kind = str(spec.get("kind", ""))
            text = str(spec.get("text", ""))
            if kind not in kinds:
                rejected.append(f"{role}[{i}] kind={kind!r} 不在封闭词表内")
                continue
            if not traceable(text, doc):
                rejected.append(f"{role}[{i}] text 无法在原文溯源: {text[:50]!r}")
                continue
            value = spec.get("value") or {}
            if not value_in_doc(value, doc):
                rejected.append(f"{role}[{i}] value 与原文不一致: {value}")
                continue
            clean[role].append(
                {"kind": kind, "text": text, "value": value, "reason": str(spec.get("reason", ""))}
            )
    if not clean["input"] and not clean["output"]:
        return None, rejected
    return clean, rejected


def proposals_dir(annotations_dir: str = "") -> Path:
    """提案目录。

    在 ``data/annotations/proposals/`` 而不是 ``config/`` 下: 提案逐条对应某个客户
    型号的规格书条款, 与已签字注记同属**运行时数据**, 进版本库等于把客户判据
    连同需求编号一起公开 (同 ``config.py`` 里 annotations_dir 的理由)。

    ``annotations_dir`` 可显式传入 —— 合并路径 (``review_annotation
    --accept-proposals``) 要把提案目录与注记文件放在**同一个**根下, 各读各的
    Settings 会让两者在配置被改时悄悄分家。
    """
    root = annotations_dir or get_settings().annotations_dir
    return Path(root) / "proposals"


def write_proposals(model_id: str, entries: dict, *, rejected: dict[str, list[str]]) -> Path:
    """落草案。**绝不带 status: approved** —— 签字只能由 review_annotation 写。"""
    p = proposals_dir() / f"{model_id}.proposals.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 1,
        "model_id": model_id,
        "_proposal": {
            "status": "待人工评审",
            "generated_by": "scripts/llm_draft_annotations.py",
            "warning": (
                "本文件是 LLM 起草的**草案**, 未经人签字不得视为判据。"
                "合并: scripts/review_annotation.py -m "
                f"{model_id} --accept-proposals"
                "（合并后仍为 draft, 需再 --approve 签字才进 CI 门禁的绿灯）"
            ),
            "rejected_clauses": rejected,
        },
        "entries": entries,
    }
    p.write_text(
        "# LLM 起草的注记草案 —— 未经人签字, 不是判据\n"
        "# 由 scripts/llm_draft_annotations.py 生成; 运行时永不读本目录\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return p


def draft_all(model_id: str, *, limit: int = 0, dry_run: bool = False) -> dict:
    """主流程: 找待注记行 -> 调 LLM -> 校验 -> 落草案。返回统计。"""
    from aterag.models import LLMClient

    settings = get_settings()
    profile_name = ""
    try:
        from aterag.extract.api import model_profile_name

        profile_name = model_profile_name(model_id)
    except Exception:  # noqa: BLE001 没在册就用默认档案
        profile_name = ""
    targets = rows_needing_annotation(model_id, profile_name)
    if limit:
        targets = targets[:limit]
    doc = _doc_text(model_id)

    stats = {"candidates": len(targets), "accepted": 0, "rejected": 0, "errors": 0}
    if not targets:
        return stats
    if not doc:
        # 没有原文就无法溯源 -> 一个草案都不该出。此时继续跑等于批量造判据。
        print(f"{FAIL} 读不到 {model_id} 的规格书原文, 无法溯源 -> 不产出任何草案")
        print("     (LLM 起草的价值全在「原文可溯」这条红线上; 没有原文就只能编造)")
        return stats

    book = PatternBook.load(settings.condition_patterns_path)
    kinds = tuple(book.kinds)
    client = LLMClient(settings)
    entries: dict[str, dict] = {}
    rejected: dict[str, list[str]] = {}

    for row in targets:
        req_id = row["req_id"]
        try:
            raw = asyncio.run(
                client.chat(
                    [{"role": "user", "content": draft_prompt(row, kinds)}],
                    temperature=0.0,
                    max_tokens=800,
                )
            )
            parsed = _parse_llm_json(raw)
        except Exception as e:  # noqa: BLE001 单条失败不该中断整批
            stats["errors"] += 1
            rejected[req_id] = [f"LLM 调用或解析失败: {type(e).__name__}: {e}"]
            print(f"  {WARN} {req_id}: LLM 失败 ({type(e).__name__})")
            continue
        clean, why = validate_draft(req_id, parsed, kinds, doc)
        if clean is None:
            stats["rejected"] += 1
            rejected[req_id] = why or ["子句全部被拒 (无可溯源内容)"]
            print(f"  {FAIL} {req_id}: 子句全部不可溯源, 已丢弃")
            continue
        stats["accepted"] += 1
        if why:
            rejected[req_id] = why
            print(f"  {WARN} {req_id}: 丢弃 {len(why)} 个子句, 保留其余")
        entries[req_id] = {
            "status": "draft",
            "fingerprint": row["fingerprint"],
            "source": "llm_draft",
            "reason": str(parsed.get("overall_reason", ""))[:300],
            **clean,
        }
        print(f"  {PASS} {req_id}: 激励 {len(clean['input'])} 条 / 响应 {len(clean['output'])} 条")

    if dry_run:
        print(f"\n{FAIL} --dry-run: 未落盘 (共 {stats['accepted']} 条可用)")
        return stats
    if entries:
        p = write_proposals(model_id, entries, rejected=rejected)
        print(f"\n草案已写入: {p}  ({stats['accepted']} 条)")
        print(
            f"下一步: 人工核对 -> review_annotation.py -m {model_id} --accept-proposals "
            "-> 再 --show/--approve 签字"
        )
    else:
        print(f"\n{FAIL} 没有一条草案可用 (共 {stats['candidates']} 条候选)")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 起草注记草案 (离线; 运行期永不调 LLM)")
    ap.add_argument("-m", "--model", required=True, help="型号 ID")
    ap.add_argument("--limit", type=int, default=0, help="最多起草多少条 (0=不限)")
    ap.add_argument("--dry-run", action="store_true", help="只跑校验不落盘")
    ap.add_argument("--list-candidates", action="store_true", help="只列出规则切不出的行, 不调 LLM")
    args = ap.parse_args()

    if args.list_candidates:
        profile_name = ""
        try:
            from aterag.extract.api import model_profile_name

            profile_name = model_profile_name(args.model)
        except Exception:  # noqa: BLE001
            pass
        rows = rows_needing_annotation(args.model, profile_name)
        print(f"=== {args.model}: 规则切不出条件的行 {len(rows)} 条 ===")
        for r in rows:
            name = r["title"] or r["requirement_text"] or r["subject"] or "(无条目名)"
            print(f"  {r['req_id']}  [{r['section_path']}] {name[:50]}  备注: {r['notes'][:40]}")
        return 0

    stats = draft_all(args.model, limit=args.limit, dry_run=args.dry_run)
    if stats["candidates"] == 0:
        print(f"{PASS} 无需起草: 规则已覆盖全部条目")
        return 0
    return 1 if stats["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
