# ATERag 存储栈部署与接入说明

> 目标机: 192.168.5.25 (Debian 12, ARM64, 内核 6.1 高度裁剪)
> 方式: **原生安装** (该内核无 BPF/bridge/veth/mqueue, 容器方案不可行)

## 架构

| 组件 | 版本 | 安装方式 | 端口 |
|---|---|---|---|
| PostgreSQL | 17.11 (PGDG) | apt | 5432 |
| pgvector | PGDG apt | apt | - |
| Apache AGE | release/PG17/1.7.0 | 源码编译 | - |
| pg_textsearch | v1.4.0 | 源码编译 | - |
| zhparser + SCWS 1.2.3 | 最新 | 源码编译 | - |

## 执行步骤 (按序)

```bash
# 0. 上传 deploy/native/ 到板卡 ~/aterag/native/

# 1. PostgreSQL 17 + pgvector + 编译依赖 (~5min)
bash ~/aterag/native/01-install-postgres.sh

# 2. 扩展编译: AGE -> SCWS -> zhparser -> pg_textsearch (~10min)
bash ~/aterag/native/02-build-extensions.sh
# 注: scws 若 git clone 缺 configure, 用官方包:
#   curl -o /tmp/scws.tar.bz2 http://www.xunsearch.com/scws/down/scws-1.2.3.tar.bz2
#   cd /tmp && tar xjf scws.tar.bz2 && cd scws-1.2.3
#   ./configure --prefix=/usr/local --disable-static && make -j8 && sudo make install && sudo ldconfig
# 注: pg_textsearch 若 make install 报错, 手动安装:
#   PKGLIB=/usr/lib/postgresql/17/bin/pg_config --pkglibdir
#   find . -name '*.so' | xargs -I{} sudo install -m755 {} $PKGLIB/
#   find . -name '*.control' -o -name '*.sql' | xargs -I{} sudo install -m644 {} /usr/share/postgresql/17/extension/


# 4. PG 配置 (shared_preload=age,pg_textsearch) + 数据库/角色/扩展初始化
bash ~/aterag/native/04-init-postgres.sh
# 若因扩展名报错, 重跑修正版: sudo -u postgres psql -d power_specs -f 05-extensions-fixed.sql

# 5. AGE 目录权限补丁 (powerspec 需读 ag_graph/ag_label)
sudo -u postgres psql -d power_specs -c "GRANT SELECT ON ag_catalog.ag_graph TO powerspec; GRANT SELECT ON ag_catalog.ag_label TO powerspec; GRANT USAGE ON SCHEMA ag_catalog TO powerspec;"
```

## 凭据 (部署后请修改)

- PG 管理员: `postgres / <pg_admin_password>`
- 业务账号: `powerspec / <pg_app_password>` (库 `power_specs`)
- pg_hba: 允许 192.168.5.0/24 (scram-sha-256)

## 服务管理

```bash
sudo systemctl disable --now podman-restart  # 如存在
```

## 验证

```bash
# 板卡本机
psql "postgresql://powerspec:<password>@localhost:5432/power_specs" -c "SELECT extname FROM pg_extension;"
curl -s http://localhost:6333/healthz

# 开发机 (Windows)
```

## 数据目录

- PG 数据: `/var/lib/postgresql/17/main` (系统默认)
- 部署脚本: `~/aterag/native/`

## 已知注意点

1. 板卡内核裁剪严重 (无 BPF/bridge/veth/POSIX mqueue), **任何容器方案都会失败**, 勿再尝试
2. pg_textsearch 需在 `shared_preload_libraries` 中 (已配置)
3. 中文 BM25 用 `public.chinese` text search 配置 (zhparser, 已创建)
4. LightRAG workspace 每型号独立 AGE graph (`{workspace}_{namespace}`, `-` 自动转 `_`)
5. **LightRAG workspace 必须全小写**: merge 阶段以 `{graph_name}.base` 不带引号拼接,
   PostgreSQL 折叠大写标识符会报 `关系不存在`; 代码用 `lrag_workspace()` 归一化
6. pg_textsearch BM25 检索需显式 `to_bm25query('查询', '索引名')`, 隐式探测对带
   WHERE 的查询不生效
8. 阿里云 MaaS embedding 走 DashScope 原生协议, 实测维度 1024 (qwen3.7-text-embedding)
9. AGE 权限: powerspec 需 ag_catalog USAGE + ag_graph/ag_label SELECT (脚本 05 已含)
10. Semantica 语义图 (`power_rules`) 与 LightRAG 独立: `powerspec` 非超级用户, **不能 `LOAD 'age'`**,
    `scripts/sync_semantica.py` 的 `ApacheAgeStore` 子类已跳过 LOAD, 只做 `SET search_path = ag_catalog` + `create_graph`
11. **workspace 必须全小写且经 `lrag_workspace()` 归一化**: 不归一化时 LightRAG 图谱写入会失败
    (报 `Error executing graph query`), 但 KV/向量层已落库 → 残留"失败却带数据"的脏 workspace。
    `build_lightrag()` 已加守卫直接拒绝; 清理用 `python scripts/board_clean_lrag.py --apply`
12. **`DROP GRAPH` 不是 SQL**, 是 AGE 的 Cypher 命令。psycopg 下用 `SELECT ag_catalog.drop_graph(name, true)`;
    走 `cypher()` 则不支持参数绑定 (只能字面量内联, 与 Semantica 同样的坑)
13. `lightrag_full_entities` / `lightrag_full_relations` 是**每文档一行**的合并结果 (`count` 为实体/关系数),
    不是每实体一行; 排查时别按行数当实体数
14. 板卡是**受限容器** (overlayfs + 裁剪内核): `<board_user>` 无法在 `/opt` 建目录, 服务账号无法穿越 `/root/.local`。
    uv 与预编译解释器必须装共享可读路径 (`/usr/local/bin/uv` + `/opt/python`),
    否则 venv 里的 `python` 是指向 `/root` 的死链, 报"权限不够"
15. **`sudo -n cmd1 && cmd2` 只给 cmd1 提权**, cmd2 仍以 SSH 用户身份跑 → `chown` 报
    "Operation not permitted"。多步 root 操作一律写 `sudo -n sh -c '<整条命令链>'`
16. 板卡 venv 由 sudo 创建时, 跑 `uv` 必须加 `--no-config`: uv 从 CWD 向上找 `uv.toml`/`pyproject.toml`,
    服务账号读不了 SSH 用户家目录会直接失败
17. `registry.yaml` 在 **MCP 服务启动时**加载 (模块级 `Registry.load`)。导入新型号后必须
    `systemctl restart aterag-mcp`, 否则"导入成功但查不到"; `upload_new_spec.py` 已自动串上这一步
18. `.env` / `registry.yaml` / `domain_rules` / `rag_storage` 全部**相对 CWD** 解析。
    板卡上经 SSH 执行时 CWD 未必是应用根 → CLI 必须先 `os.chdir(APP_ROOT)`
19. 域 workspace 是否建 AGE 图谱取决于**有无叙述层 (.md)**: 纯规则 YAML 的域
 只写业务表, 不建图谱, 属预期
20. 解析 workspace 必须走 `domain_workspace(domain)`, 不能直读 `entry.workspace` 字段

## Step 6: MCP Server 板卡常驻 (应用层)

MCP 服务已从"开发机前台进程"迁到板卡 systemd 常驻, 板卡重启自动拉起。

```bash
# 开发机一键部署 (上传源码 + 生成板卡 .env + 执行板卡 Step6 + 验证)
$env:BOARD_SSH_PASSWORD='<板卡 SSH 密码>'
python scripts/deploy_mcp_board.py       # 期望 DEPLOY_MCP_BOARD PASS
python scripts/verify_mcp_service.py     # 期望 MCP_SERVICE_VERIFY PASS 8/8
```

板卡侧手工执行 (已装过的可跳过):

```bash
sudo -i
bash /opt/aterag/native/06-install-aterag.sh   # apt + uv + CPython3.13 + venv + 依赖 + systemd
```

| 位置 | 内容 |
|---|---|
| `/opt/aterag/.venv` | venv (uv 预编译 CPython **3.13.15**, 不走源码编译) |
| `/opt/aterag/src/aterag` | 源码 (systemd 以 `PYTHONPATH` 直挂, 改代码重启即生效) |
| `/opt/aterag/.env` | 配置, 权限 600 属 `aterag`; 存储端点已改 **127.0.0.1** |
| `/opt/aterag/registry.yaml` | **注册表权威副本** (开发机副本由 `upload_new_spec.py` 自动同步) |
| `/opt/aterag/specs/` | 上传的新规格书原件 |
| `/var/log/aterag/mcp.{log,err}` | 服务日志 |

```bash
sudo systemctl status aterag-mcp
sudo systemctl restart aterag-mcp
journalctl -u aterag-mcp -f        # 或直接 tail /var/log/aterag/mcp.err
```

## ATERag 应用层部署与验证 (存储栈就绪后)

```bash
python scripts/deploy_board.py       # 期望 DEPLOY_BOARD PASS 6/6

# 启动 MCP 服务 (需先启动, 图导航服务态验证依赖它)
python -m aterag.mcp_server.server   # http://0.0.0.0:8080/mcp

# 一键全链路端到端验证 (17 个套件)
python scripts/verify_all.py         # 期望 VERIFY_ALL PASS 17/17

# 清理未归一化(大写)的脏 LightRAG workspace
python scripts/board_clean_lrag.py --drop-duplicates            # 预览
python scripts/board_clean_lrag.py --drop-duplicates --apply    # 执行
```

排障脚本: `board_preflight.py` (存储栈/落库量) `board_lrag_status.py` (LightRAG 文档状态与合并实体数)
`board_lrag_detail.py` (各表按 workspace 分布) `board_clean_lrag.py` (脏 workspace 清理)
`board_tables.py` (表清单) `board_schema.py` (表结构) `board_age_funcs.py` (ag_catalog 函数签名)
`debug_graph_mix.py` (图检索返回结构) `debug_graph_parse.py` (上下文段落解析)
`rules_selftest.py` + `debug_rule_test.py` (单条规则)
