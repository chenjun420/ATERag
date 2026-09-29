"""工作台写入闸验证 (P1b) —— 安全边界必须被证明, 不能只靠代码看着对。

这是本仓库唯一一处"能改入库代码"的通道, 所以验证重点不在功能,
而在"不该发生的写操作确实没发生":

  1. 路径闸: 绝对路径 / 路径穿越 / 白名单外 / 首尾空白 一律拒绝
  2. 受保护字段: id / fingerprint 不可被请求覆盖
  3. 单文件单提交: 一次批准只产生一个 commit, 且工作区干净时才有 commit
  4. 幂等: 重复批准同一状态不产生空提交(否则审计历史会被噪音淹没)
  5. 鉴权: 令牌缺失 -> 503 拒绝; 令牌错误 -> 401; 不提供未鉴权模式
  6. 读接口无副作用: 两次读取结果一致

用法: .venv\\Scripts\\python.exe scripts/verify_workbench.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.workbench import WriteNotAllowed, WriteRejected, approve_entry  # noqa: E402
from aterag.workbench.gate import WritePolicy, repo_is_clean  # noqa: E402

REPO = Path(".").resolve()
PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"✅ {name}" + (f" | {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"❌ {name} | {detail}")


def _raises(exc: type[Exception], fn) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    (抛出了非预期异常: {type(e).__name__}: {e})")
        return False
    return False


def make_repo() -> Path:
    """复制一份真实仓库到临时目录 —— 绝不拿工作仓库做写测试。"""
    tmp = Path(tempfile.mkdtemp(prefix="aterag_wb_"))
    dst = tmp / "repo"
    dst.mkdir()
    for item in ("config",):
        shutil.copytree(REPO / item, dst / item)
    subprocess.run(["git", "init", "-q"], cwd=str(dst), check=True)  # noqa: S603, S607
    subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        cwd=str(dst),
        check=True,
    )
    return dst


def main() -> int:
    repo = make_repo()
    ann_dir = repo / "config" / "annotations"
    ann_files = sorted(ann_dir.glob("*.yaml"))
    if not ann_files:
        check("前置-存在注记文件", False, "仓库内无注记文件, 无法验证")
        return 1
    rel = ann_files[0].relative_to(repo).as_posix()

    policy = WritePolicy(repo_root=repo, allowed_rel=frozenset({rel}))
    import yaml

    doc = yaml.safe_load(ann_files[0].read_text(encoding="utf-8")) or {}
    key = next(iter((doc.get("entries") or {}).keys()), None)
    if not key:
        check("前置-注记含条目", False, str(ann_files[0]))
        return 1

    # ---------- 1. 路径闸 ----------
    for bad, why in (
        ("C:/Windows/System32/evil.yaml", "绝对路径"),
        ("../../etc/passwd", "路径穿越"),
        ("config/../../../outside.yaml", "中途穿越"),
        ("config/condition_patterns.yaml", "白名单外(抽取规则)"),
        ("config/doc_profiles.yaml", "白名单外(抽取规则)"),
        (" config/annotations/x.yaml ", "首尾空白"),
    ):
        check(
            f"路径闸-拒绝{why}",
            _raises(WriteNotAllowed, lambda b=bad: policy.resolve(b)),
            bad,
        )
    check(
        "路径闸-白名单内放行",
        policy.resolve(rel) == (repo / rel).resolve(),
        rel,
    )

    # ---------- 2. 受保护字段 ----------
    check(
        "字段闸-拒绝改 id",
        _raises(
            WriteRejected,
            lambda: approve_entry(policy, rel, "entries", key, "tester", {"id": "x"}),
        ),
        "id 不可覆盖",
    )
    check(
        "字段闸-拒绝改 fingerprint",
        _raises(
            WriteRejected,
            lambda: approve_entry(policy, rel, "entries", key, "tester", {"fingerprint": "x"}),
        ),
        "fingerprint 不可覆盖",
    )

    # ---------- 3. 提交与幂等 ----------
    def git(*a: str) -> str:
        # 用 subprocess + utf-8 显式解码: os.popen 走系统代码页(GBK),
        # 中文签字人会被读成乱码, 断言"commit 记了签字人"就会假失败。
        return subprocess.run(  # noqa: S603
            ["git", *a],  # noqa: S607
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout.strip()

    check("git-初始工作区干净", repo_is_clean(repo))
    n0 = int(git("rev-list", "--count", "HEAD"))
    r1 = approve_entry(policy, rel, "entries", key, "张三")
    n1 = int(git("rev-list", "--count", "HEAD"))
    check("批准-产生回执", bool(r1.commit), f"commit={r1.commit} fields={list(r1.changed_fields)}")
    check("批准-写后工作区干净", repo_is_clean(repo), "提交后不应残留改动")
    check("批准-恰好新增一个 commit", n1 - n0 == 1, f"{n0} -> {n1}")
    top = git("log", "-1", "--pretty=%s")
    check("批准-commit 记了签字人", "张三" in top, top[:60])

    # 重复批准 -> 拒绝且不产生新提交
    commits_before = n1
    dup = _raises(WriteRejected, lambda: approve_entry(policy, rel, "entries", key, "张三"))
    commits_after = int(git("rev-list", "--count", "HEAD"))
    check("幂等-重复批准被拒", dup, "无变化时不提交, 避免审计噪音")
    check(
        "幂等-提交数未变", commits_before == commits_after, f"{commits_before} -> {commits_after}"
    )

    # ---------- 4. 参数校验 ----------
    check(
        "校验-空白签字人被拒",
        _raises(WriteRejected, lambda: approve_entry(policy, rel, "entries", key, "   ")),
        "空白签字无审计价值",
    )
    check(
        "校验-不存在的键被拒",
        _raises(
            WriteRejected,
            lambda: approve_entry(policy, rel, "entries", "SR-NOT-EXIST-9999", "李四"),
        ),
        "防写错需求",
    )

    # ---------- 5. 鉴权 ----------
    # fastapi 是可选依赖(ATERag 主仓不需要 web 框架, 只有部署工作台时才装)。
    # 缺失时跳过这一段而不是让整个验证崩掉 —— 但要明确报出来, 不能静默跳过。
    try:
        from fastapi.testclient import TestClient
    except ModuleNotFoundError:
        check("鉴权-接口层", False, "未安装 fastapi, 跳过接口层验证 (pip install fastapi httpx)")
        print(f"\n===== {PASSED}/{PASSED + FAILED} passed =====")
        shutil.rmtree(repo.parent, ignore_errors=True)
        return 1 if FAILED else 0

    from aterag.workbench.api import TOKEN_ENV, create_app

    os.environ.pop(TOKEN_ENV, None)
    app = create_app(repo_root=repo)
    client = TestClient(app)
    h = client.get("/health")
    check(
        "鉴权-令牌缺失时 health 报告未配置",
        h.json().get("token_configured") is False,
        str(h.json().get("token_configured")),
    )
    r = client.get("/workbench/snapshot", params={"model_id": "PA601-D54A"})
    check("鉴权-令牌缺失拒绝读(503)", r.status_code == 503, f"status={r.status_code}")
    r = client.post("/workbench/approve", json={"model_id": "x", "req_id": "y", "actor": "z"})
    check("鉴权-令牌缺失拒绝写(503)", r.status_code == 503, f"status={r.status_code}")

    os.environ[TOKEN_ENV] = "s3cret-for-test"
    client2 = TestClient(create_app(repo_root=repo))
    r = client2.get("/workbench/snapshot", params={"model_id": "PA601-D54A"})
    check("鉴权-无令牌拒绝(401)", r.status_code == 401, f"status={r.status_code}")
    r = client2.get(
        "/workbench/snapshot",
        params={"model_id": "PA601-D54A"},
        headers={"X-Workbench-Token": "wrong"},
    )
    check("鉴权-错令牌拒绝(401)", r.status_code == 401, f"status={r.status_code}")
    r = client2.get(
        "/workbench/snapshot",
        params={"model_id": "PA601-D54A", "detail": "false"},
        headers={"X-Workbench-Token": "s3cret-for-test"},
    )
    check("鉴权-正确令牌放行(200)", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        body = r.json()
        check(
            "读-首屏不含全量明细",
            "conditions" not in body and "scenarios" not in body,
            f"detail=false 仅回统计+任务 ({len(body.get('tasks', []))} 任务)",
        )
        check(
            "读-回统计",
            "stats" in body and "rows_total" in body["stats"],
            f"rows_total={body['stats'].get('rows_total')}",
        )
    # 两次读一致(无副作用)
    a = client2.get(
        "/workbench/snapshot",
        params={"model_id": "PA601-D54A", "detail": "false"},
        headers={"X-Workbench-Token": "s3cret-for-test"},
    ).json()
    b = client2.get(
        "/workbench/snapshot",
        params={"model_id": "PA601-D54A", "detail": "false"},
        headers={"X-Workbench-Token": "s3cret-for-test"},
    ).json()
    check("读-两次结果一致", a.get("stats") == b.get("stats"), "无副作用")
    os.environ.pop(TOKEN_ENV, None)

    shutil.rmtree(repo.parent, ignore_errors=True)
    print(f"\n===== {PASSED}/{PASSED + FAILED} passed =====")
    if FAILED:
        print(f"WORKBENCH FAIL ({FAILED} 项)")
        return 1
    print("WORKBENCH PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
