"""板卡应用层部署: 上传源码 + .env + systemd unit -> 执行 Step6 -> 验证常驻.

用法:
    $env:BOARD_SSH_PASSWORD='<板卡 SSH 密码>'
    .venv\\Scripts\\python.exe scripts\\deploy_mcp_board.py

幂等: 重复执行只覆盖文件, 不影响已入库知识。
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.25")
USER = os.environ.get("BOARD_SSH_USER", "")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")
APP_DIR = "/opt/aterag"
PORT = "8080"

#: 全系统嵌入维度, 由 ADR-013 定死(``halfvec``)。改这里等于改 ADR。
#:
#: 本地 `.env` 缺 ``EMBED_DIM`` 或值不等于它时, :func:`board_env` 直接 raise ——
#: 让部署在写板卡 ``.env`` 之前就失败, 而不是在灌库时被 pgvector 的 2000 维
#: 上限拦下(那时错���已经写进板卡了)。
EMBED_DIM = 1024

# (本地路径, 板卡远端相对路径)
FILES = [
    ("src/aterag", "src/aterag"),
    ("domain_rules", "domain_rules"),
    # 种子知识: 实体/关系/公理/定理/corrections_applied。**必须整目录部署** ——
    # 板卡 KG 与 Explorer 都从 data/seed/power_domain_seed.json 读领域知识,
    # 而 corrections.yaml 是 corrections_applied 的唯一来源(规格表里查证的
    # 权威等级、废止关系、条款号都写在里面)。漏传的后果不是报错, 而是板卡上
    # 知识层静默停留在上一次部署的版本 —— 实测过。
    ("data/seed", "data/seed"),
    # 表结构档案: entity_extract 运行时唯一的表头语义来源, 缺失会直接抛
    # FileNotFoundError (而不是静默丢列), 故必须随代码一起部署
    # config/ 同时承载表结构档案与抽取档案 (表头语义/章节先验/剔除词/条件规则),
    # 缺失会让 ingest 与条件抽取直接抛错, 故整目录部署
    ("config", "config"),
    ("registry.yaml", "registry.yaml"),
    ("pyproject.toml", "pyproject.toml"),
    # 板卡侧新规格书导入 CLI (upload_new_spec.py 远程调用的入口)
    ("scripts/ingest_new_spec.py", "scripts/ingest_new_spec.py"),
    # 板卡侧验证脚本: 对账/覆盖度需在 APP_ROOT 下运行 (相对 CWD 解析配置与侧车)
    ("scripts/verify_requirement_coverage.py", "scripts/verify_requirement_coverage.py"),
    # rag_storage/blocks 侧车: 条件抽取的离线确定性通道依赖它
    ("rag_storage", "rag_storage"),
    ("deploy/native/aterag-mcp.service", "native/aterag-mcp.service"),
    ("deploy/native/06-install-aterag.sh", "native/06-install-aterag.sh"),
    ("deploy/native/06a-install-uv-python313.sh", "native/06a-install-uv-python313.sh"),
]

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def board_env(local_env: str) -> str:
    """生成板卡侧 .env: 密钥沿用开发机, 存储端点改指本机回环.

    MCP 服务现在跑在板卡上, 再用 192.168.5.25 自指没有必要且依赖网卡地址;
    统一改 127.0.0.1 更快也更抗 IP 变更。
    """
    txt = open(local_env, encoding="utf-8").read()
    txt = re.sub(
        r"^(POSTGRES_DSN=postgresql://[^:@]+:[^@]+@)[^:/]+",
        r"\g<1>127.0.0.1",
        txt,
        flags=re.MULTILINE,
    )
    txt = re.sub(r"^(QDRANT_URL=https?://)[^:/]+", r"\g<1>127.0.0.1", txt, flags=re.MULTILINE)
    txt = re.sub(r"^(MCP_HOST=).*$", r"\g<1>0.0.0.0", txt, flags=re.MULTILINE)
    txt = re.sub(r"^(MCP_PORT=).*$", rf"\g<1>{PORT}", txt, flags=re.MULTILINE)
    if "MCP_PORT=" not in txt:
        txt += f"\nMCP_HOST=0.0.0.0\nMCP_PORT={PORT}\n"

    # ---- 嵌入服务: 三个键都**必须存在且显式** ------------------------------
    #
    # 为什么不能只靠「从开发机 .env 派生」: ``EMBED_DIM`` 缺失时不会报错, 而是
    # 回落到**服务端原生维度** —— 实测 ``doubao-embedding-vision`` 原生 2048,
    # 不传 ``dimensions`` 就是 2048。而 pgvector 的 HNSW 索引上限 2000 维, 于是
    # 灌库时才炸:
    #
    #   ProgramLimitExceeded: column cannot have more than 2000 dimensions
    #
    # 而库里的列是 ``vector(1024)``(ADR-013 定死全系统 1024 维 halfvec)。
    # 维度错配若没有这层兜底, 表现是「检索质量下降」而不是启动失败 ——
    # ADR-013 明确把它列为要避免的「静默降质」。
    #
    # 所以这里断言 EMBED_DIM 存在且等于 1024, 不满足直接 raise: 让部署在**上传
    # .env 之前**就失败, 而不是灌库时。
    for key in ("EMBED_BASE", "EMBED_MODEL", "EMBED_API_KEY"):
        # 取**最后一个非注释的非空**值, 而不是第一个。``.env`` 里同键出现多行是
        # 真实会发生的(改配置时追加而非替换, 或从 ``.env.example`` 复制时带了
        # 一行注释形态), 取第一个会读到被后面覆盖的那行。
        vals = [
            v.strip()
            for v in re.findall(rf"(?m)^{key}=(.*)$", txt)
            if v.strip()
        ]
        if not vals:
            raise RuntimeError(
                f"板卡 .env 缺 {key} —— 嵌入服务无法配置。"
                "缺它不会报错, 而是回落到服务端默认行为, 失败点被推迟到灌库时。"
            )

    dim = re.search(r"(?m)^#?EMBED_DIM=(\d+)\s*$", txt)
    if not dim:
        raise RuntimeError(
            "板卡 .env 缺 EMBED_DIM。缺它会回落到服务端原生维度(豆包 2048), "
            "与 ADR-013 定的 1024 维(halfvec)冲突, 灌库时 pgvector 会报 "
            "'more than 2000 dimensions'。"
        )
    if int(dim.group(1)) != EMBED_DIM:
        raise RuntimeError(
            f"板卡 .env 的 EMBED_DIM={dim.group(1)}, 应为 {EMBED_DIM}"
            "(ADR-013: 全系统固定 1024 维)。改动需先开 ADR 推翻, 不要在脚本里加特例。"
        )
    # 去掉注释形态, 让「这个键存在」这件事在文件里看得见
    txt = re.sub(r"(?m)^#(EMBED_DIM=)", r"\1", txt)

    return (
        "# ATERag 板卡运行时配置 (由 scripts/deploy_mcp_board.py 生成, "
        "存储端点=127.0.0.1)\n" + txt
    )


def upload_tree(sftp, local: str, remote: str) -> int:
    n = 0
    for root, dirs, files in os.walk(local):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(root, local).replace("\\", "/")
        rdir = remote if rel == "." else posixpath.join(remote, rel)
        try:
            sftp.mkdir(rdir)
        except OSError as e:
            # 已存在可忽略; 权限不足必须暴露 (否则后续 put 报 ENOENT, 根因难查)
            if not sftp.stat(rdir):
                raise RuntimeError(f"无法创建远端目录 {rdir}: {e}") from e
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            sftp.put(os.path.join(root, f), posixpath.join(rdir, f))
            n += 1
    return n


def run(cli: paramiko.SSHClient, cmd: str, timeout: int = 2400, sudo: bool = False) -> int:
    # sudo 必须包裹整条命令链: `sudo -n a && b` 只提权 a, b 仍以 SSH 用户身份跑
    full = f"sudo -n sh -c {shlex.quote(cmd)}" if sudo else cmd
    _, out, err = cli.exec_command(full, timeout=timeout)
    so = out.read().decode("utf-8", "replace")
    se = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    for ln in so.strip().splitlines()[-45:]:
        print("    " + ln[:200])
    if rc != 0:
        for ln in se.strip().splitlines()[-15:]:
            print("    ERR " + ln[:200])
    return rc


def main() -> int:
    # 用户名/口令都来自环境变量 (属环境口令, 禁止入库)。缺任一都必须 fail-fast:
    # 用空用户名去连接会得到语焉不详的 "Authentication failed", 根因极难定位
    # (曾经就因此误判成"口令失效")。
    missing = [n for n, v in (("BOARD_SSH_USER", USER), ("BOARD_SSH_PASSWORD", PWD)) if not v]
    if missing:
        print("未设置: " + ", ".join(missing))
        print("用法: $env:BOARD_SSH_USER='<ssh 用户>'; $env:BOARD_SSH_PASSWORD='<口令>'")
        return 1
    print(f"=== 板卡应用层部署 -> {USER}@{HOST}:{APP_DIR} ===")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        HOST,
        username=USER,
        password=PWD or None,
        timeout=20,
        # 密码未配置时回退到密钥认证 (取决于部署机持有私钥)。两条路都支持,
        # 部署才会是同模板可重复的: 否则换一台无密码缓存的机器就断
        look_for_keys=not PWD,
        allow_agent=False,
    )
    if not PWD:
        print("(BOARD_SSH_PASSWORD 未设, 使用 SSH 密钥认证)")
    try:
        # SFTP 以 SSH 用户身份写入, /opt/aterag 默认属 root -> 先授权给部署用户,
        # 安装脚本 (sudo) 会在最后把属主改回服务账号 aterag
        run(
            cli,
            f"mkdir -p {APP_DIR}/src {APP_DIR}/native && chown -R {USER}:{USER} {APP_DIR}",
            sudo=True,
        )

        sftp = cli.open_sftp()
        try:
            total = 0
            for local, remote in FILES:
                r = posixpath.join(APP_DIR, remote)
                if os.path.isdir(local):
                    n = upload_tree(sftp, local, r)
                    print(f"  [PUT] {local} -> {r} ({n} 文件)")
                    total += n
                else:
                    # 单文件: 先确保父目录存在 (SFTP 无 mkdir -p)
                    parent = posixpath.dirname(r)
                    try:
                        sftp.mkdir(parent)
                    except OSError:
                        pass
                    sftp.put(local, r)
                    print(f"  [PUT] {local} -> {r}")
                    total += 1
            # .env 单独生成 (端点改回环)
            with sftp.open(posixpath.join(APP_DIR, ".env"), "w") as fh:
                fh.write(board_env(".env"))
            print(f"  [PUT] .env -> {APP_DIR}/.env (端点改 127.0.0.1)")
        finally:
            sftp.close()
        print(f"  共上传 {total + 1} 项")

        # .env 含 API Key -> 仅属主可读, 且必须属服务账号。
        #
        # 原为 ``chmod 600``。但服务以 ``User=aterag`` 跑而文件属主也是 aterag,
        # 600 本身够用; 真正的问题是历史遗留的 750 —— 属组不匹配时读取直接
        # ``PermissionError: [Errno 13] Permission denied: '.env'``(实测踩到)。
        # 640 = 属主可读写、属组只读, 不开放给 world, 是配置文件该有的形态。
        run(
            cli,
            f"chown aterag:aterag {APP_DIR}/.env; "
            f"chmod 640 {APP_DIR}/.env; "
            f"chmod +x {APP_DIR}/native/*.sh",
            sudo=True,
        )
        # 读权限自检: 装完不验证, 等到第一次服务启动失败才发现, 根因却在权限上
        run(
            cli,
            f"sudo -u aterag sh -c 'head -1 {APP_DIR}/.env >/dev/null' "
            f"&& echo 'env 属主可读 OK' || echo 'env 读取失败(权限或属主)'",
            sudo=False,
        )

        print("\n>>> 知识门 (种子质量门禁, 在装依赖**之前**)")
        # 门禁必须在装依赖之前: 门禁只需要 python3 + PyYAML(读 corrections),
        # 而完整的 venv 要几分钟才装好。让一个「种子里引用解析不到」的问题
        # 在装完 127 个包之后才报, 是把便宜的检查排在了昂贵的后面。
        #
        # 知识门只报 ERROR(引用解析不到 / id 重复 / standard 却没标准号 /
        # confidence 越界), 非零退出即中止部署。WARN 只显示不拦 ——
        # 「无出处」是知识层的合法状态, 逼着人编出处比留着它坏。
        rc = run(
            cli,
            "cd /opt/aterag && python3 scripts/knowledge_gate.py "
            "--seed data/seed/power_domain_seed.json",
            timeout=120,
            sudo=False,
        )
        print(f"    rc={rc}")
        if rc != 0:
            print("DEPLOY_MCP_BOARD FAIL (知识门未通过; 种子有引用/形态问题, 不往下装)")
            return 1

        print("\n>>> 板卡 Step6 安装 (apt + uv + CPython3.13 + venv + 依赖 + systemd)")
        t0 = time.time()
        rc = run(cli, f"bash {APP_DIR}/native/06-install-aterag.sh", timeout=3000, sudo=True)
        print(f"    rc={rc} ({time.time() - t0:.0f}s)")
        if rc != 0:
            print("DEPLOY_MCP_BOARD FAIL (安装步骤)")
            return 1

        print("\n>>> 灌领域知识库 (build_domain)")
        # 这一步**不能省**。领域知识(116 条领域规则)与型号知识是两个 workspace:
        #   _domain_power  领域规则 + 公式 + 来源, 供 search_cases 的 domain 层
        #   PA601-D54A     型号规格数据
        # 漏掉它不会报错, 只会让 `search_cases` 的 domain 层**恒空** ——
        # 实测过: 未灌库时板卡 aterag_chunks 里 domain workspace 一条都没有,
        # 而工具照常返回结果(只是没有领域知识), 看起来一切正常。
        # build_domain 幂等(先清空该 workspace 再整批写), 重复执行安全。
        rc = run(
            cli,
            "cd /opt/aterag && sudo -u aterag /opt/aterag/.venv/bin/python -c "
            '"import asyncio,json,sys; sys.path.insert(0,\'/opt/aterag/src\'); '
            "from aterag.config import get_settings; from aterag.registry import Registry; "
            "from aterag.models import EmbeddingClient; "
            "from aterag.ingest.pipeline import build_domain; "
            "s=get_settings(); r=Registry.load(s); "
            "print(json.dumps(asyncio.run(build_domain(\'power\', s, r, "
            "EmbeddingClient(s), None)), ensure_ascii=False))\"",
            timeout=900,
            sudo=False,
        )
        print(f"    rc={rc}")
        if rc != 0:
            print("DEPLOY_MCP_BOARD WARN (领域库灌入失败; domain 层检索会恒空)")

        print("\n>>> 领域库落地核对")
        # 不看 build_domain 的返回值, 直接查库: 它返回 chunks 计数, 但真正要确认的是
        # 这些 chunk 真的进了 domain workspace —— 中间隔了几层(分块/嵌入/写库),
        # 任何一层静默失败都只表现为「检索结果少」。
        run(
            cli,
            "sudo -u postgres psql -d power_specs -Atc "
            '"select workspace_id || \' | layer=\' || layer || \' | n=\' || count(*) '
            "from public.aterag_chunks group by 1,2 order by 1\"",
            sudo=True,
        )

        print("\n>>> 服务状态与健康")
        run(cli, "systemctl is-enabled aterag-mcp; systemctl is-active aterag-mcp", sudo=True)
        run(cli, f"ss -ltn | grep ':{PORT}' || echo '{PORT} 未监听'", sudo=True)
        run(
            cli,
            "/opt/aterag/.venv/bin/python -V; echo '--- 错误日志 ---'; tail -25 /var/log/aterag/mcp.err || true",
            sudo=True,
        )
    finally:
        cli.close()
    print("DEPLOY_MCP_BOARD PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
