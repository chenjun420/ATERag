# ATERag 产测规格书 RAG 服务

规格书驱动的产测智能体 RAG 服务: 型号隔离 + 产品类型域隔离 + 章节级过滤 + Datalog 公式推理 + SHACL 约束验证 + 决策溯源, 经 MCP (streamable-http) 对外服务。

## 架构

```
MCP 客户端 (需求/用例/代码/参数 Agent)
        │ streamable-http
        ▼
┌─────────────────────────────────────────────┐
│ MCP Server (8 业务工具 + 5 管理工具)          │
├─────────────────────────────────────────────┤
│ RagService  三层装配 [model, _domain_T, _common] │
│   ├─ Qdrant 预过滤向量检索 (章节/类别/优先级)   │
│   ├─ PG pg_textsearch BM25 (zhparser 中文)     │
│   ├─ RRF 融合                                 │
│   └─ LightRAG mix (图导航, 型号 workspace)     │
├─────────────────────────────────────────────┤
│ InferenceEngine                              │
│   ├─ domain_rules/{common,power}/rules.yaml   │
│   │    115 规则 (37 公式 derive + 77 SHACL)      │
│   ├─ 多步推导链 (缺失输入自动派生)              │
│   └─ 决策溯源 (前提逐条标注 layer)             │
├─────────────────────────────────────────────┤
│ 存储: 192.168.5.24                            │
│   PG17 (vector/AGE/pg_textsearch/zhparser)    │
│   Qdrant v1.19.1 (workspace is_tenant 索引)   │
├─────────────────────────────────────────────┤
│ 模型: 阿里云 MaaS (供应商可换)                 │
│   LLM qwen3.7-flash (OpenAI 兼容协议)          │
│   Embedding qwen3.7-text-embedding (1024维)   │
└─────────────────────────────────────────────┘
```

## 知识隔离 (三级)

| workspace | 内容 | 可见性 |
|---|---|---|
| `{model_id}` | 型号个性化参数 | 仅本型号 |
| `_domain_{type}` | 产品类型通用知识/规则 | 同类型型号共享, 跨域拒绝 |
| `_common` | 单位换算等普适内核 | 全型号 |

- 导入规格书自动识别型号 ID 与产品类型; 新类型自动建域 (`registry.yaml` + 规则包骨架)
- 查询自动识别型号 (`model_id` 可省略); 多型号歧义/无法识别 → fail-closed 拒绝

## 快速开始

```bash
# 1. 环境 (Python 3.13)
uv sync

# 2. 配置
cp .env.example .env   # 填入 API Key (Key 禁止提交 git)

# 3. 部署存储栈 (192.168.5.24, 见 deploy/native/README)
# ... 一次性执行 01~05 脚本

# 4. 初始化 Qdrant (维度自动探测, 当前 1024)
QDRANT_VECTOR_SIZE=1024 python deploy/qdrant/init_tenant.py

# 5. 导入规格书 (自动识别型号/类型/实体)
python scripts/ingest_pa601.py

# 6. 构建领域知识库
python scripts/build_domain.py power common

# 7. 验证
python scripts/validate_pa601.py

# MCP Server 现已常驻板卡 (systemd), 开发机无需再手工启动
# 远程验证: $env:MCP_BASE='http://192.168.5.24:8080/mcp'; python scripts/mcp_e2e.py
```

## 新产品规格书上传与处理

```powershell
$env:BOARD_SSH_PASSWORD='<板卡密码>'
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe scripts\upload_new_spec.py "D:\docs\PN2000-24A 定制电源技术规格书.md"
```

前置校验 → 上传板卡 → 全链路导入 → 重启 MCP → 同步注册表。详见 [docs/规格书上传与处理.md](docs/规格书上传与处理.md)。

## 板卡 MCP 服务运维

```bash
sudo systemctl status aterag-mcp      # 查看
sudo systemctl restart aterag-mcp     # 改代码/改 registry.yaml 后重启
tail -f /var/log/aterag/mcp.err       # 日志
```

## 一键部署 / 一键验证 (板卡 192.168.5.24)

```bash
python scripts/deploy_board.py    # 全链路部署: 预检->Qdrant->双型号->Semantica->LightRAG
python scripts/verify_all.py      # 全链路验证: 17 个套件
```

Windows 控制台默认 GBK, 跑脚本前先设 `$env:PYTHONIOENCODING='utf-8'`。

## MCP 工具

业务: `search_requirements` `query_parameters` `calculate` `validate_constraints`
`get_test_cases` `get_fixture_spec` `search_cases` `optimize_process`

管理: `ingest_document` `build_domain_kb` `list_models` `list_domain_rules` `health`

## 领域规则

`domain_rules/{common,power}/rules.yaml` — 每条规则含公式/SHACL/来源 URL/置信度/自验用例。
新增规则只改 YAML (websearch 迭代采集 → 形式化 → test 自验)。

## 测试

```bash
python -m pytest tests/ -q            # 纯本地单测
python scripts/validate_pa601.py      # 集成验证 (需存储栈+模型)
```
