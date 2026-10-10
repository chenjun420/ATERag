"""AGE 访问路径门禁: 官方 API 与旁路各自的状态。

**为什么钉这个**
--------------
板卡上服务角色 ``powerspec`` 非超级用户, ``LOAD 'age'`` 被拒, 所以
``ApacheAgeStore.connect()`` **必然失败**, 而 ``sync_semantica`` 用
``_AgeStoreNoLoad``(search_path 旁路)绕开。这是既有条件, 不是缺陷。

但它是一根**随时会断的弦**: 上游升级若改了 connect 的初始化方式, 旁路会
静默失效(不报错, 只是写不进图)。所以两条路径都必须被观察:

* ``official`` 一直是失败 -> 权限模型未变, 旁路仍是唯一可行路径
* ``official`` 变成可用 -> **有人给服务角色提了权**, 该重新评估旁路是否还有
  必要(以及那次提权是否是有意的)

**没有 PG 时整组跳过**: CI 没有 PG service(实测), 门禁不该因环境缺依赖而
恒红。但也不能因此静默 —— skip 数量会被 CI 日志看见。

**为什么不给服务角色提权**
------------------------
让官方 API 原样可用最直接的办法是把 ``powerspec`` 提成超级用户。那是**安全
降级**: 应用角色一旦是超级用户, 任何注入或误操作都能读写整库, 而它本该只碰
``aterag_entities`` / ``aterag_chunks`` / ``alembic_version`` 三张表。所以选
旁路 —— 权限模型不变, 代价只是多一个子类。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def _dsn() -> str | None:
    try:
        from aterag.config import get_settings

        return get_settings().postgres_dsn
    except Exception:  # noqa: BLE001
        return None


DSN = _dsn()


def _pg_available() -> bool:
    if not DSN:
        return False
    try:
        import psycopg

        with psycopg.connect(DSN, connect_timeout=5) as c:
            c.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="需要可连的 PG")


class TestAgeAccessPaths:
    def test_both_paths_are_reported_not_just_the_working_one(self):
        """两条路径都返回结果。

        只报能用的那条会吞掉「官方 API 可用了」这个信号 —— 而那是权限模型
        变了的唯一迹象。
        """
        from sync_semantica import age_access_paths

        out = age_access_paths(DSN, "power_rules")
        assert set(out) >= {"official", "bypass"}, out
        assert isinstance(out["official"], str)
        assert isinstance(out["bypass"], str)

    def test_bypass_can_read_the_graph(self):
        """旁路必须真能读出图谱规模, 不只是 connect 成功。"""
        from sync_semantica import age_access_paths

        out = age_access_paths(DSN, "power_rules")
        assert out["bypass"] == "ok", f"旁路不可用: {out['bypass']}"
        stats = out.get("stats") or {}
        assert stats.get("node_count", 0) > 0, f"图谱为空: {stats}"
        assert stats.get("relationship_count", 0) > 0, f"图谱无边: {stats}"

    def test_rule_nodes_match_the_rules_file(self):
        """AGE 里的 Rule 节点数应等于 rules.yaml 的规则数。

        AGE 是分析图之外的一份派生副本(规则本身已直接进图, 见
        ``aterag.kg.rule_graph``), 所以两者必须一致 —— 不一致说明 AGE 过期了,
        而它过期不会自己暴露。
        """
        import yaml
        from sync_semantica import age_access_paths  # noqa: I001

        rules = yaml.safe_load(
            (ROOT / "domain_rules/power/rules.yaml").read_text(encoding="utf-8")
        )["rules"]
        want = sum(1 for r in rules if r.get("id"))
        out = age_access_paths(DSN, "power_rules")
        got = (out.get("stats") or {}).get("label_counts", {}).get("Rule")
        assert got == want, f"AGE 里有 {got} 条 Rule, rules.yaml 有 {want} 条 —— AGE 过期了"

    def test_official_api_state_is_explicit(self):
        """官方 API 的状态被明确记录, 而不是「没测过就算过」。

        当前板卡条件下它**应当**失败(非超级用户)。若哪天它通了, 说明权限模型
        变了 —— 那时应当重新评估 ``_AgeStoreNoLoad`` 还有没有必要, 并更新
        ``sync_semantica`` 的说明。此断言不强制它必须失败, 只强制「结果被测过
        且被记录」, 免得有人改配置后没人发现。
        """
        from sync_semantica import age_access_paths

        out = age_access_paths(DSN, "power_rules")
        assert out["official"] is not None
        if out["official"] == "ok":
            pytest.skip("官方 API 已可用 —— 权限模型已变更, 请重新评估 _AgeStoreNoLoad")

    def test_shim_does_not_grant_superuser(self):
        """旁路不靠提权, 因此服务角色仍必须是非超级用户。"""
        import psycopg

        with psycopg.connect(DSN, connect_timeout=5) as c:
            is_super = c.execute(
                "SELECT usesuper FROM pg_user WHERE usename = current_user"
            ).fetchone()[0]
        assert not is_super, (
            "服务角色成了超级用户 —— _AgeStoreNoLoad 旁路就没有存在理由了, 且应用角色不该有这个权限"
        )
