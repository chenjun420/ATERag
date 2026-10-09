"""``ProvenanceStorage`` 的 PostgreSQL 后端。

为什么必须写这个
----------------
Semantica 自带的后端只有 :class:`~semantica.provenance.storage.InMemoryStorage`
与 :class:`~semantica.provenance.storage.SQLiteStorage`。用 SQLite 的话,
知识在 PG 而谱系在 SQLite —— 两份数据两个事务, 且**谱系侧写失败不会让知识
侧回滚**。追溯审计是本项目的硬要求(§1.6 原则六), 所以谱系必须与知识同库,
同事务。

因此这里实现 :class:`ProvenanceStorage` 的 5 个抽象方法 + 2 个需要正确
语义的钩子(``get_chain_head`` / ``transaction``), 让
:class:`~semantica.provenance.manager.ProvenanceManager` 直接用 PG 后端 ——
**复用上游的链维护、校验、导出、审计逻辑**, 而不是自己重写一遍。

PG 与 SQLite 的三处实质差异
---------------------------
1. **并发**: SQLite 后端靠 ``BEGIN IMMEDIATE`` 串行化「读链头 -> 加 1 ->
   写」这个读-改-写序列(它的 docstring 明说了这一点)。PG 没有等价物, 所以
   插入前取 ``pg_advisory_xact_lock``; 再加 ``uq_provenance_sequence`` 唯一
   索引兜底 —— 竞态变成约束冲突(响亮), 而不是两条同号记录让
   ``verify_chain()`` 静默判错。
2. **锁的生命周期**: ``pg_advisory_xact_lock`` 在事务结束时自动释放, 所以
   必须在**事务内**取。``store()`` 若在事务外调用, 自己开一个短事务。
3. **类型**: ``used_entities`` / ``informed_by_activities`` 是 ``TEXT[]``,
   ``meta`` 是 ``JSONB``, 时间戳一律 TEXT(上游就是 ISO 字符串, 转
   ``TIMESTAMPTZ`` 会在跨时区比较时引入本不该有的偏移)。

不实现的
--------
``trace_descendants`` 用 ABC 提供的默认实现 —— 它只依赖 ``retrieve_all()``,
正确性有保证(ABC docstring 明说 "Correct for any backend")。数据量到需要
索引查询时再 override, 现在 override 只会写一份「看起来更快、其实没测过」的
SQL。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from semantica.provenance.schemas import ProvenanceEntry
from semantica.provenance.storage import ProvenanceStorage

#: 谱系表的 schema。领域知识是跨型号共享的, 所以放 L0(§6.2), 不进型号 schema。
PROV_SCHEMA = "l0_term"

#: 追加谱系时取的咨询锁键。**必须全库唯一且稳定** —— 换了它, 两个版本的
#: 服务会各锁各的, 竞态照旧。用一个固定常量而不是 hash(表名), 因为表名
#: 将来若被重命名, 换锁键会让滚动升级期间的两个进程互相不阻塞。
_APPEND_LOCK_KEY = 0x0A7E_6003  # "aterag" + 谱系

#: ProvenanceEntry 的字段 -> 列名。刻意显式列出而不是用 ``asdict``:
#: dataclass 字段名(``recorded_at`` 对 ``timestamp``)与列名不同, 而且
#: asdict 会把将来新增的字段一并带进来 —— 那是静默写进不存在的列的好办法。
_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity_id", "entity_id"),
    ("entity_type", "entity_type"),
    ("activity_id", "activity_id"),
    ("agent_id", "agent_id"),
    ("agent_type", "agent_type"),
    ("is_automated", "is_automated"),
    ("role", "role"),
    ("source_document", "source_document"),
    ("source_location", "source_location"),
    ("source_quote", "source_quote"),
    ("timestamp", "recorded_at"),
    ("first_seen", "first_seen"),
    ("last_updated", "last_updated"),
    ("confidence", "confidence"),
    ("credibility", "credibility"),
    ("checksum", "checksum"),
    ("sequence_id", "sequence_id"),
    ("previous_checksum", "previous_checksum"),
    ("parent_entity_id", "parent_entity_id"),
    ("used_entities", "used_entities"),
    ("previous_version_id", "previous_version_id"),
    ("derived_from_id", "derived_from_id"),
    ("activity_started_at_time", "activity_started_at_time"),
    ("activity_ended_at_time", "activity_ended_at_time"),
    ("acted_on_behalf_of", "acted_on_behalf_of"),
    ("informed_by_activities", "informed_by_activities"),
    ("valid_from", "valid_from"),
    ("valid_until", "valid_until"),
    ("revision_type", "revision_type"),
    ("supersedes", "supersedes"),
    ("bundle_id", "bundle_id"),
    ("invalidated", "invalidated"),
    ("invalidated_at_time", "invalidated_at_time"),
    ("invalidated_by", "invalidated_by"),
    ("invalidation_reason", "invalidation_reason"),
    ("start_index", "start_index"),
    ("end_index", "end_index"),
    ("metadata", "meta"),
    ("version", "prov_schema_version"),
)

_COLUMNS: tuple[str, ...] = tuple(col for _f, col in _FIELDS)

#: 需要 JSON 编码的字段。**按 dataclass 字段名索引**, 因为 ``_to_param``
#: 收到的是字段名 —— 早先按列名(``meta``)索引, 于是 ``metadata`` 这个字段
#: 没被编码, dict 原样进了 JSONB 列, 运行时才炸。
_JSONB_FIELDS = frozenset(fld for fld, col in _FIELDS if col == "meta")

#: TEXT[] 字段。
_ARRAY_FIELDS = frozenset(
    fld for fld, col in _FIELDS if col in ("used_entities", "informed_by_activities")
)

#: 列侧的同款集合, 给 ``_to_entry`` 用(它拿到的是行, 按列名索引)。
#: 两组都由 ``_FIELDS`` 推导, 所以加列时不会漏改。
_JSONB_COLUMNS = frozenset(col for _f, col in _FIELDS if col == "meta")
_ARRAY_COLUMNS = frozenset(
    col for _f, col in _FIELDS if col in ("used_entities", "informed_by_activities")
)

#: NUMERIC 列 —— 读回来必须转 float, 否则 ``Decimal`` 与 float 算出的
#: checksum 不同, 链会被误判为断(见 ``_to_entry`` 里的说明)。
_NUMERIC_COLUMNS = frozenset({"confidence", "credibility", "prov_schema_version"})

#: float -> NUMERIC(4,3)。四舍五入到 3 位小数: 列定义只留 3 位, 不显式舍入
#: 的话 psycopg 会按列精度静默截断, 于是「读回来与写进去的不等」,
#: 而 verify_chain 的 checksum 是覆盖不到数值列的。
_NDIGITS = 3


class PGProvenanceStorage(ProvenanceStorage):
    """把谱系写进 PostgreSQL 的 ``l0_term.provenance``。

    :param dsn: PostgreSQL DSN。
    :param connect_factory: 建连接的工厂。**只为测试可注入** —— 单测要能
        拿到假连接, 而真连 PG 的测试需要真库。生产路径留 None 走 psycopg。
    :param table: 表名(默认 ``provenance``)。留这个参数是因为
        :func:`aterag.storage.schema.quote_ident` 那套白名单校验要求调用方
        显式给出受控名字, 不接受字符串拼接。
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect_factory: Any = None,
        table: str = "provenance",
    ) -> None:
        # 白名单校验: 表名进 SQL 标识符位置, 而 PostgreSQL 不支持标识符占位符。
        # 与 aterag.storage.schema.quote_ident 同一套字符集。
        if not table.replace("_", "").isalnum() or not table[0].isalpha():
            raise ValueError(f"表名不合法: {table!r}")
        self._dsn = dsn
        self._connect = connect_factory or _psycopg_connect
        self._table = f"{PROV_SCHEMA}.{table}"
        # 每个线程一条连接: psycopg 的连接不是线程安全的, 而 ProvenanceManager
        # 会在 to_thread 里被调(Explorer 的路由就那么干)。
        self._local = threading.local()

    # ---------------- 连接 ----------------

    def _conn(self) -> Any:
        c = getattr(self._local, "conn", None)
        if c is None or getattr(c, "closed", False):
            c = self._connect(self._dsn)
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            finally:
                self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """开一个事务, 并在整个事务期间持有谱系追加锁。

        锁取在**事务内**: ``pg_advisory_xact_lock`` 随事务结束自动释放, 所以
        「先加锁再开事务」等于没加。

        读路径**不走这里** —— 它们靠连接级的 ``autocommit=True``(见
        :func:`_psycopg_connect`), 所以不会留下打开的事务。
        """
        conn = self._conn()
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_APPEND_LOCK_KEY,))
            yield conn

    @contextmanager
    def savepoint(self, conn: Any = None) -> Iterator[Any]:
        """嵌套 savepoint。没给 conn 就退化成完整事务 —— 与 SQLiteStorage 同语义。"""
        if conn is None:
            with self.transaction() as tx:
                yield tx
            return
        name = "sp_prov"
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except Exception:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            raise
        else:
            with conn.cursor() as cur:
                cur.execute(f"RELEASE SAVEPOINT {name}")

    # ---------------- 写 ----------------

    def store(self, entry: ProvenanceEntry) -> None:
        with self.transaction() as conn:
            self._store_with_conn(conn, entry)

    def _store_with_conn(self, conn: Any, entry: ProvenanceEntry) -> None:
        """把一条 entry 写入谱系表 —— **同一 entity_id 是替换, 不是追加**。

        这是与上游存储对齐的关键一处, 也是本层最初踩的坑。上游以
        ``entity_id`` 为主键(``compute_checksum`` 的注释原文: "entity_id is
        the storage primary key"), ``store()`` 对已存在的 entity_id 做替换。
        用纯 INSERT 替��的话, 同一个实体被装载两次就会留下两行:

            第一次装载  -> 行 A (entity_id=X, sequence_id=1042)
            第二次装载  -> 归档行 (entity_id='X:v:<ts>', sequence_id=1042)  # 设计如此
                        -> 行 B (entity_id=X, sequence_id=1043)            # 应该是替换 A

        板卡实测: 这样会多出一行, ``{sequence_id}`` 不再是连续的 ``{1..N}``,
        而 ``verify_chain()`` 严格检查 ``sequence_id == 前驱 + 1`` 且明确声明
        「archival relabels 总是保留原 sequence_id, 所以现存 sequence_id 集合
        恒等于 {1..N}」—— 于是它把同号的第二行判成 chain_break。

        对照组证明这是本层的问题而不是上游的: 同一份装载灌进上游自带的
        ``InMemoryStorage`` 与 ``SQLiteStorage`` 都是 ``valid=True / 0 断``。

        ``checksum`` / ``sequence_id`` / ``previous_checksum`` 由
        ``ProvenanceManager._save_entry`` 算好后带进来, 这里**不重算** ——
        重算会与 manager 内部的哈希对不上, 而 ``verify_chain()`` 拿的是
        manager 那份做比对。
        """
        params = {col: _to_param(entry, fld) for fld, col in _FIELDS}
        cols = ", ".join(_COLUMNS)
        marks = ", ".join(f"%({c})s" for c in _COLUMNS)
        # ON CONFLICT (entity_id) DO UPDATE: 替换除主键外的全部列。
        # 不用 DO NOTHING —— 同一实体重新装载时出处/置信度可能已变(种子重跑、
        # 修正表更新), DO NOTHING 会把旧值永久留下, 而谱系的价值就在于
        # 「当前这条是从哪来的」是准的。
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c != "entity_id")
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} ({cols}) VALUES ({marks}) "  # noqa: S608
                f"ON CONFLICT (entity_id) DO UPDATE SET {updates}",
                params,
            )

    def clear(self) -> int:
        """清空谱系。返回删除行数。

        **刻意不做「软删除」**: ``clear()`` 在上游语义里就是「丢掉全部」,
        它的调用方 (``ProvenanceManager.clear``) 已经先 ``audit_log`` 过。
        要保留可审计的撤回记录, 该用 ``invalidate()`` —— 它写墓碑而不是删行。
        """
        with self.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table}")  # noqa: S608
                return cur.rowcount

    # ---------------- 读 ----------------

    def get_chain_head(self, conn: Any = None) -> tuple[int, str] | None:
        """最近一条带 ``sequence_id`` 的 entry 的 ``(sequence_id, checksum)``。

        ``ORDER BY sequence_id DESC, prov_id DESC``: 后者是必要的平票裁决 ——
        ``track_entity`` 的归档路径会短暂地让历史行与即将被覆盖的现行行
        **共用同一个 sequence_id**(历史行是从现行行拷来的)。只按
        sequence_id 排序时选哪一行是实现定义的, 于是「链头」可能取到旧值,
        下一条就接到错误的前驱上。``prov_id`` 是单调的, 按它排能确定地取到
        最近写入的那一行(与 SQLiteStorage 用 rowid 的做法同理)。
        """
        sql = (
            f"SELECT sequence_id, checksum FROM {self._table} "  # noqa: S608
            f"WHERE sequence_id IS NOT NULL "
            f"ORDER BY sequence_id DESC, prov_id DESC LIMIT 1"
        )
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
        else:
            with self._conn().cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
        return (int(row[0]), row[1]) if row and row[1] is not None else None

    def retrieve(self, entity_id: str) -> ProvenanceEntry | None:
        """取该实体的**最新一条**谱系。

        只返回一条而不是全部: ``ProvenanceManager.get_provenance`` 的语义是
        「当前有效的那条」, 历史走 ``revision_history``。
        """
        sql = (
            f"SELECT {', '.join(_COLUMNS)} FROM {self._table} "  # noqa: S608
            f"WHERE entity_id = %s AND invalidated = false "
            f"ORDER BY sequence_id DESC NULLS LAST, prov_id DESC LIMIT 1"
        )
        with self._conn().cursor() as cur:
            cur.execute(sql, (entity_id,))
            row = cur.fetchone()
        return _to_entry(row) if row else None

    def retrieve_all(self, entity_type: str | None = None) -> list[ProvenanceEntry]:
        """全部谱系, 可按 ``entity_type`` 筛。按插入序返回。

        **包含已撤回的行**: 审计要能证明「这条存在过、被审过、被撤回」,
        默认过滤会让撤回等于消失(``invalidate()`` 写的墓碑就白写了)。
        """
        sql = f"SELECT {', '.join(_COLUMNS)} FROM {self._table}"  # noqa: S608
        params: list[Any] = []
        if entity_type:
            sql += " WHERE entity_type = %s"
            params.append(entity_type)
        sql += " ORDER BY sequence_id NULLS LAST, prov_id"
        with self._conn().cursor() as cur:
            cur.execute(sql, params)
            return [_to_entry(r) for r in cur.fetchall()]

    def trace_lineage(self, entity_id: str, max_depth: int | None = None) -> list[ProvenanceEntry]:
        """谱系链: 从 ``entity_id`` 出发, 沿 ``parent_entity_id`` /
        ``previous_version_id`` / ``derived_from_id`` 递归向上。

        向上走 = 「这条东西是从哪儿来的」, 这才是审计要的方向; 反向(谁由它
        推出)由 ABC 的 ``trace_descendants`` 负责。
        """
        seen: set[str] = set()
        collected: dict[str, ProvenanceEntry] = {}
        frontier = [entity_id]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            sql = (
                f"SELECT {', '.join(_COLUMNS)} FROM {self._table} "  # noqa: S608
                f"WHERE entity_id = ANY(%s) "
                f"ORDER BY sequence_id NULLS LAST, prov_id"
            )
            with self._conn().cursor() as cur:
                cur.execute(sql, (frontier,))
                rows = cur.fetchall()
            nxt: list[str] = []
            for row in rows:
                e = _to_entry(row)
                # 同一实体的多条谱系只保留第一次见到的(插入序最靠前的即最早),
                # 否则一条链会被同实体的每条 entry 重复展开。
                if e.entity_id in collected:
                    continue
                collected[e.entity_id] = e
                for parent in _parents_of(e):
                    if parent and parent not in seen:
                        seen.add(parent)
                        nxt.append(parent)
            frontier = nxt
            depth += 1
        return list(collected.values())

    def count(self) -> int:
        with self._conn().cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {self._table}")  # noqa: S608
            return int(cur.fetchone()[0])


# ---------------------------------------------------------------- 辅助


def _psycopg_connect(dsn: str) -> Any:
    """建一条 **autocommit** 连接。

    ``autocommit=True`` 不是偷懒, 是修一个实测出来的严重问题: psycopg3 在
    非 autocommit 模式下**第一条语句就隐式开事务, 且永不自 commit** ——
    除非你显式用 ``conn.transaction()`` 或 ``conn.commit()``。

    本模块的读方法(``retrieve`` / ``retrieve_all`` / ``trace_lineage`` /
    ``get_chain_head`` / ``count``)都只做 SELECT, 原来直接用缓存连接发语句,
    于是**每个读都留下一个打开的事务**。板卡上实测的后果:

        backend A: idle in transaction   (ClientRead)   <- 某个 SELECT 之后没提交
        backend B: active, wait=Lock/transactionid     <- 写入被它挡住

    即「查一次谱系」就能让后续写入无限等待, 而且 A 持有的 MVCC 快照会一直
    累积(bloat)。这不会自己暴露成报错, 只表现为「偶发卡住」。

    写路径不受影响: ``transaction()`` 在 autocommit 连接上会显式发
    BEGIN/COMMIT, 咨询锁也仍在事务内(见 :meth:`PGProvenanceStorage.transaction`)。
    """
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def _parents_of(entry: ProvenanceEntry) -> Sequence[str | None]:
    return (entry.parent_entity_id, entry.previous_version_id, entry.derived_from_id)


def _to_param(entry: ProvenanceEntry, field: str) -> Any:
    """dataclass 字段 -> 列参数, 按列类型做必要转换。``field`` 是**字段名**。"""
    val = getattr(entry, field, None)
    if field == "credibility" and val is None:
        # ``ProvenanceManager.track_entity`` **不接受** credibility 参数 ——
        # 上游只在 ``track_property_source`` 里从 ``SourceReference.metadata``
        # 取它(manager.py:604), 而那个入口的语义是「某个属性的出处」, 不是
        # 「这条事实的出处」, 与我们的用法(整条记录的来源可信度)不符。
        #
        # 所以由本层从 ``entry.metadata`` 提出来落到专列。为什么要专列而不是
        # 只留在 JSONB 里: 冲突消解只按 credibility 排序(ConflictResolver 的
        # voting/credibility 策略), 「取某概念全部来源按 credibility 降序」是
        # 它的核心查询 —— 埋在 JSONB 里就只能全表扫, 而 idx_prov_credibility
        # 正是为这个查询建的。
        meta = getattr(entry, "metadata", None) or {}
        val = meta.get("credibility")
    if field in _JSONB_FIELDS:
        return json.dumps(val or {}, ensure_ascii=False, default=str)
    if field in _ARRAY_FIELDS:
        return list(val or [])
    if field in ("confidence", "credibility") and val is not None:
        return round(float(val), _NDIGITS)
    return val


def _to_entry(row: Sequence[Any]) -> ProvenanceEntry:
    """行 -> :class:`ProvenanceEntry`。

    ``meta`` 从 JSONB 还原成 dict, ``TEXT[]`` 还原成 list, 数值列还原成 float
    —— 上游的 ``compute_checksum`` 是按 dataclass 字段算的, 类型不一致会让
    读回来的 checksum 与写入时不同, 而 ``verify_chain()`` 就会误报。
    """
    data = dict(zip(_COLUMNS, row, strict=True))
    for col in _JSONB_COLUMNS:
        raw = data.get(col)
        data[col] = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    for col in _ARRAY_COLUMNS:
        data[col] = list(data.get(col) or [])
    for col in _NUMERIC_COLUMNS:
        if data.get(col) is not None:
            # NUMERIC 列在 psycopg 里回来是 ``Decimal``。而
            # ``compute_checksum`` 按 dataclass 字段值算哈希,
            # ``Decimal('1.00')`` 与 ``1.0`` 算出的结果不同 —— 于是「写进去的链」
            # 与「读出来的链」对不上, ``verify_chain()`` 报的是 ``chain_break``
            # 而真正的原因是类型。实测正是这样: 归档行与现行行共享
            # sequence_id(上游设计), 但 checksum 不同, 链被判为断。
            data[col] = float(data[col])
    # 列名 -> dataclass 字段名
    return ProvenanceEntry(**{fld: data.get(col) for fld, col in _FIELDS})


__all__ = ["PGProvenanceStorage", "PROV_SCHEMA"]
