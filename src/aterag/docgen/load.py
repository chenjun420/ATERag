"""``seed/formula.csv`` -> PostgreSQL 装载器。

## 为什么需要这个模块

§18.10 注 9 要求 ``seed/*.csv`` 由 ``docgen`` **生成**(已由
:mod:`aterag.docgen.registry` 完成)。但「生成」只做了一半 —— 仓库里原本
**没有任何代码把这些 CSV 读进库**。

这不是小事:2026-10-04 首次在板卡(192.168.5.25)实测时,装载动作是手写
bash 完成的,而 CSV 的编码**不是 PostgreSQL 字面量**,连撞三次才成功:

==============================  ==================================  ==========
CSV 里的样子                       直\\\\copy 的结果                 实际编码
==============================  ==================================  ==========
25 列                             「最后期望字段后有额外数据」列数不匹配
``变换器拓扑``(无花括号)            「有缺陷的数组常量」              **竖线** 分隔
``2.0000 1.0000 -3.0000 ...``     「无效的类 numeric 输入语法」       **空格** 分隔
==============================  ==================================  ==========

手抄这段转换意味着「文档与代码不同源」—— 正��� §18.10 注 9 要防的事。所以
它必须落成代码, ��且**转换规则要有单一真相源**。

## 三种编码的区分(踩过才知道)

- 数组列(``var_refs``/``domain_tags``/``derive_from``/``used_by_*``)用 ``|``
  分隔, 不是 ``,`` —— 用 ``,`` 会与 CSV 自身的分隔符混淆。
- ``dimension_vec`` 用**空格**分隔, 不是逗号。它是 7 个 ``NUMERIC(8,4)``,
  必须逐分量转型, 整串转型会失败。
- 空数组序列化成**空字符串**, 不是 ``{}``。所以不能无脑
  ``string_to_array('')`` —— 那会得到 ``{''}``(含一个空串元素), 而不是空数组。

## 为什么不直接写 INSERT

用 ``INSERT ... SELECT`` + ``string_to_array`` 而不是让 ``\\\\copy`` 直灌:

- CSV 的 25 列里有 3 列(``name_zh_declared``/``name_source``/``source_kind``)
  是**溯源列, 目标表没有**。``COPY`` 的列清单必须与文件列数**完全一致**,
  不能跳过 —— 所以必须先落 staging 再投影。
- 数组转换必须在 SQL 里做, 因为 ``|`` -> ``text[]`` 的映射要靠
  ``string_to_array``, 而这正是「编码规则」的代码化落点。

生成的 DDL 里数组列是 ``TEXT[]``/``NUMERIC(8,4)[]``, 所以
``dimension_vec`` 转成 ``numeric[]`` 后由 PostgreSQL 在写入时报精度是否越界 ——
这正是我们想要的: **让数据库来判精度, 不在 Python 里四舍五入。**
"""

from __future__ import annotations

import csv
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

__all__ = [
    "ARRAY_SEPARATOR",
    "LoadResult",
    "count_rows",
    "load_formula_csv",
    "render_load_sql",
]

#: 数组列在 CSV 里的分隔符。见模块文档「三种编码的区分」。
ARRAY_SEPARATOR = "|"

#: ``dimension_vec`` 在 CSV 里的分量分隔符(空格)。
VEC_SEPARATOR = " "

#: CSV 列 -> 目标表列。**只列 CSV 里存在且表上也有的列**; 三个溯源列
#: (``name_zh_declared`` / ``name_source`` / ``source_kind``)目标表没有,
#: 刻意不投影 —— 它们只留在 CSV 里供人核对。
_CSV_TO_TABLE: tuple[tuple[str, str], ...] = (
    ("formula_id", "formula_id"),
    ("name_zh", "name_zh"),
    ("name_en", "name_en"),
    ("domain", "domain"),
    ("section", "section"),
    ("domain_tags", "domain_tags"),
    ("var_refs", "var_refs"),
    ("dimension_vec", "dimension_vec"),
    ("dimension_ok", "dimension_ok"),
    ("derive_from", "derive_from"),
    ("boundary", "boundary"),
    ("confidence", "confidence"),
    ("scope", "scope"),
    ("used_by_rule", "used_by_rule"),
    ("used_by_test", "used_by_test"),
    ("used_by_axon", "used_by_axon"),
    ("errata", "errata"),
    ("source_ref", "source_ref"),
    ("expr_latex", "expr_latex"),
    ("expr_plaintext", "expr_plaintext"),
    ("expr_ascii", "expr_ascii"),
    ("expr_ast", "expr_ast"),
)

#: CSV 里有、目标表**刻意没有**的溯源列。只留在 CSV 供人核对, 不入库 ——
#: 它们的用途是让「name_zh 来自标准名还是方案原文」「source_ref 是不是真
#: 标准号」这类判断**在 CSV 里可查**, 而不是变成库里的第二份真相。
_CSV_ONLY: frozenset[str] = frozenset(
    {"name_zh_declared", "name_source", "source_kind"}
)

#: 目标表里需要类型转换的列 -> 目标 SQL 类型。
_CASTS: dict[str, str] = {
    "domain_tags": "text[]",
    "var_refs": "text[]",
    "derive_from": "text[]",
    "used_by_rule": "text[]",
    "used_by_test": "text[]",
    "used_by_axon": "text[]",
    "dimension_vec": "numeric[]",
    "expr_ast": "jsonb",
}


@dataclass(frozen=True)
class LoadResult:
    """一次装载的结果。

    ``rows`` 是**回读**的行数, 不是「预计会插入多少」—— 两者不一致时
    :meth:`mismatched` 会给出对照, 避免「SQL 没报错就当成功」。
    """

    rows: int
    ddl_ok: bool
    error: str | None = None
    expected: int = 0
    _table: str = ""
    _dsn: str = ""
    _env: dict[str, str] | None = None

    def mismatched(self) -> bool:
        """回读行数与 CSV 行数是否不符。**不符就是失败**, 不因无报错而放过。"""
        return self.ddl_ok and self.rows != self.expected

    def verify(self) -> LoadResult:
        """回读行数并补进结果。查不到行数时置 ``rows=-1``, 不伪装成 0。"""
        if not self.ddl_ok:
            return self
        count = count_rows(self._dsn, self._table)
        return replace(self, rows=-1 if count is None else count)


def _sql_text(value: str) -> str:
    """CSV 文本 -> SQL 文本字面量(``None`` 表示 SQL NULL)。

    注意: 这里**刻意不做**「空串当 NULL」的处理 —— ``boundary`` 这类列
    「空串」与「NULL」语义不同(前者是方案写了空, 后者是方案没写),
    §18.4 的 ``name_zh`` / ``derive_from`` 缺口就是这么区分的。统一当 NULL
    会把两者的区别抹掉。
    """
    if value == "":
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _sql_array(col: str, sep: str, cast: str) -> str:
    """CSV 里的分隔串 -> SQL 数组。

    ``col`` 是**列引用**, 不是字面量 —— 早先一版把它当字符串加了引号, 于是
    生成 ``string_to_array('var_refs','|')``, 对每个公式都插出同一个常量数组。
    ``ast`` 照样通过, 行数也照样对, 但 ``var_refs`` 全库变成同一个值 ——
    又是一次「看起来完全正常」的静默失效。

    **空串必须映成空数组**而不是 ``{''}``: ``string_to_array('', '|')`` 在
    PostgreSQL 里返回 ``{''}`` —— 一个含单个空串元素的数组, 不是空数组。
    写进去会让 ``cardinality(used_by_test) = 1`` 这种假数据蒙混过关。
    """
    if col == "":
        return f"'{{}}'::{cast}"
    # COALESCE 不能省: ``var_refs``/``derive_from``/``domain_tags`` 在表上是
    # NOT NULL, 而 ``NULLIF`` 把空串变成 NULL —— 直接写进去会违反 NOT NULL。
    # 空数组与 NULL 语义不同: 前者是「方案没给符号/上游」, 后者是「没读过」。
    return (
        f"COALESCE(string_to_array(NULLIF({col}, ''), {_sql_text(sep)}), '{{}}')::{cast}"
    )


def render_load_sql(
    csv_path: Path,
    table: str = "l0_term.formula",
    *,
    csv_location: str | None = None,
) -> str:
    """生成装载 SQL: staging(全 25 列) -> 目标表(22 列)。

    为什么不直接 ``\\\\copy`` 进目标表: CSV 25 列而目标表只吃 22 列, 而
    ``COPY`` 的列清单必须与文件列数**完全一致**, 不能跳过 —— 必须先落
    staging 再投影。

    ``csv_location``: ``\\\\copy`` 里写的路径是**由 PostgreSQL 服务器进程读取的**,
    不是客户端。部署到别的机器时(板卡 192.168.5.25), 必须给服务器侧能读到的
    路径, 否则报「权限不够」—— 而这个错看起来像权限问题, 实际是路径指到了
    **客户端**的相对路径。表头校验始终读本地 ``csv_path``, 两者可以不同。
    """
    with csv_path.open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    known = set(header)
    missing = [src for src, _ in _CSV_TO_TABLE if src not in known]
    if missing:
        raise ValueError(f"CSV 缺列: {missing}; 实际表头 {header}")
    extra = sorted(known - {src for src, _ in _CSV_TO_TABLE} - _CSV_ONLY)
    if extra:
        raise ValueError(
            f"CSV 出现未登记的列 {extra}; 若是有意的溯源列请登记进 _CSV_ONLY"
        )

    # staging 的列与类型**照 CSV 原文**: 数组列一律按 text 收, 转换留到
    # SELECT 里做 —— 转换规则只有一处(``_sql_array``), 不会两处漂移。
    stg_cols = []
    for col in header:
        if col == "dimension_ok":
            stg_cols.append(f"{col} boolean")
        elif col == "confidence":
            stg_cols.append(f"{col} numeric")
        elif col == "expr_ast":
            stg_cols.append(f"{col} text")  # 校验交给 INSERT 时的 ::jsonb
        else:
            stg_cols.append(f"{col} text")

    table_cols = ", ".join(dst for _, dst in _CSV_TO_TABLE)
    select_items = []
    for src, dst in _CSV_TO_TABLE:
        if dst in _CASTS:
            cast = _CASTS[dst]
            if dst == "dimension_vec":
                expr = (
                    f"regexp_split_to_array(btrim({src}), "
                    f"{_sql_text(VEC_SEPARATOR)})::{cast}"
                )
            elif dst == "expr_ast":
                expr = f"{src}::{cast}"
            else:
                expr = _sql_array(src, ARRAY_SEPARATOR, cast)
        elif src == "dimension_ok":
            expr = src
        elif src == "confidence":
            expr = src
        else:
            expr = _sql_text_sql(src)
        select_items.append(f"  {expr} AS {dst}")

    return "\n".join(
        [
            "-- 由 docgen.load 生成, 请勿手工编辑(§18.10 注 9: 禁止手工维护)。",
            f"-- 源: {csv_path.name}",
            "",
            "BEGIN;",
            "CREATE TEMP TABLE stg (",
            "  " + ",\n  ".join(stg_cols),
            ");",
            # \copy 是 psql 元命令, 不能放进 dollar-quoted 块。
            f"\\copy stg FROM {_sql_text(csv_location or csv_path.as_posix())}"
            " WITH (FORMAT csv, HEADER true)",
            "",
            f"INSERT INTO {table} ({table_cols})",
            "SELECT",
            ",\n".join(select_items),
            "FROM stg;",
            "COMMIT;",
            "",
        ]
    )


def _sql_text_sql(col: str) -> str:
    """列 -> ``NULLIF`` 处理后的文本表达式。

    ``boundary``/``errata``/``source_ref`` 这类可空文本列: 空串保持空串
    (方案写了空), 只有想写 NULL 时才显式给 NULL。
    """
    return f"NULLIF({col}, '')" if col in _NULLABLE_TEXT else col


#: 目标表里可为 NULL 的文本列。空串**不**映射成 NULL —— 见 ``_sql_text`` 注释。
_NULLABLE_TEXT = frozenset({"boundary", "errata", "source_ref", "name_en"})


def load_formula_csv(
    csv_path: Path,
    dsn: str,
    table: str = "l0_term.formula",
    model_key: str = "pw_sr5400",
    *,
    csv_location: str | None = None,
) -> LoadResult:
    """把 CSV 装进 ``table``, 返回结果。

    需要本机可执行 ``psql``。两个部署环境相关的坑:

    1. RLS 依赖 ``app.current_model`` GUC, 经 ``PGOPTIONS`` 传入。漏了它不会
       报错, 而是**静默插入 0 行** —— 看着像成功, 实际什么都没进。
    2. ``\\\\copy`` 的路径由**服务器进程**读取。库在别的机器上时必须给
       ``csv_location``, 否则报「权限不够」(看着像权限问题, 其实是路径指到了
       客户端)。

    装载后会**回读行数并与 CSV 行数比对**, 不一致即视为失败 —— 见
    :meth:`LoadResult.mismatched`。
    """
    sql = render_load_sql(csv_path, table, csv_location=csv_location)
    env = {"PGOPTIONS": f"-c app.current_model={model_key}", "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
        input=sql,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        return LoadResult(rows=0, ddl_ok=False, error=proc.stderr.strip()[:2000])
    return LoadResult(rows=0, ddl_ok=True, _table=table, _dsn=dsn, _env=env)


def count_rows(
    dsn: str,
    table: str = "l0_term.formula",
    model_key: str = "pw_sr5400",
) -> int | None:
    """回读 ``table`` 的行数。**必须带 GUC**, 否则 RLS 下返回 0。

    返回 ``None`` 表示查询失败 —— 调用方**不得**把 ``None`` 当 0: 「查不到」
    与「确实是 0 行」是两件事, 混起来就会把装载失败报成「装载了 0 条」。
    """
    proc = subprocess.run(
        ["psql", dsn, "-tAc", f"SELECT count(*) FROM {table}"],
        capture_output=True,
        text=True,
        env={"PGOPTIONS": f"-c app.current_model={model_key}", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None
