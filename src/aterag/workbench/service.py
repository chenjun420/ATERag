"""工作台服务层 (P1b) —— 向导调用的评审读写操作。

与闸 (gate.py) 的分工:
    gate.py    只管"能不能写、怎么写", 不知道业务概念
    本模块     只管"评审什么", 不碰文件写入, 全部经由闸

所有读操作都重新跑一次抽取, 不缓存 —— 抽取是确定性的, 重跑成本低,
而缓存会在配置变更后给出过期结果, 让评审基于旧状态做决定, 那是评审系统
最危险的一种错误: 人签字签的是 A, 系统里生效的却是 B。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aterag.extract import (
    PatternBook,
    ProfileBook,
    extract_test_conditions,
    load_annotations,
)
from aterag.extract.models import STATUS_APPROVED, ReviewItem
from aterag.workbench.gate import WritePolicy, WriteReceipt, approve_entry


@dataclass(frozen=True, slots=True)
class ReviewTask:
    """一条待人工处置的任务 (向导列表项)。"""

    task_id: str
    kind: str
    req_id: str
    title: str
    section_path: str
    detail: str
    hint: str
    #: 该任务若批准, 应写入哪个文件/段/键
    target_file: str = ""
    target_section: str = ""
    target_key: str = ""
    method_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "req_id": self.req_id,
            "title": self.title,
            "section_path": self.section_path,
            "detail": self.detail,
            "hint": self.hint,
            "target_file": self.target_file,
            "target_section": self.target_section,
            "target_key": self.target_key,
            "method_ref": self.method_ref,
        }


@dataclass(frozen=True, slots=True)
class WorkbenchSnapshot:
    """一次抽取的全量快照 (向导首屏用)。"""

    model_id: str
    doc_version: str
    stats: Mapping[str, Any] = field(default_factory=dict)
    conditions: list[Any] = field(default_factory=list)
    scenarios: list[Any] = field(default_factory=list)
    excluded: list[Any] = field(default_factory=list)
    assessments: list[Any] = field(default_factory=list)
    tasks: list[ReviewTask] = field(default_factory=list)

    def to_dict(self, *, detail: bool = True) -> dict[str, Any]:
        """detail=False 时只回统计与任务清单, 不回全量明细。

        供向导首屏使用: 95 条需求 x 190 个场景的全量 JSON 有数百 KB,
        首屏渲染不需要, 也不该让浏览器一次性解析。
        """
        out: dict[str, Any] = {
            "model_id": self.model_id,
            "doc_version": self.doc_version,
            "stats": dict(self.stats),
            "task_count": len(self.tasks),
            "tasks": [t.to_dict() for t in self.tasks],
        }
        if detail:
            out["conditions"] = [c.to_dict() for c in self.conditions]
            out["scenarios"] = [s.to_dict() for s in self.scenarios]
            out["excluded"] = [e.to_dict() for e in self.excluded]
            out["assessments"] = [
                a.to_dict() if hasattr(a, "to_dict") else a for a in self.assessments
            ]
        return out


class Workbench:
    """工作台门面。"""

    def __init__(
        self,
        repo_root: Path,
        *,
        annotations_rel: str = "config/annotations",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.annotations_rel = annotations_rel
        self.profiles = ProfileBook.load(self.repo_root / "config" / "doc_profiles.yaml")
        self.patterns = PatternBook.load(self.repo_root / "config" / "condition_patterns.yaml")

    # ---------- 读 ----------

    def snapshot(
        self, model_id: str, doc_version: str = "B", *, detail: bool = True
    ) -> WorkbenchSnapshot:
        """跑一次抽取并组装快照。"""
        ann_path = self._annotation_path(model_id)
        ann = load_annotations(str(ann_path)) if ann_path.exists() else None
        result = extract_test_conditions(
            model_id,
            doc_version=doc_version,
            profiles=self.profiles,
            patterns=self.patterns,
            annotations=ann,
        )
        return WorkbenchSnapshot(
            model_id=result.model_id,
            doc_version=result.doc_version,
            stats=result.stats,
            conditions=list(result.conditions),
            scenarios=list(result.scenarios),
            excluded=list(result.excluded),
            assessments=list(result.assessments),
            tasks=[self._as_task(it) for it in result.needs_review],
        )

    def _annotation_path(self, model_id: str) -> Path:
        return self.repo_root / self.annotations_rel / f"{model_id}.conditions.yaml"

    @staticmethod
    def _req_id_of(item: ReviewItem) -> str:
        """从待审详情里取需求编号。

        不做正则猜测: 详情串格式会随 hint 变化, 猜错就把批准写到别的需求上。
        取不到就留空, 由上层决定是否可批准。
        """
        for token in str(item.detail or "").split():
            if "-" in token and token[:2].isupper() and token[2:3].isdigit():
                return token
        return ""

    def _as_task(self, item: ReviewItem) -> ReviewTask:
        req_id = self._req_id_of(item)
        return ReviewTask(
            task_id=f"{item.kind}:{req_id or item.section_path}:{item.method_ref or ''}",
            kind=item.kind,
            req_id=req_id,
            title=item.heading,
            section_path=item.section_path,
            detail=item.detail,
            hint=item.hint,
            method_ref=item.method_ref,
        )

    def tasks(self, model_id: str, doc_version: str = "B") -> list[ReviewTask]:
        return self.snapshot(model_id, doc_version, detail=False).tasks

    # ---------- 写 ----------

    def policy(self) -> WritePolicy:
        """可写策略: 只放行注记文件。

        注意白名单里没有 table_schemas / condition_patterns / doc_profiles ——
        那三份是抽取规则本身, 改动它们等价于改变全量抽取语义, 必须走人工
        评审 + git PR, 不能由一个评审端点顺手改掉。
        """
        return WritePolicy(
            repo_root=self.repo_root,
            allowed_rel=frozenset(
                p.relative_to(self.repo_root).as_posix()
                for p in (self.repo_root / self.annotations_rel).glob("*.yaml")
            ),
        )

    def approve(self, model_id: str, req_id: str, actor: str, *, reason: str = "") -> WriteReceipt:
        """批准一条注记 (签字)。理由可选但会记入条目, 便于日后追溯。"""
        rel = self._annotation_path(model_id).relative_to(self.repo_root).as_posix()
        extra: dict[str, Any] = {}
        if reason:
            extra["approval_reason"] = reason
        return approve_entry(self.policy(), rel, "entries", req_id, actor, extra=extra or None)


def draft_clause_count(conditions: Sequence[Any]) -> int:
    """统计未批准子句数 —— 供向导顶部常驻红点。"""
    return sum(
        1
        for c in conditions
        for cl in (*getattr(c, "input_conditions", []), *getattr(c, "output_conditions", []))
        if getattr(cl, "status", STATUS_APPROVED) != STATUS_APPROVED
    )
