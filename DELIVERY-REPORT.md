# ATERag 交付报告 (2026-09-29)

## 交付物清单

| 交付物 | 位置 | 状态 |
|---|---|---|
| RAG 服务源码 (Python 3.13 / uv) | `src/aterag/` | ✅ |
| MCP Server (streamable-http, 13 工具) | `src/aterag/mcp_server/server.py` | ✅ 板卡常驻 :8080 (systemd `aterag-mcp`) |
| 领域规则库 (power 116 + common 1 = 117 规则 / 78 SHACL) | `domain_rules/{common,power}/` | ✅ |
| 规则自验器 (逐条执行 test 块) | `scripts/rules_selftest.py` | ✅ 115/115 |
| Semantica 语义图双写 | `scripts/sync_semantica.py` + `verify_semantica.py` | ✅ 378 节点/460 边 |
| 板卡原生部署包 | `deploy/native/` (01~05 脚本 + README) | ✅ 已执行 |
| Qdrant 初始化脚本 | `deploy/qdrant/init_tenant.py` | ✅ 已执行 |
| 全链路部署编排 | `scripts/deploy_board.py` | ✅ 6/6 |
| 全链路验证编排 | `scripts/verify_all.py` | ✅ **17/17** |
| 板卡存储栈预检 | `scripts/board_preflight.py` | ✅ 11/11 |
| 板卡 LightRAG 落库完整性 | `scripts/verify_lightrag_deployed.py` | ✅ 19/19 |
| MCP Server 板卡常驻 (systemd) | `deploy/native/aterag-mcp.service` + `06-install-aterag.sh` | ✅ 8/8 |
| 应用层部署编排 (源码+venv+systemd) | `scripts/deploy_mcp_board.py` | ✅ PASS |
| 板卡 SSH 运维工具 | `scripts/board_ssh.py` / `board_ssh_probe.py` | ✅ |
| 新规格书上传与导入 | `scripts/upload_new_spec.py` + `docs/规格书上传与处理.md` | ✅ PN2000-24A 实测 |
| 新规格书导入校验 | `scripts/verify_new_spec.py` / `verify_mcp_new_model.py` | ✅ 7/7 + 4/4 |
| 脏 workspace 清理 | `scripts/board_clean_lrag.py` | ✅ 已执行 |
| 测试夹具 (第二型号) | `tests/fixtures/PN1000-48A 迷你规格书.md` | ✅ 已导入 |

## 存储栈 (192.168.5.24, 原生安装)

- PostgreSQL 17.11 + pgvector + Apache AGE 1.7.0 + pg_textsearch 1.4.0 + zhparser/SCWS
- Qdrant v1.19.1 (aarch64-musl 静态二进制 + systemd)
- 关键事实: 板卡内核高度裁剪 (无 BPF/bridge/veth/mqueue), **容器方案不可行**, 已全部原生部署
- 中文 BM25 索引 `idx_aterag_chunks_bm25` (text_config=public.chinese) 生效

## 模型接入 (供应商可换)

- LLM: qwen3.7-flash-2026-07-15 (OpenAI 兼容协议) — chat 验证通过
- Embedding: qwen3.7-text-embedding (DashScope 原生协议, **实测 1024 维**)
- 适配层支持协议自动识别 + 维度探针实测 + 分批重试; 换供应商零代码改动

## 知识隔离架构 (实测验证)

| 层 | workspace | 验证结果 |
|---|---|---|
| 型号 | `PA601-D54A` / `PN1000-48A` | 互相不可见 (PA601 查不到 PN1000 的 20.8A) ✅ |
| 类型域 | `_domain_power` | 两型号共享, 规则可检索 ✅ |
| 普适 | `_common` | 单位换算可达 ✅ |
| 跨域/未注册 | — | fail-closed 拒绝 ✅ |

## 验证结果汇总

一键编排: `python scripts/verify_all.py` → **17/17 PASS** (47s)

| 套件 | 结果 |
|---|---|
| 纯本地单测 (注册表/分类/解析/抽取) | 11/11 |
| 板卡存储栈预检 (PG 扩展/Qdrant/AGE graph/落库量) | **11/11** |
| 板卡 LightRAG 落库完整性 (表/状态/归一化/实体关系/向量层/图谱) | **19/19** |
| 板卡 MCP systemd 常驻 (active/enabled/账号/无崩溃) | **8/8** |
| 新规格书导入校验 (PN2000-24A 全链路) | **7/7** |
| 新型号 MCP 服务态可查 (自动识别/判据 22~30A) | **4/4** |
| 规则自验 (derive 数值 + SHACL 违规双判) | **115/115** |
| 推理引擎 (多步推导 + 溯源 + SHACL 正反例) | 全绿 |
| 端点连通 (PG/Qdrant/LLM/Embedding) | 4/4 |
| 三层装配隔离 | 全绿 |
| 检索链路 (BM25 中文/向量/RRF 融合) | 全绿 |
| 图谱导航 mix 引用解析 (脚本态) | **5/5** |
| 图导航 MCP 服务态 (确认服务端已跑修复代码) | **5/5** |
| 实体抽取 (保护点/需求/信号) | **9/9** |
| 工具深验 (双轨保护点/判据生成/型号事实接线) | 全绿 |
| PA601 全量验证 (章节过滤/术语/公式/SHACL/隔离/自动识别) | **18/18** |
| PN1000 隔离验证 (型号识别/参数不冲突) | **6/6** |
| MCP e2e (13 工具 + 自动识别 + 隔离 + 健康) | **16/16** |
| Semantica 语义图回读 (节点数/公式边/约束边/溯源) | **5/5** |
| 新增规则 MCP 端到端 (calculate 10 项) | **10/10** |

## 全链路部署 (192.168.5.24)

一键编排: `python scripts/deploy_board.py` → **6/6 PASS**

| 步骤 | 结果 |
|---|---|
| 板卡存储栈预检 | 11/11 (PG 17.11 / vector 0.8.6 / age 1.7.0 / pg_textsearch 1.4.0 / zhparser 2.4 / Qdrant green) |
| Qdrant 租户索引初始化 | collection + workspace_id 租户索引就绪 |
| PA601-D54A 规格书导入 | 47 块 / 187 实体 / 106 分块 |
| PN1000-48A 规格书导入 | 4 块 / 5 实体 / 4 分块 |
| Semantica 语义图同步 | 378 节点 / 460 边 (幂等) |
| 领域知识库 -> LightRAG | `_domain_power` 115 块 / `_common` 1 块 |

落库实况: `aterag_entities` = {PA601-D54A: 187, PN1000-48A: 5}; `aterag_chunks` = {_domain_power: 115, PA601-D54A: 106, PN1000-48A: 4, _common: 1}; AGE graph 3 个 (pa601_d54a / pn1000_48a / _domain_power)。

## LightRAG 落库实况与脏数据清理

LightRAG 11 张表齐备, 5 个文档全部 `processed`, 合并后实体/关系:

| workspace | 实体 | 关系 | 分块 | 实体向量 | 关系向量 | AGE 图谱 |
|---|---|---|---|---|---|---|
| `pa601_d54a` | 436 | 393 | 23 | 610 | 393 | ✅ |
| `_domain_power` | 168 (30+38+100) | 92 (15+19+58) | 9 | 128 | 81 | ✅ |
| `pn1000_48a` | 5 | 4 | 1 | 9 | 4 | ✅ |

清理了一个**失败的脏 workspace `PA601-D54A`** (未归一化的大写标识符): 该次入库 `status=failed`,
错误为 `Error executing graph query`, 但 KV 与向量层已落库, 残留 23 分块 / 187 实体向量 /
480 实体 / 46 条 LLM 缓存 / 独立 AGE graph `PA601_D54A_chunk_entity_relation`。
已用 `scripts/board_clean_lrag.py --apply` 全量清除 (含 `DROP GRAPH`), 并加双保险:

1. `build_lightrag()` 增加守卫: workspace 未经 `lrag_workspace()` 归一化直接 `ValueError` 失败,
   不再产生"半份脏数据" (原来会静默写入 KV/向量层后图谱失败)。
2. `verify_lightrag_deployed.py` 断言"所有 workspace 已归一化"+"无真实入库失败", 该类问题复发即红。

> 说明: `lightrag_full_entities` / `full_relations` 是**每文档一行**的合并结果表 (`count` 字段才是实体数),
> 不是每实体一行。LightRAG 每次 `ainsert` 相同内容会新增一条 `dup-* / status=failed /
> "File name already exists"` 幂等簿记记录, 属正常行为, 不断言其不存在。

## 关键验证用例 (对应规格书清单)

- 章节过滤: "输出过流保护点" + `section_path=4.3.3` → 仅命中 SR-1309 (12~18A), 无 4.3.2 的 11.1A、无 4.2.4.2 的 30A 通流
- 公式链: 54V × 11.1A = **599.4W** → 效率 93.07% (自动多步推导), 溯源逐前提标注 `layer: power`
- SHACL: trip=12/recovery=10/hyst=3 → 违规识别 ✅; 合法数据 conforms ✅
- 测试用例生成: SR-1309 → 判据 `12.0 ≤ 值 ≤ 18.0 A` (主输出轨 -54V 优先)
- 探针选型: 11.1A → 最小直径 2.5mm (K-FIX-004)
- 查询自动识别: "PA601-D54A 的输出电流" 免传 model_id; 双型号歧义拒绝

## 摄取数据规模

- PA601-D54A: 47 章节块 / 106 分块 / 187 结构化实体 / LightRAG 436 实体+393 关系 (LLM 抽取)
- PN1000-48A: 4 块 / 5 实体 (隔离夹具)
- 领域库: power 114 规则块 + knowledge.md 叙述层 (9 章节); common 1 块
- Semantica 语义图 `power_rules`: 378 节点 (Rule 115 / Category 25 / Scope 35 / Source 88 / Formula 38 / Shape 77) + 460 边

## 通用电源产品知识扩充 (联网检索, Tavily)

按"产测阶段实测/判定"口径, 分 6 批新增 **82 条** 规则 (power 32 → 114), 并修正 K-FIX-007 探针寿命口径 (1万~10万 → 100k 标称 / 50k~150k 实际):

| 批次 | 主题 | 规则数 | 主要 ID 段 |
|---|---|---|---|
| 1 | 探针/工装/测量 | 10 | K-FIX-009~018 |
| 2 | DC-DC 可产测判据 | 9 | K-PWR-101~109 |
| 3 | 保护矩阵 + 欠压/过压/回差 | 19 | K-PROT-101~119 |
| 3b | 输出规格/效率/功率/瞬态 | 12 | K-PWR-110~121 |
| 5 | 安规产测项 (耐压/爬电/Y 电容/保险丝) | 9 | K-SAF-101~109 |
| 6 | 遥测/遥信/遥控 + 校准降额 | 23 | K-TLM-101~112, K-CTL-101~104, K-CAL-101~107 |

规则构成: 37 条 derive (可计算) + 77 条 SHACL constraint (可判定), 全部带 test 块; 来源 46 条外部标准/行业文献 + 41 条 `local://` 行业惯例。

覆盖要点: 四线 Kelvin 与半 Kelvin 边界、10:1 与 %GRR 测量系统、大电流探针中心距与交错排布; 纹波成因式 (ΔI/8fC + ESR) 与实测判据、负载/输入瞬态量化、保持时间储能式 (C=2Pt/η(V₁²−V₂²)); OVP 跟踪式、UVLO 回差与阈值分层、五类保护完整性、hiccup/恒流/折返/锁存四模式、保护点精度与回差下限式; 电压调整率定义、效率多点测试与功率测量同步采样判据、空载功率与功率平衡自检; IEC 62368-1 耐压/间隙/Y 电容容值反推/保险丝 I²t 配合; 遥测精度与采样混叠、去抖、NTC B 值换算、遥控限幅与回读一致性、标定残差与降额判据。

已按要求剔除产测不涉及项: EMC/浪涌/EFT、环境试验 (IEC 60068)、MTBF/FIT 预计、老化筛选、PMBus 协议、风扇与缺相。

## 沉淀的工程要点 (详见 deploy/native/README)

1. LightRAG workspace 必须全小写 (merge 阶段 schema 不带引号, 大写被折叠)
2. pg_textsearch 需显式 `to_bm25query(query, '索引名')`
3. Qdrant 点 ID 必须确定性 (uuid5); LightRAG embedding 返回须 numpy
4. 实体抽取采用锚点右对齐 (等级/备注锚定) 解决表格列错位
5. 保护逻辑约束含方向性 (欠压类恢复点高于保护点)
6. Semantica `ApacheAgeStore.connect()` 硬编码 `LOAD 'age'`, 非超级用户会 `InsufficientPrivilege`; 已用子类跳过 LOAD, 仅 `SET search_path = ag_catalog,...` + `create_graph` (与 LightRAG 同一路径)
7. Semantica `execute_query` 的参数走 `parameters=` 关键字, 且 cypher() 不支持 `$param` 绑定 — 库内部会做字面量内联, 传参必须用 `parameters`
8. SHACL SPARQL 约束里 `BIND` 不能作为 `sh:select` 首个子句 (rdflib 解析为 `Expected SelectQuery, found 'BIND'`); 需改用 `FILTER NOT EXISTS` 组合
9. 板卡 PG 偶发 `10013 Permission denied` 是瞬时网络/防火墙抖动, 非配置错误, 重试即可
10. `sudo -n cmd1 && cmd2` **只给 cmd1 提权**, cmd2 仍以 SSH 用户身份运行 → `chown` 报 "Operation not permitted"。必须写成 `sudo -n sh -c '<整条命令链>'`
11. 板卡是受限容器 (overlayfs): `<board_user>` 无法在 `/opt` 建目录, 服务账号无法穿越 `/root/.local`。因此 uv 与预编译解释器都必须装到共享可读路径 (`/usr/local/bin/uv` + `/opt/python`), 否则 venv 内的 `python` 是指向 `/root` 的死链
12. 板卡 venv 由 `sudo` 创建时, 跑 `uv` 需加 `--no-config`: uv 会从 CWD 向上找 `uv.toml`/`pyproject.toml`, 服务账号读不了 SSH 用户家目录会直接报错
13. 注册表加载在服务启动时完成 (模块级 `Registry.load`), **导入新型号后必须重启 MCP 服务**, 否则"导入成功但查不到"。`upload_new_spec.py` 已自动串上这一步
14. `.env` / `registry.yaml` / `domain_rules` / `rag_storage` 全部相对 **CWD** 解析。板卡上经 SSH 执行时 CWD 未必是应用根, 已在 `ingest_new_spec.py` 里 `os.chdir(APP_ROOT)` 固化
15. `lightrag_full_entities` 是**每文档一行**, 实体总数要 `sum(count)`; 直接数行数会得出"未落库"的错误结论
16. 域 workspace 是否建 AGE 图谱取决于**有无叙述层 (.md)**: 纯规则 YAML 的域 (如 common 仅 1 条 K-CMN-001) 只写业务表, 不建图谱。校验脚本不能对所有 `populated` 域一视同仁
17. `registry.domains['common'].workspace` 字段 (`_domain_common`) **从不生效** —— `domain_workspace('common')` 有特判返回 `_common`。解析 workspace 必须走该方法, 不能直读字段
18. 自验器 `test.given` 的键是 snake_case, 而 SHACL shape 里是 camelCase, **两者必须由 `PS_PROP` 显式映射**。映射缺失时测试数据与 shape 查询的属性永不相交 → 约束**永不触发**、形同虚设; `SCOPE_CLASS` 同理, scope 未映射会落到默认类导致 `sh:targetClass` 匹配不上。K-PWR-123 首跑即踩此坑
19. 代码里不得留未定义辅助函数: `optimize_process` 引用了未定义的 `_safe_calc`, 本地没跑到该分支, 只有板卡服务态才暴露
20. MCP `calculate` 的入参名是 `inputs` 而非 `given`; 测试脚本照抄 `given` 会静默取不到值
21. 数值解析必须同时接受**显式正号**: 规格书写正轨电压为 `+3.45` (SR-PA601-D54A-1200#3.45V),
    而 `_parse_num` 原正则只有 `-?` → 该轨额定电压整条丢失且**不报错**; `_RANGE` 更是完全无符号位,
    `-5 ~ +5` / `-54~-52` 这类双极性区间也匹配不到。均已修, 并加 11 条解析回归用例
    (`verify_extract.py` 9→22 项)。**这类静默丢失比"编造"更隐蔽**: fail-closed 只能防编造, 防不住漏抽

## 低线输入降额 / 第二路输出 (本轮新增)

- **K-PWR-122** (derive) `derated_output_current = p_line_derated / v_out`
- **K-PWR-123** (SHACL) 低线判据不得沿用额定值; 判据与保护点须取同一输入电压条件的值

来源为真实可查文献 (XP Power 低线降额技术文档 / TDK CUS250M 应用笔记 / TI tiduet4a 54V 1kW 参考设计)。

PA601 双路输出实测结论 (`verify_rail2_345v.py` 6/6):

| 项目 | 3.45V 第二路 | -54V 主路 |
|---|---|---|
| 额定输出电流 | 0.1A | 11.1A |
| 过流保护 | 0.4~1.5A (无低压备注) | 12~18A (<176Vac 降为 8.1~18A) |
| 低线降额 | 规格书**未声明** | 400W@90~176Vac → 7.4A |
| 额定功率 | 0.345W (占 600W 的 0.06%) | 599.4W |

**`SR-PA601-D54A-1204 输出功率` 无轨道列** (`rail=''`), 400W/600W 只能归属主轨;
若误套到第二路得 400/3.45 = 115.9A, 是额定值的 1160 倍 —— 该荒谬值已固化为回归断言,
证明降额功率**不可跨轨套用**。此为规格书数据缺口, 需研发补充分轨功率或明确降额归属。

## P0 反幻觉修复 (消除伪造源, 全部 fail-closed)

面向工业产测"每个数据真实可靠 / 有明确规则推理"的要求, 走查 13 个工具后定位并修复 **3 处会伪造数值的代码**:

| # | 位置 | 原问题 | 修复 |
|---|---|---|---|
| 1 | `server.py::_model_facts` | `abs(float(...or 54))` —— 抽取失败时**静默填 54V**, 且 `except Exception: pass` 吞掉所有异常。PA601 恰为 54V, 该错误在当前型号下**完全不可见**, 换型号才暴露 | 删除兜底改抛 `FactUnavailable`; 主轨改为**按额定电流最大者动态选取**(原写死 `rail in ("-54V","")`, 换 -48V 型号会静默取不到电流); 实体查询异常直接上抛; 每事实附 `_provenance`(req_id + section_path) |
| 2 | `server.py::get_fixture_spec` | `("tolerance", {"components": [0.3, 0.3]})` —— 注释自承是示例值, 却与真实计算结果混在同一返回里, 调用方无法区分 | 改为 `status: not_computed` + `required_input` 说明; 需计算时显式传 `calculate(inputs={...})` |
| 3 | `server.py::optimize_process` | `("probe_life", {"rated_life": 100000, "used_count": 90000})` —— 工装台账数据被写死冒充实测 | 改为入参 `probe_rated_life` / `probe_used_count`, 未传则 `not_computed` |

验证 `scripts/verify_fail_closed.py` → **FAILCLOSED_VERIFY 12/12** (走板卡 MCP 服务态): 事实溯源 /
主轨动态选取 / **缺事实报错且不泄露任何默认电压值** / 缺输入报错不兜底 / 示例值不再出现。

## 低线输入降额规则补齐 (K-PWR-122 / K-PWR-123)

规则库此前**完全没有**低线输入降额规则 (现有 derating 类只覆盖电容电压 / 结温 / 环境温度), 补两条:

- **K-PWR-122** (derive): `derated_output_current = p_line_derated / v_out`
- **K-PWR-123** (SHACL): 低线判据不得沿用额定值; 判据与保护点须取同一输入电压条件的值

来源为真实可查文献 (XP Power 低线降额技术文档 / TDK CUS250M 应用笔记 / TI tiduet4a 54V 1kW 参考设计),
非编造 URL。接线验证 `scripts/verify_low_line_derating.py` → **7/7**。

## Semantica 的真实定位 (澄清)

全量搜索确认: `src/` 中**只有两个配置字段、零使用点** —— 它是**只写不读**的派生数据, 不是运行时依赖。
数据在 `power_specs` 的 AGE 图谱 `power_rules` (384 节点 / 468 边), 与 LightRAG 图谱彼此独立。
MCP 13 个工具全部走 LightRAG + Qdrant + Datalog + SHACL, 不查 Semantica。
其不可替代价值是**溯源与治理** (规则出处完整性校验 / 作用域隔离 / 无据答案检测), 尚未接入。

## 硬编码审计与清理 (面向"数据真实可靠")

系统化扫描 `src/` (93 处默认值逐一核对) 后, 定位 3 处方向性错误的硬编码并全部清除:

| # | 位置 | 原问题 | 修复 | 风险等级 |
|---|---|---|---|---|
| 1 | `server.py::get_test_cases` | 写死 `rail in ("-54V", "-48V")` 轨名白名单来挑主输出轨。换 -12V/-28V 型号时主轨不匹配, 会**静默降级**到次优先级, 可能把辅助轨 (3.45V 0.1A) 当主轨出判据 | 改为复用 `_model_facts` 的"额定输出电流最大者"动态判定 | 高 (PA601/PN1000 恰好都在白名单内, 测不出来) |
| 2 | `engine.py` | `confidence=rule.get("confidence", 1.0)` —— 缺标注时给**最高可信度**。方向完全反了: 缺失应降低可信度 | 改为 `rule.get("confidence")` → `None`(未知), 并在返回体显式加 `confidence_unknown` 标记; `Derivation`/`DecisionTrace` 类型改 `float \| None` | 中 (已核实 116/116 规则均带该字段, 默认值**从未触发**, 属潜在风险) |
| 3 | `rag/service.py` | 未知 workspace 标 `"model"` 层 —— 未注册 workspace 的命中会被当成型号事实, 污染溯源分层与隔离判断 | 新增 `UNREGISTERED_LAYER = "unregistered"` 常量, 3 处兜底全改 | 低 |

回归测试 `scripts/verify_no_hardcoded.py` → **NO_HARDCODED_VERIFY 15/15**:
- 构造 **-12V / -28V / -5V / -48V / -54V** 五种轨名(含单字符 `-5V`, 最易被字面量匹配漏掉),
  断言主轨判定均正确且**不依赖任何白名单**; 每组均含一条电流更小的辅助轨作为干扰项
- 摘掉真实规则 `K-ELEC-001` 的 `confidence`, 断言结果为 `None` 而非 `1.0`, 且数值仍正确
- **AST 扫描**: 只在真实代码的字面量里找轨名, 排除注释与文档字符串
  (注释里的 `"-54V"` 是记录历史修复, 属正常; 用正则扫全文会误报 —— 本轮已踩过)

**已确认干净的部分**: `src/` 无 IP/端口/凭据硬编码(全走 `.env`); 116/116 规则均带 `source`;
无"编造规格书数据"级硬编码; `service.py` 其余 `"model"` 取值均为语义正确(型号 workspace / 图检索结果)。

## 遗留与后续建议

1. `optimize_process` 的时序预测为规则版 (规格方案标注的演进项)
2. Word/Excel 规格书解析未含 (当前 MD 原生) — 新规格书须先转 Markdown
3. 换 embedding 模型时需重建 Qdrant collection (维度变化), init_tenant.py 已支持
4. 建议修改板卡默认口令 (SSH 用户与 PG 业务口令 (见 .env / 部署说明) 见 deploy README)
5. `registry.yaml` 权威副本在板卡 `/opt/aterag/`, 开发机副本由 `upload_new_spec.py` 自动同步; 切勿在开发机手改
6. 规格书重导为幂等覆盖, **无历史版本留存**, 需保留旧版请自行归档原件
7. 板卡 `.env` 权限 600 属服务账号 `aterag`; 改配置后需 `systemctl restart aterag-mcp`
