# 第 1 章 方案概览

## 1.1 核心能力

本方案实现以下能力链路：

- **输入**：一份产品规格书（DOCX/XLSX/MD）
- **自动构建**：该产品的专属 RAG 知识库（含知识图谱、向量索引、BM25 索引、章节层级、领域规则）
- **多 Agent 协同**：通过 MCP 协议驱动四个专业 Agent 完成全链路输出
  - 产测需求 Agent → 测试需求清单
  - 产测用例 Agent → 结构化测试用例
  - 产测代码 Agent → 可执行产测软件代码
  - 产测参数 Agent → 工装参数计算 + 工艺优化建议
- **输出**：产测软件代码 + 工装设计参数 + 工艺优化建议

## 1.2 端到端业务流

```mermaid
flowchart LR
    A[规格书上传<br/>DOCX/XLSX/MD] --> B[结构化解析]
    B --> C[产品RAG构建]
    C --> D{四个专业Agent}
    D --> E[产测需求Agent]
    D --> F[产测用例Agent]
    D --> G[产测代码Agent]
    D --> H[产测参数Agent]
    E --> I[测试需求清单]
    F --> J[结构化测试用例]
    G --> K[产测软件代码]
    H --> L[工装参数 + 工艺优化建议]
    I --> M[产测方案交付]
    J --> M
    K --> M
    L --> M

    style C fill:#e1f5ff
    style D fill:#fff4e1
    style M fill:#e8f5e9
```

## 1.3 方案与传统 RAG 的本质差异

| 维度 | 传统 RAG | 本方案 |
|---|---|---|
| 检索目标 | 相似文本块 | 结构化实体 + 关系 + 章节上下文 |
| 推理能力 | 无，仅检索 | Datalog 公式计算 + SHACL 约束验证 |
| 多型号支持 | 无隔离或需多实例 | workspace + payload + 行级三层隔离 |
| 章节感知 | 无 | 保留章节层级，支持按章节过滤 |
| 决策可追溯 | 无 | 每条决策有因果链和来源引用 |
| Agent 接口 | 自定义 API | 标准 MCP 协议 |

## 1.4 适用场景与边界

**适用场景**：

- 定制电源、电子产品、通信设备的规格书管理
- 产测软件研发的自动化辅助
- 工装设计参数的自动推导
- 工艺参数的 AI 优化建议

**当前边界**：

- 文档格式限定为 Word / Excel / Markdown，不含 PDF 扫描件
- 公式推理限定为 Datalog 可表达的规则，不含微分方程等复杂数值计算
- 代码生成限定为 Python + pytest + pyvisa 栈，其他栈需扩展模板

## 1.5 阅读指引

| 章节 | 内容 | 目标读者 |
|---|---|---|
| 第 2 章 | 设计思路与架构视图 | 架构师、技术负责人 |
| 第 3 章 | 技术选型与决策依据 | 架构师、运维工程师 |
| 第 4 章 | 产品 RAG 构建（含章节关系） | 开发工程师 |
| 第 5 章 | 推理层设计 | 开发工程师 |
| 第 6 章 | Agent 设计 | 开发工程师、Agent 工程师 |
| 第 7 章 | 部署与运维 | 运维工程师 |
| 第 8 章 | 实施路线图 | 项目经理 |

# 第 2 章 设计思路

## 2.1 总体设计哲学

本方案的核心设计哲学是 **"神经-符号-结构"三层混合**：用神经网络方法处理非结构化文本的模糊检索，用符号逻辑方法处理结构化知识的精确推理，用结构层保留文档原生章节语义。

```mermaid
flowchart TB
    subgraph Neural[神经网络层 - 模糊能力]
        N1[语义嵌入]
        N2[向量相似度]
        N3[LLM 语义理解]
    end

    subgraph Symbolic[符号逻辑层 - 精确能力]
        S1[本体公理]
        S2[Datalog 规则]
        S3[SHACL 约束]
        S4[公式计算]
    end

    subgraph Structural[结构层 - 章节感知]
        ST1[章节层级保留]
        ST2[元数据过滤]
        ST3[章节级溯源]
    end

    subgraph Bridge[协同层]
        B1[共享知识图谱]
        B2[统一事实空间]
        B3[决策溯源链]
    end

    Neural --> Bridge
    Symbolic --> Bridge
    Structural --> Bridge
    Bridge --> Output[可信工程决策]

    style Neural fill:#e3f2fd
    style Symbolic fill:#fce4ec
    style Structural fill:#f3e5f5
    style Bridge fill:#fff9c4
    style Output fill:#e8f5e9
```

### 2.1.1 为什么需要三层混合？

产测软件研发对 AI 系统提出三个看似矛盾的要求：

- **广度**：需要从数十页规格书中快速定位相关信息（神经网络的强项）
- **深度**：需要对参数进行精确计算、逻辑推导和约束验证（符号逻辑的强项）
- **精度**：需要按章节精确检索，避免跨章节的语义污染（结构层的强项）

单一技术无法同时满足。纯向量检索能找到"功率"相关文本，但无法计算 `P = U × I`；纯规则引擎能计算，但无法从非结构化文本中提取参数；无章节感知的检索会把"保护功能"和"监控功能"混在一起。三层混合架构是唯一可行的工程路径。

### 2.1.2 三层职责边界

| 层 | 职责 | 典型技术 | 输出 |
|---|---|---|---|
| 神经网络层 | 模糊检索、语义理解 | Embedding、BM25、LLM | 候选文本块、实体列表 |
| 符号逻辑层 | 公式计算、逻辑推理、约束验证 | Datalog、SHACL、OWL | 派生事实、违规报告 |
| 结构层 | 章节保留、章节过滤、溯源 | 元数据、层级树 | 章节路径、过滤条件 |

## 2.2 三引擎架构的设计动机

```mermaid
flowchart LR
    subgraph Problem[问题空间]
        P1[术语召回难<br/>SR-PA601-D54A-1203]
        P2[公式计算难<br/>公差链/功率]
        P3[关系导航难<br/>需求→用例→工装]
        P4[约束验证难<br/>探针间距/ESD]
        P5[章节污染<br/>跨章节语义混杂]
    end

    subgraph Solution[解决方案映射]
        S1[BM25 + 向量混合检索]
        S2[Datalog 推理引擎]
        S3[知识图谱 + 图遍历]
        S4[SHACL 约束验证]
        S5[章节元数据过滤]
    end

    P1 --> S1
    P2 --> S2
    P3 --> S3
    P4 --> S4
    P5 --> S5

    style Problem fill:#ffebee
    style Solution fill:#e8f5e9
```

### 2.2.1 问题-方案对照表

| 问题 | 单一方案失效原因 | 本方案对策 |
|---|---|---|
| 术语召回 | 嵌入模型对产品编号、信号名等罕见词表示差，向量相似度退化为噪声 | BM25 精确匹配 + 向量语义匹配，RRF 融合 |
| 公式计算 | 向量检索无法执行 `54 × 11.1 = 599.4` 的数值运算 | Datalog 递归 Horn 子句，半朴素不动点评估 |
| 关系导航 | 纯向量检索返回相似文本块，无法沿关系多跳扩展 | 知识图谱 + Cypher 图遍历，支持需求→用例追溯 |
| 约束验证 | SPARQL 算术能力有限，无法表达"保护点≥恢复点+3" | SHACL 约束 + SPARQL SELECT 规则 |
| 章节污染 | 无章节感知的检索跨越章节边界，返回语义相近但语境错误的内容 | 章节元数据 + MetadataFilter 预过滤 |

### 2.2.2 各问题在 PA601-D54A 规格书中的具体表现

**术语召回问题**：查询"SR-PA601-D54A-1210 的效率判据"，纯向量检索可能返回"SR-PA601-D54A-1203 输出电流"的条目，因为两者都包含"输出"和"参数"的语义。BM25 能精确匹配"1210"这个编号。

**公式计算问题**：规格书中给出"额定输出电压 -54V"和"输出电流 11.1A"，但未直接给出"输出功率 599.4W"。Datalog 规则可自动推导。

**章节污染问题**：查询"过流保护点"，规格书 4.3.2 节提到"输出电流 11.1A"，4.3.3 节提到"过流保护 12~18A"，4.2.4.2 节提到"输出连接器 30A 通流"。无章节感知的检索会返回全部三条，章节过滤只返回 4.3.3 节的结果。

## 2.3 本体引入的必要性

### 2.3.1 为什么不能只依赖 LightRAG 的自动图谱？

LightRAG 自动构建的图谱是**文本相似度驱动**的——它识别"实体A与实体B在文本中共同出现"，但不知道"实体A是一个需求，实体B是一个参数，两者是 hasParameter 关系"。这种图谱缺少**类型系统**和**公理约束**，无法支撑产测场景的精确推理。

```mermaid
flowchart TB
    subgraph Auto[LightRAG 自动图谱]
        A1[节点: "输出过流保护"]
        A2[节点: "12A~18A"]
        A3[边: 相关]
        A1 -.->|相关| A2
    end

    subgraph Onto[本体引导图谱]
        O1[Protection: 输出过流保护]
        O2[Parameter: tripPoint<br/>min=12, max=18, unit=A]
        O3[Parameter: recoveryPoint]
        O4[Parameter: hysteresis]
        O1 -->|hasTripPoint| O2
        O1 -->|hasRecoveryPoint| O3
        O1 -->|hasHysteresis| O4
    end

    Auto -->|映射| Onto
    Onto --> R[可推理: hysteresis = tripPoint - recoveryPoint]

    style Auto fill:#fff3e0
    style Onto fill:#e1f5ff
    style R fill:#e8f5e9
```

### 2.3.2 本体带来的四类能力

1. **类型系统**：每个实体有明确的类（Requirement/Parameter/Protection），支持类型级推理
2. **公理约束**：如"每个 Protection 必须有 tripPoint 和 recoveryPoint"，保证数据完整性
3. **关系语义**：`hasTripPoint` 与 `hasRecoveryPoint` 的语义明确，可被推理引擎识别
4. **规则挂载点**：Datalog 规则和 SHACL 约束挂载在本体类上，自动应用于所有实例

### 2.3.3 本体的最小可用设计

不需要一次性建完整的本体。最小可用本体只需：

- **5 个核心类**：Requirement、Parameter、Interface、Signal、Product
- **5 条核心关系**：hasParameter、belongsToModel、hasConnector、hasPin、carriesSignal

从 PA601-D54A 规格书中直接提取这 5 类实体和 5 条关系，即可支撑产测需求 Agent 的基础推理。后续每处理一份新规格书，本体模板就进化一次。

## 2.4 章节关系保留的设计动机

### 2.4.1 为什么需要章节级检索？

产测规格书的结构本身就是重要的知识组织方式。PA601-D54A 规格书中：

- **4.2.4.2 输出接口** 定义了 -54V/3.45V 的输出参数
- **4.3.2 输出特性** 定义了输出电压、电流、效率、纹波等参数
- **4.3.3 保护功能** 定义了输入过压/欠压、输出过流/短路、过温保护
- **4.3.4 监控功能** 定义了遥信、遥控、遥测功能

如果没有章节感知，查询"过流保护点是多少"可能返回：

- "4.3.2 输出特性"中提到的"输出电流 11.1A"（这是额定电流，不是保护点）
- "4.3.3 保护功能"中的"12~18A"（这是正确的保护点）
- "4.2.4.2 输出接口"中的"30A 通流"（这是连接器通流能力，不是保护点）

**章节过滤确保查询在正确的语义边界内执行**。

```mermaid
flowchart TB
    subgraph Without[无章节感知]
        W1[查询: 过流保护点]
        W2[检索: 全局向量空间]
        W3[结果混杂:<br/>4.3.2 输出电流 11.1A<br/>4.3.3 保护点 12~18A<br/>4.2.4.2 接口 30A]
    end

    subgraph With[有章节感知]
        Y1[查询: 过流保护点]
        Y2[元数据过滤:<br/>section = "4.3.3 保护功能"]
        Y3[检索: 仅 4.3.3 章节]
        Y4[结果精确:<br/>保护点 12~18A]
    end

    Without --> With

    style Without fill:#ffebee
    style With fill:#e8f5e9
```

### 2.4.2 章节关系保留的三要素

```mermaid
flowchart LR
    subgraph Parse[解析阶段]
        P1[Smart Heading 识别]
        P2[P 策略分块]
        P3[层级路径构建]
    end

    subgraph Store[存储阶段]
        S1[blocks.jsonl]
        S2[chunk metadata]
        S3[section 字段索引]
    end

    subgraph Query[查询阶段]
        Q1[MetadataFilter]
        Q2[mode=mix]
        Q3[预过滤 WHERE section=?]
    end

    Parse --> Store
    Store --> Query

    style Parse fill:#e3f2fd
    style Store fill:#fff9c4
    style Query fill:#f3e5f5
```

### 2.4.3 章节过滤与其他过滤的协同

| 过滤维度 | 实现机制 | 适用场景 |
|---|---|---|
| 型号隔离 | workspace + payload + 行级 | 跨型号安全边界 |
| 章节过滤 | MetadataFilter + section 字段 | 同型号内语义边界 |
| 类别过滤 | MetadataFilter + category 字段 | 保护/监控/EMC 分类 |
| 优先级过滤 | MetadataFilter + priority 字段 | 强制/推荐/不要求 |

多维度过滤可组合，例如"在 PA601-D54A 的 4.3.3 章节中检索所有强制保护需求"。

## 2.5 多型号隔离的设计原则

### 2.5.1 三层防御架构

```mermaid
flowchart TB
    subgraph Layer1[第一层: 检索隔离]
        L1A[LightRAG workspace 参数]
        L1B[查询时自动注入 workspace 过滤]
    end

    subgraph Layer2[第二层: 存储隔离]
        L2A[PostgreSQL: workspace 列]
        L2B[Qdrant: payload workspace_id]
        L2C[行级/向量级物理隔离]
    end

    subgraph Layer3[第三层: 服务隔离]
        L3A[API 网关校验权限]
        L3B[model_id 由令牌自动注入]
        L3C[跨型号需显式授权]
    end

    Query[Agent 查询] --> Layer1
    Layer1 --> Layer2
    Layer2 --> Layer3
    Layer3 --> Result[隔离结果]

    style Layer1 fill:#e3f2fd
    style Layer2 fill:#f3e5f5
    style Layer3 fill:#fff9c4
```

### 2.5.2 核心原则

1. **默认隔离**：查询必须携带 `model_id`，服务端强制注入过滤条件
2. **预过滤优先**：Qdrant 的 `is_tenant` 索引确保过滤在 ANN 搜索之前执行，而非搜索后过滤
3. **物理隔离兜底**：即使应用层被绕过，存储层的 `workspace` 列仍能阻止跨型号访问
4. **共享本体**：单位、标准、判据等公共知识只读共享，型号实例数据独立
5. **章节隔离**：同一型号内按章节过滤，避免跨章节语义污染

### 2.5.3 隔离失效的典型场景与对策

| 失效场景 | 后果 | 对策 |
|---|---|---|
| 查询未携带 model_id | 可能返回所有型号数据 | API 网关强制校验，缺失则拒绝 |
| Qdrant 未建 tenant 索引 | 过滤在搜索后执行，性能差且可能泄漏 | 部署时创建 `is_tenant=True` 索引 |
| LightRAG 未启用 workspace | 多型号数据混在同一实例 | 配置 `workspace=model_id` |
| 跨型号查询未授权 | 越权访问其他型号数据 | 显式 `compare` 接口 + 权限校验 |

## 2.6 公式推理的实现思路

### 2.6.1 "事实-规则-推导"三段式

公式推理是本方案区别于普通 RAG 的关键。设计思路是 **"事实-规则-推导"三段式**：

```mermaid
flowchart LR
    subgraph Facts[事实层]
        F1[Voltage PA601 = 54]
        F2[Current PA601 = 11.1]
    end

    subgraph Rules[规则层]
        R1["Power(?x,?p) :- Voltage(?x,?u), Current(?x,?i), ?p = ?u * ?i"]
    end

    subgraph Derive[推导层]
        D1[引擎执行规则]
        D2[Power PA601 = 599.4]
    end

    Facts --> Rules
    Rules --> Derive
    D2 --> Trace[决策溯源]

    style Facts fill:#e3f2fd
    style Rules fill:#fff9c4
    style Derive fill:#e8f5e9
```

### 2.6.2 与 SPARQL 简单运算的本质区别

| 维度 | SPARQL BIND 运算 | Datalog 规则引擎 |
|---|---|---|
| 表达能力 | 单条查询内的四则运算 | 递归 Horn 子句，支持多步推导 |
| 推导链 | 不支持（每次独立） | 支持（A→B→C→D） |
| 终止性 | N/A | 半朴素不动点评估保证 |
| 规则复用 | 每次查询需重写 | 一次定义，所有查询复用 |
| 推理路径 | 不可追溯 | 可生成自然语言解释 |

### 2.6.3 多步推导链示例

```mermaid
flowchart LR
    V[电压 54V] --> P[功率 599.4W]
    I[电流 11.1A] --> P
    P --> E[效率 93%]
    PIN[输入功率 644W] --> E
    E --> L[损耗 45W]
    L --> T[温升 22℃]
    RTH[热阻 0.5℃/W] --> T
    T --> C[散热需求]

    style P fill:#fff9c4
    style E fill:#fff9c4
    style L fill:#fff9c4
    style T fill:#fff9c4
```

每一步推导都记录在决策溯源链中，最终可回答"为什么散热需求是 X"这类问题，并给出完整的推导路径。

## 2.7 Agent 协同的设计思路

### 2.7.1 四个 Agent 的依赖关系

四个 Agent 不是独立的，而是有明确依赖关系的流水线：

```mermaid
sequenceDiagram
    participant User as 用户
    participant MCP as MCP Server
    participant Req as 需求Agent
    participant Case as 用例Agent
    participant Code as 代码Agent
    participant Param as 参数Agent

    User->>MCP: 上传规格书 + 启动流水线
    MCP->>Req: Phase 1: 提取测试需求
    Req->>MCP: 返回需求清单

    par Phase 2: 并行执行
        MCP->>Case: 生成测试用例
        Case->>MCP: 返回用例集
    and
        MCP->>Param: 计算工装参数
        Param->>MCP: 返回参数集
    end

    MCP->>Code: Phase 3: 生成产测代码
    Code->>MCP: 返回代码
    MCP->>User: 返回完整产测方案
```

### 2.7.2 设计要点

1. **需求 Agent 必须先行**：其他三个 Agent 都依赖需求清单作为输入
2. **用例与参数可并行**：两者互不依赖，可并行执行缩短链路时间
3. **代码 Agent 最后执行**：需要用例作为输入，且需要参数 Agent 的工装规格
4. **决策溯源贯穿全链**：每个 Agent 的决策都记录因果链，便于审计
5. **章节过滤贯穿检索**：每个 Agent 在检索时可根据语义定位指定章节范围

### 2.7.3 Agent 与章节的对应关系

| Agent | 主要检索章节 | 章节过滤示例 |
|---|---|---|
| 需求 Agent | 4.3 功能/性能要求 | section="4.3.3 保护功能" |
| 用例 Agent | 4.3.2 输出特性、4.3.3 保护功能 | section IN ["4.3.2", "4.3.3"] |
| 代码 Agent | 4.2.4 接口要求 | section="4.2.4.2 输出接口" |
| 参数 Agent | 4.1 环境条件、4.3.2 输出特性 | section="4.1 环境条件" |

## 2.8 Reranker 不引入的决策依据

### 2.8.1 实测数据对比

Reranker 的收益高度依赖配置。实测数据表明：

| Reranker | 参数 | HotpotQA Top-3 | NQ Top-3 |
|---|---|---|---|
| **bge-reranker-v2-m3** | 568M | **53.30** | **69.70** |
| Qwen3-Reranker-8B | 8B | 51.90 | 68.80 |
| Qwen3-Reranker-4B | 4B | 50.10 | 64.00 |

**关键发现**：568M 的 bge-reranker-v2-m3 在 Top-3 精度上优于 8B 的 Qwen3-Reranker。

### 2.8.2 不引入的核心理由

1. **与 LightRAG 混合检索重叠**：`enable_hybrid=True` 已在实体种子层执行 RRF 融合，Reranker 只是对融合后的列表再重排
2. **主模型可替代**：Qwen3.8-27B 的推理能力可用于判断候选相关性，比独立 Reranker 更灵活
3. **边际收益递减**：混合检索已解决术语召回问题，Reranker 的增量收益有限
4. **降低部署复杂度**：少一个模型意味着少一份显存和运维开销
5. **章节过滤已提升精度**：章节元数据过滤在检索阶段就排除了跨章节噪声，减少了 Reranker 需要处理的候选数量

### 2.8.3 例外情况

如果后续发现检索精度不足，可引入 **bge-reranker-v2-m3**（568M，约 2GB 显存），而非 Qwen3-Reranker 系列。引入时机建议为：

- 混合检索的 NDCG@10 在内部测试集上低于 0.85
- 章节过滤后仍有明显的语义噪声
- Agent 反馈检索结果与预期偏差较大

## 2.9 关键设计决策速查

| 决策点 | 选择 | 核心理由 |
|---|---|---|
| 架构风格 | 神经-符号-结构三层混合 | 单一技术无法同时满足广度、深度、精度 |
| 图谱构建 | LightRAG 自动图谱 + 本体引导 | 自动图谱缺类型系统，本体提供公理约束 |
| 章节处理 | Smart Heading + P 策略 + MetadataFilter | 保留章节层级，支持章节级精确检索 |
| 多型号隔离 | workspace + payload + 行级三层 | 预过滤 + 物理隔离兜底 |
| 公式推理 | Datalog 递归 Horn 子句 | 支持多步推导，保证终止性 |
| 约束验证 | SHACL + SPARQL SELECT | 空间/电气/安全约束的声明式表达 |
| Agent 接口 | MCP 标准协议 | 兼容主流 Agent 框架 |
| Reranker | 不引入 | 与混合检索+章节过滤功能重叠 |

# 第 3 章 技术选型

## 3.1 选型总览

本方案的技术选型遵循四条原则：

1. **运维面最小化**：组件数量控制在可维护的范围内，优先选择可复用同一数据源的组件
2. **生产稳定性优先**：排除已归档或停止维护的项目，即使其技术指标优秀
3. **领域匹配度**：定制电源规格书是结构化程度较高的技术文档，不需要重型 OCR 或视觉模型
4. **章节感知能力**：文档的章节层级是重要的语义组织方式，检索层必须支持章节级过滤

### 3.1.1 选型结果

| 层次 | 组件 | 选型 | 版本要求 | 选型理由 |
|---|---|---|---|---|
| 文档解析 | LangParse / MarkItDown | Python | — | DOCX/XLSX/MD 原生结构化解析，零 OCR 依赖 |
| 章节识别 | LightRAG Native Parser | 内置 | ≥ v1.5.6 | Smart Heading + P 策略，保留章节层级 |
| RAG 检索引擎 | LightRAG | Python | ≥ v1.5.6 | 图增强检索 + workspace 隔离 + BM25 混合检索 + 元数据过滤 |
| 知识图谱推理 | Semantica | Python | ≥ v0.9 | Datalog 公式计算 + 8 种推理模式 + 决策溯源 |
| 图存储 | PostgreSQL + Apache AGE | PG 扩展 | PG 17 + AGE 1.7 | Cypher 图查询，与 pgvector 同库 |
| 向量存储 | Qdrant | Rust | ≥ v1.9 | Payload 预过滤多租户，混合检索性能优 |
| 关系/KV 存储 | PostgreSQL | — | PG 17 | workspace 列隔离 + 章节元数据索引 |
| BM25 全文检索 | pg_textsearch | PG 扩展 | ≥ v1.3 | 中文分词 BM25，与 PostgreSQL 同库 |
| 主推理 LLM | Qwen3.8-27B | vLLM 部署 | FP8 量化 | 262K 上下文，整份规格书加载 |
| Embedding | Qwen3-Embedding-4B | vLLM 部署 | — | MTEB 检索得分 ~72，资源可控 |
| Agent 接口 | MCP Server | Python | — | 标准协议，兼容 Claude/Copilot/Cursor 等 |
| Reranker | 不引入 | — | — | 与混合检索+章节过滤功能重叠 |

### 3.1.2 组件拓扑

```mermaid
flowchart TB
    subgraph Client[客户端]
        CL1[Claude Desktop]
        CL2[Cursor]
        CL3[自定义 Agent]
    end

    subgraph Service[服务层]
        SV1[MCP Server]
        SV2[LightRAG 服务]
        SV3[Semantica 服务]
    end

    subgraph Storage[存储层]
        ST1[PostgreSQL 17<br/>pgvector + AGE + pg_textsearch]
        ST2[Qdrant]
        ST3[blocks.jsonl<br/>章节元数据]
    end

    subgraph LLM[模型层]
        LM1[vLLM<br/>Qwen3.8-27B-FP8]
        LM2[vLLM<br/>Qwen3-Embedding-4B]
    end

    Client -->|MCP 协议| Service
    Service --> Storage
    Service --> LLM

    style Storage fill:#e1f5ff
    style LLM fill:#fce4ec
```

**独立服务数量**：3 个（PostgreSQL、Qdrant、vLLM）+ 2 个 Python 进程（LightRAG、Semantica）。相比“Qdrant + FalkorDB + MySQL”的三存储方案，运维面减少 33%。

## 3.2 文档解析层选型

### 3.2.1 候选方案对比

| 工具 | DOCX 表格 | 章节层级 | Excel 深度 | Markdown | 部署 |
|---|---|---|---|---|---|
| **LangParse** | ✅ 原生 | ✅ 保留 | ✅ 跨 Sheet | ✅ GFM | 纯 Python |
| MarkItDown | ✅ 转 Markdown | ✅ | ✅ | ✅ | 纯 Python |
| Unstructured | ✅ | ✅ | 有限 | ✅ | 重依赖 |
| python-docx | ✅ | 需手动 | ❌ | ❌ | 纯 Python |

### 3.2.2 选型决策

**主解析器：LangParse**

LangParse 对 Excel 的处理最为深入——保留公式、合并单元格、跨 Sheet 关系，而不是将工作簿扁平化为纯文本。对于定制电源规格书中可能存在的多 Sheet 参数表、跨表引用场景，LangParse 的语义重建能力是明确优势。

**轻量备选：MarkItDown**

如果只需要简单的文本提取和 Markdown 转换，MarkItDown 更轻量。它由微软开源，支持 DOCX/XLSX/MD 转 Markdown，适合快速验证阶段。

**章节识别专用：LightRAG Native Parser**

从 LightRAG v1.5.6 开始，内置 DOCX 解析器引入 **Smart Heading** 功能，即使 Word 大纲级别配置不规范，也能识别视觉上的章节标题[reference:0]。配合 **P 策略**（段落语义分块），分块边界对齐文档的原生语义边界，并保留 `parent_headings` 和 `level` 字段。

**配置方式**：

```bash
LIGHTRAG_PARSER="*:native-teP"
```

## 3.3 RAG 检索引擎选型

### 3.3.1 候选方案对比

| 框架 | 图增强 | 多型号隔离 | BM25 混合 | 章节过滤 | 运维复杂度 |
|---|---|---|---|---|---|
| **LightRAG** | ✅ 双引擎 | ✅ workspace | ✅ enable_hybrid | ✅ MetadataFilter | 低 |
| Microsoft GraphRAG | ✅ 社区检测 | ❌ 需多实例 | ❌ | ❌ | 中高 |
| RAGFlow | ✅ | 有限 | ✅ | ❌ | 中 |
| LangChain | 需自行组装 | ❌ | 需自行实现 | ❌ | 低但工作量大 |

### 3.3.2 选型决策：LightRAG

**核心理由**：

1. **图增强检索**：LightRAG 将知识图谱与向量检索深度融合，形成双引擎检索架构[reference:1]。查询时同时走图遍历和向量搜索，结果通过 RRF 融合。

2. **多型号隔离**：通过 `workspace` 参数实现逻辑隔离，所有存储后端使用该参数进行隔离。PostgreSQL 后端每张表增加 `workspace` 列，Qdrant 使用 `workspace_id` payload 过滤。

3. **BM25 混合检索**：`enable_hybrid=True` 启用 BM25+向量 RRF 融合，BM25 只索引实体名称（短字符串），解决术语密集型查询的召回问题。

4. **章节元数据过滤**：从 v1.5.6 开始支持元数据过滤，PR #2187 添加了完整的 MetadataFiltering 实现，**仅支持 PostgreSQL (PGVectorStorage) 后端**[reference:2]。查询时使用 `mode="mix"` 并传入 `MetadataFilter`。

**重要限制**（来自 LightRAG 官方文档）[reference:3]：

| 限制项 | 说明 |
|---|---|
| 存储后端 | 仅支持 PostgreSQL (PGVectorStorage) |
| 查询模式 | 仅 Mix 和 Naive 模式 |
| 过滤性质 | 硬过滤（hard in-filter），在向量搜索之前执行 |

**版本要求**：稳定线 ≥ v1.5.6（2026-08-06 发布）[reference:4]。

## 3.4 知识图谱推理引擎选型

### 3.4.1 候选方案对比

| 引擎 | 公式计算 | 多步推导 | 决策溯源 | Python 集成 | 运维复杂度 |
|---|---|---|---|---|---|
| **Semantica** | ✅ Datalog | ✅ 递归 Horn | ✅ 内置 | 原生 Python | 低（pip install） |
| Nemo | ✅ Datalog | ✅ | ❌ | CLI/Web | 中 |
| Jena Rules | ⚠️ 小数精度问题 | ✅ | ❌ | Java | 中 |
| PySHACL | ❌ 仅验证 | ❌ | ❌ | Python | 低 |

### 3.4.2 选型决策：Semantica

**核心理由**：

1. **Datalog 公式计算**：Semantica 的 `DatalogReasoner` 支持递归 Horn 子句规则，通过**半朴素不动点评估**保证终止性[reference:5]。适用于多跳传递关系（供应链、组织图）和数值计算场景。

2. **8 种推理模式**：覆盖演绎推理、定理证明、溯因假设生成、Datalog 程序评估、SPARQL 查询、Rete 网络增量规则求值[reference:6]。无需切换框架即可解决不同类型的推理问题。

3. **决策溯源**：内置 Decision Intelligence 模块，将每个决策记录为一等图对象，包含因果链、置信度和决策者信息。

4. **知识图谱加载**：`DatalogReasoner.load_from_graph()` 自动将知识图谱的节点和边转换为 Datalog 事实[reference:7]。

**公式推理示例**：

```python
from semantica.reasoning import DatalogReasoner

reasoner = DatalogReasoner()
reasoner.add_fact("Voltage(PA601, 54)")
reasoner.add_fact("Current(PA601, 11.1)")
reasoner.add_rule("Power(?x, ?p) :- Voltage(?x, ?u), Current(?x, ?i), ?p = ?u * ?i")
results = reasoner.derive_all()
# 输出: Power(PA601, 599.4)
```

**为什么排除 Nemo**：Nemo 是 Rust 编写的 Datalog 引擎，性能优秀，但需要独立进程或 CLI 调用，与 Python 技术栈的集成不如 Semantica 原生。Semantica 的 `DatalogReasoner` 在功能上已覆盖 Nemo 的核心能力，且提供决策溯源等额外能力。

**为什么排除 Jena Rules**：Jena 的数学内置函数不支持 `xsd:decimal` 类型，`sum(1.1, 1.1)` 返回整数 `2.0` 而非 `2.2`。对于定制电源中大量的小数参数（54.0V、11.1A），这是严重的精度问题。

## 3.5 存储层选型

### 3.5.1 候选方案对比

| 方案 | 组件数 | 运维门槛 | 生产稳定性 | 公式推理 | 章节过滤 | 推荐度 |
|---|---|---|---|---|---|---|
| **PostgreSQL + Qdrant** | 2 | 低 | 高 | Semantica 原生 | PG 原生 | ⭐⭐⭐⭐⭐ |
| Qdrant + FalkorDB + MySQL | 3 | 中高 | 中 | 需额外集成 | 需额外实现 | ⭐⭐⭐ |
| SeleneDB | 1 | 极低 | 已归档 | 需额外集成 | 需额外集成 | ❌ |
| Kùzu + LanceDB | 3（嵌入） | 低 | 已归档 | 需额外集成 | 需额外集成 | ❌ |

### 3.5.2 选型决策：PostgreSQL + Qdrant

**PostgreSQL 作为统一存储**：

PostgreSQL 17 同时承载三类数据：

- **关系型数据**：业务元数据、workspace 隔离列
- **向量数据**：pgvector HNSW 索引（当向量规模 < 50 万时）
- **图数据**：Apache AGE 扩展，支持 Cypher 查询
- **BM25 全文检索**：pg_textsearch 扩展，中文分词

三个扩展在同一实例中无根本性冲突，但需要自定义 Docker 镜像来组合安装[reference:8]。

**Qdrant 作为专用向量存储**：

当向量规模超过 50 万，或过滤密集型查询增多时，将向量检索从 pgvector 迁移至 Qdrant。Qdrant 的 **可过滤 HNSW** 在过滤条件下遍历最近邻图，避免了 pgvector 的“后过滤”性能瓶颈。

**多型号隔离三层机制**：

| 存储 | 隔离机制 | 隔离级别 |
|---|---|---|
| PostgreSQL | 每张表增加 `workspace` 列 | 行级逻辑隔离 |
| Qdrant | Payload 字段 `workspace_id` + `is_tenant` 索引 | 向量级逻辑隔离 |
| 文件存储 | 子目录 `{working_dir}/{workspace}/` | 文件级物理隔离 |

**排除已归档项目的理由**：SeleneDB 和 Kùzu 的 GitHub 仓库均已归档，进入只读状态，不再有社区更新和安全补丁，不适合长期维护的生产系统。

## 3.6 LLM 与 Embedding 选型

### 3.6.1 主推理 LLM：Qwen3.8-27B

**选型理由**：

- **262K 原生上下文**：可整份加载 38~50 页规格书，无需分块处理跨章节参数关联
- **FP8 量化部署**：约 26GB 权重，单张 48GB 显卡（L40S、RTX A6000、RTX 6000 Ada）即可部署并留有上下文空间[reference:9]
- **vLLM 原生支持**：提供 OpenAI 兼容的 chat completions 接口

**部署配置**：

```bash
vllm serve Qwen/Qwen3.8-27B-FP8 \
  --tensor-parallel-size 1 \
  --max-model-len 65536 \
  --kv-cache-dtype fp8 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --gpu-memory-utilization 0.92
```

**显存规划**：

| 量化精度 | 权重占用 | 推荐硬件 | 适用场景 |
|---|---|---|---|
| FP8 | ~26 GB | 1× L40S 48GB | **推荐**，留有上下文空间 |
| FP8 (双卡) | ~28 GB/卡 | 2× RTX 4090 | 需更大上下文或更高并发 |
| NVFP4 | ~24.6 GB | 1× RTX 5090 32GB | 成本最低 |

### 3.6.2 Embedding：Qwen3-Embedding-4B

**选型理由**：

- **MTEB 多语言得分 69.45**，英文 MTEB 74.60，中文 C-MTEB 72.27[reference:10]
- **检索任务得分 69.60**，指令检索 11.56（注：指令检索得分低是因为该指标衡量的是特定指令格式下的检索，常规检索场景不受影响）
- **维度 2560**，支持自定义输出维度（32~4096）
- **上下文 32K**，足以处理规格书中的单个章节

**部署配置**：

```bash
vllm serve Qwen/Qwen3-Embedding-4B \
  --tensor-parallel-size 1 \
  --port 8001
```

### 3.6.3 为什么升级 Qwen2.5 14B 到 Qwen3.8-27B

| 维度 | Qwen2.5 14B | Qwen3.8-27B | 提升 |
|---|---|---|---|
| 上下文 | 32K | **262K** | 8 倍 |
| GPQA | 83.3 | **89.9** | +6.6 |
| 显存 (FP8) | ~10 GB | ~26 GB | 增加 16 GB |
| 跨章节推理 | 需分块 | **整份加载** | 质变 |

对于产测场景中“从 38 页规格书中一次性理解所有参数关联”的需求，这个升级是值得的。

## 3.7 Agent 接口选型：MCP Server

**选型理由**：

MCP 正在成为连接 LLM 与外部工具的标准协议。Azure AI Search 等平台已为知识库公开 MCP 终结点，供 MCP 兼容的智能体使用。通过 MCP Server 暴露 RAG 能力，Agent 无需理解底层图数据库和向量库细节。

**MCP 工具定义**：

| 工具名 | 功能 | 章节过滤 |
|---|---|---|
| `search_requirements` | 按关键词/类别检索需求项 | ✅ |
| `query_parameters` | 查询参数值、条件、判据 | ✅ |
| `calculate` | 执行 Datalog 公式计算 | — |
| `validate_constraints` | 执行 SHACL 约束验证 | — |
| `get_test_cases` | 获取/生成测试用例 | ✅ |
| `get_fixture_spec` | 获取工装规格和探针选型 | ✅ |
| `search_cases` | 检索历史工装/测试案例 | ✅ |
| `optimize_process` | 工艺参数优化建议 | — |

## 3.8 Reranker 不引入的决策依据

### 3.8.1 实测数据对比

| Reranker | 参数 | HotpotQA Top-3 | NQ Top-3 |
|---|---|---|---|
| **bge-reranker-v2-m3** | 568M | **53.30** | **69.70** |
| Qwen3-Reranker-8B | 8B | 51.90 | 68.80 |
| Qwen3-Reranker-4B | 4B | 50.10 | 64.00 |

568M 的 bge-reranker-v2-m3 在 Top-3 精度上优于 8B 的 Qwen3-Reranker。

### 3.8.2 不引入的核心理由

1. **与 LightRAG 混合检索重叠**：`enable_hybrid=True` 已在实体种子层执行 RRF 融合
2. **主模型可替代**：Qwen3.8-27B 的推理能力可用于判断候选相关性
3. **边际收益递减**：混合检索已解决术语召回问题
4. **降低部署复杂度**：少一个模型意味着少一份显存和运维开销
5. **章节过滤已提升精度**：章节元数据过滤在检索阶段就排除了跨章节噪声

**例外情况**：如果后续发现检索精度不足，可引入 **bge-reranker-v2-m3**（568M，约 2GB 显存），而非 Qwen3-Reranker 系列。

## 3.9 选型决策树

```mermaid
flowchart TD
    Start[开始选型] --> Q1{需要公式推理?}
    Q1 -->|是| Q2{需要从非结构化文本检索?}
    Q1 -->|否| Simple[单一向量库即可]

    Q2 -->|是| Hybrid[神经-符号混合架构]
    Q2 -->|否| SymbolicOnly[纯规则引擎]

    Hybrid --> Q3{需要章节级检索?}
    Q3 -->|是| Q4{向量规模?}
    Q3 -->|否| NoSection[无章节感知检索]

    Q4 -->|< 50万| PGOnly[PostgreSQL 单库<br/>pgvector + AGE + 章节元数据]
    Q4 -->|> 50万| PGPQ[PostgreSQL + Qdrant<br/>推荐]

    PGPQ --> Q5{需要决策溯源?}
    Q5 -->|是| Final[+ Semantica]
    Q5 -->|否| Basic[基础 RAG]

    Final --> Result[本方案]

    style Result fill:#e8f5e9
    style PGPQ fill:#e1f5ff
```

## 3.10 版本兼容性矩阵

| 组件 | 最低版本 | 推荐版本 | 关键特性依赖 |
|---|---|---|---|
| LightRAG | v1.5.5 | v1.5.6+ | Smart Heading (v1.5.5)、MetadataFiltering (PR #2187) |
| Semantica | v0.9 | 最新 | DatalogReasoner、load_from_graph |
| PostgreSQL | 17 | 17 | pgvector、AGE、pg_textsearch 兼容 |
| Apache AGE | 1.7 | 1.7 | PG17 支持 |
| pg_textsearch | 1.3 | 1.3+ | 中文分词 BM25 |
| Qdrant | 1.9 | 最新 | is_tenant 索引 |
| vLLM | 0.6 | 最新 | FP8 KV cache、tool call parser |
| Qwen3.8-27B | — | FP8 | 262K 上下文、reasoning parser |

# 第 4 章 产品 RAG 构建（含章节关系）

## 4.1 构建流程总览

产品 RAG 的构建是一次性任务，但支持增量更新。构建流程分为四个阶段：

```mermaid
flowchart LR
    subgraph Step1[步骤1: 文档解析]
        D1[识别格式]
        D2[Smart Heading 识别]
        D3[P 策略分块]
        D4[生成 blocks.jsonl]
    end

    subgraph Step2[步骤2: 本体映射]
        O1[实体抽取]
        O2[关系抽取]
        O3[本体对齐]
        O4[章节路径关联]
    end

    subgraph Step3[步骤3: 索引构建]
        I1[图三元组入库]
        I2[向量索引]
        I3[BM25 索引]
        I4[章节元数据索引]
    end

    subgraph Step4[步骤4: 规则注入]
        R1[Datalog 规则]
        R2[SHACL 约束]
        R3[决策溯源配置]
    end

    Step1 --> Step2
    Step2 --> Step3
    Step3 --> Step4
```

**各阶段产出**：

| 阶段 | 输入 | 输出 | 存储位置 |
|---|---|---|---|
| 文档解析 | DOCX/XLSX/MD | 结构化 Markdown + 表格 JSON + blocks.jsonl | 文件系统 |
| 本体映射 | 结构化文本 | 实体列表 + 关系列表 + 章节路径 | 内存 |
| 索引构建 | 实体+关系+chunk | 图三元组 + 向量 + BM25 + 章节索引 | PostgreSQL + Qdrant |
| 规则注入 | 领域知识模板 | Datalog 规则 + SHACL 约束 | Semantica |

## 4.2 文档解析设计

### 4.2.1 格式处理策略

三种格式走原生结构化解析路线，核心是**保留结构信息**：

| 格式 | 解析重点 | 输出 |
|---|---|---|
| Word (DOCX) | 标题层级、表格结构、合并单元格 | 结构化 Markdown + 表格 JSON + 章节元数据 |
| Excel (XLSX) | 公式、合并单元格、跨 Sheet 关系 | 保留依赖图的 JSON |
| Markdown | 标题层级、GFM 表格 | 标题树 + 表格结构化 |

**关键设计**：解析阶段**不丢失来源坐标**。每个 chunk 记录其在原文中的章节路径和表格位置，为后续引用溯源和章节过滤提供基础。

### 4.2.2 LightRAG Native DOCX Parser

LightRAG 从 v1.5.5 开始引入 **Smart Heading** 功能，专门解决 DOCX 文档“视觉上有清晰章节结构但 Word 大纲级别配置不规范”的问题[reference:0]。

**Smart Heading 解决的问题**：

| 问题 | 表现 | 后果 |
|---|---|---|
| 章节标题缺失 | 视觉上是标题但未配置 outline level | 标题被当作正文，章节结构丢失 |
| 正文误判为标题 | 普通段落错误携带标题样式 | 产生无意义的 chunk 边界 |
| 标题层级不一致 | 兄弟标题级别不统一、父子倒置 | 分块边界偏离真实语义 |
| 合并文档的多内容 | 多个文档合并后标题级别互相干扰 | 章节级别混乱 |

**Smart Heading 的识别机制**：当文档的物理大纲（`w:outlineLvl`）缺失或不可靠时，从**多维物理信号**（字体大小、编号形状、居中、加粗、分页）中恢复章节标题及其层级，并辅以 LLM 标题块判断。

**配置方式**：

```bash
# 在 LIGHTRAG_PARSER 中启用
LIGHTRAG_PARSER="*:native(smart_heading=true)"
```

### 4.2.3 P 策略（段落语义分块）

LightRAG 的切分器内置三种策略：**R（递归）、V（向量）、P（段落语义）**[reference:2]。P 策略面向 DOCX/PDF 等带明确章节结构的文档，目标是让 chunk 边界贴合文档**原生的语义边界**（标题、段落、表格行），而不是单纯按 token 长度切割[reference:3]。

**P 策略的核心机制**：

| 机制 | 说明 |
|---|---|
| 标题继承 | 分块时继承或提升标题信息 |
| 父标题路径 | 保留 `parent_headings` 和 `level` 字段，形成章节层级路径 |
| 表格行边界 | 超长章节不在任意 token 位置硬切，而是在自然语义点切分 |
| 子块继承 | 子块继承可读标题与父标题路径 |
| 侧车文件 | 生成 `.blocks.jsonl` 携带 `heading`、`level`、`parent_headings` 等元数据 |

**配置方式**：

```bash
# P 策略配置
LIGHTRAG_PARSER="*:native-teP"
```

**章节元数据示例**（`.blocks.jsonl` 中的一条记录）：

```json
{
  "chunk_id": "chunk_0042",
  "heading": "4.3.3 保护功能",
  "level": 3,
  "parent_headings": ["4 技术要求", "4.3 功能/性能要求"],
  "section_path": "4.3.3",
  "text": "输出过流保护 12~18A，打隔保护..."
}
```

## 4.3 章节关系保留机制

### 4.3.1 章节层级构建

```mermaid
flowchart TB
    subgraph Input[原始文档]
        D1[DOCX 文件]
    end

    subgraph Parse[Native Parser]
        P1[Smart Heading 识别]
        P2[P 策略分块]
        P3[章节层级构建]
    end

    subgraph Meta[章节元数据]
        M1[heading: 4.3.3 保护功能]
        M2[level: 3]
        M3[parent_headings: 4 技术要求 / 4.3 功能性能要求]
        M4[section_path: 4.3.3]
    end

    subgraph Store[存储]
        S1[blocks.jsonl]
        S2[chunk metadata]
        S3[PostgreSQL section 列]
    end

    Input --> Parse
    Parse --> Meta
    Meta --> Store

    style Parse fill:#e3f2fd
    style Meta fill:#fff9c4
    style Store fill:#e8f5e9
```

### 4.3.2 PA601-D54A 规格书章节树示例

```mermaid
flowchart TB
    Root[PA601-D54A 规格书] --> C4[4 技术要求]
    C4 --> C41[4.1 环境条件]
    C4 --> C42[4.2 结构/工艺要求]
    C4 --> C43[4.3 功能/性能要求]
    C4 --> C44[4.4 可靠性要求]

    C43 --> C431[4.3.1 输入特性]
    C43 --> C432[4.3.2 输出特性]
    C43 --> C433[4.3.3 保护功能]
    C43 --> C434[4.3.4 监控功能]

    C432 --> P1[SR-PA601-D54A-1203<br/>输出电流 11.1A]
    C433 --> P2[SR-PA601-D54A-1309<br/>过流保护 12~18A]
    C433 --> P3[SR-PA601-D54A-1308<br/>短路保护]

    style C433 fill:#e8f5e9
    style P2 fill:#fff9c4
```

### 4.3.3 章节元数据的持久化

章节元数据通过三层持久化保障：

| 层次 | 存储位置 | 用途 |
|---|---|---|
| 侧车文件 | `blocks.jsonl` | 原始解析结果，支持重新索引 |
| chunk 元数据 | PostgreSQL KV 存储 | 查询时元数据过滤 |
| 向量 metadata | Qdrant payload | 向量检索时的预过滤 |

**元数据字段设计**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `heading` | string | 章节标题，如“4.3.3 保护功能” |
| `level` | int | 章节层级（1~6） |
| `parent_headings` | list[string] | 父章节路径列表 |
| `section_path` | string | 章节路径，如“4.3.3” |
| `doc_version` | string | 文档版本（A/B） |

## 4.4 章节级查询过滤

### 4.4.1 MetadataFilter 机制

LightRAG 通过**元数据过滤（Metadata Filtering）**实现查询时的章节过滤。分块阶段的章节标题信息会被写入 chunk 的元数据中，查询时通过 `MetadataFilter` 按章节字段进行过滤。

**核心限制**（来自 LightRAG 官方文档）：

| 限制项 | 说明 |
|---|---|
| 存储后端 | 仅支持 **PostgreSQL (PGVectorStorage)** |
| 查询模式 | 仅 **Mix 和 Naive 模式** |
| 过滤性质 | 是**硬过滤（hard in-filter）**，在向量搜索之前执行 WHERE 条件 |

**关键设计**：元数据过滤是**预过滤（pre-filter）**，而非搜索后过滤（post-filter）。这意味着过滤条件在向量搜索之前就注入到 SQL WHERE 子句中，确保**性能和隔离安全**。

### 4.4.2 查询配置示例

**基础章节过滤**：

```python
from lightrag import QueryParam
from lightrag.types import MetadataFilter

# 按章节标题过滤（只检索"4.3.3 保护功能"章节的内容）
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="AND",
        operands=[{"section_path": "4.3.3"}]
    )
)
```

**复合过滤**：

```python
# 章节为 4.3.3 保护功能 且 优先级为强制
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="AND",
        operands=[
            {"section_path": "4.3.3"},
            {"priority": "强制"},
        ]
    )
)

# 章节为 4.3.2 输出特性 或 4.3.3 保护功能
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="OR",
        operands=[
            {"section_path": "4.3.2"},
            {"section_path": "4.3.3"},
        ]
    )
)
```

**嵌套过滤**：

```python
# (章节为 4.3.3) AND (优先级为强制 OR 类别为保护)
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="AND",
        operands=[
            {"section_path": "4.3.3"},
            MetadataFilter(
                operator="OR",
                operands=[
                    {"priority": "强制"},
                    {"category": "protection"},
                ]
            )
        ]
    )
)
```

### 4.4.3 章节过滤在 Agent 中的应用

| Agent | 查询场景 | 章节过滤参数 |
|---|---|---|
| 需求Agent | 提取所有保护需求 | `section_path="4.3.3"` |
| 需求Agent | 提取所有监控需求 | `section_path="4.3.4"` |
| 用例Agent | 获取输出参数的测试判据 | `section_path="4.3.2"` |
| 用例Agent | 获取保护功能的测试判据 | `section_path="4.3.3"` |
| 代码Agent | 获取接口定义的引脚映射 | `section_path="4.2.4.2"` |
| 参数Agent | 获取环境条件参数 | `section_path="4.1"` |

### 4.4.4 章节过滤的降级策略

当存储后端不是 PostgreSQL 时，章节过滤不可用。降级策略：

| 场景 | 降级方案 |
|---|---|
| Qdrant 后端 | 通过 payload 字段 `section_path` 进行预过滤（需在插入时写入 payload） |
| 无章节元数据 | 在查询后对结果进行二次过滤（后过滤，性能较差） |
| 不支持过滤的模式 | 使用 `mix` 或 `naive` 模式替代 `local`/`global`/`hybrid` |

## 4.5 本体设计

### 4.5.1 分层本体架构

本体采用 **“共享本体 + 型号实例”** 的分层设计：

```mermaid
flowchart TB
    subgraph Shared[共享本体层 - 只读]
        S1[单位本体]
        S2[标准本体]
        S3[判据本体]
        S4[参数概念本体]
        S5[信号类型本体]
        S6[章节结构本体]
    end

    subgraph Model[型号实例层 - 隔离]
        M1[PA601-D54A 实例]
        M2[PN1000-48A 实例]
        M3[...]
    end

    Shared -.->|引用| Model

    style Shared fill:#e1f5ff
    style Model fill:#fff4e1
```

### 4.5.2 核心类设计

| 类 | 关键属性 | 关系 |
|---|---|---|
| Product | 型号、文件编号、版本 | hasSection → Section |
| Section | 章节ID、标题、层级、父章节路径 | containsRequirement → Requirement |
| Requirement | 需求ID、优先级、类别、章节路径 | hasParameter → Parameter |
| Parameter | 名称、最小值、典型值、最大值、单位、条件 | appliesUnder → Condition |
| Protection | 保护类型、动作、恢复条件 | hasTripPoint → Parameter |
| Interface | 连接器ID、类型 | hasPin → Pin |
| Signal | 信号名、方向、电平 | carriedBy → Pin |
| TestRequirement | 测试项、判据、标准 | derivedFrom → Requirement |
| Fixture | 工装类型、精度、通道数 | hasProbe → Probe |
| Probe | 型号、针尖类型、行程、寿命 | contacts → TestPoint |
| ProcessParam | 参数名、当前值、参考值 | optimizes → Fixture |

### 4.5.3 本体构建方式

使用 Semantica 的 `ContextGraph` 作为知识层，提供确定性的图构建能力[reference:6]：

```python
from semantica.context import ContextGraph
graph = ContextGraph(advanced_analytics=True)
```

Semantica 提供完整的知识图谱构建流水线[reference:7]：

```python
# 1. 摄入文档
from semantica.ingest import FileIngestor
ingestor = FileIngestor()
sources = ingestor.ingest("data/PA601-D54A规格书.docx")

# 2. 解析
from semantica.parse import DocumentParser
parser = DocumentParser()
parsed = parser.parse(sources[0].path)

# 3. 抽取实体和关系
from semantica.semantic_extract import NERExtractor, RelationExtractor
ner = NERExtractor(method="pattern")
entities = ner.extract(parsed["full_text"])

rel = RelationExtractor(method="pattern")
relationships = rel.extract(parsed["full_text"], entities=entities)

# 4. 构建知识图谱
from semantica.kg import GraphBuilder
builder = GraphBuilder(merge_entities=True)
graph = builder.build({
    "entities": entities,
    "relationships": relationships,
    "workspace": model_id,
})
```

`merge_entities=True` 会自动解析重复实体引用。

## 4.6 多型号隔离

### 4.6.1 三层隔离架构

```mermaid
flowchart TB
    subgraph L1[第一层: 应用层]
        A1[LightRAG workspace]
        A2[查询自动注入]
        A3[章节元数据过滤]
    end

    subgraph L2[第二层: 存储层]
        B1[PostgreSQL<br/>workspace 列]
        B2[Qdrant<br/>payload workspace_id]
        B3[文件<br/>working_dir/workspace/]
    end

    subgraph L3[第三层: 服务层]
        C1[API 网关权限校验]
        C2[令牌自动注入 model_id]
        C3[跨型号显式授权]
    end

    L1 --> L2
    L2 --> L3

    style L1 fill:#e3f2fd
    style L2 fill:#f3e5f5
    style L3 fill:#fff9c4
```

### 4.6.2 LightRAG Workspace 机制

LightRAG 支持在一个服务实例内创建多个隔离的工作区，每个工作区拥有独立的存储后端、文档索引、知识图谱和流水线状态，所有工作区共享相同的 LLM 和存储配置。

**隔离机制对比**：

| 存储后端 | 隔离机制 | 隔离级别 |
|---|---|---|
| PostgreSQL | 每张表增加 `workspace` 列 | 行级逻辑隔离 |
| Qdrant | Payload 字段 `workspace_id` filter | 向量级逻辑隔离 |
| JSON/NetworkX | 子目录 `{working_dir}/{workspace}/` | 文件级物理隔离 |
| MongoDB | 集合前缀 `{workspace}_{namespace}` | 集合级逻辑隔离 |
| Redis | 键前缀 `{workspace}_{namespace}:*` | 键级逻辑隔离 |
| Milvus | 集合前缀 `{workspace}_{namespace}` | 集合级逻辑隔离 |

**配置方式**：

```python
from lightrag import LightRAG

rag = LightRAG(
    working_dir="./rag_storage",
    workspace="PA601-D54A",
    kv_storage="PGKVStorage",
    vector_storage="PGVectorStorage",
    graph_storage="PGGraphStorage",
    doc_status_storage="PGDocStatusStorage",
    enable_hybrid=True,
)
```

**通过 HTTP Header 指定 workspace**：

```
LIGHTRAG-WORKSPACE: PA601-D54A
```

### 4.6.3 Qdrant 多租户 Payload 配置

```python
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams,
    PayloadSchemaType, KeywordIndexParams,
)

WORKSPACE_ID_FIELD = "workspace_id"

def setup_qdrant_multitenancy(
    client: QdrantClient,
    collection_name: str = "lightrag_vectors",
    vector_size: int = 2560,  # Qwen3-Embedding-4B 维度
):
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=vector_size,
            distance=Distance.COSINE,
        ),
    )

    # 创建 workspace_id 的 tenant 索引
    client.create_payload_index(
        collection_name=collection_name,
        field_name=WORKSPACE_ID_FIELD,
        field_schema=KeywordIndexParams(
            type=PayloadSchemaType.KEYWORD,
            is_tenant=True,
        ),
    )
```

**预过滤查询**：

```python
from qdrant_client.models import Filter, FieldCondition, MatchValue

results = client.search(
    collection_name="lightrag_vectors",
    query_vector=embedding,
    query_filter=Filter(
        must=[
            FieldCondition(
                key=WORKSPACE_ID_FIELD,
                match=MatchValue(value="PA601-D54A"),
            ),
        ],
    ),
    limit=top_k,
)
```

### 4.6.4 隔离失效的典型场景与对策

| 失效场景 | 后果 | 对策 |
|---|---|---|
| 查询未携带 model_id | 可能返回所有型号数据 | API 网关强制校验，缺失则拒绝 |
| Qdrant 未建 tenant 索引 | 过滤在搜索后执行，性能差且可能泄漏 | 部署时创建 `is_tenant=True` 索引 |
| LightRAG 未启用 workspace | 多型号数据混在同一实例 | 配置 `workspace=model_id` |
| 跨型号查询未授权 | 越权访问其他型号数据 | 显式 `compare` 接口 + 权限校验 |
| 章节过滤未启用 | 跨章节语义污染 | 查询时传入 `MetadataFilter` |

## 4.7 混合检索设计

### 4.7.1 为什么需要 BM25？

产测规格书中存在大量术语密集型查询：

| 查询类型 | 示例 | 向量检索问题 | BM25 优势 |
|---|---|---|---|
| 需求编号 | SR-PA601-D54A-1203 | 编号在嵌入训练数据中代表性不足 | 精确逐字匹配 |
| 信号名 | VL_DOWN_ALM | 下划线命名，分词器可能误切 | 保留原始 token |
| 产品代码 | CLVTTL、ENIG | 缩写词，嵌入表示质量差 | 精确匹配 |

**实测数据**：在技术术语密集的 EZIS 数据集上，BM25 的 NDCG@10 达到 0.92，稠密检索仅为 0.80，BM25 反超 12 个百分点。

### 4.7.2 RRF 融合机制

```mermaid
flowchart LR
    Q[查询] --> B[BM25 检索]
    Q --> V[向量检索]
    Q --> F[章节元数据过滤]
    B --> R1[排序列表1]
    V --> R2[排序列表2]
    F --> R1
    F --> R2
    R1 --> RRF[RRF 融合]
    R2 --> RRF
    RRF --> TopK[Top-K 结果]

    style RRF fill:#fff9c4
    style F fill:#f3e5f5
```

**设计要点**：BM25 只索引**实体名称**（约 10⁴~10⁵ 条短字符串），不索引边关键词或 chunk 正文，索引规模极小，避开了语料统计漂移问题。

### 4.7.3 配置方式

```python
from lightrag import QueryParam

# hybrid 模式自动执行 BM25 + 向量检索 + RRF 融合
query_param = QueryParam(
    mode="hybrid",
    top_k=5,
    enable_hybrid=True,
)
```

**降级策略**：如果 BM25 不可用（`bm25s` 未安装或 `pg_textsearch` 扩展缺失），hybrid 模式会回退到 vector 模式并记录警告[reference:11]。

## 4.8 构建后的验证清单

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 章节层级正确 | 检查 `blocks.jsonl` 中的 `parent_headings` | 与原文目录一致 |
| 实体抽取完整 | 检查需求项数量 | 与规格书中的 SR 编号数量一致 |
| 关系建立正确 | 检查 `hasParameter`、`belongsToModel` 关系 | 抽样验证准确率 > 90% |
| 多型号隔离 | 用型号 A 查询型号 B 的数据 | 返回空结果 |
| 章节过滤 | 指定 `section_path` 查询 | 仅返回目标章节内容 |
| BM25 可用 | 查询需求编号 | 精确命中 |
| 向量检索 | 语义查询 | 返回相关 chunk |
| 公式推理 | 执行 Datalog 规则 | 推导出正确数值 |

# 第 5 章 推理层设计

## 5.1 推理层总览

推理层是本方案区别于普通 RAG 的核心。它由三个引擎组成，分别处理不同类型的推理任务：

```mermaid
flowchart TB
    subgraph Query[查询进入]
        Q1[语义检索类]
        Q2[公式计算类]
        Q3[约束验证类]
        Q4[关系导航类]
    end

    subgraph Router[查询路由器]
        RT[意图分类]
    end

    subgraph Engines[三大推理引擎]
        E1[LightRAG<br/>混合检索+章节过滤]
        E2[Semantica DatalogReasoner<br/>公式计算]
        E3[Semantica Reasoner<br/>IF/THEN规则]
        E4[PySHACL<br/>约束验证]
    end

    subgraph Fuse[结果融合]
        F1[RRF 排序]
        F2[引用溯源组装]
        F3[决策链生成]
    end

    Q1 --> RT
    Q2 --> RT
    Q3 --> RT
    Q4 --> RT
    RT --> E1
    RT --> E2
    RT --> E3
    RT --> E4
    E1 --> F1
    E2 --> F1
    E3 --> F1
    E4 --> F1
    F1 --> F2
    F2 --> F3
    F3 --> Result[Agent 可用结果]

    style Router fill:#fff9c4
    style Engines fill:#fce4ec
    style Fuse fill:#e8f5e9
```

### 5.1.1 三引擎职责边界

| 引擎 | 职责 | 输入 | 输出 | 适用查询 |
|---|---|---|---|---|
| LightRAG | 语义检索 + 章节过滤 | 自然语言查询 | 相关文本块 + 实体 | “查找保护功能相关的需求” |
| DatalogReasoner | 公式计算 + 多步推导 | 事实 + 规则 | 派生事实 | “计算输出功率” |
| Reasoner | IF/THEN 逻辑推理 | 规则 + 事实 | 新事实或违规 | “检查保护点是否合规” |
| PySHACL | 约束验证 | 数据图 + Shapes | 验证报告 | “探针间距是否满足约束” |

### 5.1.2 查询路由决策

```mermaid
flowchart TD
    Start[用户查询] --> Classify{意图分类}
    Classify -->|自然语言检索| R1[LightRAG]
    Classify -->|数值计算| R2[DatalogReasoner]
    Classify -->|逻辑判断| R3[Reasoner]
    Classify -->|约束检查| R4[PySHACL]
    Classify -->|混合意图| R5[多引擎并行]

    R1 --> Merge[结果融合]
    R2 --> Merge
    R3 --> Merge
    R4 --> Merge
    R5 --> Merge

    style Classify fill:#fff9c4
    style Merge fill:#e8f5e9
```

**意图分类规则**：

| 查询特征 | 路由目标 | 示例 |
|---|---|---|
| 包含“是多少”“计算”“推导” | DatalogReasoner | “输出功率是多少” |
| 包含“是否满足”“检查”“验证” | PySHACL / Reasoner | “探针间距是否合规” |
| 包含“哪些”“查找”“相关” | LightRAG | “哪些需求涉及保护功能” |
| 包含“为什么”“如何推导” | 多引擎 + 溯源 | “为什么探针间距是 0.5mm” |

## 5.2 Datalog 公式规则设计

### 5.2.1 规则分类体系

产测工装领域的 Datalog 规则分为六类：

```mermaid
flowchart TB
    subgraph Rules[Datalog 规则体系]
        R1[基础电学公式]
        R2[效率与损耗]
        R3[工装精度]
        R4[公差链]
        R5[探针选型]
        R6[产能与寿命]
    end

    R1 --> Output1[功率计算]
    R2 --> Output2[效率评估、损耗分析]
    R3 --> Output3[精度分配]
    R4 --> Output4[均方根法公差分析]
    R5 --> Output5[探针选型建议]
    R6 --> Output6[通道数、维护预警]

    style Rules fill:#e1f5ff
```

### 5.2.2 基础电学公式

**功率计算**：

```datalog
% 功率 = 电压 × 电流
Power(?x, ?p) :-
    Voltage(?x, ?u),
    Current(?x, ?i),
    ?p = ?u * ?i .
```

**效率计算**：

```datalog
% 效率 = 输出功率 / 输入功率 × 100
Efficiency(?x, ?eff) :-
    Power(?x, ?p),
    InputPower(?x, ?pin),
    ?pin > 0,
    ?eff = ?p / ?pin * 100 .
```

**损耗计算**：

```datalog
% 损耗 = 输入功率 - 输出功率
Loss(?x, ?l) :-
    InputPower(?x, ?pin),
    Power(?x, ?p),
    ?l = ?pin - ?p .
```

### 5.2.3 工装精度公式

**精度分配（三分法则）**：

```datalog
% 工装定位精度 = 被测参数公差 / 3
FixturePrecision(?f, ?p) :-
    fixtureForProduct(?f, ?prod),
    testRequirement(?prod, ?param, ?tol),
    ?p = ?tol / 3 .
```

**公差链（均方根法）**：

```datalog
% 总公差 = sqrt(Σ 各环节公差²)
TotalTolerance(?a, ?t) :-
    toleranceComponent(?a, ?t1),
    toleranceComponent(?a, ?t2),
    ?t = sqrt(?t1 * ?t1 + ?t2 * ?t2) .
```

### 5.2.4 探针选型规则

**按电流选型**：

```datalog
% 电流 > 5A 使用梅花针
ProbeType(?point, '梅花针') :-
    currentRating(?point, ?i),
    ?i > 5 .
```

**按频率选型**：

```datalog
% 频率 > 1GHz 使用同轴探针
ProbeType(?point, '同轴探针') :-
    signalFrequency(?point, ?f),
    ?f > 1000000000 .
```

**按焊盘类型选型**：

```datalog
% ENIG 焊盘且非大电流非高频，使用圆头针
ProbeType(?point, '圆头针') :-
    padType(?point, 'ENIG'),
    not highCurrent(?point),
    not highFrequency(?point) .
```

**探针间距校验**：

```datalog
% 探针间距必须大于最小允许间距
probeSpacingOK(?fixture) :-
    not exists ?p1, ?p2 (
        probe(?fixture, ?p1),
        probe(?fixture, ?p2),
        ?p1 != ?p2,
        distance(?p1, ?p2, ?d),
        minSpacing(?fixture, ?min),
        ?d < ?min
    ) .
```

### 5.2.5 产能与寿命规则

**通道数计算**：

```datalog
% 所需通道数 = ceil(目标产能 × 单次测试时间 / 可用工作时间)
RequiredChannels(?p, ?n) :-
    targetThroughput(?p, ?tp),
    testTime(?p, ?tt),
    availableTime(?p, ?at),
    ?n = ceil(?tp * ?tt / ?at) .
```

**探针寿命预测**：

```datalog
% 剩余寿命 = 额定寿命 - 已使用次数
RemainingLife(?probe, ?r) :-
    ratedLife(?probe, ?rated),
    usageCount(?probe, ?used),
    ?r = ?rated - ?used .

% 维护预警：剩余寿命低于阈值
MaintenanceAlert(?probe) :-
    RemainingLife(?probe, ?r),
    maintenanceThreshold(?probe, ?th),
    ?r < ?th .
```

### 5.2.6 多步推导链

Datalog 的核心优势是支持多步推导链。以下是产测场景中的完整推导链：

```mermaid
flowchart LR
    V[电压 54V] --> P[功率 599.4W]
    I[电流 11.1A] --> P
    PIN[输入功率 644W] --> E[效率 93%]
    P --> E
    E --> L[损耗 45W]
    L --> T[温升 22.5℃]
    RTH[热阻 0.5℃/W] --> T
    T --> C[散热需求 22.5W]

    style P fill:#fff9c4
    style E fill:#fff9c4
    style L fill:#fff9c4
    style T fill:#fff9c4
    style C fill:#e8f5e9
```

每一步推导都记录在决策溯源链中，最终可回答“为什么散热需求是 22.5W”这类问题，并给出完整的推导路径。

### 5.2.7 规则注入代码示例

```python
from semantica.reasoning import DatalogReasoner

def create_reasoner(model_id: str) -> DatalogReasoner:
    reasoner = DatalogReasoner()

    # ====== 基础电学公式 ======
    reasoner.add_rule(
        "Power(?x, ?p) :- "
        "Voltage(?x, ?u), Current(?x, ?i), ?p = ?u * ?i"
    )
    reasoner.add_rule(
        "Efficiency(?x, ?eff) :- "
        "Power(?x, ?p), InputPower(?x, ?pin), "
        "?pin > 0, ?eff = ?p / ?pin * 100"
    )
    reasoner.add_rule(
        "Loss(?x, ?l) :- "
        "InputPower(?x, ?pin), Power(?x, ?p), ?l = ?pin - ?p"
    )

    # ====== 工装精度公式 ======
    reasoner.add_rule(
        "FixturePrecision(?f, ?p) :- "
        "fixtureForProduct(?f, ?prod), "
        "testRequirement(?prod, ?param, ?tol), ?p = ?tol / 3"
    )
    reasoner.add_rule(
        "TotalTolerance(?a, ?t) :- "
        "toleranceComponent(?a, ?t1), toleranceComponent(?a, ?t2), "
        "?t = sqrt(?t1 * ?t1 + ?t2 * ?t2)"
    )

    # ====== 探针选型规则 ======
    reasoner.add_rule(
        "ProbeType(?point, '梅花针') :- "
        "currentRating(?point, ?i), ?i > 5"
    )
    reasoner.add_rule(
        "ProbeType(?point, '同轴探针') :- "
        "signalFrequency(?point, ?f), ?f > 1000000000"
    )

    # ====== 产能与寿命 ======
    reasoner.add_rule(
        "RequiredChannels(?p, ?n) :- "
        "targetThroughput(?p, ?tp), testTime(?p, ?tt), "
        "availableTime(?p, ?at), ?n = ceil(?tp * ?tt / ?at)"
    )
    reasoner.add_rule(
        "RemainingLife(?probe, ?r) :- "
        "ratedLife(?probe, ?rated), usageCount(?probe, ?used), "
        "?r = ?rated - ?used"
    )

    return reasoner
```

### 5.2.8 从知识图谱加载事实

```python
def load_facts_from_graph(reasoner: DatalogReasoner, graph):
    """
    从知识图谱加载事实到推理器。
    Semantica 自动将节点和边转换为 Datalog 事实。
    """
    reasoner.load_from_graph(graph)
    return reasoner

def derive_all(reasoner: DatalogReasoner) -> list:
    """执行所有推理，返回推导结果。"""
    return reasoner.derive_all()
```

## 5.3 SHACL 约束验证设计

### 5.3.1 约束分类体系

产测工装场景的硬约束分为五类：

```mermaid
flowchart TB
    subgraph Constraints[SHACL 约束体系]
        C1[空间约束]
        C2[电气约束]
        C3[逻辑约束]
        C4[安全约束]
        C5[完整性约束]
    end

    C1 --> O1[探针间距 > 最小间距]
    C2 --> O2[接触电阻 ≤ 0.1Ω]
    C3 --> O3[保护点 ≥ 恢复点 + 回差]
    C4 --> O4[工装必须有 ESD 保护]
    C5 --> O5[强制需求必须有关联测试用例]

    style Constraints fill:#fce4ec
```

### 5.3.2 空间约束

```turtle
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ps: <https://example.org/power-supply#> .

ps:ProbeSpacingShape a sh:NodeShape ;
    sh:targetClass ps:Probe ;
    sh:sparql [
        sh:message "探针间距 {?spacing} 小于最小允许间距 {?minSpacing}" ;
        sh:select """
            SELECT $this ?spacing ?minSpacing WHERE {
                $this ps:spacing ?spacing .
                $this ps:probeOn ?product .
                ?product ps:minProbeSpacing ?minSpacing .
                FILTER (?spacing < ?minSpacing)
            }
        """ ;
    ] .
```

### 5.3.3 电气约束

```turtle
ps:ContactResistanceShape a sh:NodeShape ;
    sh:targetClass ps:Probe ;
    sh:property [
        sh:path ps:contactResistance ;
        sh:maxInclusive 0.1 ;
        sh:message "探针接触电阻必须 ≤ 0.1Ω" ;
    ] .
```

### 5.3.4 逻辑约束

```turtle
ps:ProtectionHysteresisShape a sh:NodeShape ;
    sh:targetClass ps:Protection ;
    sh:sparql [
        sh:message "保护点 {?trip} 必须大于恢复点 {?recovery}" ;
        sh:select """
            SELECT $this ?trip ?recovery WHERE {
                $this ps:hasTripPoint ?trip .
                $this ps:hasRecoveryPoint ?recovery .
                FILTER (?trip < ?recovery)
            }
        """ ;
    ] .
```

### 5.3.5 安全约束

```turtle
ps:ESDProtectionShape a sh:NodeShape ;
    sh:targetClass ps:Fixture ;
    sh:property [
        sh:path ps:hasESDProtection ;
        sh:hasValue true ;
        sh:message "工装必须具备 ESD 保护" ;
    ] .
```

### 5.3.6 完整性约束

```turtle
ps:TestCoverageShape a sh:NodeShape ;
    sh:targetClass ps:Requirement ;
    sh:sparql [
        sh:message "强制需求 {?this} 缺少关联的测试用例" ;
        sh:select """
            SELECT $this WHERE {
                $this ps:hasPriority "强制" .
                FILTER NOT EXISTS { $this ps:verifiedBy ?test }
            }
        """ ;
    ] .
```

### 5.3.7 约束验证流程

```mermaid
flowchart LR
    D[数据图] --> V[SHACL 验证器]
    S[SHACL Shapes] --> V
    V --> R{符合?}
    R -->|是| Pass[通过]
    R -->|否| Report[违规报告]
    Report --> Fix[修正建议]

    style Report fill:#ffebee
    style Pass fill:#e8f5e9
```

### 5.3.8 验证代码示例

```python
from pyshacl import validate
from rdflib import Graph

SHACL_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ps: <https://example.org/power-supply#> .

ps:ProbeSpacingShape a sh:NodeShape ;
    sh:targetClass ps:Probe ;
    sh:sparql [
        sh:message "探针间距 {?spacing} 小于最小允许间距 {?minSpacing}" ;
        sh:select \"\"\"
            SELECT $this ?spacing ?minSpacing WHERE {
                $this ps:spacing ?spacing .
                $this ps:probeOn ?product .
                ?product ps:minProbeSpacing ?minSpacing .
                FILTER (?spacing < ?minSpacing)
            }
        \"\"\" ;
    ] .

ps:ContactResistanceShape a sh:NodeShape ;
    sh:targetClass ps:Probe ;
    sh:property [
        sh:path ps:contactResistance ;
        sh:maxInclusive 0.1 ;
        sh:message "探针接触电阻必须 ≤ 0.1Ω" ;
    ] .
"""

def validate_against_shacl(data_graph: str) -> tuple:
    conforms, results_graph, results_text = validate(
        data_graph=data_graph,
        shacl_graph=SHACL_SHAPES,
        data_graph_format="turtle",
        shacl_graph_format="turtle",
    )
    return conforms, results_graph, results_text
```

## 5.4 决策溯源设计

### 5.4.1 溯源模型

Semantica 的决策溯源能力将每个决策记录为一等图对象，包含因果链、置信度和决策者信息：

```mermaid
flowchart TB
    subgraph Decision[决策节点]
        D[决策: 探针间距 = 0.5mm]
    end

    subgraph Premises[前提]
        P1[规则: 探针间距 > 最小间距]
        P2[参数: 最小间距 = 0.4mm]
        P3[来源: 规格书 4.2.1 节]
    end

    subgraph Trace[溯源链]
        T1[决策 ID]
        T2[因果链]
        T3[置信度]
        T4[决策者: 参数Agent]
        T5[章节路径]
    end

    P1 --> D
    P2 --> D
    P3 --> D
    D --> T1
    T1 --> T2
    T2 --> T3
    T3 --> T4
    T4 --> T5

    style Decision fill:#e1f5ff
    style Trace fill:#e8f5e9
```

### 5.4.2 溯源价值

产测软件研发需要严格的审计追踪。当需要解释“为什么探针间距设置为 0.5mm”时，系统可以回溯到：

- 该决策由**规则A**触发（探针间距 > 最小间距）
- 规则A依赖**参数B**（最小间距 = 0.4mm）
- 参数B来自**规格书C的第D页**（4.2.1 节）
- 决策者是**参数Agent**
- 置信度为 **1.0**

### 5.4.3 章节溯源增强

由于章节元数据被持久化，溯源链可以精确到**章节路径**，而非仅页码：

```
决策: 探针间距 = 0.5mm
├── 规则: ProbeSpacingShape (SHACL)
├── 前提1: minProbeSpacing = 0.4mm
│   └── 来源: 4.2.1 结构要求 > SR-PA601-D54A-0300
├── 前提2: spacing = 0.5mm
│   └── 来源: 工装设计方案
└── 推导: 0.5 > 0.4 → 满足约束
```

### 5.4.4 溯源代码示例

```python
from semantica.context import ContextGraph
from semantica.decision import DecisionRecorder
from semantica.reasoning import ExplanationGenerator

class DecisionTracer:
    def __init__(self, graph: ContextGraph):
        self.graph = graph
        self.recorder = DecisionRecorder(graph)
        self.explainer = ExplanationGenerator()

    def record_decision(
        self,
        agent_name: str,
        decision_type: str,
        decision_value: str,
        premises: list[str],
        rule_used: str,
        confidence: float = 1.0,
        section_path: str = "",
    ) -> str:
        """记录一条决策及其因果链。"""
        decision_id = self.recorder.record(
            agent=agent_name,
            decision_type=decision_type,
            value=decision_value,
            premises=premises,
            rule=rule_used,
            confidence=confidence,
            metadata={"section_path": section_path},
        )
        return decision_id

    def explain(self, decision_id: str) -> str:
        """生成决策的自然语言解释。"""
        result = self.graph.query_decision(decision_id)
        return self.explainer.generate_explanation(result)

    def trace_chain(self, decision_id: str) -> list:
        """追溯决策的完整因果链。"""
        return self.graph.trace_causal_chain(decision_id)
```

## 5.5 结果融合设计

### 5.5.1 多引擎结果融合流程

```mermaid
flowchart TB
    subgraph Inputs[多引擎输出]
        I1[LightRAG: 文本块+实体]
        I2[Datalog: 派生事实]
        I3[SHACL: 验证报告]
        I4[Reasoner: 新事实]
    end

    subgraph Fuse[融合层]
        F1[归一化]
        F2[RRF 排序]
        F3[引用溯源组装]
        F4[章节路径标注]
    end

    subgraph Output[输出]
        O1[结构化结果]
        O2[引用列表]
        O3[决策链]
    end

    Inputs --> F1
    F1 --> F2
    F2 --> F3
    F3 --> F4
    F4 --> Output

    style Fuse fill:#fff9c4
    style Output fill:#e8f5e9
```

### 5.5.2 融合规则

| 结果类型 | 融合策略 | 优先级 |
|---|---|---|
| 文本块 | RRF 排序 | 中 |
| 派生事实 | 直接采纳 | 高 |
| 验证报告 | 直接采纳 | 高 |
| 新事实 | 去重后合并 | 中 |
| 引用来源 | 合并去重 | 低 |

### 5.5.3 引用溯源组装

每条返回给 Agent 的结果都必须包含引用溯源信息：

```json
{
  "result": "输出过流保护点为 12~18A",
  "citations": [
    {
      "source": "SR-PA601-D54A-1309",
      "section_path": "4.3.3 保护功能",
      "doc_version": "B",
      "model_id": "PA601-D54A"
    }
  ],
  "derivation": {
    "rule": "protectionTripPoint",
    "premises": ["规格书 4.3.3 节"],
    "confidence": 1.0
  }
}
```

### 5.5.4 章节路径标注

由于章节元数据被持久化，每条结果都可以标注其章节路径：

```
结果: 输出过流保护 12~18A
章节路径: 4 技术要求 > 4.3 功能/性能要求 > 4.3.3 保护功能 > SR-PA601-D54A-1309
```

这个标注在 Agent 生成最终报告时可直接引用，满足产测场景的审计需求。

## 5.6 推理性能优化

### 5.6.1 缓存策略

| 缓存对象 | 缓存位置 | 失效条件 |
|---|---|---|
| Datalog 推导结果 | 内存 LRU | 规则或事实变更 |
| SHACL 验证报告 | 内存 LRU | 数据图变更 |
| 混合检索结果 | Redis | 文档索引更新 |
| 决策溯源链 | 图数据库 | 永久存储 |

### 5.6.2 增量推理

当新增事实时，只对受影响的部分重新推理，而非全量重算：

```python
def incremental_derive(reasoner, new_facts):
    """
    增量推理：只对新增事实相关的规则重新评估。
    """
    reasoner.add_facts(new_facts)
    affected_rules = reasoner.find_affected_rules(new_facts)
    results = reasoner.derive_rules(affected_rules)
    return results
```

### 5.6.3 推理超时保护

Datalog 推理通过半朴素不动点评估保证终止性，但仍需设置超时保护：

```python
reasoner = DatalogReasoner(timeout_seconds=30)
try:
    results = reasoner.derive_all()
except TimeoutError:
    logger.warning("推理超时，返回部分结果")
    results = reasoner.partial_results()
```

## 5.7 推理层验证清单

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 公式计算正确性 | 用已知输入验证输出 | 功率 = 电压 × 电流，误差 < 0.1% |
| 多步推导完整性 | 检查推导链是否完整 | 从电压电流到散热需求全链路可追溯 |
| SHACL 约束有效性 | 构造违规数据验证 | 违规被正确识别 |
| 决策溯源可追溯 | 查询决策 ID | 返回完整因果链和章节路径 |
| 推理终止性 | 执行递归规则 | 在超时前完成 |
| 增量推理正确性 | 新增事实后对比全量推理 | 结果一致 |
| 结果融合一致性 | 多引擎并行查询 | 结果无冲突，引用完整 |

# 第 6 章 Agent 设计

## 6.1 Agent 设计总览

四个专业 Agent 通过统一的 MCP Server 访问知识层，各自承担明确的职责。它们的协作遵循“需求先行、用例与参数并行、代码最后”的流水线模式。

```mermaid
flowchart TB
    subgraph Input[输入]
        I1[规格书 RAG<br/>含章节元数据]
    end

    subgraph Agents[四个专业 Agent]
        A1[产测需求 Agent<br/>按章节提取需求]
        A2[产测用例 Agent<br/>按章节生成用例]
        A3[产测代码 Agent<br/>按接口章节生成代码]
        A4[产测参数 Agent<br/>按环境/保护章节计算]
    end

    subgraph Output[输出]
        O1[需求清单]
        O2[用例集]
        O3[产测代码]
        O4[工装参数 + 工艺优化]
    end

    Input --> A1
    A1 --> A2
    A1 --> A4
    A2 --> A3
    A4 --> A3

    A1 --> O1
    A2 --> O2
    A3 --> O3
    A4 --> O4

    style A1 fill:#e1f5ff
    style A2 fill:#fff4e1
    style A3 fill:#fce4ec
    style A4 fill:#e8f5e9
```

### 6.1.1 Agent 职责矩阵

| Agent | 核心职责 | 主要输入 | 主要输出 | 依赖 |
|---|---|---|---|---|
| 产测需求 Agent | 从规格书提取可测试参数和判据 | 产品 RAG | 结构化需求清单 | 无（先行） |
| 产测用例 Agent | 为每条需求生成测试用例 | 需求清单 + 产品 RAG | 结构化用例集 | 需求 Agent |
| 产测代码 Agent | 将用例转为可执行代码 | 用例集 + 工装规格 | 产测代码 | 用例 Agent + 参数 Agent |
| 产测参数 Agent | 计算工装参数和工艺优化 | 产品 RAG + 历史数据 | 工装参数 + 优化建议 | 需求 Agent |

### 6.1.2 Agent 与章节的对应关系

| Agent | 主要检索章节 | 章节过滤示例 |
|---|---|---|
| 需求 Agent | 4.3 功能/性能要求 | `section_path IN ["4.3.1", "4.3.2", "4.3.3", "4.3.4"]` |
| 用例 Agent | 4.3.2 输出特性、4.3.3 保护功能 | `section_path IN ["4.3.2", "4.3.3"]` |
| 代码 Agent | 4.2.4 接口要求 | `section_path="4.2.4.2"` |
| 参数 Agent | 4.1 环境条件、4.3.2 输出特性 | `section_path IN ["4.1", "4.3.2"]` |

## 6.2 MCP 工具接口

### 6.2.1 工具定义

所有 Agent 通过统一的 MCP Server 访问知识层。MCP Server 暴露以下工具：

| 工具名 | 功能 | 调用的 Agent | 支持章节过滤 |
|---|---|---|---|
| `search_requirements` | 按关键词/类别检索需求项 | 需求 Agent、用例 Agent | ✅ |
| `query_parameters` | 查询参数值、条件、判据 | 全部 Agent | ✅ |
| `calculate` | 执行 Datalog 公式计算 | 参数 Agent | — |
| `validate_constraints` | 执行 SHACL 约束验证 | 参数 Agent、用例 Agent | — |
| `get_test_cases` | 获取/生成测试用例 | 用例 Agent、代码 Agent | ✅ |
| `get_fixture_spec` | 获取工装规格和探针选型 | 代码 Agent、参数 Agent | ✅ |
| `search_cases` | 检索历史工装/测试案例 | 全部 Agent | ✅ |
| `optimize_process` | 工艺参数优化建议 | 参数 Agent | — |

### 6.2.2 工具输入 Schema

**search_requirements**：

```json
{
  "name": "search_requirements",
  "description": "在指定型号的规格书中搜索需求项，支持按章节过滤。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "model_id": {"type": "string", "description": "型号，如 PA601-D54A"},
      "query": {"type": "string", "description": "自然语言查询"},
      "section_path": {
        "type": "string",
        "description": "可选，限定检索的章节路径，如 '4.3.3'"
      },
      "category": {
        "type": "string",
        "enum": ["protection", "monitoring", "emc", "environment", "all"],
        "default": "all"
      },
      "priority": {
        "type": "string",
        "enum": ["强制", "推荐", "不要求", "all"],
        "default": "all"
      },
      "top_k": {"type": "integer", "default": 5}
    },
    "required": ["model_id", "query"]
  }
}
```

**calculate**：

```json
{
  "name": "calculate",
  "description": "执行 Datalog 公式计算（功率、公差链、探针选型、节拍计算）。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "model_id": {"type": "string"},
      "formula_type": {
        "type": "string",
        "enum": [
          "power", "efficiency", "tolerance",
          "probe_selection", "channel_count", "probe_life"
        ]
      },
      "inputs": {"type": "object", "description": "公式输入参数"}
    },
    "required": ["model_id", "formula_type"]
  }
}
```

### 6.2.3 MCP 客户端配置

```json
{
  "mcpServers": {
    "power-spec-rag": {
      "command": "python",
      "args": ["-m", "src.mcp_server.server"],
      "env": {
        "POSTGRES_DSN": "postgresql://user:pass@localhost:5432/power_specs",
        "QDRANT_URL": "http://localhost:6333",
        "VLLM_LLM_BASE": "http://localhost:8000/v1",
        "VLLM_EMBED_BASE": "http://localhost:8001/v1",
        "RAG_STORAGE_ROOT": "./rag_storage"
      }
    }
  }
}
```

## 6.3 章节过滤在 Agent 中的应用

### 6.3.1 章节过滤的典型场景

```mermaid
flowchart TB
    subgraph Agent[Agent 查询]
        A1[需求Agent: 提取保护需求]
        A2[用例Agent: 获取测试判据]
        A3[代码Agent: 获取引脚映射]
        A4[参数Agent: 获取环境参数]
    end

    subgraph Filter[章节过滤]
        F1["section_path = '4.3.3'"]
        F2["section_path IN ['4.3.2', '4.3.3']"]
        F3["section_path = '4.2.4.2'"]
        F4["section_path = '4.1'"]
    end

    subgraph Result[检索结果]
        R1[保护功能需求]
        R2[输出特性+保护功能判据]
        R3[X2 连接器引脚定义]
        R4[工作/存储环境参数]
    end

    A1 --> F1 --> R1
    A2 --> F2 --> R2
    A3 --> F3 --> R3
    A4 --> F4 --> R4

    style Filter fill:#f3e5f5
    style Result fill:#e8f5e9
```

### 6.3.2 章节过滤的 API 调用示例

```python
# 需求 Agent：提取所有保护需求
result = await mcp.call_tool("search_requirements", {
    "model_id": "PA601-D54A",
    "query": "所有保护功能需求",
    "section_path": "4.3.3",
    "category": "protection",
    "priority": "强制",
})

# 用例 Agent：获取输出特性章节的测试判据
result = await mcp.call_tool("query_parameters", {
    "model_id": "PA601-D54A",
    "param_name": "整机效率",
    "section_path": "4.3.2",
})

# 代码 Agent：获取输出接口的引脚映射
result = await mcp.call_tool("query_parameters", {
    "model_id": "PA601-D54A",
    "param_name": "输出连接器引脚",
    "section_path": "4.2.4.2",
})
```

### 6.3.3 章节过滤的降级策略

当章节过滤不可用时（如存储后端非 PostgreSQL），Agent 采用降级策略：

| 场景 | 降级方案 |
|---|---|
| Qdrant 后端 | 通过 payload 字段 `section_path` 预过滤 |
| 无章节元数据 | 在查询后对结果二次过滤 |
| 不支持过滤的模式 | 使用 `mix` 或 `naive` 模式替代 |

## 6.4 产测需求 Agent

### 6.4.1 职责与工作流

**职责**：从规格书中提取所有可测试参数和判据，生成结构化测试需求清单。

```mermaid
flowchart LR
    subgraph Input[输入]
        I1[产品RAG]
        I2[章节元数据]
    end

    subgraph Process[处理流程]
        P1[混合检索参数]
        P2[本体关系查询]
        P3[优先级分类]
        P4[条件标注]
        P5[章节分组]
    end

    subgraph Output[输出]
        O1[结构化需求清单]
    end

    Input --> P1
    P1 --> P2
    P2 --> P3
    P3 --> P4
    P4 --> P5
    P5 --> Output

    style Process fill:#e1f5ff
    style Output fill:#e8f5e9
```

### 6.4.2 工作步骤详解

1. **参数检索**：按章节分组，从产品 RAG 中检索所有可测试参数
2. **测试项推导**：根据参数类别和判据，推导测试项目
3. **测试点识别**：从接口定义中识别物理测试点
4. **优先级标注**：根据“强制/推荐/不要求”等级标注
5. **章节分组**：将需求按章节路径分组，便于后续用例生成

### 6.4.3 章节分组输出示例

```
需求清单（按章节）
├── 4.3.1 输入特性
│   ├── SR-PA601-D54A-1100 标称输入电压范围
│   ├── SR-PA601-D54A-1101 输入工作电压范围
│   └── ...
├── 4.3.2 输出特性
│   ├── SR-PA601-D54A-1200 额定输出电压
│   ├── SR-PA601-D54A-1203 输出电流
│   ├── SR-PA601-D54A-1210 整机效率
│   └── ...
├── 4.3.3 保护功能
│   ├── SR-PA601-D54A-1300 输入过压保护
│   ├── SR-PA601-D54A-1309 输出过流保护
│   └── ...
└── 4.3.4 监控功能
    ├── SR-PA601-D54A-1402 输入掉电告警
    └── ...
```

### 6.4.4 Prompt 模板

```python
REQUIREMENT_AGENT_PROMPT = """
你是产测需求分析专家，负责从产品规格书中提取测试需求。

## 任务
从以下规格书段落中提取所有可测试的参数和对应判据，生成结构化测试需求清单。

## 输出格式
```json
{
  "requirements": [
    {
      "req_id": "SR-PA601-D54A-1210",
      "title": "整机效率",
      "category": "performance",
      "priority": "强制",
      "section_path": "4.3.2",
      "parameters": [
        {
          "name": "整机效率",
          "output_rail": "-54V",
          "min": 86,
          "unit": "%",
          "condition": {
            "input_voltage": "220Vac",
            "load_percent": 20
          }
        }
      ],
      "test_standard": "YD/T 731"
    }
  ]
}
```

## 规则
1. 每条测试需求必须关联原始需求 ID（SR-PA601-D54A-xxxx）
2. 标注测试条件（输入电压、负载比例、温度）
3. 标注判据（上下限、精度、标准）
4. 按优先级排序（强制 > 推荐 > 不要求）
5. 保护类需求需同时提取保护点、恢复点、动作和回差
6. 标注章节路径，便于后续溯源

## 规格书内容
{context}

## 历史参考案例
{similar_cases}
"""
```

## 6.5 产测用例 Agent

### 6.5.1 职责与工作流

**职责**：为每条测试需求生成可执行的测试用例。

```mermaid
flowchart LR
    subgraph Input[输入]
        I1[需求清单]
        I2[产品RAG]
    end

    subgraph Process[处理流程]
        P1[模板选择]
        P2[步骤生成]
        P3[边界值补充]
        P4[判据绑定]
        P5[SHACL验证]
    end

    subgraph Output[输出]
        O1[结构化用例集]
    end

    Input --> P1
    P1 --> P2
    P2 --> P3
    P3 --> P4
    P4 --> P5
    P5 --> Output

    style Process fill:#fff4e1
    style Output fill:#e8f5e9
```

### 6.5.2 用例模板

覆盖五类测试场景：

| 模板类型 | 适用参数 | 关键步骤 |
|---|---|---|
| 电压测试 | 输出电压、输入电压 | 设置输入 → 测量输出 → 比对判据 |
| 电流测试 | 输出电流、输入电流 | 设置负载 → 测量电流 → 比对判据 |
| 效率测试 | 整机效率 | 设置输入/负载 → 读取功率 → 计算效率 |
| 保护测试 | 过压/过流/短路/过温 | 模拟故障 → 验证保护动作 → 验证恢复 |
| 通信测试 | I2C/PMBUS 信号 | 发送命令 → 读取响应 → 验证协议 |

### 6.5.3 边界值补充策略

根据参数范围自动生成边界测试用例：

| 测试点 | 生成规则 |
|---|---|
| 最小值 | 参数最小值 |
| 最小值下限 | 参数最小值 - ε |
| 典型值 | 参数典型值 |
| 最大值 | 参数最大值 |
| 最大值上限 | 参数最大值 + ε |

### 6.5.4 用例输出示例

```json
{
  "testCaseId": "TC:PA601-D54A:1210:01",
  "requirementRef": "SR-PA601-D54A-1210",
  "section_path": "4.3.2",
  "title": "整机效率测试@220Vac/50%负载",
  "preconditions": ["输入电压：220Vac", "环境温度：25℃", "负载：电子负载"],
  "steps": [
    {"step": 1, "action": "设置输入电压 220Vac", "expected": "电源正常启动"},
    {"step": 2, "action": "设置负载为 50% 最大输出", "expected": "输出稳定"},
    {"step": 3, "action": "读取输入功率和输出功率", "expected": "功率读数有效"},
    {"step": 4, "action": "计算效率 = 输出功率/输入功率", "expected": "效率 ≥ 91%"}
  ],
  "criterion": "效率≥91%",
  "standard": "YD/T 731",
  "priority": "强制"
}
```

### 6.5.5 SHACL 覆盖率验证

用例生成后，通过 SHACL 检查覆盖完整性：

```turtle
ps:TestCoverageShape a sh:NodeShape ;
    sh:targetClass ps:Requirement ;
    sh:sparql [
        sh:message "强制需求 {?this} 缺少关联的测试用例" ;
        sh:select """
            SELECT $this WHERE {
                $this ps:hasPriority "强制" .
                FILTER NOT EXISTS { $this ps:verifiedBy ?test }
            }
        """ ;
    ] .
```

## 6.6 产测代码 Agent

### 6.6.1 职责与工作流

**职责**：将测试用例转为可执行的产测软件代码。

```mermaid
flowchart LR
    subgraph Input[输入]
        I1[用例集]
        I2[工装规格]
        I3[历史代码库]
    end

    subgraph Process[处理流程]
        P1[代码模板检索]
        P2[接口绑定]
        P3[流程编排]
        P4[判据嵌入]
        P5[语法检查]
    end

    subgraph Output[输出]
        O1[产测代码]
    end

    Input --> P1
    P1 --> P2
    P2 --> P3
    P3 --> P4
    P4 --> P5
    P5 --> Output

    style Process fill:#fce4ec
    style Output fill:#e8f5e9
```

### 6.6.2 代码生成策略

**关键设计**：代码生成需要**领域适配**。通用 LLM 在领域特定代码生成任务上成功率较低，因为领域特定任务的解决方案在通用训练数据中代表性不足。本方案通过将**自上而下的知识图谱推理**与**自下而上的案例推理**相结合，提升生成质量。

```mermaid
flowchart TB
    subgraph TopDown[自上而下推理]
        T1[知识图谱查询工装规格]
        T2[本体查询接口定义]
        T3[Datalog 计算参数]
    end

    subgraph BottomUp[自下而上案例]
        B1[检索相似产品的测试代码]
        B2[提取代码模板]
        B3[适配当前产品]
    end

    subgraph Generate[代码生成]
        G1[LLM 生成]
        G2[语法检查]
        G3[模拟执行]
    end

    TopDown --> Generate
    BottomUp --> Generate

    style Generate fill:#e8f5e9
```

### 6.6.3 代码生成 Prompt

```python
CODE_GENERATION_PROMPT = """
你是产测软件代码生成专家。根据测试用例和工装规格，生成可执行的 Python 产测代码。

## 输入
- 测试用例：{test_case_json}
- 工装规格：{fixture_spec}
- 仪器配置：{instrument_config}
- 历史代码模板：{code_template}

## 输出要求
1. 使用 pytest 框架组织测试
2. 使用 pyvisa 控制仪器（SCPI 命令）
3. 每条测试用例对应一个 test_ 函数
4. 判据转为 assert 断言
5. 包含 fixture 用于仪器初始化和清理
6. 添加注释关联需求 ID 和章节路径

## 代码结构
```python
# 自动生成：{model_id} 产测代码
# 关联需求：{req_ids}
# 章节路径：{section_paths}
import pyvisa
import pytest
import time

class Test{ModelId}:
    def setup_method(self):
        # 仪器初始化
        ...

    @pytest.mark.parametrize(...)
    def test_{test_name}(self):
        # 测试步骤
        ...
        # 判据断言
        assert ...
```

## 测试用例
{test_case_json}

## 历史代码参考
{historical_code}
"""
```

### 6.6.4 代码注释示例

```python
# 自动生成：PA601-D54A 效率测试
# 关联需求：SR-PA601-D54A-1210
# 章节路径：4 技术要求 > 4.3 功能/性能要求 > 4.3.2 输出特性 > SR-PA601-D54A-1210
# 测试条件：220Vac, 50%负载
# 生成时间：2026-09-24T10:30:00Z
```

## 6.7 产测参数 Agent

### 6.7.1 职责与工作流

**职责**：计算工装设计参数，生成工艺优化建议。这是推理能力要求最高的 Agent。

```mermaid
flowchart TB
    S1[Step 1: 公差链计算<br/>来源: 4.3.2 输出特性] --> S2[Step 2: 工装定位精度]
    S2 --> S3[Step 3: 探针选型<br/>来源: 4.2.4.2 输出接口]
    S3 --> S4[Step 4: 节拍与通道数]
    S4 --> S5[Step 5: SHACL 约束验证]
    S5 --> S6[Step 6: 工艺优化建议]

    S1 -.->|溯源| T[决策追踪]
    S2 -.->|溯源| T
    S3 -.->|溯源| T
    S4 -.->|溯源| T

    style S1 fill:#e8f5e9
    style S6 fill:#e8f5e9
```

### 6.7.2 工作步骤详解

1. **公差链计算**：从产品公差推导工装定位精度要求
2. **探针选型**：根据焊盘类型、电流、频率自动推荐探针
3. **节拍与通道数计算**：根据目标产能和单次测试时间计算
4. **约束验证**：通过 SHACL 检查工装方案是否满足约束
5. **工艺参数优化**：根据历史生产数据和实时质量反馈，推荐工艺参数调整

### 6.7.3 决策溯源记录

每一步计算都记录决策溯源：

```python
class ParameterAgent:
    def __init__(self, model_id: str, mcp_client, graph):
        self.model_id = model_id
        self.mcp = mcp_client
        self.reasoner = create_reasoner(model_id)
        self.tracer = DecisionTracer(graph)

    async def derive_fixture_parameters(self) -> dict:
        results = {}

        # Step 1: 公差链计算
        tolerance = await self.mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "tolerance",
        })
        results["tolerance"] = tolerance
        self.tracer.record_decision(
            agent_name="parameter_agent",
            decision_type="tolerance_chain",
            decision_value=str(tolerance),
            premises=["产品公差", "工装公差", "测试重复性"],
            rule_used="均方根法",
            section_path="4.3.2",
        )

        # Step 2: 工装定位精度
        precision = await self.mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "probe_selection",
            "inputs": {"tolerance": tolerance},
        })
        results["fixture_precision"] = precision

        # Step 3: 探针选型
        probe = await self.mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "probe_selection",
        })
        results["probe_selection"] = probe

        # Step 4: 节拍与通道数
        channel = await self.mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "channel_count",
        })
        results["channel_count"] = channel

        # Step 5: SHACL 约束验证
        validation = await self.mcp.call_tool("validate_constraints", {
            "model_id": self.model_id,
            "constraint_type": "probe_spacing",
        })
        results["constraint_validation"] = validation

        return results
```

### 6.7.4 工艺优化的时序预测分层

| 预测任务 | 推荐模型 | 理由 |
|---|---|---|
| 短期节拍预测（日/周） | XGBoost | 制造业聚合预测最优，误差 <3% |
| 季节性产能波动 | SARIMA / Prophet | 稳定季节性数据上表现好 |
| 长周期趋势预测 | LSTM / Transformer | 捕捉长期依赖关系 |
| 动态模型选择 | TimeSpeaks | 自适应选择最优算法 |

### 6.7.5 工艺优化闭环

```mermaid
flowchart LR
    subgraph Production[生产现场]
        P1[实际节拍]
        P2[良率数据]
        P3[探针寿命]
    end

    subgraph Feedback[数据回写]
        F1[回写知识图谱]
        F2[更新历史案例]
    end

    subgraph Optimize[优化建议]
        O1[节拍优化]
        O2[参数调整]
        O3[维护预警]
    end

    Production --> Feedback
    Feedback --> Optimize
    Optimize --> Production

    style Feedback fill:#fff9c4
    style Optimize fill:#e8f5e9
```

## 6.8 Agent 协同编排

### 6.8.1 编排器实现

```python
from enum import Enum
from dataclasses import dataclass, field
import asyncio

class AgentType(Enum):
    REQUIREMENT = "requirement"
    TEST_CASE = "test_case"
    CODE = "code"
    PARAMETER = "parameter"

@dataclass
class AgentPipeline:
    """Agent 流水线"""
    model_id: str
    requirement_output: dict | None = None
    test_case_output: dict | None = None
    code_output: dict | None = None
    parameter_output: dict | None = None

    async def run(self, mcp_client) -> dict:
        """执行完整流水线。"""
        # Phase 1: 需求分析（必须先执行）
        self.requirement_output = await self._run_requirement(mcp_client)

        # Phase 2: 用例生成 + 参数计算（可并行）
        case_task = self._run_test_case(mcp_client)
        param_task = self._run_parameter(mcp_client)
        self.test_case_output, self.parameter_output = await asyncio.gather(
            case_task, param_task,
        )

        # Phase 3: 代码生成（依赖用例输出）
        self.code_output = await self._run_code(mcp_client)

        return {
            "model_id": self.model_id,
            "requirements": self.requirement_output,
            "test_cases": self.test_case_output,
            "code": self.code_output,
            "parameters": self.parameter_output,
        }

    async def _run_requirement(self, mcp) -> dict:
        """产测需求 Agent"""
        return await mcp.call_tool("search_requirements", {
            "model_id": self.model_id,
            "query": "所有可测试参数和判据",
            "category": "all",
            "top_k": 50,
        })

    async def _run_test_case(self, mcp) -> dict:
        """产测用例 Agent"""
        reqs = self.requirement_output.get("graph_results", [])
        cases = []
        for req in reqs[:20]:
            case = await mcp.call_tool("get_test_cases", {
                "model_id": self.model_id,
                "requirement_id": req.get("req_id", ""),
            })
            cases.append(case)
        return {"test_cases": cases}

    async def _run_code(self, mcp) -> dict:
        """产测代码 Agent"""
        return {"status": "generated", "language": "python"}

    async def _run_parameter(self, mcp) -> dict:
        """产测参数 Agent"""
        tolerance = await mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "tolerance",
        })
        probe = await mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "probe_selection",
        })
        channel = await mcp.call_tool("calculate", {
            "model_id": self.model_id,
            "formula_type": "channel_count",
        })
        return {
            "tolerance": tolerance,
            "probe": probe,
            "channel_count": channel,
        }
```

### 6.8.2 流水线执行时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant M as MCP Server
    participant R as LightRAG
    participant S as Semantica
    participant A1 as 需求Agent
    participant A2 as 用例Agent
    participant A3 as 代码Agent
    participant A4 as 参数Agent

    U->>M: 上传规格书 + 启动流水线
    M->>R: 构建产品RAG（保留章节）
    M->>S: 初始化知识图谱
    R-->>M: 就绪（含章节元数据）
    S-->>M: 就绪

    rect rgb(225, 245, 255)
        Note over M,A1: Phase 1: 需求提取
        M->>A1: 启动需求分析
        A1->>R: 混合检索 + 章节过滤
        R-->>A1: 候选需求列表
        A1->>S: 查询本体关系
        S-->>A1: 结构化需求
        A1-->>M: 需求清单
    end

    rect rgb(255, 244, 225)
        Note over M,A4: Phase 2: 并行执行
        par 用例生成
            M->>A2: 启动用例生成
            A2->>R: 检索历史用例 + 章节过滤
            A2->>S: 验证约束
            A2-->>M: 测试用例集
        and 参数计算
            M->>A4: 启动参数计算
            A4->>S: Datalog 公式计算
            S-->>A4: 计算结果
            A4->>S: SHACL 约束验证
            A4-->>M: 工装参数
        end
    end

    rect rgb(252, 228, 236)
        Note over M,A3: Phase 3: 代码生成
        M->>A3: 启动代码生成
        A3->>R: 检索代码模板
        A3->>S: 获取工装规格
        A3-->>M: 产测代码
    end

    M-->>U: 完整产测方案
```

### 6.8.3 异常处理

| 异常场景 | 处理策略 |
|---|---|
| 需求 Agent 失败 | 终止流水线，返回错误详情 |
| 用例 Agent 失败 | 保留参数 Agent 结果，标记用例缺失 |
| 参数 Agent 失败 | 保留用例 Agent 结果，标记参数缺失 |
| 代码 Agent 失败 | 保留前序结果，返回用例和参数供人工使用 |
| SHACL 验证不通过 | 标记违规项，生成修正建议 |

## 6.9 Agent 验证清单

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 需求 Agent 抽取完整性 | 与规格书 SR 编号对比 | 覆盖率 > 95% |
| 用例 Agent 覆盖率 | SHACL 覆盖率检查 | 强制需求 100% 覆盖 |
| 代码 Agent 语法正确性 | 编译/语法检查 | 无语法错误 |
| 参数 Agent 计算正确性 | 与人工计算对比 | 误差 < 1% |
| 章节过滤有效性 | 指定章节查询 | 仅返回目标章节内容 |
| 多型号隔离 | 跨型号查询 | 返回空结果 |
| 决策溯源完整性 | 查询决策 ID | 返回完整因果链和章节路径 |
| 流水线端到端 | 完整执行一次 | 输出完整产测方案 |

# 第 7 章 部署与运维

## 7.1 部署架构总览

### 7.1.1 部署拓扑

```mermaid
flowchart TB
    subgraph Client[客户端]
        CL1[Claude Desktop]
        CL2[Cursor]
        CL3[自定义 Agent]
    end

    subgraph Gateway[网关层]
        GW[API 网关<br/>Nginx / Traefik]
    end

    subgraph Service[服务层]
        SV1[LightRAG 服务]
        SV2[Semantica 服务]
        SV3[MCP Server]
    end

    subgraph Storage[存储层]
        ST1[PostgreSQL 17<br/>pgvector + AGE + pg_textsearch]
        ST2[Qdrant]
        ST3[blocks.jsonl<br/>章节元数据]
    end

    subgraph Model[模型层]
        LM1[vLLM<br/>Qwen3.8-27B-FP8]
        LM2[vLLM<br/>Qwen3-Embedding-4B]
    end

    Client -->|HTTPS| Gateway
    Gateway --> Service
    Service --> Storage
    Service --> Model

    style Storage fill:#e1f5ff
    style Model fill:#fce4ec
```

**独立服务数量**：5 个（PostgreSQL、Qdrant、vLLM-LLM、vLLM-Embed、MCP Server）+ 1 个 Python 进程（LightRAG）。相比“Qdrant + FalkorDB + MySQL”的三存储方案，运维面减少 33%。

### 7.1.2 部署模式选择

| 部署模式 | 适用场景 | 组件数 | 运维复杂度 |
|---|---|---|---|
| **单机 Docker Compose** | 开发验证、单团队使用 | 5 | 低 |
| **Docker Compose + GPU** | 生产环境、小规模 | 5 | 中 |
| **Kubernetes + Helm** | 多团队、高可用 | 8+ | 高 |

**推荐**：从单机 Docker Compose 起步，验证通过后根据并发量决定是否迁移到 Kubernetes。

## 7.2 组件部署配置

### 7.2.1 PostgreSQL 17 自定义镜像

PostgreSQL 需要同时加载三个扩展：pgvector、Apache AGE、pg_textsearch。三个扩展在同一数据库中无根本性冲突，但需要自定义 Docker 镜像来组合安装[reference:0]。

**Dockerfile**：

```dockerfile
FROM postgres:17

# 安装构建依赖
RUN apt-get update && apt-get install -y \
    build-essential postgresql-server-dev-17 \
    git wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 pgvector
RUN git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git \
    && cd pgvector && make && make install

# 安装 Apache AGE（v1.7.0 支持 PG17）
RUN git clone --branch release/PG17/1.7.0 https://github.com/apache/age.git \
    && cd age && make && make install

# 安装 pg_textsearch（BM25 全文检索）
RUN git clone --branch v1.3.1 https://github.com/timescale/pg_textsearch.git \
    && cd pg_textsearch && make && make install

# 安装中文分词器（zhparser）
RUN git clone https://github.com/amutu/zhparser.git \
    && cd zhparser && make && make install

# 配置 shared_preload_libraries
RUN echo "shared_preload_libraries = 'age'" >> /usr/share/postgresql/postgresql.conf.sample
```

**关键配置**：Apache AGE 必须通过 `shared_preload_libraries` 加载，不能动态加载[reference:1]。

**初始化脚本**（`docker/initdb/01-extensions.sql`）：

```sql
-- 创建扩展
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;

-- 加载 AGE
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- 创建图谱
SELECT create_graph('power_specs');

-- 创建 BM25 中文索引
CREATE INDEX IF NOT EXISTS idx_chunk_content_bm25
    ON chunks USING bm25 (content) WITH (text_config = 'zh_cn');
```

**中文 BM25 配置**：pg_textsearch 通过 `text_config` 索引选项支持多语言。中文配置需要 zhparser 扩展，通过 `public.zh_cn` 配置使用[reference:2]。

### 7.2.2 Qdrant 多租户配置

**Docker Compose 配置**：

```yaml
qdrant:
  image: qdrant/qdrant:latest
  ports:
    - "6333:6333"
    - "6334:6334"
  volumes:
    - ./qdrant_data:/qdrant/storage
  environment:
    - QDRANT__SERVICE__GRPC_PORT=6334
    - QDRANT__STORAGE__SNAPSHOT_PATH=/qdrant/snapshots
  deploy:
    resources:
      limits:
        memory: 4G
```

**多租户索引创建**：

```python
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams,
    PayloadSchemaType, KeywordIndexParams,
)

client = QdrantClient(url="http://localhost:6333")

# 创建 collection
client.create_collection(
    collection_name="lightrag_vectors",
    vectors_config=VectorParams(size=2560, distance=Distance.COSINE),
)

# 创建 tenant 索引（is_tenant=true 优化租户过滤性能）
client.create_payload_index(
    collection_name="lightrag_vectors",
    field_name="workspace_id",
    field_schema=KeywordIndexParams(
        type=PayloadSchemaType.KEYWORD,
        is_tenant=True,
    ),
)
```

**关键配置**：`is_tenant=true` 告诉 Qdrant 优化索引结构以支持频繁的租户过滤[reference:3]。不设置 `is_tenant=true` 会导致顺序读取性能下降[reference:4]。

### 7.2.3 vLLM 模型部署

**Qwen3.8-27B-FP8 部署**：

```bash
vllm serve Qwen/Qwen3.8-27B-FP8 \
  --tensor-parallel-size 1 \
  --max-model-len 262144 \
  --kv-cache-dtype fp8 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --gpu-memory-utilization 0.92 \
  --port 8000
```

**显存规划**：

| 量化精度 | 权重占用 | 推荐硬件 | 适用场景 |
|---|---|---|---|
| FP8 | ~26 GB | 1× L40S 48GB | **推荐**，留有上下文空间 |
| FP8 (TP2) | ~28 GB/卡 | 2× RTX 4090 | 需更大上下文或更高并发 |
| NVFP4 | ~24.6 GB | 1× RTX 5090 32GB | 成本最低 |

单张 48GB 显卡运行 FP8 量化时，权重约占 26GB，剩余显存用于 KV 缓存[reference:5]。两张 48GB 显卡通过张量并行（`--tensor-parallel-size 2`）可提供更大的上下文长度或更高并发余量[reference:6]。

**Qwen3-Embedding-4B 部署**：

```bash
vllm serve Qwen/Qwen3-Embedding-4B \
  --tensor-parallel-size 1 \
  --port 8001
```

**模型服务安全**：vLLM 默认不认证大部分端点，节点间通信默认不安全。必须将部署置于隔离网络中，并放在强制执行认证和 TLS 的反向代理或 API 网关后面[reference:7]。

### 7.2.4 Semantica MCP Server

**安装**：

```bash
pip install semantica
```

**MCP 客户端配置**（`claude_desktop_config.json` 或 `.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "semantica": {
      "command": "semantica-mcp",
      "env": {
        "SEMANTICA_KG_PATH": "/path/to/power_specs_graph.json",
        "SEMANTICA_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Semantica MCP Server 通过 stdio 通信，暴露 15 个 MCP 工具和 3 个可读资源[reference:8]。`SEMANTICA_KG_PATH` 环境变量指定知识图谱持久化文件路径，服务启动时自动加载[reference:9]。

**重要注意事项**：MCP Server 通过 stdio 通信，**不要向 stdout 添加日志**。任何 `print()` 或日志输出到 stdout 会破坏 JSON-RPC 消息流。所有日志写入 stderr[reference:10]。

## 7.3 Docker Compose 完整配置

```yaml
version: '3.8'

services:
  postgres:
    build: ./docker/postgres
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: powerspec
      POSTGRES_PASSWORD: ${PG_APP_PASSWORD}
      POSTGRES_DB: power_specs
    volumes:
      - ./pg_data:/var/lib/postgresql/data
      - ./docker/initdb:/docker-entrypoint-initdb.d
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U powerspec"]
      interval: 10s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 16G

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - ./qdrant_data:/qdrant/storage
      - ./qdrant_snapshots:/qdrant/snapshots
    deploy:
      resources:
        limits:
          memory: 4G

  vllm-llm:
    image: vllm/vllm-openai:latest
    ports:
      - "8000:8000"
    volumes:
      - ./models:/models
    command: >
      --model Qwen/Qwen3.8-27B-FP8
      --tensor-parallel-size 1
      --max-model-len 262144
      --kv-cache-dtype fp8
      --reasoning-parser qwen3
      --enable-auto-tool-choice
      --tool-call-parser qwen3_coder
      --gpu-memory-utilization 0.92
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  vllm-embed:
    image: vllm/vllm-openai:latest
    ports:
      - "8001:8001"
    volumes:
      - ./models:/models
    command: >
      --model Qwen/Qwen3-Embedding-4B
      --tensor-parallel-size 1
      --port 8001
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  mcp-server:
    build: .
    ports:
      - "8080:8080"
    environment:
      POSTGRES_DSN: postgresql://powerspec:<password>@postgres:5432/power_specs
      QDRANT_URL: http://qdrant:6333
      VLLM_LLM_BASE: http://vllm-llm:8000/v1
      VLLM_EMBED_BASE: http://vllm-embed:8001/v1
      RAG_STORAGE_ROOT: /app/rag_storage
    volumes:
      - ./rag_storage:/app/rag_storage
    depends_on:
      postgres:
        condition: service_healthy
      qdrant:
        condition: service_started
      vllm-llm:
        condition: service_started
      vllm-embed:
        condition: service_started
```

## 7.4 资源规划

### 7.4.1 硬件资源估算

| 组件 | CPU | 内存 | 存储 | GPU | 说明 |
|---|---|---|---|---|---|
| PostgreSQL | 4 vCPU | 16 GB | 50 GB SSD | — | 图+向量+关系+BM25 |
| Qdrant | 2 vCPU | 4 GB | 20 GB SSD | — | 向量索引 |
| vLLM-LLM | 8 vCPU | 32 GB | 60 GB SSD | 1× 48GB | Qwen3.8-27B-FP8 |
| vLLM-Embed | 4 vCPU | 16 GB | 20 GB SSD | 1× 16GB | Qwen3-Embedding-4B |
| MCP Server | 2 vCPU | 4 GB | 10 GB SSD | — | LightRAG + Semantica |
| **总计** | **20 vCPU** | **72 GB** | **160 GB** | **2 GPU** | 单机可部署 |

### 7.4.2 GPU 资源规划

**单机部署**：

- GPU 0：Qwen3.8-27B-FP8（约 26GB 权重 + KV 缓存）
- GPU 1：Qwen3-Embedding-4B（约 8GB 权重）

**MIG 分区方案**（可选，用于多模型共享 GPU）：

如果只有一张大显存 GPU（如 H100 80GB），可通过 NVIDIA MIG 分区将 GPU 划分为多个实例，分别运行 LLM 和 Embedding 模型：

```bash
# 启用 MIG 模式
nvidia-smi mig -cgi 1g.10gb,2g.20gb -C
```

NVIDIA GPU Operator 支持通过 `mig.strategy=single` 或 `mig.strategy=mixed` 自动配置 MIG[reference:11]。MIG 配置相比纯 vLLM 执行可提升总吞吐量约 700-950 tokens/s[reference:12]。

## 7.5 运维监控

### 7.5.1 监控架构

```mermaid
flowchart LR
    subgraph Exporters[指标暴露]
        E1[PostgreSQL Exporter]
        E2[Qdrant /metrics]
        E3[vLLM /metrics]
        E4[DCGM Exporter]
    end

    subgraph Prometheus[Prometheus]
        P1[指标采集]
        P2[告警规则]
    end

    subgraph Grafana[Grafana]
        G1[仪表板]
        G2[告警通知]
    end

    Exporters --> Prometheus
    Prometheus --> Grafana

    style Prometheus fill:#e1f5ff
    style Grafana fill:#e8f5e9
```

### 7.5.2 关键监控指标

**vLLM 指标**（通过 `/metrics` 端点暴露）[reference:13]：

| 指标 | 说明 | 告警阈值 |
|---|---|---|
| `vllm:num_requests_running` | 正在处理的请求数 | > 90% 容量 |
| `vllm:num_requests_waiting` | 等待中的请求数 | > 10 持续 5 分钟 |
| `vllm:gpu_cache_usage_perc` | KV 缓存使用率 | > 90% |
| `vllm:corrupted_requests` | 损坏的请求数 | > 0 |
| `vllm:avg_prompt_throughput_toks_per_s` | 平均提示吞吐量 | < 100 tokens/s |

**PostgreSQL 指标**：

| 指标 | 说明 | 告警阈值 |
|---|---|---|
| `pg_stat_activity_count` | 活动连接数 | > 80% max_connections |
| `pg_database_size_bytes` | 数据库大小 | > 80% 磁盘容量 |
| `pg_stat_user_tables_seq_scan` | 顺序扫描次数 | 异常增长 |

**Qdrant 指标**（通过 `/metrics` 端点暴露）[reference:14]：

| 指标 | 说明 | 告警阈值 |
|---|---|---|
| `qdrant_collections_total` | 集合数量 | — |
| `qdrant_rest_responses_total` | REST 响应数 | 错误率 > 1% |
| `qdrant_collection_vectors_count` | 向量数量 | — |

### 7.5.3 告警规则

```yaml
# prometheus/alerts.yml
groups:
  - name: vllm
    rules:
      - alert: VLLMHighWaitingRequests
        expr: vllm:num_requests_waiting > 10
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "vLLM 等待队列过长"

      - alert: VLLMGpuCacheHigh
        expr: vllm:gpu_cache_usage_perc > 0.9
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "KV 缓存使用率过高"

  - name: postgres
    rules:
      - alert: PostgresHighConnections
        expr: pg_stat_activity_count > 80
        for: 5m
        labels:
          severity: warning
```

## 7.6 备份与恢复

### 7.6.1 备份策略

| 组件 | 备份方式 | 频率 | 保留期 |
|---|---|---|---|
| PostgreSQL | `pg_dump` / `pg_basebackup` | 每日全量 + WAL 归档 | 30 天 |
| Qdrant | 快照 API | 每日 | 30 天 |
| blocks.jsonl | 文件系统快照 | 每次构建后 | 永久 |
| 知识图谱 | `SEMANTICA_KG_PATH` 文件备份 | 每次变更后 | 永久 |

**PostgreSQL 备份**：

```bash
# 全量备份
pg_dump -U powerspec -d power_specs -F c -f backup_$(date +%Y%m%d).dump

# 恢复
pg_restore -U powerspec -d power_specs -c backup_20260924.dump
```

pgvector 使用标准 PostgreSQL 备份机制，支持 `pg_dump`、`pg_restore`、基础备份和时间点恢复[reference:15]。

**Qdrant 快照**：

```bash
# 创建快照
curl -X POST http://localhost:6333/collections/lightrag_vectors/snapshots

# 恢复快照
curl -X PUT http://localhost:6333/collections/lightrag_vectors/snapshots/recover \
  -H 'Content-Type: application/json' \
  -d '{"location": "/qdrant/snapshots/snapshot_20260924"}'
```

Qdrant 快照生成每个集合的时间点文件，恢复时还原该快照[reference:16]。建议将快照同步到 S3 兼容存储以实现异地备份[reference:17]。

### 7.6.2 恢复流程

```mermaid
flowchart TB
    A[检测故障] --> B{故障类型}
    B -->|PostgreSQL| C[停止写入]
    B -->|Qdrant| D[停止查询]
    B -->|vLLM| E[切换模型副本]

    C --> F[从备份恢复]
    D --> G[从快照恢复]
    E --> H[重启服务]

    F --> I[验证数据完整性]
    G --> I
    H --> I
    I --> J[恢复服务]

    style A fill:#ffebee
    style J fill:#e8f5e9
```

## 7.7 安全加固

### 7.7.1 网络安全

| 措施 | 说明 |
|---|---|
| 网络隔离 | 所有服务置于内部 Docker 网络，不直接暴露公网 |
| TLS 终止 | API 网关终止 TLS，内部通信使用 HTTP |
| API 密钥 | vLLM 启用 `--api-key`，MCP Server 校验令牌 |
| IP 白名单 | 仅允许已知客户端 IP 访问 |

**vLLM API 密钥配置**：

```bash
vllm serve Qwen/Qwen3.8-27B-FP8 \
  --api-key "your-secret-key" \
  ...
```

vLLM 支持 `--api-key` 参数，服务会要求请求头中携带该密钥[reference:18]。

### 7.7.2 数据安全

| 措施 | 说明 |
|---|---|
| 数据库加密 | PostgreSQL 卷加密，Qdrant 快照加密 |
| 访问控制 | 每个 Agent 只能访问授权的 model_id |
| 审计日志 | 记录所有查询和决策，保留 90 天 |
| 脱敏 | 敏感参数在日志中脱敏 |

### 7.7.3 多型号隔离验证

```bash
# 验证隔离：用型号 A 的令牌查询型号 B 的数据
curl -H "Authorization: Bearer <PA601-token>" \
     -H "Content-Type: application/json" \
     -d '{"model_id": "PN1000-48A", "query": "test"}' \
     http://localhost:8080/query

# 预期结果：403 Forbidden
```

## 7.8 升级与回滚

### 7.8.1 LightRAG 升级流程

LightRAG 升级需要特别注意：在对同一存储、同一 workspace 启动新版本之前，必须先停掉所有旧 writer。滚动重启时只要还留着一个旧 worker 就是故障场景[reference:19]。

```mermaid
flowchart TB
    A[停止所有 LightRAG 实例] --> B[备份数据]
    B --> C[升级 LightRAG]
    C --> D[启动新版本]
    D --> E[验证功能]
    E -->|通过| F[完成升级]
    E -->|失败| G[回滚到旧版本]

    style A fill:#ffebee
    style F fill:#e8f5e9
    style G fill:#fff9c4
```

**升级步骤**：

```bash
# 1. 停止所有 LightRAG 实例
docker-compose stop mcp-server

# 2. 备份数据
pg_dump -U powerspec -d power_specs -F c -f pre_upgrade.dump

# 3. 升级 LightRAG
pip install --upgrade lightrag-hku

# 4. 启动新版本
docker-compose up -d mcp-server

# 5. 验证
curl http://localhost:8080/health
```

**回滚**：如果不安全的是事后又把旧版本启动回来。回滚时必须确保新版本的所有 worker 已完全停止[reference:20]。

### 7.8.2 vLLM 升级流程

vLLM 升级需要重启模型服务，建议在低峰期执行：

```bash
# 1. 停止 vLLM
docker-compose stop vllm-llm

# 2. 拉取新版本镜像
docker pull vllm/vllm-openai:latest

# 3. 启动
docker-compose up -d vllm-llm

# 4. 验证模型加载
curl http://localhost:8000/v1/models
```

## 7.9 故障排查

### 7.9.1 vLLM 常见故障

| 故障现象 | 可能原因 | 排查方法 | 解决方案 |
|---|---|---|---|
| 请求响应延迟显著上升 | KV 缓存不足，抢占驱逐 | 检查 `vllm:gpu_cache_usage_perc` | 降低 `--max-model-len` 或增加 GPU |
| 请求被丢弃 | 调度器满载 | 检查日志 `Scheduler is full, dropping request` | 增加 `--max-num-seqs` 或扩展实例 |
| 模型加载卡住 | 权重文件损坏或磁盘慢 | `docker logs -f vllm-llm` | 检查权重文件完整性 |
| 日志无 "Received request" | 服务未处理请求 | `export VLLM_LOGGING_LEVEL=DEBUG` | 检查 API 网关路由 |

**日志查看**：

```bash
# 查看 vLLM 日志
docker logs -f vllm-llm

# 启用 DEBUG 日志
export VLLM_LOGGING_LEVEL=DEBUG
```

vLLM 默认输出 INFO 级别日志，包含模型加载、内存分配、推理等信息。通过 `VLLM_LOGGING_LEVEL=DEBUG` 环境变量可启用更详细日志[reference:21]。

### 7.9.2 PostgreSQL 常见故障

| 故障现象 | 可能原因 | 排查方法 |
|---|---|---|
| 连接数过多 | 连接池配置不当 | `SELECT count(*) FROM pg_stat_activity;` |
| 查询慢 | 索引缺失 | `EXPLAIN ANALYZE <query>;` |
| AGE 图查询报错 | search_path 未设置 | `SET search_path = ag_catalog, "$user", public;` |

### 7.9.3 Qdrant 常见故障

| 故障现象 | 可能原因 | 排查方法 |
|---|---|---|
| 向量搜索慢 | 未建 tenant 索引 | 检查 `is_tenant=true` 是否设置 |
| 内存不足 | 向量数据超过内存 | 检查 Qdrant 日志 OOM 错误 |
| 快照失败 | 磁盘空间不足 | `df -h /qdrant/snapshots` |

## 7.10 运维检查清单

### 7.10.1 每日检查

| 检查项 | 命令 | 预期结果 |
|---|---|---|
| 服务健康 | `curl localhost:8080/health` | 200 OK |
| vLLM 健康 | `curl localhost:8000/health` | 200 OK |
| 磁盘空间 | `df -h` | 使用率 < 80% |
| 错误日志 | `docker logs --tail 100 mcp-server` | 无 ERROR |

### 7.10.2 每周检查

| 检查项 | 命令 | 预期结果 |
|---|---|---|
| 数据库大小 | `SELECT pg_database_size('power_specs');` | 增长 < 10% |
| Qdrant 向量数 | `curl localhost:6333/collections/lightrag_vectors` | 与文档数一致 |
| 备份完整性 | 验证最新备份可恢复 | 恢复测试通过 |
| 性能指标 | Grafana 仪表板 | 延迟 P95 < 500ms |

### 7.10.3 每月检查

| 检查项 | 说明 |
|---|---|
| 安全更新 | 检查组件是否有安全补丁 |
| 容量规划 | 根据增长趋势评估是否需要扩容 |
| 成本审查 | GPU 利用率、存储成本 |
| 灾备演练 | 模拟故障恢复流程 |

## 7.11 部署清单

| 步骤 | 动作 | 验证方式 |
|---|---|---|
| 1 | 构建 PostgreSQL 镜像 | `docker build -t powerspec/postgres:17 ./docker/postgres` |
| 2 | 启动基础设施 | `docker-compose up -d postgres qdrant` |
| 3 | 初始化扩展 | `docker exec postgres psql -c "CREATE EXTENSION age;"` |
| 4 | 启动 vLLM 服务 | `docker-compose up -d vllm-llm vllm-embed` |
| 5 | 验证模型加载 | `curl localhost:8000/v1/models` |
| 6 | 初始化 Qdrant 索引 | `python -c "from src.rag.qdrant_tenant import setup_qdrant_multitenancy; ..."` |
| 7 | 启动 MCP Server | `docker-compose up -d mcp-server` |
| 8 | 端到端验证 | `curl localhost:8080/health && curl localhost:8000/health` |

# 第 8 章 实施路线图

## 8.1 实施总览

本方案的实施分为六个阶段，从基础设施搭建到端到端验证，预计总周期为 8~10 周。每个阶段都有明确的交付物和验收标准，确保风险可控、进度可追踪。

```mermaid
flowchart LR
    P1[Phase 1<br/>基础设施] --> P2[Phase 2<br/>知识构建]
    P2 --> P3[Phase 3<br/>规则注入]
    P3 --> P4[Phase 4<br/>Agent 开发]
    P4 --> P5[Phase 5<br/>MCP 集成]
    P5 --> P6[Phase 6<br/>端到端验证]

    style P1 fill:#e3f2fd
    style P2 fill:#e1f5ff
    style P3 fill:#fff9c4
    style P4 fill:#fff4e1
    style P5 fill:#fce4ec
    style P6 fill:#e8f5e9
```

### 8.1.1 阶段总览

| 阶段 | 名称 | 周期 | 核心目标 | 关键交付物 |
|---|---|---|---|---|
| Phase 1 | 基础设施 | 1 周 | 搭建存储、模型、推理服务 | 可运行的基础环境 |
| Phase 2 | 知识构建 | 1.5 周 | 定义本体模板，解析规格书 | 结构化知识图谱 |
| Phase 2.5 | 章节关系验证 | 0.5 周 | 验证章节保留与过滤 | 章节元数据 + 过滤测试报告 |
| Phase 3 | 规则注入 | 1 周 | 编写 Datalog 和 SHACL 规则 | 领域规则库 |
| Phase 4 | Agent 开发 | 2 周 | 开发四个专业 Agent | 可运行的 Agent |
| Phase 5 | MCP 集成 | 1 周 | 封装 MCP Server | Agent 可通过 MCP 调用 |
| Phase 6 | 端到端验证 | 2 周 | 完整验证并调优 | 可交付的产测方案 |

## 8.2 Phase 1：基础设施（第 1 周）

### 8.2.1 目标

搭建完整的运行环境，包括 PostgreSQL、Qdrant、vLLM 模型服务、LightRAG 和 Semantica。

### 8.2.2 任务分解

```mermaid
gantt
    title Phase 1 基础设施搭建
    dateFormat YYYY-MM-DD
    section 存储层
    PostgreSQL 镜像构建           :a1, 2025-01-01, 1d
    扩展初始化                    :a2, after a1, 0.5d
    Qdrant 部署                   :a3, after a1, 0.5d
    section 模型层
    vLLM 部署 Qwen3.8-27B         :b1, after a2, 1d
    vLLM 部署 Qwen3-Embedding-4B  :b2, after b1, 0.5d
    section 服务层
    LightRAG 部署                 :c1, after b2, 1d
    Semantica 部署                :c2, after c1, 0.5d
    section 验证
    端到端连通性测试              :d1, after c2, 1d
```

### 8.2.3 关键任务

| 任务 | 说明 | 负责人 | 验收标准 |
|---|---|---|---|
| PostgreSQL 镜像构建 | 组合 pgvector + AGE + pg_textsearch + zhparser | 运维 | 扩展全部加载成功 |
| Qdrant 部署 | 配置 tenant 索引 | 运维 | `is_tenant=true` 生效 |
| vLLM 部署 | Qwen3.8-27B-FP8 + Qwen3-Embedding-4B | 运维 | `/v1/models` 返回模型列表 |
| LightRAG 部署 | 配置 PostgreSQL 后端 + workspace | 开发 | 可插入文档并查询 |
| Semantica 部署 | 配置 DatalogReasoner | 开发 | 可执行简单推理 |

### 8.2.4 验收标准

```bash
# 1. PostgreSQL 扩展验证
docker exec postgres psql -U powerspec -d power_specs -c "\dx"
# 预期：vector, age, pg_textsearch 全部存在

# 2. Qdrant 健康检查
curl http://localhost:6333/healthz
# 预期：200 OK

# 3. vLLM 模型列表
curl http://localhost:8000/v1/models
# 预期：返回 Qwen3.8-27B-FP8

# 4. vLLM Embedding 模型
curl http://localhost:8001/v1/models
# 预期：返回 Qwen3-Embedding-4B

# 5. LightRAG 健康检查
curl http://localhost:8080/health
# 预期：200 OK
```

### 8.2.5 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| GPU 显存不足 | 模型无法加载 | 使用 FP8 量化或 MIG 分区 |
| 扩展编译失败 | PostgreSQL 无法启动 | 使用预编译镜像或调整版本 |
| Qdrant 索引未生效 | 隔离失效 | 验证 `is_tenant=true` 配置 |

## 8.3 Phase 2：知识构建（第 2~3 周）

### 8.3.1 目标

定义本体模板，上传 PA601-D54A 规格书，验证抽取质量，生成结构化知识图谱。

### 8.3.2 任务分解

```mermaid
gantt
    title Phase 2 知识构建
    dateFormat YYYY-MM-DD
    section 本体定义
    最小本体模板定义              :a1, 2025-01-08, 1d
    本体对齐规则设计              :a2, after a1, 1d
    section 文档解析
    规格书上传与解析              :b1, after a2, 1d
    章节层级验证                  :b2, after b1, 0.5d
    section 抽取验证
    实体抽取验证                  :c1, after b2, 1d
    关系抽取验证                  :c2, after c1, 1d
    section 图谱构建
    知识图谱构建                  :d1, after c2, 1d
    抽取质量评估                  :d2, after d1, 1d
```

### 8.3.3 关键任务

| 任务 | 说明 | 验收标准 |
|---|---|---|
| 最小本体模板定义 | 5 个核心类 + 5 条核心关系 | 模板可复用 |
| 规格书上传与解析 | LangParse + LightRAG Native Parser | 章节层级与原文一致 |
| 实体抽取验证 | 需求项、参数、接口、信号 | 准确率 > 90% |
| 关系抽取验证 | hasParameter、belongsToModel | 抽样验证通过 |
| 知识图谱构建 | 图三元组写入 PostgreSQL | 可 SPARQL 查询 |

### 8.3.4 最小本体模板

```python
ONTOLOGY_TEMPLATE = {
    "classes": {
        "Product":        {"key_attr": "model_id"},
        "Requirement":    {"key_attr": "req_id"},
        "Parameter":      {"key_attr": "param_name"},
        "Interface":      {"key_attr": "connector_id"},
        "Signal":         {"key_attr": "signal_name"},
    },
    "relations": [
        ("Requirement", "hasParameter",   "Parameter"),
        ("Requirement", "belongsToModel", "Product"),
        ("Interface",   "hasSignal",       "Signal"),
        ("Product",     "hasInterface",    "Interface"),
        ("Product",     "hasRequirement",  "Requirement"),
    ],
}
```

### 8.3.5 验收标准

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 章节层级正确 | 检查 `blocks.jsonl` 中的 `parent_headings` | 与原文目录一致 |
| 需求项完整 | 统计 SR 编号数量 | 与规格书一致 |
| 参数抽取准确 | 抽样 20 条人工核对 | 准确率 > 90% |
| 关系建立正确 | 抽样 20 条人工核对 | 准确率 > 85% |
| 图谱可查询 | SPARQL 查询测试 | 返回正确结果 |

## 8.4 Phase 2.5：章节关系验证（第 3 周）

### 8.4.1 目标

验证章节关系保留机制和章节级查询过滤的正确性。

### 8.4.2 任务分解

| 任务 | 说明 | 验收标准 |
|---|---|---|
| 章节元数据验证 | 检查 `blocks.jsonl` 中的章节字段 | 字段完整 |
| MetadataFilter 配置 | 配置 PostgreSQL 后端的元数据过滤 | 查询可执行 |
| 章节过滤测试 | 按 `section_path` 过滤查询 | 仅返回目标章节 |
| 复合过滤测试 | 章节 + 优先级 + 类别组合 | 过滤准确 |
| 降级策略验证 | 非 PostgreSQL 后端的降级 | 可正常降级 |

### 8.4.3 章节过滤测试用例

```python
# 测试1：单章节过滤
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="AND",
        operands=[{"section_path": "4.3.3"}]
    )
)
# 预期：仅返回 4.3.3 保护功能章节的内容

# 测试2：多章节过滤
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="OR",
        operands=[
            {"section_path": "4.3.2"},
            {"section_path": "4.3.3"},
        ]
    )
)
# 预期：返回 4.3.2 和 4.3.3 章节的内容

# 测试3：复合过滤
query_param = QueryParam(
    mode="mix",
    metadata_filter=MetadataFilter(
        operator="AND",
        operands=[
            {"section_path": "4.3.3"},
            {"priority": "强制"},
        ]
    )
)
# 预期：仅返回 4.3.3 章节中优先级为强制的内容
```

### 8.4.4 验收标准

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 章节元数据完整 | 检查 `blocks.jsonl` | 每条 chunk 有 `section_path` |
| 单章节过滤 | 指定 `section_path` 查询 | 仅返回目标章节 |
| 多章节过滤 | OR 组合查询 | 返回所有指定章节 |
| 复合过滤 | AND 组合查询 | 过滤准确 |
| 隔离验证 | 跨型号查询 | 返回空结果 |

## 8.5 Phase 3：规则注入（第 4 周）

### 8.5.1 目标

编写 Datalog 公式规则和 SHACL 约束规则，注入到 Semantica 推理层。

### 8.5.2 任务分解

```mermaid
gantt
    title Phase 3 规则注入
    dateFormat YYYY-MM-DD
    section Datalog 规则
    基础电学公式                :a1, 2025-01-22, 1d
    工装精度公式                :a2, after a1, 1d
    探针选型规则                :a3, after a2, 1d
    section SHACL 约束
    空间约束                    :b1, after a3, 0.5d
    电气约束                    :b2, after b1, 0.5d
    逻辑约束                    :b3, after b2, 0.5d
    section 验证
    规则测试                    :c1, after b3, 1d
```

### 8.5.3 关键任务

| 任务 | 说明 | 验收标准 |
|---|---|---|
| Datalog 规则编写 | 6 类公式规则 | 规则可执行 |
| SHACL 约束编写 | 5 类约束 | 违规可识别 |
| 规则测试 | 用已知输入验证输出 | 误差 < 1% |
| 约束测试 | 构造违规数据验证 | 违规被正确识别 |

### 8.5.4 验收标准

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 功率计算 | `Power = Voltage × Current` | 误差 < 0.1% |
| 公差链计算 | 均方根法 | 与人工计算一致 |
| 探针选型 | 按电流/频率推荐 | 推荐结果正确 |
| 探针间距约束 | 构造违规探针 | 违规被识别 |
| 保护回差约束 | 构造违规保护 | 违规被识别 |

## 8.6 Phase 4：Agent 开发（第 5~6 周）

### 8.6.1 目标

开发四个专业 Agent，实现各自的工作流。

### 8.6.2 任务分解

```mermaid
gantt
    title Phase 4 Agent 开发
    dateFormat YYYY-MM-DD
    section 需求Agent
    工作流开发                  :a1, 2025-01-29, 1d
    Prompt 调优                 :a2, after a1, 1d
    section 用例Agent
    用例模板开发                :b1, after a1, 1d
    边界值补充逻辑              :b2, after b1, 1d
    section 参数Agent
    Datalog 集成                :c1, after b1, 1d
    SHACL 集成                  :c2, after c1, 1d
    决策溯源集成                :c3, after c2, 1d
    section 代码Agent
    代码生成模板                :d1, after c2, 1d
    语法验证                    :d2, after d1, 1d
```

### 8.6.3 关键任务

| 任务 | 说明 | 验收标准 |
|---|---|---|
| 需求 Agent | 按章节提取需求 | 覆盖率 > 95% |
| 用例 Agent | 生成测试用例 | 强制需求 100% 覆盖 |
| 参数 Agent | 计算工装参数 | 计算正确 |
| 代码 Agent | 生成产测代码 | 语法正确 |
| 决策溯源 | 记录每条决策 | 可追溯 |

### 8.6.4 验收标准

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 需求抽取完整性 | 与 SR 编号对比 | 覆盖率 > 95% |
| 用例覆盖率 | SHACL 检查 | 强制需求 100% |
| 参数计算正确性 | 与人工计算对比 | 误差 < 1% |
| 代码语法正确性 | 编译检查 | 无语法错误 |
| 决策溯源完整性 | 查询决策 ID | 返回完整因果链 |

## 8.7 Phase 5：MCP 集成（第 7 周）

### 8.7.1 目标

将 RAG 服务和 Agent 能力封装为 MCP Server，供外部 Agent 调用。

### 8.7.2 任务分解

| 任务 | 说明 | 验收标准 |
|---|---|---|
| MCP 工具定义 | 8 个工具 | Schema 正确 |
| MCP Server 实现 | FastAPI + MCP 协议 | 可启动 |
| 权限校验 | API 网关 + 令牌校验 | 越权被拒绝 |
| 客户端配置 | Claude/Cursor 配置 | 可连接 |
| 端到端测试 | Agent 调用 MCP | 返回正确结果 |

### 8.7.3 MCP 工具清单

| 工具名 | 功能 | 章节过滤 |
|---|---|---|
| `search_requirements` | 检索需求项 | ✅ |
| `query_parameters` | 查询参数 | ✅ |
| `calculate` | Datalog 计算 | — |
| `validate_constraints` | SHACL 验证 | — |
| `get_test_cases` | 获取测试用例 | ✅ |
| `get_fixture_spec` | 获取工装规格 | ✅ |
| `search_cases` | 检索历史案例 | ✅ |
| `optimize_process` | 工艺优化建议 | — |

### 8.7.4 验收标准

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| MCP 工具可用 | 逐个调用工具 | 返回正确结果 |
| 权限校验 | 越权调用 | 返回 403 |
| 多型号隔离 | 跨型号查询 | 返回空结果 |
| 章节过滤 | 指定章节查询 | 仅返回目标章节 |
| 客户端连接 | Claude/Cursor 连接 | 工具可调用 |

## 8.8 Phase 6：端到端验证（第 8~10 周）

### 8.8.1 目标

用 PA601-D54A 的完整数据验证端到端流程，优化性能，准备交付。

### 8.8.2 任务分解

```mermaid
gantt
    title Phase 6 端到端验证
    dateFormat YYYY-MM-DD
    section 功能验证
    完整流水线执行              :a1, 2025-02-19, 2d
    结果人工审核                :a2, after a1, 2d
    section 性能优化
    检索性能调优                :b1, after a2, 2d
    推理性能调优                :b2, after b1, 2d
    section 稳定性验证
    压力测试                    :c1, after b2, 2d
    故障恢复测试                :c2, after c1, 1d
    section 交付
    文档整理                    :d1, after c2, 1d
    培训                        :d2, after d1, 1d
```

### 8.8.3 关键任务

| 任务 | 说明 | 验收标准 |
|---|---|---|
| 完整流水线执行 | 从规格书到产测方案 | 全链路可复现 |
| 结果人工审核 | 专家审核输出 | 准确率 > 90% |
| 性能调优 | 检索和推理优化 | P95 延迟 < 500ms |
| 压力测试 | 并发查询 | 无故障 |
| 故障恢复 | 模拟组件故障 | 可恢复 |

### 8.8.4 验收标准

| 验证项 | 验证方法 | 通过标准 |
|---|---|---|
| 端到端流程 | 完整执行一次 | 输出完整产测方案 |
| 结果准确性 | 专家审核 | 准确率 > 90% |
| 性能 | 并发测试 | P95 < 500ms |
| 稳定性 | 24 小时运行 | 无故障 |
| 隔离性 | 多型号并发 | 无数据泄漏 |
| 可追溯性 | 查询决策 | 返回完整溯源链 |

## 8.9 关键里程碑

```mermaid
timeline
    title 实施里程碑
    section Week 1
        基础设施就绪 : 存储、模型、推理服务可运行
    section Week 2-3
        知识图谱就绪 : 规格书解析完成，抽取验证通过
    section Week 3
        章节过滤就绪 : 章节保留和过滤验证通过
    section Week 4
        规则库就绪 : Datalog 和 SHACL 规则可执行
    section Week 5-6
        Agent 就绪 : 四个 Agent 可独立运行
    section Week 7
        MCP 就绪 : Agent 可通过 MCP 调用
    section Week 8-10
        端到端验证 : 完整产测方案可交付
```

## 8.10 团队配置

### 8.10.1 角色与职责

| 角色 | 人数 | 职责 |
|---|---|---|
| 项目经理 | 1 | 进度管理、风险管理、协调 |
| 架构师 | 1 | 技术选型、架构设计、评审 |
| 后端开发 | 2 | LightRAG、Semantica、MCP Server |
| Agent 开发 | 2 | 四个 Agent 的 Prompt 和工作流 |
| 运维工程师 | 1 | 部署、监控、备份 |
| 领域专家 | 1 | 规则编写、结果审核 |

### 8.10.2 技能矩阵

| 角色 | 必备技能 | 加分技能 |
|---|---|---|
| 架构师 | RAG、知识图谱、Datalog | 产测领域知识 |
| 后端开发 | Python、PostgreSQL、FastAPI | LightRAG、Semantica |
| Agent 开发 | Prompt Engineering、MCP | CrewAI、LangChain |
| 运维工程师 | Docker、Kubernetes、Prometheus | GPU 运维 |
| 领域专家 | 产测工装设计、测试规范 | Datalog、SHACL |

## 8.11 风险管理

### 8.11.1 风险登记表

| 风险 | 概率 | 影响 | 应对策略 |
|---|---|---|---|
| GPU 资源不足 | 中 | 高 | 提前申请资源，准备 FP8/NVFP4 量化方案 |
| 抽取质量不达标 | 中 | 高 | 增加人工审核，迭代 Prompt |
| 章节过滤失效 | 低 | 中 | 验证 PostgreSQL 后端配置 |
| 多型号隔离漏洞 | 低 | 高 | 三层隔离验证，安全审计 |
| Agent 输出不稳定 | 中 | 中 | 增加约束解码，规则校验 |
| 性能不达标 | 中 | 中 | 索引优化，缓存策略 |
| 团队技能不足 | 中 | 中 | 培训，引入外部专家 |

### 8.11.2 风险应对流程

```mermaid
flowchart TB
    A[风险识别] --> B[风险评估]
    B --> C{概率 × 影响}
    C -->|高| D[立即应对]
    C -->|中| E[制定计划]
    C -->|低| F[监控]
    D --> G[执行应对]
    E --> G
    F --> G
    G --> H[验证效果]
    H -->|有效| I[关闭]
    H -->|无效| B

    style D fill:#ffebee
    style I fill:#e8f5e9
```

## 8.12 成功标准

### 8.12.1 技术成功标准

| 指标 | 目标值 | 测量方法 |
|---|---|---|
| 需求抽取覆盖率 | > 95% | 与 SR 编号对比 |
| 用例覆盖率 | 100%（强制需求） | SHACL 检查 |
| 参数计算准确率 | > 99% | 与人工计算对比 |
| 代码语法正确率 | 100% | 编译检查 |
| 章节过滤准确率 | 100% | 指定章节查询 |
| 多型号隔离 | 零泄漏 | 跨型号查询测试 |
| 决策可追溯性 | 100% | 查询决策 ID |
| P95 延迟 | < 500ms | 性能测试 |

### 8.12.2 业务成功标准

| 指标 | 目标值 | 说明 |
|---|---|---|
| 产测方案生成时间 | 从 2 周缩短至 2 天 | 相比人工设计 |
| 工装参数计算准确率 | 与专家一致 | 专家审核 |
| 工艺优化建议采纳率 | > 70% | 实际应用统计 |
| 用户满意度 | > 4.0/5.0 | 用户反馈 |

## 8.13 后续演进方向

```mermaid
flowchart LR
    subgraph Current[当前版本]
        C1[四 Agent 协同]
        C2[章节感知检索]
        C3[Datalog 公式推理]
    end

    subgraph Next[下一版本]
        N1[多产品线支持]
        N2[工艺闭环反馈]
        N3[代码自动执行]
    end

    subgraph Future[远期]
        F1[自主工装设计]
        F2[跨产品知识迁移]
        F3[产测数字孪生]
    end

    Current --> Next
    Next --> Future

    style Current fill:#e1f5ff
    style Next fill:#fff4e1
    style Future fill:#e8f5e9
```

**短期演进（3~6 个月）**：

- 支持更多产品型号，验证多型号隔离的稳定性
- 接入生产现场数据，实现工艺优化闭环
- 增加代码自动执行能力，验证生成的产测代码

**中期演进（6~12 个月）**：

- 扩展到更多产品线（如 RRU、AAU 等）
- 引入知识迁移机制，新型号复用已有工装知识
- 增加工艺参数的时序预测能力

**长期演进（12 个月以上）**：

- 自主工装设计：从规格书直接生成工装设计方案
- 产测数字孪生：在虚拟环境中验证产测方案
- 跨产品知识迁移：将一个产品的工装知识迁移到类似产品

## 8.14 实施检查清单

### 8.14.1 Phase 1 检查清单

- [ ] PostgreSQL 镜像构建成功
- [ ] pgvector、AGE、pg_textsearch、zhparser 扩展加载成功
- [ ] Qdrant 部署成功，tenant 索引创建成功
- [ ] vLLM 部署成功，Qwen3.8-27B-FP8 可加载
- [ ] vLLM 部署成功，Qwen3-Embedding-4B 可加载
- [ ] LightRAG 部署成功，可插入文档
- [ ] Semantica 部署成功，可执行简单推理

### 8.14.2 Phase 2 检查清单

- [ ] 最小本体模板定义完成
- [ ] 规格书上传成功
- [ ] 章节层级与原文一致
- [ ] 需求项抽取完整
- [ ] 参数抽取准确率 > 90%
- [ ] 关系抽取准确率 > 85%
- [ ] 知识图谱可 SPARQL 查询

### 8.14.3 Phase 2.5 检查清单

- [ ] `blocks.jsonl` 中章节字段完整
- [ ] MetadataFilter 配置成功
- [ ] 单章节过滤正确
- [ ] 多章节过滤正确
- [ ] 复合过滤正确
- [ ] 降级策略可用

### 8.14.4 Phase 3 检查清单

- [ ] Datalog 公式规则编写完成
- [ ] SHACL 约束规则编写完成
- [ ] 功率计算正确
- [ ] 公差链计算正确
- [ ] 探针选型正确
- [ ] 约束违规可识别

### 8.14.5 Phase 4 检查清单

- [ ] 需求 Agent 可运行
- [ ] 用例 Agent 可运行
- [ ] 参数 Agent 可运行
- [ ] 代码 Agent 可运行
- [ ] 决策溯源可查询

### 8.14.6 Phase 5 检查清单

- [ ] 8 个 MCP 工具定义完成
- [ ] MCP Server 可启动
- [ ] 权限校验生效
- [ ] 多型号隔离验证通过
- [ ] 章节过滤验证通过
- [ ] Claude/Cursor 可连接

### 8.14.7 Phase 6 检查清单

- [ ] 端到端流程完整执行
- [ ] 结果专家审核通过
- [ ] P95 延迟 < 500ms
- [ ] 24 小时稳定性测试通过
- [ ] 多型号并发无泄漏
- [ ] 决策溯源完整
- [ ] 文档整理完成
- [ ] 团队培训完成