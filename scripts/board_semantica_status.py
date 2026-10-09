"""Semantica 语义图实况核查: 物理存储位置 + 节点/边统计 + **服务路径接入判定**.

用法: .venv\\Scripts\\python.exe scripts\\board_semantica_status.py

为什么要有「服务路径接入判定」这一段 (2026-10-09 补)
------------------------------------------------------
这个脚本原来只在 docstring 里承诺「运行时接入判定」, 而 main() 里没有任何判定
代码 —— 只查了物理基表和节点/边计数。docstring 承诺的东西不实现, 比不写更坏:
看脚本的人会以为「节点在、边在」就等于「这条能力在服务面上生效」, 而这两件事
需要分别核对。

所以脚本现在把两件事分开报:

* **数据面**: ``scripts/sync_semantica.py`` 把 domain_rules 写成 AGE 图谱,
  是按需的运维动作, 数据真实存在(实测 426 节点 / 520 边)。
* **服务面**: ``src/aterag`` 下确有 12 处 import semantica, 分布在
  ``api/explorer.py`` / ``kg/{analytics,graph,materialize}.py`` /
  ``provenance/{pg_storage,seed_loader}.py`` / ``conflicts/adapter.py`` /
  ``inference/decision_explain.py`` —— **都是服务路径**。其中
  ``api/explorer.py`` 是 Explorer 那个 HTTP 界面的组装入口。

**这段判定必须由脚本算出来, 不能靠记忆或一次 grep。** 浅层 glob 会漏掉函数内
import 与嵌套子目录, ``pip show`` 的返回码在 Windows venv 上也不可靠 ——
两者都会得出「服务面没用 semantica」这种会导致误删依赖的错误结论。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings

LABELS = ["Rule", "Category", "Scope", "Source", "Formula", "Shape"]
EDGES = ["BELONGS_TO", "CITES", "IN_SCOPE", "HAS_FORMULA", "HAS_CONSTRAINT"]

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "aterag"

#: 服务路径的判定口径: **src/aterag 下**任何 import semantica 都算接入。
#: scripts/ 与 tests/ 不算 —— 那是运维动作与测试, 不是被服务的代码。
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+.*\bsemantica\b", re.MULTILINE)


def service_path_consumes_semantica() -> tuple[bool, list[str]]:
    """服务路径是否真的 import semantica。返回 ``(是否接入, 命中位置)``。

    刻意扫 ``src/aterag`` 的**全部** ``.py`` 而不只是被 import 到的模块:
    静态扫文件才能覆盖「暂时没被路由到但已写好」的接入, 而动态追踪覆盖不了
    ``importlib.import_module("semantica...")`` 这类写法。

    扫不到也不等于「确定没接」—— 所以下面同时用 ``pip show`` 确认包在不在,
    两项分开报, 不合成一句结论。
    """
    hits: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _IMPORT_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            hits.append(f"{py.relative_to(REPO).as_posix()}:{line}")
    return bool(hits), hits


def _package_installed() -> str:
    """semantica 是否可用, 以及版本。

    **实际 import 而不是 ``pip show``** —— 第一版这里用的是
    ``pip show semantica`` 的返回码, 在本机 Windows venv 上它报「未安装」,
    而同一时刻 ``import semantica`` 成功且版本 0.7.0。子进程 pip 的解析结果
    不可信, 而「能不能 import」是唯一与运行时一致的事实。

    顺带把那一版踩的坑写在这里: 终端代码页会把中文吞掉, ``print`` 出来的
    「已安装」可能被读成「未安装」。所以下面只用 ASCII 输出, 且 import 失败
    时把异常类型一并打出来 —— 让人能区分「没装」与「装了但导入炸了」。
    """
    try:
        import semantica  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 装残了也要报得出真相
        return f"IMPORT-FAILED ({type(e).__name__}: {e})"
    return f"OK v{getattr(semantica, '__version__', '?')}"


def main() -> int:
    s = get_settings()
    g = s.semantica_graph
    print(f"=== Semantica 语义图实况 (graph={g}) ===")
    print(f"配置: SEMANTICA_ENABLED={s.semantica_enabled} SEMANTICA_GRAPH={g}\n")
    with psycopg.connect(s.postgres_dsn) as c, c.cursor() as cur:
        cur.execute("SELECT name FROM ag_catalog.ag_graph WHERE name = %s", (g,))
        if not cur.fetchone():
            print(f"图谱 {g} 不存在")
            return 1

        # 物理落点: AGE 每个 label / 边类型一张基表 (pg_class 里同名的还有索引, 需 relkind='r' 过滤)
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r' "
            "AND c.relname NOT LIKE 'ag_label%%' ORDER BY c.relname",
            (g,),
        )
        print("--- 物理基表 (schema = graph 名) ---")
        for (rel,) in cur.fetchall():
            cur.execute(f'SELECT count(*) FROM "{g}"."{rel}"')
            print(f"  {rel:16s} rows={cur.fetchone()[0]}")

        print("\n--- 按 label 统计节点 ---")
        total = 0
        for lb in LABELS:
            cur.execute(f'SELECT count(*) FROM "{g}"."{lb}"')
            n = cur.fetchone()[0]
            total += n
            if n:
                print(f"  {lb:12s} {n}")
        print(f"  节点合计 {total}")

        print("\n--- 按类型统计边 ---")
        etotal = 0
        for et in EDGES:
            try:
                cur.execute(f'SELECT count(*) FROM "{g}"."{et}"')
                n = cur.fetchone()[0]
                etotal += n
                if n:
                    print(f"  {et:16s} {n}")
            except psycopg.Error:
                c.rollback()
        print(f"  边合计 {etotal}")

    print(f"\n数据落点: PostgreSQL {s.postgres_dsn.split('/')[-1]} 的 AGE 图谱 {g} (schema {g})。")

    # ---- 服务路径接入判定 ----
    print("\n--- 服务路径接入判定 ---")
    connected, hits = service_path_consumes_semantica()
    print(f"  package          : semantica {_package_installed()}")
    print(f"  SEMANTICA_ENABLED: {s.semantica_enabled}")
    print(f"  SEMANTICA_GRAPH  : {g}")
    print(f"  svc-path imports : {len(hits)} hit(s) under src/aterag")
    if connected:
        for h in hits:
            print(f"      {h}")
        print()
        print("  => 数据面有数, 服务面也确实消费 semantica。")
        print(
            "     注意 SEMANTICA_ENABLED 本身只被 ops 脚本读"
            "(sync_semantica / verify_semantica / 本脚本),"
        )
        print("     服务路径不读它 —— 它不是这个图谱的开关, 别当成能关掉服务面依赖的旋钮。")
    else:
        print("  => 数据面有数, 但服务面零 import。图谱目前只是双写目标 + 运维核查对象,")
        print("     删掉它服务的任何行为都不会变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
