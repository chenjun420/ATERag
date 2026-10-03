"""storage.bitemporal 的单元测试。

§18.4.2 W0 验收判据之一: 双时态 as_of / supersede 单元测试 100% 通过。
本文件是该判据的执行体。

用假 cursor 覆盖全部控制流 (冲突检测、时间窗数学、唯一当前版本约束),
不需要真库。SQL 语句的**正确性**由 tests/contract 覆盖 —— 假 cursor
无法验证 PostgreSQL 是否接受这些语句。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from aterag.storage.bitemporal import (
    BITEMPORAL_METHODS,
    BitemporalDAO,
    BitemporalError,
    ConflictError,
    NotFoundError,
    TemporalBound,
    VersionedRow,
    build_partial_unique_index_sql,
)


def ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


class FakeCursor:
    """按 business_key 维护行的假 cursor, 并记录发出的 SQL。

    只理解本模块发出的四种语句 (SELECT / INSERT / UPDATE), 其余照常记录后
    返回空。够用: 本测试的目标是控制流与并发语义, 不是 SQL 解析。
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.statements: list[str] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.statements.append(query)
        stripped = query.strip()

        if stripped.startswith("SELECT"):
            key = _literal_after(stripped, "business_key = ")
            self._pending = [r for r in self.rows if r["business_key"] == key]
            return

        if stripped.startswith("INSERT"):
            # 解析 VALUES 元组。字段序由 insert_row() 固定:
            # (business_key, payload, valid_from, valid_until, recorded_at, recorded_by)
            values = _split_values(stripped.split("VALUES (", 1)[1])
            self.rows.append(
                {
                    "business_key": _unquote(values[0]),
                    "payload": _loads_json(values[1]),
                    "valid_from": _parse_ts(values[2]),
                    "valid_until": _parse_ts(values[3]),
                    "recorded_at": _parse_ts(values[4]),
                    "recorded_by": _unquote(values[5]),
                }
            )
            return

        if stripped.startswith("UPDATE"):
            key = _literal_after(stripped, "business_key = ")
            until = _timestamptz_after(stripped, "SET valid_until = ")
            for r in self.rows:
                if (
                    r["business_key"] == key
                    and r["valid_until"] is None
                    and r["valid_from"].isoformat() in stripped
                ):
                    r["valid_until"] = until
            return

        raise AssertionError(f"假 cursor 不认识这条语句: {stripped}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [
            (
                r["valid_from"],
                r["valid_until"],
                r["payload"],
                r.get("recorded_at"),
                r.get("recorded_by"),
            )
            for r in getattr(self, "_pending", [])
        ]


def _literal_after(sql: str, marker: str) -> str:
    tail = sql.split(marker, 1)[1]
    start = tail.index("'") + 1
    end = tail.index("'", start)
    return tail[start:end].replace("''", "'")


def _timestamptz_after(sql: str, marker: str) -> datetime:
    tail = sql.split(marker, 1)[1]
    start = tail.index("'") + 1
    end = tail.index("'", start)
    return datetime.fromisoformat(tail[start:end])


def _split_values(tail: str) -> list[str]:
    """拆 VALUES 元组为 6 个原始字段串, 正确处理引号内的逗号与 '' 转义。

    不能用 ``tail.split(",")`` —— payload 是 JSON 字符串, 里面本来就有逗号。

    每个返回值是一个完整字段, 形如 ``'K1'`` / ``'{"v": 1}'::jsonb`` /
    ``'2026-01-01T00:00:00+00:00'::timestamptz`` / ``NULL``。
    """
    fields: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    while i < len(tail):
        ch = tail[i]
        if in_quote:
            if ch == "'":
                # '' 是转义的单引号, 仍在引号内
                if i + 1 < len(tail) and tail[i + 1] == "'":
                    buf.append("''")
                    i += 2
                    continue
                in_quote = False
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == ",":
            fields.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        fields.append("".join(buf).strip())
    return fields


def _unquote(value: str) -> str:
    """剥掉外层引号并还原 '' 转义。"""
    text = value.strip()
    if text.endswith("::jsonb"):
        text = text[: -len("::jsonb")]
    if not (text.startswith("'") and text.endswith("'")):
        return text
    return text[1:-1].replace("''", "'")


def _loads_json(value: str) -> dict[str, Any]:
    import json

    return json.loads(_unquote(value))


def _parse_ts(value: str) -> datetime | None:
    text = value.strip()
    if not text or text == "NULL":
        return None
    if "::" in text:
        text = text.split("::", 1)[0]
    return datetime.fromisoformat(_unquote(text))


class SeededCursor(FakeCursor):
    """带预置版本的假 cursor。"""

    def __init__(self, versions: list[tuple[datetime, datetime | None, dict[str, Any]]]) -> None:
        super().__init__()
        self.rows = [
            {
                "business_key": "K1",
                "payload": payload,
                "valid_from": vf,
                "valid_until": vu,
            }
            for vf, vu, payload in versions
        ]


def make_dao(cur: FakeCursor, now: datetime) -> BitemporalDAO:
    return BitemporalDAO(cur, schema="pw_x", clock=lambda: now)


class TestContractSurface:
    def test_implements_all_five_methods(self) -> None:
        """§18.2.1 契约的五个方法必须都在。

        反射校验而不是逐个调用: 方法被改名时契约测试要立刻失败,
        而不是等到某个下游模块运行时才发现。
        """
        for name in BITEMPORAL_METHODS:
            assert callable(getattr(BitemporalDAO, name, None)), f"缺少契约方法 {name}"


class TestTemporalBound:
    def test_rejects_inverted_window(self) -> None:
        with pytest.raises(BitemporalError, match="起点必须严格早于终点"):
            TemporalBound(ts(2026, 2, 1), ts(2026, 1, 1))

    def test_rejects_zero_length_window(self) -> None:
        t = ts(2026, 1, 1)
        with pytest.raises(BitemporalError):
            TemporalBound(t, t)

    def test_allows_unbounded(self) -> None:
        TemporalBound(None, None)
        TemporalBound(ts(2026, 1, 1), None)
        TemporalBound(None, ts(2026, 1, 1))


class TestVersionedRow:
    def test_open_ended_is_current_at_and_after_start(self) -> None:
        row = VersionedRow("t", "K", {}, ts(2026, 1, 1), None)
        assert row.is_current(ts(2026, 1, 1)) is True
        assert row.is_current(ts(2030, 1, 1)) is True

    def test_before_start_is_not_current(self) -> None:
        row = VersionedRow("t", "K", {}, ts(2026, 1, 1), None)
        assert row.is_current(ts(2025, 12, 31)) is False

    def test_half_open_excludes_end(self) -> None:
        """端点归属必须唯一: [a, b) 在 b 处已失效。

        对应 §5.8.2 视图的 valid_until > now()。
        """
        row = VersionedRow("t", "K", {}, ts(2026, 1, 1), ts(2026, 2, 1))
        assert row.is_current(ts(2026, 1, 31)) is True
        assert row.is_current(ts(2026, 2, 1)) is False


class TestAsOf:
    def test_returns_version_at_time(self) -> None:
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1}),
                (ts(2026, 2, 1), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 3, 1))
        assert dao.as_of("t", "K1", ts(2026, 1, 15)) is not None
        assert dao.as_of("t", "K1", ts(2026, 1, 15)).payload == {"v": 1}
        assert dao.as_of("t", "K1", ts(2026, 3, 1)).payload == {"v": 2}

    def test_none_before_first_version(self) -> None:
        cur = SeededCursor([(ts(2026, 2, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 3, 1))
        assert dao.as_of("t", "K1", ts(2026, 1, 1)) is None

    def test_none_for_unknown_key(self) -> None:
        cur = SeededCursor([])
        assert make_dao(cur, ts(2026, 1, 1)).as_of("t", "NOPE", ts(2026, 1, 1)) is None

    def test_raises_on_corrupt_multi_hit(self) -> None:
        """两个版本同时有效 = 数据损坏。

        必须抛错而不是 LIMIT 1 —— §18.10 注 2 要求不可追溯时记 UNKNOWN
        而不猜, 静默取一条就是猜。
        """
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), None, {"v": 1}),
                (ts(2026, 1, 2), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 3, 1))
        with pytest.raises(ConflictError, match="至多一条当前版本"):
            dao.as_of("t", "K1", ts(2026, 3, 1))


class TestValidBetween:
    def test_window_spanning_boundary_returns_both(self) -> None:
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1}),
                (ts(2026, 2, 1), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 3, 1))
        rows = dao.valid_between("t", "K1", TemporalBound(ts(2026, 1, 15), ts(2026, 2, 15)))
        assert [r.payload["v"] for r in rows] == [1, 2]

    def test_window_inside_single_version(self) -> None:
        cur = SeededCursor([(ts(2026, 1, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 3, 1))
        rows = dao.valid_between("t", "K1", TemporalBound(ts(2026, 2, 1), ts(2026, 2, 15)))
        assert len(rows) == 1

    def test_window_touching_boundary_returns_one(self) -> None:
        """窗口起点 == 上一版本终点: 半开区间不重叠, 只命中后一个。

        若这里返回 2, 说明区间被当成闭区间了, 那么「b 时刻有效的是哪个
        版本」就不唯一 —— 这是双时态最核心的不变量。
        """
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1}),
                (ts(2026, 2, 1), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 3, 1))
        rows = dao.valid_between("t", "K1", TemporalBound(ts(2026, 2, 1), ts(2026, 3, 1)))
        assert [r.payload["v"] for r in rows] == [2]

    def test_window_before_all_versions(self) -> None:
        cur = SeededCursor([(ts(2026, 2, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 3, 1))
        assert dao.valid_between("t", "K1", TemporalBound(ts(2026, 1, 1), ts(2026, 1, 15))) == []

    def test_sorted_by_valid_from(self) -> None:
        cur = SeededCursor(
            [
                (ts(2026, 3, 1), None, {"v": 3}),
                (ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1}),
                (ts(2026, 2, 1), ts(2026, 3, 1), {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 4, 1))
        rows = dao.valid_between("t", "K1", TemporalBound(None, None))
        assert [r.payload["v"] for r in rows] == [1, 2, 3]


class TestCurrent:
    def test_returns_open_ended_version(self) -> None:
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1}),
                (ts(2026, 2, 1), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 3, 1))
        assert dao.current("t", "K1").payload == {"v": 2}

    def test_none_when_all_closed(self) -> None:
        cur = SeededCursor([(ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1})])
        assert make_dao(cur, ts(2026, 3, 1)).current("t", "K1") is None

    def test_raises_on_two_open_ended(self) -> None:
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), None, {"v": 1}),
                (ts(2026, 2, 1), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 3, 1))
        with pytest.raises(ConflictError, match="唯一当前版本"):
            dao.current("t", "K1")


class TestInsert:
    def test_first_version_uses_clock(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 5, 5))
        row = dao.insert("t", "K1", {"a": 1})
        assert row.valid_from == ts(2026, 5, 5)
        assert row.valid_until is None

    def test_explicit_valid_from_wins(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 5, 5))
        row = dao.insert("t", "K1", {"a": 1}, valid_from=ts(2026, 1, 1))
        assert row.valid_from == ts(2026, 1, 1)

    def test_conflicts_with_existing_current(self) -> None:
        """已有当前版本时 insert 必须冲突, 不得隐式收口。

        隐式收口等于替调用方决定「上一条何时失效」, 那需要判断。
        """
        cur = SeededCursor([(ts(2026, 1, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 3, 1))
        with pytest.raises(ConflictError, match="已有当前版本"):
            dao.insert("t", "K1", {"v": 2})

    def test_allows_insert_after_closed_version(self) -> None:
        cur = SeededCursor([(ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1})])
        dao = make_dao(cur, ts(2026, 3, 1))
        row = dao.insert("t", "K1", {"v": 2})
        assert row.valid_from == ts(2026, 3, 1)

    def test_rejects_start_not_after_closed_version(self) -> None:
        """开启当前版本的起点必须晚于最后一个已收口版本。

        起点早于它会让历史区间倒置, 半开区间再也无法唯一判定「哪一刻有效」。
        """
        cur = SeededCursor([(ts(2026, 2, 1), ts(2026, 3, 1), {"v": 1})])
        dao = make_dao(cur, ts(2026, 4, 1))
        with pytest.raises(ConflictError, match="不晚于已有版本的起点"):
            dao.insert("t", "K1", {"v": 2}, valid_from=ts(2026, 1, 1))

    def test_rejects_start_equal_to_closed_version(self) -> None:
        """起点恰好等于收口区间起点 = 与该版本重叠。"""
        cur = SeededCursor([(ts(2026, 2, 1), ts(2026, 3, 1), {"v": 1})])
        dao = make_dao(cur, ts(2026, 4, 1))
        with pytest.raises(ConflictError, match="不晚于已有版本的起点"):
            dao.insert("t", "K1", {"v": 2}, valid_from=ts(2026, 2, 1))

    def test_rejects_backfill_overlapping_existing(self) -> None:
        """补录区间与已有版本相交 => 同一时刻两个版本。

        补录历史本身合法, 但不能造出重叠版本。
        """
        cur = SeededCursor(
            [
                (ts(2026, 1, 1), ts(2026, 2, 1), {"v": 1}),
                (ts(2026, 3, 1), None, {"v": 2}),
            ]
        )
        dao = make_dao(cur, ts(2026, 4, 1))
        with pytest.raises(ConflictError, match="相交"):
            dao.insert(
                "t",
                "K1",
                {"v": 3},
                valid_from=ts(2026, 1, 15),
                valid_until=ts(2026, 3, 15),
            )

    def test_accepts_backfill_ending_at_open_version_start(self) -> None:
        """补录终点恰为当前版本起点 => 不相交, 合法。

        补录区间 [2,3) 与当前版本 [3,∞) 半开不相交: 3 不属于补录区间。
        若这里报错, 就说明判定被写成了闭区间, 那么「3 时刻有效的是哪个版本」
        会有两个答案 —— 双时态的核心不变量就破了。
        """
        cur = SeededCursor([(ts(2026, 3, 1), None, {"v": 2})])
        dao = make_dao(cur, ts(2026, 4, 1))
        row = dao.insert(
            "t",
            "K1",
            {"v": 1},
            valid_from=ts(2026, 2, 1),
            valid_until=ts(2026, 3, 1),
        )
        assert row.valid_until == ts(2026, 3, 1)
        # 3 这一刻唯一命中当前版本
        assert dao.as_of("t", "K1", ts(2026, 3, 1)).payload == {"v": 2}

    def test_rejects_backfill_running_into_open_version(self) -> None:
        """补录终点越过当前版本起点 => 相交, 拒绝。"""
        cur = SeededCursor([(ts(2026, 3, 1), None, {"v": 2})])
        dao = make_dao(cur, ts(2026, 4, 1))
        with pytest.raises(ConflictError, match="相交"):
            dao.insert(
                "t",
                "K1",
                {"v": 1},
                valid_from=ts(2026, 2, 1),
                valid_until=ts(2026, 3, 15),
            )

    def test_rejects_zero_length_backfill(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 4, 1))
        t = ts(2026, 2, 1)
        with pytest.raises(ConflictError, match="零长或负长区间"):
            dao.insert("t", "K1", {"v": 1}, valid_from=t, valid_until=t)

    def test_rejects_inverted_backfill(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 4, 1))
        with pytest.raises(ConflictError, match="零长或负长区间"):
            dao.insert(
                "t",
                "K1",
                {"v": 1},
                valid_from=ts(2026, 3, 1),
                valid_until=ts(2026, 2, 1),
            )

    def test_accepts_backfill_in_gap_before_all_versions(self) -> None:
        """补录历史链条最早处缺的一段 —— 合法。

        这是追溯链有缺口时的正常修复动作: 补上 [1,2), 已有 [2,3) 与 [3,∞)。
        """
        cur = SeededCursor(
            [
                (ts(2026, 2, 1), ts(2026, 3, 1), {"v": 2}),
                (ts(2026, 3, 1), None, {"v": 3}),
            ]
        )
        dao = make_dao(cur, ts(2026, 4, 1))
        row = dao.insert(
            "t",
            "K1",
            {"v": 1},
            valid_from=ts(2026, 1, 1),
            valid_until=ts(2026, 2, 1),
        )
        assert row.valid_from == ts(2026, 1, 1)
        assert row.valid_until == ts(2026, 2, 1)

    def test_backfilled_version_is_queryable(self) -> None:
        """补录后 as_of 必须能命中补录的那一段。

        这正是补录的意义: 缺口区间内的查询从 None (UNKNOWN) 变成有据可查。
        """
        cur = SeededCursor(
            [
                (ts(2026, 2, 1), ts(2026, 3, 1), {"v": 2}),
                (ts(2026, 3, 1), None, {"v": 3}),
            ]
        )
        dao = make_dao(cur, ts(2026, 4, 1))
        dao.insert(
            "t",
            "K1",
            {"v": 1},
            valid_from=ts(2026, 1, 1),
            valid_until=ts(2026, 2, 1),
        )
        assert dao.as_of("t", "K1", ts(2026, 1, 15)).payload == {"v": 1}
        assert dao.as_of("t", "K1", ts(2026, 2, 15)).payload == {"v": 2}


class TestSupersede:
    def test_closes_old_and_opens_new(self) -> None:
        cur = SeededCursor([(ts(2026, 1, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 5, 5))
        new, old = dao.supersede("t", "K1", {"v": 2})
        assert old.valid_until == ts(2026, 5, 5)
        assert new.valid_from == ts(2026, 5, 5)
        assert new.valid_until is None
        # 假 cursor 模拟了 UPDATE 副作用
        assert cur.rows[0]["valid_until"] == ts(2026, 5, 5)

    def test_timeline_stays_half_open(self) -> None:
        cur = SeededCursor([(ts(2026, 1, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 5, 5))
        dao.supersede("t", "K1", {"v": 2})
        assert dao.as_of("t", "K1", ts(2026, 5, 5)) is not None
        assert dao.as_of("t", "K1", ts(2026, 5, 5)).payload == {"v": 2}
        assert dao.as_of("t", "K1", ts(2026, 4, 30)).payload == {"v": 1}

    def test_explicit_at(self) -> None:
        cur = SeededCursor([(ts(2026, 1, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 5, 5))
        _, old = dao.supersede("t", "K1", {"v": 2}, at=ts(2026, 3, 1))
        assert old.valid_until == ts(2026, 3, 1)

    def test_not_found(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 5, 5))
        with pytest.raises(NotFoundError, match="无当前版本"):
            dao.supersede("t", "K1", {"v": 1})

    def test_rejects_at_before_current_start(self) -> None:
        cur = SeededCursor([(ts(2026, 2, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 5, 5))
        with pytest.raises(ConflictError, match="负长度版本"):
            dao.supersede("t", "K1", {"v": 2}, at=ts(2026, 1, 1))

    def test_rejects_at_equal_to_current_start(self) -> None:
        """零长度取代 = 该版本从未存在过。

        允许它会让 as_of 在该时刻命中零个版本, 「当时有效的是哪个版本」
        就无解。
        """
        cur = SeededCursor([(ts(2026, 2, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 5, 5))
        with pytest.raises(ConflictError, match="零长度"):
            dao.supersede("t", "K1", {"v": 2}, at=ts(2026, 2, 1))

    def test_close_statement_pins_valid_from(self) -> None:
        """UPDATE 的 WHERE 必须带 valid_from。

        只按 business_key + valid_until IS NULL 更新, 在并发下会把别人刚
        插入的新版本也收口掉, 而 partial unique index 那时已生效,
        这一 UPDATE 会匹配 0 行却不报错 —— 静默丢一次取代。
        """
        cur = SeededCursor([(ts(2026, 1, 1), None, {"v": 1})])
        dao = make_dao(cur, ts(2026, 5, 5))
        dao.supersede("t", "K1", {"v": 2})
        update = [s for s in cur.statements if s.startswith("UPDATE")][0]
        assert "valid_from = " in update
        assert "valid_until IS NULL" in update


class TestPartialUniqueIndex:
    def test_shape(self) -> None:
        sql = build_partial_unique_index_sql("pw_x", "fact", "business_key")
        assert 'CREATE UNIQUE INDEX IF NOT EXISTS "ux_pw_x_fact_current"' in sql
        assert 'ON "pw_x"."fact" ("business_key")' in sql
        assert "WHERE valid_until IS NULL" in sql

    def test_rejects_bad_column_name(self) -> None:
        with pytest.raises(BitemporalError, match="业务键列名不合法"):
            build_partial_unique_index_sql("pw_x", "fact", "business key; DROP TABLE y")


class TestClockInjection:
    def test_clock_is_called_for_recorded_at_and_valid_from(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 7, 7))
        row = dao.insert("t", "K1", {})
        assert row.valid_from == ts(2026, 7, 7)
        assert row.recorded_at == ts(2026, 7, 7)
        assert row.recorded_by == "system"

    def test_actor_is_recorded(self) -> None:
        cur = SeededCursor([])
        dao = make_dao(cur, ts(2026, 7, 7))
        row = dao.insert("t", "K1", {}, actor="alice")
        assert row.recorded_by == "alice"

    def test_default_clock_is_timezone_aware(self) -> None:
        """默认时钟必须带时区。

        naive datetime 写进 timestamptz 后, 语义取决于服务器本地时区设置。
        跨时区部署时错法是「as_of 查到错误的版本」而不是报错 ——
        比抛异常难查得多。
        """
        cur = SeededCursor([])
        dao = BitemporalDAO(cur, schema="pw_x")  # 不注入 clock
        row = dao.insert("t", "K1", {})
        assert row.valid_from.tzinfo is not None
        assert row.valid_from.utcoffset() is not None

    def test_default_clock_is_close_to_now(self) -> None:
        cur = SeededCursor([])
        before = datetime.now(UTC)
        dao = BitemporalDAO(cur, schema="pw_x")
        row = dao.insert("t", "K1", {})
        after = datetime.now(UTC)
        assert before <= row.valid_from <= after
