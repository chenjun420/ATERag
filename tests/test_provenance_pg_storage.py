"""``PGProvenanceStorage`` 的契约测试 —— 用假连接, 不连真库。

三件最容易被重构悄悄弄坏、且坏了不报错的事, 在这里钉住:

1. **字段映射双向完全一致**。列名与 dataclass 字段名大多不同
   (``timestamp`` -> ``recorded_at``、``metadata`` -> ``meta``), 多一个少一个
   都不会报错, 只会让那一列永远读成默认值 —— 谱系少一列, 审计看不出问题。
2. **checksum / sequence_id / previous_checksum 原样落库**。这三个由
   ``ProvenanceManager._save_entry`` 算好; 存储层若重算, 就与 manager 内部
   的哈希不一致, ``verify_chain()`` 报的是「链断了」而不是「哪一列错了」。
3. **回环谱系不死循环**。知识图谱里 A 由 B 推出、B 又引用 A 是可能的
   (定义与被定义互相引用), 递归必须靠已见集合收敛。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from semantica.provenance.manager import ProvenanceManager
from semantica.provenance.schemas import ProvenanceEntry
from semantica.provenance.storage import ProvenanceStorage

from aterag.provenance.pg_storage import _FIELDS, PGProvenanceStorage

# ---------------------------------------------------------------- 假连接


class FakeCursor:
    """记录 SQL 与参数, 并按调用脚本返回行。"""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self._rows: list[tuple] = []
        self.closed = False

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.statements.append((_norm(sql), params))
        self._rows = self._conn.next_rows(_norm(sql), params)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows

    @property
    def rowcount(self) -> int:
        return self._conn.deleted


class FakeTxn:
    def __enter__(self) -> FakeTxn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConn:
    """按 SQL 关键词分派的假连接。``rows_by_kind`` 决定各类查询的返回。"""

    def __init__(self, rows_by_kind: dict[str, list[tuple]] | None = None) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.rows_by_kind = rows_by_kind or {}
        self.deleted = 0
        self.closed = False
        #: ``transaction()`` 被调用的次数。读路径必须为 0 —— 真 PG 上那等于
        #: 一个 idle in transaction 的后端, 会挡住后续写入(实测过)。
        self.tx_started = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def transaction(self) -> FakeTxn:
        self.tx_started += 1
        return FakeTxn()

    def close(self) -> None:
        self.closed = True

    def next_rows(self, sql: str, params: Any) -> list[tuple]:
        for kind, rows in self.rows_by_kind.items():
            if kind in sql:
                return rows
        return []

    def inserts(self) -> list[tuple[str, Any]]:
        return [s for s in self.statements if s[0].startswith("insert into")]


def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


def _row(**over: Any) -> tuple:
    """造一条「行」: 先按列名铺满 None, 再覆盖给定列, 最后按列序成 tuple。

    单独抽出来是因为三处都要用, 而内联写 ``{c: None for c in cols} | {...}[c]
    for c in cols`` 会被优先级坑到(``[c]`` 绑到第二个字典上, 于是拿 str 去
    ``dict | str``)—— 这正是我第一版写错的地方。
    """
    cols = [c for _f, c in _FIELDS]
    vals: dict[str, Any] = {c: None for c in cols}
    vals.update(
        {
            "entity_id": "E",
            "entity_type": "t",
            "activity_id": "a",
            "agent_id": "g",
            "source_document": "",
            "recorded_at": "2026-01-01T00:00:00Z",
            "meta": "{}",
            "used_entities": [],
            "informed_by_activities": [],
            "is_automated": True,
            "invalidated": False,
        }
    )
    vals.update(over)
    return tuple(vals[c] for c in cols)


def _entry(entity_id: str = "E1", **kw: Any) -> ProvenanceEntry:
    base: dict[str, Any] = {
        "entity_id": entity_id,
        "entity_type": "power_concept",
        "activity_id": "seed_load",
        "agent_id": "build_seed_data",
        "source_document": "YD/T 1817-2017",
        "confidence": 0.9,
    }
    base.update(kw)
    return ProvenanceEntry(**base)


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
def storage(conn: FakeConn) -> PGProvenanceStorage:
    return PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)


# ---------------------------------------------------------------- 契约


class TestNoTransactionLeak:
    """读路径**不得**留下打开的事务。

    实测故障(板卡): 查一次谱系之后, 后端停在 ``idle in transaction / ClientRead``,
    紧接着的写入卡在 ``wait=Lock/transactionid`` 直到超时。根因是 psycopg3 在
    非 autocommit 模式下第一条语句就隐式开事务, 而读方法没有 commit。

    这里断言连接是 autocommit 的 —— 那是唯一能保证「只读方法不留事务」的地方
    (逐个读方法包 ``conn.transaction()`` 也能修, 但那让每个 SELECT 都多两次
    往返, 而读是这条路径上的主要操作)。
    """

    def test_connect_factory_defaults_to_autocommit(self) -> None:
        import inspect

        from aterag.provenance.pg_storage import _psycopg_connect

        src = inspect.getsource(_psycopg_connect)
        assert "autocommit=True" in src, "连接没开 autocommit, 读方法会泄漏事务"

    def test_read_methods_do_not_open_a_transaction(self, conn: FakeConn) -> None:
        """读方法不得调用 ``conn.transaction()``。

        写路径(``store`` / ``clear``)必须调用 —— 咨询锁要在事务内。
        """
        st = PGProvenanceStorage("dsn", connect_factory=lambda d: conn)
        st.retrieve("X")
        st.retrieve_all()
        st.get_chain_head()
        st.trace_lineage("X")
        assert not conn.tx_started, "读路径开了事务 -> 会 idle in transaction -> 挡住写入"

    def test_count_does_not_open_a_transaction(self) -> None:
        """``count()`` 单独跑一次(上面的用例里它会因为假连接没有行而抛)。"""
        conn = FakeConn({"count(*)": [(7,)]})
        st = PGProvenanceStorage("dsn", connect_factory=lambda d: conn)
        assert st.count() == 7
        assert not conn.tx_started, "count() 开了事务"

    def test_write_methods_do_open_a_transaction(self) -> None:
        """反向: 写路径必须开事务, 否则咨询锁在事务外取等于没取。"""
        conn = FakeConn()
        PGProvenanceStorage("dsn", connect_factory=lambda d: conn).store(_entry("X"))
        assert conn.tx_started == 1, "store() 没开事务, 咨询锁会落在事务外"


class TestFieldMapping:
    def test_mapping_is_bijective_over_dataclass_fields(self) -> None:
        """列名与字段名**双向**完全一致。

        只查「每个字段都有映射」不够 —— 多出来一个映射会让 ``_to_entry`` 往
        dataclass 传不存在的参数, 报的是 TypeError 而不是「映射表该更新了」。
        """
        fields = {f.name for f in dataclasses.fields(ProvenanceEntry)}
        mapped = {a for a, _b in _FIELDS}
        assert mapped == fields, f"差集: 只在 fields={fields - mapped} 只在 map={mapped - fields}"
        assert len(_FIELDS) == len(set(_FIELDS)), "同一 dataclass 字段被映射两次"

    def test_column_names_are_unique(self) -> None:
        cols = [c for _f, c in _FIELDS]
        assert len(cols) == len(set(cols)), f"列名重复: {cols}"

    def test_every_column_appears_in_the_ddl(self) -> None:
        """每一列都必须真的建在迁移里 —— 少一列就是运行时才发现。"""
        import re
        from pathlib import Path

        ddl = Path("alembic/versions/0003_l0_provenance.py").read_text(encoding="utf-8")
        body = ddl.split("CREATE TABLE", 1)[1]
        for _f, col in _FIELDS:
            assert re.search(rf"\b{re.escape(col)}\b", body), f"迁移里没有列 {col}"

    def test_entity_id_is_unique_in_the_ddl(self) -> None:
        """``entity_id`` 必须 UNIQUE —— 这是「同实体是替换而不是追加」的前提。

        没有它, ``ON CONFLICT (entity_id)`` 无处可冲突, 重复装载就会留下两行,
        sequence_id 不再是连续的 {1..N}, 而 ``verify_chain()`` 明确按
        「sequence_id == 前驱+1」判定 —— 症状是一条看不懂的 chain_break。
        """
        import re
        from pathlib import Path

        ddl = Path("alembic/versions/0003_l0_provenance.py").read_text(encoding="utf-8")
        assert re.search(r"entity_id\s+TEXT NOT NULL UNIQUE", ddl), "entity_id 缺 UNIQUE 约束"

    def test_sequence_id_index_is_not_unique(self) -> None:
        """``sequence_id`` **不能**有唯一约束。

        上游归档路径(``track_entity`` 的 versioning / ``invalidate()``)会写一行
        ``entity_id='X:v:<ts>'`` 的历史, 并**故意复用同一个 sequence_id** ——
        上游注释原文: "archival relabels always preserve their existing
        sequence_id rather than consuming a new one"。给它加唯一索引会让合法的
        归档写入直接失败(板卡实测: 重复键违反 uq_provenance_sequence)。
        """
        import re
        from pathlib import Path

        ddl = Path("alembic/versions/0003_l0_provenance.py").read_text(encoding="utf-8")
        assert not re.search(r"UNIQUE[^,)]*\bsequence_id\b", ddl), (
            "sequence_id 被加了唯一约束, 会堵死归档路径"
        )
        assert re.search(r"CREATE INDEX (IF NOT EXISTS )?idx_prov_sequence", ddl), (
            "缺 sequence_id 的普通索引(链头查询要用)"
        )


class TestStorageContract:
    def test_is_a_provenance_storage(self, storage: PGProvenanceStorage) -> None:
        assert isinstance(storage, ProvenanceStorage)
        assert not storage.__abstractmethods__, f"未实现: {storage.__abstractmethods__}"

    def test_table_name_is_validated(self) -> None:
        """表名进 SQL 标识符位置, 必须白名单校验而不是拼字符串。"""
        for bad in ("prov; drop table x", "a-b", "1abc", "a.b"):
            with pytest.raises(ValueError, match="表名不合法"):
                PGProvenanceStorage("dsn", table=bad)

    def test_table_name_allows_underscore_and_alnum(self) -> None:
        PGProvenanceStorage("dsn", table="provenance_v2")

    def test_works_with_the_real_manager(self, storage: PGProvenanceStorage) -> None:
        """能挂到真正的 ``ProvenanceManager`` 上 —— 这是本层的全部意义。"""
        mgr = ProvenanceManager(storage=storage)
        assert mgr.storage is storage


class TestInsert:
    def test_insert_covers_every_column(self, storage: PGProvenanceStorage, conn: FakeConn) -> None:
        storage.store(_entry())
        ins = conn.inserts()
        assert len(ins) == 1
        cols, params = ins[0]
        for _f, col in _FIELDS:
            assert f"{col}" in cols, f"INSERT 缺列 {col}"
        for _f, col in _FIELDS:
            assert col in params, f"参数缺 {col}"

    def test_chain_fields_written_verbatim(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        """checksum / sequence_id / previous_checksum 原样落库。

        这三个是 ``ProvenanceManager._save_entry`` 算的。存储层重算就与
        manager 内部哈希不一致, 而 ``verify_chain()`` 只会说「链断了」。
        """
        e = _entry(sequence_id=7, checksum="abc123", previous_checksum="zzz")
        storage.store(e)
        _cols, params = conn.inserts()[0]
        assert params["sequence_id"] == 7
        assert params["checksum"] == "abc123"
        assert params["previous_checksum"] == "zzz"

    def test_confidence_rounded_to_column_precision(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        """列是 NUMERIC(4,3), 不显式舍入的话 PG 会静默截断,
        于是「读回来与写进去的不等」而 checksum 覆盖不到数值列。"""
        storage.store(_entry(confidence=0.123456, credibility=0.98765))
        _c, params = conn.inserts()[0]
        assert params["confidence"] == 0.123
        assert params["credibility"] == 0.988

    def test_metadata_and_arrays_are_wrapped(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        e = _entry(metadata={"a": 1}, used_entities=["X", "Y"], informed_by_activities=["act1"])
        storage.store(e)
        _c, params = conn.inserts()[0]
        assert json.loads(params["meta"]) == {"a": 1}
        assert params["used_entities"] == ["X", "Y"]
        assert params["informed_by_activities"] == ["act1"]

    def test_store_is_an_upsert_on_entity_id(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        """同一 entity_id 必须**替换**, 不是追加。

        上游以 entity_id 为主键(``compute_checksum`` 注释: "entity_id is the
        storage primary key"), ``store()`` 对已存在的 entity_id 做替换。纯 INSERT
        会让同一实体装载两次后留下两行, sequence_id 不再是连续的 {1..N}, 而
        ``verify_chain()`` 严格按「sequence_id == 前驱+1」判定 —— 症状是一条
        看不懂的 chain_break。板卡实测过这个失败。
        """
        storage.store(_entry("X"))
        sql, _p = conn.inserts()[0]
        assert "on conflict (entity_id) do update" in sql

    def test_upsert_updates_everything_except_the_key(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        """``DO NOTHING`` 会把旧值永久留下。

        同一实体重新装载时出处/置信度可能已变(种子重跑、修正表更新),
        ``DO NOTHING`` 让「当前这条是从哪来的」变成过期的, 而那正是谱系表
        存在的理由。所以除主键外全部列都要更新。
        """
        storage.store(_entry("X"))
        sql, _p = conn.inserts()[0]
        tail = sql.split("do update set", 1)[1]
        # 词边界匹配: ``parent_entity_id`` 里含子串 ``entity_id``, 用 in 判会误报
        import re

        assigned = set(re.findall(r"(\w+)\s*=\s*excluded\.", tail))
        for _f, col in _FIELDS:
            if col == "entity_id":
                assert col not in assigned, "不能更新主键本身"
            else:
                assert col in assigned, f"UPSERT 没更新 {col}"

    def test_takes_the_advisory_lock_in_the_same_transaction(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        """锁必须在**事务内**取。

        ``pg_advisory_xact_lock`` 随事务结束自动释放, 所以「先加锁再开事务」
        等于没加 —— 竞态照旧, 而症状是 ``verify_chain`` 偶发误报, 极难定位。
        """
        storage.store(_entry())
        stmts = [s[0] for s in conn.statements]
        lock_at = next(i for i, s in enumerate(stmts) if "pg_advisory_xact_lock" in s)
        ins_at = next(i for i, s in enumerate(stmts) if s.startswith("insert into"))
        assert lock_at < ins_at, "插入前必须已经取过锁"


class TestChainHead:
    def test_returns_none_when_empty(self, storage: PGProvenanceStorage) -> None:
        assert storage.get_chain_head() is None

    def test_returns_seq_and_checksum(self) -> None:
        conn = FakeConn({"sequence_id": [(9, "chk9")]})
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        assert st.get_chain_head() == (9, "chk9")

    def test_tie_broken_by_prov_id_desc(self) -> None:
        """链头必须确定性地取**最近写入**的那行。

        ``track_entity`` 的归档路径会让历史行与即将被覆盖的现行行短暂共用
        同一个 sequence_id(历史行是从现行行拷来的)。只按 sequence_id 排时
        选哪行是实现定义的, 于是链头可能取到旧值。
        """
        conn = FakeConn({"sequence_id": []})
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        st.get_chain_head()
        sql = next(s for s, _p in conn.statements if "sequence_id is not null" in s)
        assert "order by sequence_id desc" in sql
        assert "prov_id desc" in sql

    def test_row_without_checksum_is_not_a_head(self) -> None:
        """checksum 为空的行不能当链头 —— 下一条就会接到一个空的 previous_checksum。"""
        conn = FakeConn({"sequence_id": [(9, None)]})
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        assert st.get_chain_head() is None


class TestRetrieve:
    def test_returns_latest_non_invalidated(self) -> None:
        conn = FakeConn(
            {
                "from l0_term.provenance": [
                    _row(
                        entity_id="E1",
                        entity_type="power_concept",
                        activity_id="a",
                        agent_id="ag",
                        confidence=0.9,
                        sequence_id=3,
                        checksum="c3",
                    )
                ]
            }
        )
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        e = st.retrieve("E1")
        assert e is not None and e.entity_id == "E1" and e.sequence_id == 3
        sql, _p = conn.statements[-1]
        assert "invalidated = false" in sql
        assert "order by sequence_id desc" in sql

    def test_row_to_entry_restores_types(self) -> None:
        """JSONB / TEXT[] / NUMERIC 必须还原成 dataclass 的原始类型。

        类型不一致时 ``compute_checksum`` 算出的东西与写入时不同,
        ``verify_chain()`` 就会误报「链断了」。
        """
        conn = FakeConn(
            {
                "from l0_term.provenance": [
                    _row(meta=json.dumps({"k": "v"}), used_entities=["A"], confidence=0.5)
                ]
            }
        )
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        e = st.retrieve("E")
        assert e is not None
        assert e.metadata == {"k": "v"}, f"metadata 类型错: {type(e.metadata)}"
        assert e.used_entities == ["A"]
        assert isinstance(e.confidence, float)

    def test_missing_row_returns_none(self, storage: PGProvenanceStorage) -> None:
        assert storage.retrieve("nope") is None


class TestRetrieveAll:
    def test_includes_invalidated_rows(self) -> None:
        """审计要能证明「存在过、被审过、被撤回」。

        默认过滤掉墓碑行的话, ``invalidate()`` 就等于删除, 而它写的
        ``invalidated_by`` / ``invalidation_reason`` 全是白写。
        只查 WHERE 子句 —— SELECT 列表里本来就有 ``invalidated`` 那一列。
        """
        conn = FakeConn()
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        st.retrieve_all()
        sql = conn.statements[-1][0]
        where = sql.split(" where ", 1)[1] if " where " in sql else ""
        assert "invalidated" not in where, f"过滤掉了墓碑行: {where}"

    def test_entity_type_filter_is_parameterized(self) -> None:
        """过滤值必须走参数, 不拼进 SQL。"""
        conn = FakeConn()
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        st.retrieve_all("power_concept")
        sql, params = conn.statements[-1]
        assert "entity_type = %s" in sql
        assert params == ["power_concept"]


class TestTraceLineage:
    def test_terminates_on_cycles(self, storage: PGProvenanceStorage) -> None:
        """A 的父是 B、B 的父是 A —— 必须靠已见集合收敛, 不能死循环。

        定义与被定义互相引用在知识图谱里是真实存在的(公理引用公式、公式
        又标注了公理), 所以这不是假想输入。
        """
        conn = FakeConn(
            {
                "from l0_term.provenance": [
                    _row(entity_id="A", sequence_id=1, parent_entity_id="B"),
                    _row(entity_id="B", sequence_id=2, parent_entity_id="A"),
                ]
            }
        )
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        assert [e.entity_id for e in st.trace_lineage("A")] == ["A", "B"]

    def test_respects_max_depth(self, storage: PGProvenanceStorage) -> None:
        # 链 A -> B -> C -> D
        rows = {
            "A": [_row(entity_id="A", sequence_id=1, parent_entity_id="B")],
            "B": [_row(entity_id="B", sequence_id=2, parent_entity_id="C")],
            "C": [_row(entity_id="C", sequence_id=3, parent_entity_id="D")],
        }

        class Selective(FakeConn):
            def next_rows(self, sql: str, params: Any) -> list[tuple]:
                ids = list((params or [[]])[0] or [])
                return rows[ids[0]] if ids and ids[0] in rows else []

        conn = Selective()
        st = PGProvenanceStorage("dsn", connect_factory=lambda dsn: conn)
        assert [e.entity_id for e in st.trace_lineage("A", max_depth=2)] == ["A", "B"]
        assert [e.entity_id for e in st.trace_lineage("A")] == ["A", "B", "C"]


class TestClear:
    def test_clear_deletes_and_reports_count(
        self, storage: PGProvenanceStorage, conn: FakeConn
    ) -> None:
        conn.deleted = 42
        assert storage.clear() == 42
        assert any("delete from" in s for s, _p in conn.statements)
