"""把 ``calculate`` 的推理链写进 ``l0_term.provenance``。

**为什么要写**: :class:`~aterag.inference.engine.InferenceEngine` 早就有
``DecisionTrace``, 但它只在进程内存里 —— 进程退出就没有。审计答案是
「推理发生过, 但查不到」。而种子侧的谱系(``load_seed_provenance``)已经
落库(1173 行 provenance / 75 条 trace)。推理侧不落库, 谱系就只有
「知识从哪来」, 没有「推理用了什么知识」。

**为什么走 ProvenanceManager 而不是自己写 psycopg**: 谱系表的 schema、
checksum 链、credibility JSONB 列映射、幂等语义都已经在
:class:`aterag.provenance.pg_storage.PGProvenanceStorage` 里维护(种子装载
用的就是它)。推理记录走同一个 manager, 意味着同一套校验与查询入口,
不出现第二份写入逻辑 —— 双份写入逻辑就是双源。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aterag.provenance.pg_storage import PROV_SCHEMA, PGProvenanceStorage
from aterag.provenance.seed_loader import build_manager


def datetime_now_iso() -> str:
    return datetime.now(UTC).isoformat()


#: 决策谱系记录的 ``entity_type``。不用 ``entity``: 查询入口按类型筛,
#: 混进种子实体的类型池会让 ``retrieve_all(entity_type=...)`` 也拉到这些行。
DECISION_TYPE = "decision"


def decision_entity_id(decision_id: str) -> str:
    """谱系里的 entity_id。加 ``dec:`` 前缀是为了与种子实体 id 空间**可区分**
    而不是可混淆 —— 种子 id 是 ``HYSTERESIS`` / ``SR-1201`` 这类, 万一将来
    产测程序也拿 uuid4 记 id, 不加前缀就会撞。"""
    return f"dec:{decision_id}"


class DecisionRecorder:
    """``calculate`` -> 谱系的通道。构造一次, 每次计算自动记账。

    ``manager`` 是 :func:`aterag.provenance.seed_loader.build_manager` 构造的
    ``ProvenanceManager``。这里**不**构造它: 连接生命周期归调用方, 推理引擎
    只持引用 —— 否则每条 calculate 开一个连接, 而板卡实测的教训正是
    读路径每读一次泄漏一个事务会让写入卡死在
    ``wait=Lock/transactionid``。
    """

    def __init__(self, manager: Any, agent: str = "aterag.inference.engine"):
        self._manager = manager
        self._agent = agent

    def record(self, result: dict[str, Any]) -> str:
        """把 ``InferenceEngine.calculate`` 的返回值记进谱系, 返回 entity_id。

        **记录失败必须炸, 不静默吞掉**: 推理不落谱系等于「审计缺口」, 而调用
        方拿到的是一个看起来成功的结果 —— 与 ``ConflictDetector`` 缺 ``source``
        时把 document 记成 ``"unknown"`` 是同一类静默降质。所以这里 re-raise,
        让 calculate 整体失败(算得出但不记录 = 没算)。
        """
        entity_id = decision_entity_id(str(result["decision_id"]))
        source = dict(result.get("source") or {})
        # 出处可定位性: url 优先(开放的 A 级平台可直接核), 其次名称。
        locator = source.get("url") or source.get("name") or "unknown"

        meta: dict[str, Any] = {
            # 冲突消解**只用 credibility**(红线)。种子谱系按
            # ``authority_kind -> CREDIBILITY_BY_AUTHORITY`` 分档; 领域规则
            # 没带 authority_kind, 但 rules.yaml 头部已定义它的 ``confidence``
            # 就是「出处可信度」, 与种子同一量纲 —— 沿用它, 不另发明分数。
            "credibility": result.get("confidence"),
            "confidence": result.get("confidence"),
            # 显式落库「可信度未知」这个事实, 而不是让 None 在查询端被当地 0
            "confidence_unknown": bool(result.get("confidence_unknown", True)),
            "rule_id": result.get("rule_id"),
            "statement": result.get("statement"),
            "output": result.get("output"),
            # 值可能是没法 JSON 序列化的类型; metadata 落 JSONB, 用 repr
            # 保证总能写成可读文本 —— 丢精度可接受, 丢记录不可接受。
            "inputs": {k: repr(v) for k, v in (result.get("inputs") or {}).items()},
            "value": repr(result.get("value")),
            "domain_layer": result.get("domain_layer"),
            "source_name": source.get("name"),
            # 审计链: 每个表达式输入 <- 它在型号事实里的 SR 条目。
            # 没有对应条目的输入显式标 "(caller)"":
            #「这个输入是调用方给的, 不在型号事实里」, 不留空让人猜。
            "input_sources": result.get("input_sources") or {},
        }
        ak = source.get("authority_kind")
        if ak:
            meta["authority_kind"] = ak
        entry = self._manager.track_entity(
            entity_id=entity_id,
            source=locator,
            entity_type=DECISION_TYPE,
            agent_id=self._agent,
            agent_type="software_agent",
            is_automated=True,
            activity_started_at_time=result.get("activity_started_at_time")
            or datetime_now_iso(),
            metadata=meta,
        )
        if entry is None:
            raise RuntimeError(f"provenance 记录返回空: {entity_id}")
        return entity_id


def get_decision_provenance(dsn: str, decision_id: str) -> dict[str, Any] | None:
    """按 decision_id 查谱系。独立函数而不是挂在 Recorder 上: 查询是
    只读操作, 不应该要求调用方先构造一个带连接的 recorder 实例。"""
    storage = PGProvenanceStorage(dsn)
    try:
        entry = storage.retrieve(decision_entity_id(decision_id))
    finally:
        storage.close()
    if entry is None:
        return None
    return entry.__dict__


__all__ = [
    "DECISION_TYPE",
    "DecisionRecorder",
    "PROV_SCHEMA",
    "build_manager",
    "decision_entity_id",
    "get_decision_provenance",
]
