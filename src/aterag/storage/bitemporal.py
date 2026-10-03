"""双时态 DAO。

依据 V6.0:
    §5.8.2  双时态视图 fact_as_of
    §5.8.3  双时态 + 权限叠加查询
    §18.2.1 storage.bitemporal 接口契约
    原则四  单一 PostgreSQL 底座, ACID

双时态的两个时间轴:
    valid_from / valid_until   业务有效时间 (这条结论从什么时候成立)
    插入行本身的 created_at    记录时间 (我们什么时候知道的)

只暴露「当时有效且有权看」的版本 —— §5.8 开篇句。RLS 管第二个条件
(有权看), 本模块管第一个 (当时有效), 两者叠加。

核心不变量:
    1. 同一 (schema, 表, 业务键) 在任一时刻至多一条 valid_until IS NULL 的行
    2. supersede 不覆盖旧行, 只把旧行的 valid_until 收口
    3. 冲突时抛 ConflictError, 绝不静默取「最近一条」

第 3 条是硬要求。§1.6 原则六要求六级可追溯, 而「最近一条是谁」如果
由数据库隐式决定, 追溯链上就会出现一个无法解释的版本跳变。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .schema import CursorLike

__all__ = [
    "BITEMPORAL_METHODS",
    "BitemporalDAO",
    "BitemporalError",
    "ConflictError",
    "CursorLike",
    "NotFoundError",
    "TemporalBound",
    "VersionedRow",
    "WriteReceipt",
    "build_partial_unique_index_sql",
    "close_row",
    "insert_row",
    "select_rows",
]


class BitemporalError(RuntimeError):
    """双时态操作失败。"""


class ConflictError(BitemporalError):
    """写入冲突。

    触发场景:
      - 向已收口区间 (valid_from <= t < valid_until) 插入新版本
      - 对已收口的行再次 supersede
      - 并发下两个事务试图同时为同一业务键开启当前版本

    统一抛异常而不是返回 False: 调用方若忽略返回值就会静默丢数据,
    而 §18.10 注 2 要求「不可追溯的值记为 UNKNOWN, 不猜」——
    静默丢弃与猜错是同一类错误。
    """


class NotFoundError(BitemporalError):
    """按业务键找不到当前版本。"""


#: §18.2.1 契约: 双时态 DAO 的五个方法名。列在这里而不是散在各处, 是为了让
#: 契约测试可以反射校验实现类是否覆盖了全部方法 (tests/contract)。
BITEMPORAL_METHODS: tuple[str, ...] = (
    "insert",
    "supersede",
    "as_of",
    "valid_between",
    "current",
)


@dataclass(frozen=True)
class TemporalBound:
    """查询的时间窗。

    ``start_inclusive`` 闭、``end_exclusive`` 开。半开区间是刻意的:
    相邻两个版本 [a, b) 与 [b, c) 在 b 处不重叠, 用闭区间会让 b 同时
    命中两行, 而「b 时刻有效的是哪个版本」必须唯一。

    对应 §5.8.2 视图里的 ``valid_from <= now() AND (valid_until IS NULL OR valid_until > now())``
    —— 那正是半开区间的写法。
    """

    start_inclusive: datetime | None
    end_exclusive: datetime | None

    def __post_init__(self) -> None:
        if (
            self.start_inclusive is not None
            and self.end_exclusive is not None
            and self.start_inclusive >= self.end_exclusive
        ):
            raise BitemporalError(
                "时间窗起点必须严格早于终点: "
                f"{self.start_inclusive.isoformat()} >= {self.end_exclusive.isoformat()}。"
                " 半开区间 [start, end) 要求 start < end; 相邻版本在边界处不应重叠。"
            )


@dataclass(frozen=True)
class VersionedRow:
    """一个版本行。

    ``payload`` 存业务列。刻意不逐列建模: 双时态是横切关注点,
    每个业务表都套一遍 dataclass 会产出几十个几乎相同的类, 而它们的
    唯一区别正是 payload 里的键。
    """

    table: str
    business_key: str
    payload: Mapping[str, Any]
    valid_from: datetime
    valid_until: datetime | None
    recorded_at: datetime | None = None
    recorded_by: str | None = None

    def is_current(self, at: datetime) -> bool:
        """该版本在时刻 ``at`` 是否业务有效。

        半开区间判定, 与 TemporalBound 和 §5.8.2 视图一致。
        """
        if at < self.valid_from:
            return False
        return self.valid_until is None or at < self.valid_until

    def overlaps(self, window: TemporalBound) -> bool:
        """该版本是否与给定时间窗有交集。"""
        # 起点比较: 本版本终点 vs 窗口起点
        my_end = self.valid_until
        if window.start_inclusive is not None:
            if my_end is not None and my_end <= window.start_inclusive:
                return False
        # 终点比较: 本版本起点 vs 窗口终点
        if window.end_exclusive is not None and self.valid_from >= window.end_exclusive:
            return False
        return True


@dataclass
class WriteReceipt:
    """写回执。

    对应 workbench/gate.py 现有的 WriteReceipt 模式 (actor / commit /
    changed_fields / at) —— 那是本仓库已经验证过的审计形态, 不另造。

    迁移到双时态后多两个字段:
      valid_from  业务有效起点
      superseded  被取代时的收口时间 (新版本则为 None)
    """

    table: str
    business_key: str
    action: str
    actor: str
    at: datetime
    valid_from: datetime
    superseded: datetime | None = None
    changed_fields: tuple[str, ...] = field(default_factory=tuple)


class BitemporalDAO:
    """双时态读写。

    构造时不连库 —— 与库交互全部通过传入的 cursor。这让控制流可测,
    也让事务边界由调用方决定 (psycopg 的默认事务块即可, 不需要额外
    包装层)。

    :param cur: 游标
    :param schema: 目标 schema (``l0_term`` 或 ``pw_<model_key>``)
    :param clock: 取当前时刻的函数。默认用 ``datetime.now(UTC)``。
        注入点, 让测试能确定性地构造时间线。
    """

    def __init__(
        self,
        cur: CursorLike,
        *,
        schema: str,
        clock: Any | None = None,
    ) -> None:
        self._cur = cur
        self._schema = schema
        self._clock = clock if clock is not None else _utcnow

    @property
    def schema(self) -> str:
        return self._schema

    # ---- 读 ----

    def as_of(self, table: str, business_key: str, at: datetime) -> VersionedRow | None:
        """查询 ``at`` 时刻业务有效的版本。

        对应 §5.8.2 的 ``fact_as_of`` 视图与 §5.8.3 的叠加查询。
        本方法不做权限过滤 —— 那由 RLS 在数据库侧完成 (§5.8 纪律 2)。
        这里额外收口「只能取到唯一一行」: 若因数据损坏出现多行, 抛
        ConflictError 而不是 ``LIMIT 1`` 静默取一条。
        """
        rows = select_rows(self._cur, self._schema, table, business_key)
        hits = [r for r in rows if r.is_current(at)]
        if not hits:
            return None
        if len(hits) > 1:
            raise ConflictError(
                f"{self._schema}.{table} 业务键 {business_key!r} 在 "
                f"{at.isoformat()} 有 {len(hits)} 个有效版本, 违反「至多一条当前版本」。"
                " 数据已损坏, 拒绝静默取一条。"
            )
        return hits[0]

    def valid_between(
        self, table: str, business_key: str, window: TemporalBound
    ) -> list[VersionedRow]:
        """查询与 ``window`` 有交集的全部版本, 按 valid_from 升序。

        对应 §18.2.1 契约的 valid_between。返回列表而非单值:
        时间窗跨版本边界时本就应命中多行。
        """
        rows = select_rows(self._cur, self._schema, table, business_key)
        return sorted(
            (r for r in rows if r.overlaps(window)),
            key=lambda r: r.valid_from,
        )

    def current(self, table: str, business_key: str) -> VersionedRow | None:
        """查询当前有效版本 (``valid_until IS NULL``)。

        「当前」指业务时间的当前, 不是记录时间的当前 ——
        后者对应「我们最后知道的是哪条」, 是另一组问题。
        """
        rows = select_rows(self._cur, self._schema, table, business_key)
        hits = [r for r in rows if r.valid_until is None]
        if not hits:
            return None
        if len(hits) > 1:
            raise ConflictError(
                f"{self._schema}.{table} 业务键 {business_key!r} 有 {len(hits)} "
                "个 valid_until IS NULL 的版本, 违反唯一当前版本约束。"
            )
        return hits[0]

    # ---- 写 ----

    def insert(
        self,
        table: str,
        business_key: str,
        payload: Mapping[str, Any],
        *,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        actor: str = "system",
    ) -> VersionedRow:
        """插入版本。

        两种形态, 对应两种真实需求:

        **开启当前版本** (``valid_until`` 缺省, 开放右端)
            该业务键当前没有有效版本 (全部已收口), 且 ``valid_from`` 晚于
            最后一个版本的起点。此时新行成为当前版本。

            已有开放右端的版本时冲突, 不隐式收口 —— 隐式收口等于替调用方
            决定「上一条何时失效」, 而那是需要判断的事。请改用 supersede()。

        **补录历史片段** (``valid_until`` 显式给定, 闭合区间)
            用于追溯链有缺口时补录。允许起点早于现有版本, 因为它填的是空白
            而不是覆盖。唯一硬约束: 不得与任何已有版本的时间区间相交。

        为什么必须支持补录: as_of / valid_between 的用途是「查某时刻的结论」。
        若缺口无法补录, 缺口区间内的查询就只能返回 None, 而 None 在
        §18.10 注 2 的语义里是 UNKNOWN (不可追溯) 而不是「当时没有这个结论」。
        两者对产测判定的含义完全不同。

        返回新插入的 VersionedRow。
        """
        start = valid_from if valid_from is not None else self._clock()

        if valid_until is not None and valid_until <= start:
            raise ConflictError(
                f"{self._schema}.{table} 业务键 {business_key!r} 的新版本终点 "
                f"{valid_until.isoformat()} 不晚于起点 {start.isoformat()}。"
                " 零长或负长区间意味着该版本从未成立, 不构成一个版本。"
            )

        existing = select_rows(self._cur, self._schema, table, business_key)

        if valid_until is None:
            self._assert_can_open_current(table, business_key, start, existing)
        else:
            self._assert_no_overlap(table, business_key, start, valid_until, existing)

        row = VersionedRow(
            table=table,
            business_key=business_key,
            payload=dict(payload),
            valid_from=start,
            valid_until=valid_until,
            recorded_at=self._clock(),
            recorded_by=actor,
        )
        insert_row(self._cur, self._schema, row)
        return row

    def _assert_can_open_current(
        self,
        table: str,
        business_key: str,
        start: datetime,
        existing: list[VersionedRow],
    ) -> None:
        """校验能否开启一个新的当前版本。"""
        for row in existing:
            if row.valid_until is None:
                raise ConflictError(
                    f"{self._schema}.{table} 业务键 {business_key!r} 已有当前版本 "
                    f"(valid_from={row.valid_from.isoformat()})。"
                    " 请改用 supersede() 显式收口 —— 静默覆盖会断掉追溯链。"
                )
            if start <= row.valid_from:
                raise ConflictError(
                    f"新版本起点 {start.isoformat()} 不晚于已有版本的起点 "
                    f"{row.valid_from.isoformat()}。"
                    " 开启当前版本必须晚于最后一个已收口版本, 否则历史区间会被"
                    " 倒置 —— 半开区间无法再唯一判定「哪一刻有效」。"
                    " 若要补录历史, 请显式给定 valid_until。"
                )

    def _assert_no_overlap(
        self,
        table: str,
        business_key: str,
        start: datetime,
        end: datetime,
        existing: list[VersionedRow],
    ) -> None:
        """校验补录的闭合区间不与任何已有版本相交。"""
        for row in existing:
            row_end = row.valid_until
            # 半开区间相交判定: [start, end) ∩ [row.valid_from, row_end) 非空
            after_start = end > row.valid_from
            before_end = row_end is None or start < row_end
            if after_start and before_end:
                row_end_text = "∞" if row_end is None else row_end.isoformat()
                raise ConflictError(
                    f"补录区间 [{start.isoformat()}, {end.isoformat()}) 与已有版本 "
                    f"[{row.valid_from.isoformat()}, {row_end_text}) 相交。"
                    " 一个业务键在同一时刻只能对应一个版本。"
                )

    def supersede(
        self,
        table: str,
        business_key: str,
        payload: Mapping[str, Any],
        *,
        at: datetime | None = None,
        actor: str = "system",
    ) -> tuple[VersionedRow, VersionedRow]:
        """用新版本取代当前版本。

        返回 ``(新版本, 被收口的旧版本)``。被收口的旧版本带的是**收口之后**的
        valid_until, 而不是收口前读到的 NULL —— 返回实际落库的区间, 免得
        调用方把返回值当成「旧版本一直有效到今天」而据此生成变更摘要。
        旧版本的 payload 一并带出, 因为调用方常需要新旧对照, 而重新查一次
        既多一次往返, 又可能因并发拿到已被别人再次取代的版本。

        步骤 (顺序不可换):
          1. 查当前版本, 无则 NotFoundError
          2. 收口旧版本 valid_until = at
          3. 插入新版本 valid_from = at

        第 2 步与第 3 步必须在同一事务内。调用方用 psycopg 默认事务块
        即可满足; 本方法不自行 commit, 也不自行开事务 ——
        事务边界属于调用方, 混进来会让「批量 supersede 后统一提交」
            这种合理用法变得不可能。
        """
        effective_at = at if at is not None else self._clock()
        old = self.current(table, business_key)
        if old is None:
            raise NotFoundError(
                f"{self._schema}.{table} 业务键 {business_key!r} 无当前版本, 无法 supersede。"
            )
        if effective_at < old.valid_from:
            raise ConflictError(
                f"收口时刻 {effective_at.isoformat()} 早于当前版本起点 "
                f"{old.valid_from.isoformat()}。这会产生负长度版本。"
            )
        if effective_at == old.valid_from:
            raise ConflictError(
                f"收口时刻 {effective_at.isoformat()} 等于当前版本起点。"
                " 新旧版本零长度, 等于该版本从未存在过, 不构成取代。"
            )

        closed = replace(old, valid_until=effective_at)
        close_row(self._cur, self._schema, table, business_key, old.valid_from, effective_at)
        new = VersionedRow(
            table=table,
            business_key=business_key,
            payload=dict(payload),
            valid_from=effective_at,
            valid_until=None,
            recorded_at=self._clock(),
            recorded_by=actor,
        )
        insert_row(self._cur, self._schema, new)
        return new, closed


# ---- SQL 层 ----
# 下面三个模块级函数是 BitemporalDAO 与数据库之间的全部接口。
# 不写成实例方法的理由: 它们不接受 self 的业务状态, 只接受显式参数,
# 因此可以直接被单元测试覆盖, 不必构造 DAO 与假 cursor 的组合。


def _utcnow() -> datetime:
    """默认时钟。带时区, 因为 valid_from/valid_until 是 timestamptz。

    用 ``datetime.now()`` (naive) 会让写入的时区依赖服务器本地设置,
    跨时区部署后 as_of 的比较结果会错, 而错法是「查到错误的版本」而非报错。
    """
    return datetime.now(UTC)


def build_partial_unique_index_sql(schema: str, table: str, business_key_column: str) -> str:
    """构造「至多一条当前版本」的 partial unique index。

    这是不变量 1 的数据库级实现。放在 storage 层而不是 DDL 种子文件里的
    理由: 它必须对每张双时态表都成立, 而「哪些表是双时态」由本层知道。

    ``valid_until IS NULL`` 作为 partial index 的谓词 —— 只约束当前版本,
    历史版本可以有任意多条。
    """
    from .schema import quote_ident

    if not business_key_column.isidentifier():
        raise BitemporalError(f"业务键列名不合法: {business_key_column!r}")
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS "
        f"{quote_ident(f'ux_{schema}_{table}_current')} "
        f"ON {quote_ident(schema)}.{quote_ident(table)} ({quote_ident(business_key_column)}) "
        f"WHERE valid_until IS NULL"
    )


def _json_dumps(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _ts(value: datetime | None) -> str:
    """时间 -> SQL 表达式。None 渲染成 NULL 字面量。"""
    if value is None:
        return "NULL"
    from .schema import quote_literal

    return f"{quote_literal(value.isoformat())}::timestamptz"


def select_rows(cur: CursorLike, schema: str, table: str, business_key: str) -> list[VersionedRow]:
    """读一个业务键的全部版本, 按 valid_from 升序。"""
    from .schema import quote_ident, quote_literal

    sql = (
        f"SELECT valid_from, valid_until, payload, recorded_at, recorded_by "
        f"FROM {quote_ident(schema)}.{quote_ident(table)} "
        f"WHERE business_key = {quote_literal(business_key)} "
        f"ORDER BY valid_from ASC"
    )
    cur.execute(sql)
    out: list[VersionedRow] = []
    for valid_from, valid_until, payload, recorded_at, recorded_by in cur.fetchall():
        out.append(
            VersionedRow(
                table=table,
                business_key=business_key,
                payload=payload if isinstance(payload, Mapping) else {},
                valid_from=valid_from,
                valid_until=valid_until,
                recorded_at=recorded_at,
                recorded_by=recorded_by,
            )
        )
    return out


def insert_row(cur: CursorLike, schema: str, row: VersionedRow) -> None:
    """插入一个版本行。"""
    from .schema import quote_ident, quote_literal

    sql = (
        f"INSERT INTO {quote_ident(schema)}.{quote_ident(row.table)} "
        f"(business_key, payload, valid_from, valid_until, recorded_at, recorded_by) "
        f"VALUES ({quote_literal(row.business_key)}, "
        f"{quote_literal(_json_dumps(row.payload))}::jsonb, "
        f"{_ts(row.valid_from)}, {_ts(row.valid_until)}, "
        f"{_ts(row.recorded_at)}, "
        f"{quote_literal(row.recorded_by or '')})"
    )
    cur.execute(sql)


def close_row(
    cur: CursorLike,
    schema: str,
    table: str,
    business_key: str,
    valid_from: datetime,
    valid_until: datetime,
) -> None:
    """收口旧版本。

    WHERE 里带上 valid_from 是必要的: 只按 business_key + valid_until IS NULL
    更新, 在并发下会把别人刚插入的新版本也收口掉 —— 而 partial unique
    index 此时已生效, 那一 UPDATE 会匹配到 0 行却不报错, 静默丢一次取代。
    """
    from .schema import quote_ident, quote_literal

    sql = (
        f"UPDATE {quote_ident(schema)}.{quote_ident(table)} "
        f"SET valid_until = {_ts(valid_until)} "
        f"WHERE business_key = {quote_literal(business_key)} "
        f"  AND valid_from = {_ts(valid_from)} "
        f"  AND valid_until IS NULL"
    )
    cur.execute(sql)
