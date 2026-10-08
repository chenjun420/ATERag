# ATERag 产测规格书 RAG 服务

规格书驱动的产测智能体 RAG 服务: 型号隔离 + 产品类型域隔离 + 章节级过滤 + 规则驱动公式计算 + SHACL 约束验证 + 决策溯源, 经 MCP (streamable-http) 对外服务。

三项能力的**真实实现**（别按名字想当然）：

| 能力 | 实际实现 | 不是 |
|---|---|---|
| 公式计算 | `InferenceEngine` + `domain_rules/*/rules.yaml` 的声明式 `derive.expr`(安全表达式求值), 130 条规则自校(`scripts/rules_selftest.py`) | **不含 Datalog 引擎** —— semantica 的 `DatalogReasoner` 在本项目只被 `tests/` 与 `scripts/verify_joint_reasoning.py` 使用 |
| SHACL 约束验证 | `pyshacl`, 约束内嵌在 `rules.yaml` 的 `constraint.shape` 段(77 条) | 不是 semantica 的校验器 |
| 决策溯源 | 落 `l0_term.provenance`(`ProvenanceManager` + psycopg), 进程重启后仍可按 decision_id 查 | 不是进程内存里的 trace |

## 架构

```
┌──────────────────────────────────────────────────┐
│ MCP Server (8 业务工具 + 5 管理工具)             │
│ RagService  两层装配 [model, _domain_T]          │
│   ├─ pgvector 向量检索 (章节/类别/优先级预过滤)  │
│   ├─ PG pg_textsearch BM25 (zhparser 中文)       │
│   ├─ RRF 融合 (k=60)                             │
│   └─ 知识图谱导航 (型号/域隔离)                  │
│ InferenceEngine                                  │
│   └─ domain_rules/power/rules.yaml               │
│   │    130 规则 (53 公式 derive + 77 SHACL)      │
│   ├─ 多步推导链 (缺失输入自动派生)               │
│   └─ 决策溯源 (前提逐条标注 layer)               │
│ 存储: 192.168.5.25                               │
│   PG17 (vector/age/pg_textsearch/zhparser)       │
│ 模型: 阿里云 MaaS (供应商可换)                   │
│   LLM qwen3.7-flash (OpenAI 兼容协议)            │
│   Embedding qwen3.7-text-embedding (1024维)      │
└──────────────────────────────────────────────────┘
```

## 知识隔离 (三级)

| workspace | 内容 | 可见性 |
|---|---|---|
| `{model_id}` | 型号个性化参数 | 仅本型号 |
| `_domain_{type}` | 产品类型通用知识/规则 | 同类型型号共享, 跨域拒绝 |

- 导入规格书自动识别型号 ID 与产品类型; 新类型自动建域 (`registry.yaml` + 规则包骨架)
- 查询自动识别型号 (`model_id` 可省略); 多型号歧义/无法识别 → fail-closed 拒绝

## 快速开始

```bash
# 1. 环境 (Python 3.13)
uv sync

# 2. 配置
cp .env.example .env   # 填入 API Key (Key 禁止提交 git)

# 3. 部署存储栈 (192.168.5.25, 见 deploy/native/README)
# ... 一次性执行 01~05 脚本

# 4. (原「初始化 Qdrant」一步已删除)
#    Qdrant 按 ADR-014 整层退场, 向量列在 PG 里(pgvector), 由上面第 3 步的
#    01~05 脚本一并建好(扩展名是 vector 不是 pgvector, 后者不存在)。
#    维度探测改看 aterag_chunks 的向量列, 见 `python -m aterag.storage.cli`。

# 5. 导入规格书 (自动识别型号/类型/实体)
python scripts/ingest_pa601.py

# 6. 构建领域知识库
python scripts/build_domain.py power

# 7. 验证
python scripts/validate_pa601.py

# 8. 产测条件抽取 (章节 4.3 功能/性能要求)
python scripts/extract_test_conditions.py -m PA601-D54A --brief

# MCP Server 现已常驻板卡 (systemd), 开发机无需再手工启动
# 远程验证: $env:MCP_BASE='http://192.168.5.25:8080/mcp'; python scripts/mcp_e2e.py
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

## 一键部署 / 一键验证 (板卡 192.168.5.25)

```bash
python scripts/deploy_board.py    # 全链路部署: 预检->存储栈->双型号->Semantica
python scripts/verify_all.py      # 全链路验证: 17 个套件
```

Windows 控制台默认 GBK, 跑脚本前先设 `$env:PYTHONIOENCODING='utf-8'`。

## MCP 工具

业务: `search_requirements` `query_parameters` `extract_test_conditions` `calculate` `validate_constraints`
`get_test_cases` `get_fixture_spec` `search_cases` `optimize_process`

管理: `ingest_document` `build_domain_kb` `list_models` `list_domain_rules` `health`

## 领域规则

`domain_rules/power/rules.yaml` — 每条规则含公式/SHACL/来源 URL/置信度/自验用例。
新增规则只改 YAML (websearch 迭代采集 → 形式化 → test 自验)。

## 产测条件抽取 (输入条件 → 输出条件)

把规格书里"产品依赖的外部状态"与"产品自身要产生的信号状态"抽成对, 供产测用例生成。

```powershell
# 人类可读摘要 (含统计/剔除/待审三桶)
.venv\Scripts\python.exe scripts\extract_test_conditions.py -m PA601-D54A --brief
# 结构化 JSON 落盘, 可 git diff 评审
.venv\Scripts\python.exe scripts\extract_test_conditions.py -m PA601-D54A --out conditions.json
# 换章节关键字 / 换档案 / 读 RAG 落库实体
... --section-keyword "技术要求"  --profile minimal  --source postgres
# 拿某条目指纹 (写人工注记时用)
... --print-fingerprint SR-PA601-D54A-1213
```

MCP 工具同能力: `extract_test_conditions(model_id, section_keyword, include_excluded)`。

**四缝架构 — 认知全在 YAML, 代码不含具体文档词汇**:

| 缝 | 文件 | 管什么 | 换文档模板时 |
|---|---|---|---|
| ⓪ | `config/table_schemas.yaml` | 表头 → 规范字段 (ingest 侧) | 加 schema 段 |
| ① | `config/doc_profiles.yaml` | 抽哪章节 / 剔哪些词 / 章节角色先验 | 加 profile |
| ④ | `config/condition_patterns.yaml` | 一句话怎么切成条件子句 + 封闭 kind 词表 | 加规则 |
| ③ | `config/annotations/{model}.conditions.yaml` | 规则切不出时的人工注记 (按内容指纹自动失效) | 补注记 |

四层自适应, 认知全部外置, 运行时永不调 LLM (保证同一输入可复现):

| 层 | 机制 | 配置成本 | 保证 |
|---|---|---|---|
| T1 | 通用列角色推断: 任意表按值形态识别 id/数值/单位/枚举 | 零配置 | 不丢行、不误解 (未映射表仍被捕获) |
| T2 | 声明式表头映射 (`table_schemas.yaml`) | 每模板一次 | 语义正确 |
| T3 | 提案闭环 (`--propose [--llm]`) 生成候选, 人审后并入 T2 | 人工只做 review | 接入成本从"手写同义词"降为"确认合并" |
| 缝④ | 条件装配规则 (`condition_patterns.yaml`) + 人工注记 | 逐条补 | 复合条件可人工兜底, 指纹失配自动失效 |

未映射的表不产生实体, 但行保留在 `blocks.jsonl` (无损底座), 并出现在
`table_schema_report.py` 的缺口清单里 —— 可见, 不静默丢。

新规格书接入流程:
```
python scripts/table_schema_report.py --doc "新型号规格书.md"   # ① 看缺口
# ② 把缺口表头补进 config/table_schemas.yaml
python scripts/ingest_pa601.py                                    # ③ 重入库
python scripts/table_schema_report.py                             # ④ 验收至 0 缺口
```

语义要点 (两条容易被混为一谈, 已分别实现并有回归):
- **等级列"不要求"** → 整条剔除, 进入 `excluded` 并带原因
- **单元格 `-`** → 该维度无数据, 保留条目并标 `no_data:*` (SR-1217 温度系数 3.45V 轨
  无数据, 但 -54V 轨有效; 若按"不要求"剔除会误删)

## 测试

```bash
python -m pytest tests/ -q                    # 纯本地单测 (53 项, 无需存储栈)
python scripts/verify_conditions.py           # 抽取条件验证 (47 项, 走 blocks 侧车)
python scripts/validate_configs.py            # 所有 YAML 配置解析+结构自检
python scripts/validate_pa601.py              # 集成验证 (需存储栈+模型)
```
