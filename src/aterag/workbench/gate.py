"""工作台写入闸 (P1b) —— 管理面唯一允许的落盘通道。

为什么要有独立的"闸"
--------------------
评审签字要写 YAML, 而 YAML 是入库代码、由 git 审计。把这件事做成 HTTP 端点
就等于把仓库写权限暴露给了网络请求 —— 一次路径穿越或参数拼接失误, 就可能
改掉抽取规则本身, 让后续所有抽取结果悄无声息地变样。这个闸把可写范围、
写入方式和提交粒度全部固定下来, 端点只负责"声明意图", 不直接碰文件。

三条硬约束 (违反即抛, 不降级)
--------------------------------
1. **白名单路径**: 只有落在 allowed 内的文件可写。路径先规范化再比对,
   ``..``、绝对路径、符号链接一律拒绝 —— 比对前不规范化等于没比。
2. **单文件单提交**: 一次批准只产生一个 commit。批量写多个文件会让审计
   失真: 出问题时无法判断是哪一项批准导致的, 也无法单独回滚。
3. **内容形状校验**: 写之前先解析回 YAML 确认仍是合法映射, 防止把
   截断/损坏的内容提交进仓库(那会让下次启动直接加载失败)。

不做的事
--------
* 不自动 push。提交到本地 git 即止 —— 推送时机由人决定。
* 不改写已有条目的内容, 只允许按 key 更新受控字段(见 ALLOWED_FIELDS)。
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class WriteNotAllowed(PermissionError):
    """请求写入白名单外的路径。"""


class WriteRejected(ValueError):
    """请求合法但内容形状不对, 或请求改动受保护字段。"""


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """可写路径策略。

    Attributes:
        repo_root: 仓库根, 所有路径在其下解析
        allowed_rel: 允许写入的相对路径(精确匹配, 不支持通配)
        commit_message_template: commit 消息模板
    """

    repo_root: Path
    allowed_rel: frozenset[str]
    commit_message_template: str = "chore(review): 批准 {key} ({actor})"
    protected_fields: frozenset[str] = frozenset({"id", "fingerprint"})

    def resolve(self, rel: str) -> Path:
        """把请求路径解析为仓库内绝对路径, 并校验在白名单内。

        顺序很关键: 先 resolve 再比对相对路径。若先比对字符串, ``a/../b``
        这类写法就能骗过白名单。
        """
        if not rel or rel.strip() != rel:
            raise WriteNotAllowed(f"路径含首尾空白, 拒绝: {rel!r}")
        if Path(rel).is_absolute():
            raise WriteNotAllowed(f"只接受仓库内相对路径, 拒绝绝对路径: {rel}")
        root = self.repo_root.resolve()
        target = (root / rel).resolve()
        # 逃逸检测: 解析后必须仍在仓库根之下
        try:
            target.relative_to(root)
        except ValueError as e:
            raise WriteNotAllowed(f"路径逃逸出仓库: {rel}") from e
        norm = target.relative_to(root).as_posix()
        if norm not in self.allowed_rel:
            raise WriteNotAllowed(f"不在可写白名单内: {norm} (允许: {sorted(self.allowed_rel)})")
        return target

    def check_shape(self, doc: Mapping[str, Any], key: str, section: str = "") -> None:
        """写前校验: 必须是可回读的映射, 且 section 下的 key 存在。

        section 不可省: 注记文件的结构是 ``entries: {需求编号: {...}}``,
        需求编号不在根节点上。若按根节点找 key, 合法请求会被误判为
        "不存在", 而更糟的是将来若某个 key 恰好与根节点某个字段重名,
        就会写到错误的位置上去。
        """
        if not isinstance(doc, Mapping):
            raise WriteRejected("配置根节点必须是映射")
        container: Any = doc
        if section:
            if section not in doc:
                raise WriteRejected(f"段不存在: {section}")
            container = doc[section]
            if not isinstance(container, Mapping):
                raise WriteRejected(f"段必须是映射: {section}")
        if key not in container:
            raise WriteRejected(f"待更新项不存在: {section + '.' if section else ''}{key}")
        if not isinstance(container[key], Mapping):
            raise WriteRejected(f"待更新项必须是映射: {key}")


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """一次写入的回执 —— 审计凭据, 必须能被外部验证。"""

    rel_path: str
    key: str
    actor: str
    commit: str
    changed_fields: tuple[str, ...]
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "key": self.key,
            "actor": self.actor,
            "commit": self.commit,
            "changed_fields": list(self.changed_fields),
            "at": self.at,
        }


def _run_git(repo: Path, *args: str) -> str:
    """执行 git 命令并返回 stdout; 失败抛错(不吞 —— 静默失败会让审计说谎)。"""
    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def repo_is_clean(repo: Path) -> bool:
    """工作区是否干净。

    批准前必须干净: 否则 commit 会把评审人尚未保存的改动一起带走,
    审计上就分不清哪部分变化来自本次批准。
    """
    return not _run_git(repo, "status", "--porcelain")


def approve_entry(
    policy: WritePolicy,
    rel_path: str,
    section: str,
    key: str,
    actor: str,
    extra: Mapping[str, Any] | None = None,
) -> WriteReceipt:
    """批准一个条目: 翻转状态 + 记签字人, 写盘并单文件提交。

    Args:
        policy: 写入策略
        rel_path: 相对路径(必须在白名单内)
        section: 顶层段名(如 entries / review_dispositions)
        key: 条目键(通常是需求编号)
        actor: 签字人
        extra: 追加字段(如 reason), 不得触碰受保护字段

    Returns:
        WriteReceipt: 含 commit 号, 供审计与回滚定位。
    """
    if not actor or not actor.strip():
        raise WriteRejected("必须提供签字人(actor), 空白签字无审计价值")
    target = policy.resolve(rel_path)
    repo = policy.repo_root.resolve()

    header = (
        "# 本文件由工作台评审写入, 改动会进入 git 审计。\n"
        "# 手工编辑同样有效, 但请保持字段结构以便机器读写。\n"
    )
    raw = target.read_text(encoding="utf-8") if target.exists() else header
    doc = yaml.safe_load(raw) or {}
    policy.check_shape(doc, key, section)

    container: Any = doc[section] if section else doc
    entry = dict(container[key])
    protected_hit = policy.protected_fields & set(extra or {})
    if protected_hit:
        raise WriteRejected(f"拒绝修改受保护字段: {sorted(protected_hit)}")

    changed: list[str] = []
    for field_name, value in (extra or {}).items():
        if entry.get(field_name) != value:
            entry[field_name] = value
            changed.append(field_name)
    if "status" in entry and entry["status"] != "approved":
        entry["status"] = "approved"
        changed.append("status")

    now = datetime.now().astimezone()
    for fname, fval in (("approved_by", actor.strip()), ("approved_at", now.date().isoformat())):
        if entry.get(fname) != fval:
            entry[fname] = fval
            changed.append(fname)

    if not changed:
        # 无实际变化时不产生 commit —— 否则每次点"批准"都会多一条空提交,
        # 久而久之审计历史里全是噪音, 真正的批准反而被淹没。
        raise WriteRejected("该条目已处于目标状态, 无需重复批准")

    container[key] = entry
    if section:
        doc[section] = container
    target.write_text(
        header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # 写后回读校验: 确认落盘内容仍可解析且 key 还在 section 下。
    verify = yaml.safe_load(target.read_text(encoding="utf-8"))
    v_container = verify.get(section, verify) if isinstance(verify, Mapping) else None
    if not isinstance(v_container, Mapping) or key not in v_container:
        raise WriteRejected("写后回读校验失败, 已中止提交")

    rel = target.relative_to(repo).as_posix()
    _run_git(repo, "add", "--", rel)
    msg = policy.commit_message_template.format(key=key, actor=actor.strip())
    commit = _run_git(repo, "commit", "-m", msg, "--", rel)
    return WriteReceipt(
        rel_path=rel,
        key=key,
        actor=actor.strip(),
        commit=commit[:12],
        changed_fields=tuple(changed),
        at=now.isoformat(timespec="seconds"),
    )
