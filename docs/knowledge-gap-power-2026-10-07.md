# 通用电源知识缺口评估（2026-10-07）

基准: 板卡 192.168.5.25 / DB `power_specs`（板卡重建后本轮已重新落库）
数据面: `pw_sr5400.test_requirement` 95 条（COVERED 40 / PENDING 45 / GAP 10）、
`domain_rules/power/rules.yaml` 130 条 26 类、`config/test_methods.yaml` 18 条方法、
`domain_rules/power/knowledge.md`（22 节，含 4c983f0 起的通用产测工艺知识）。

## 一、缺口分层结论

coverage_status 三态语义（红线: 猜错代价 > 漏判，不自动改判）:

| 状态 | 语义 | 性质 | 条数 |
|---|---|---|---|
| COVERED | 人审签字过、判定可构造 | 无缺口 | 40 |
| PENDING | 有条件、含未人审 draft 提案 | **流程缺口（签字），不是知识缺口** | 45 |
| GAP | assessment 判 insufficient —— 条件齐了也测不了/判不了 | **知识/能力缺口所在** | 10 |

按 signal_type（role 判据落库后）:

| signal_type | COVERED | PENDING | GAP | 读法 |
|---|---|---|---|---|
| PROT | 14 | 1 | 0 | 保护判据知识覆盖最厚 |
| YX | 12 | 5 | 0 | 遥信基本齐 |
| YC | 3 | 5 | 0 | 遥测 0 GAP 但 5 条未签字 |
| TEST | 11 | 34 | 10 | **缺口全部集中在此**（44/55 欠覆盖行） |

## 二、GAP 10 条归因（真正要补的知识）

| 缺口类型 | 条目 | 缺什么 | 落点建议 |
|---|---|---|---|
| **方法缺失** | SR-1219 负载均流度 | method_refs=[] —— rules.yaml 有 current-sharing 3 条、knowledge.md 有「并联均流与下垂法」，**有规则有常识但没有可执行的测量方法** | test_methods 增补 `load_share_measurement`（各轨轻载/满载/50% 读数 + 均流度公式已可从 Formula 库取） |
| **语义混入** | SR-1701 版本管理 | 引用穿透产生的 spec={} 空条件——它不是「测不了」，是**不该出条件**；不该占 GAP 语义 | assess 增加「spec 空 +clause_sources=[block]」→ 报 unnecessary 或不出条，把 GAP 留给真的缺 |
| **值缺口(产品侧)** | SR-1101 Vdc / SR-1213 / SR-1203(第1变体) / SR-1217(3.45V) | spec 无数值或空 {} —— 规格书里就没有 | 需求方澄清清单（并入 SR-1309/1211/2503） |
| **仪器能力缺口** | SR-1217 温度系数(-54V ±0.02%/℃) | 走 `thermal_telemetry_read`（读遥温）—— 但可测温度系数需要**能控温的环境腔**；现 instrument_need 只出了 dmm，无 thermal_chamber | 需求推导层把「温度系数」kind 传播出 thermal_chamber + N 点温度阶跃的采样要求 |
| **rail 归属疑点** | SR-1105 输入冲击电流 rail='-54V' | 输入侧事件挂了输出轨（inrush 由 en300132 方法应带 input 侧条件） | 校验: `supplies=input` 的方法与 rail 非空互斥时告警 |

## 三、知识库存量盘点（三种库存对不上缺口的各一块）

- **rules.yaml（130 条）**: 分类分布厚在 protection 21 / control 11 / fixture 23 / telemetry 8；
  薄在 thermal 1、reliability 1、limits 1。缺口 kind 里「温度系数」「温漂回差」只有 1 条 thermal 规则可挂。
- **test_methods.yaml（18 条）**: 抗扰跌落/冲击/四线感测/效率/纹波/动态/时序/遥信告警/热遥测有；
  **均流度测量、温度系数测量、电池管理验证**（SR-1904 系）无方法 id。
- **knowledge.md（22 节）**: 基本电学/交流/效率/热/保护矩阵/安规/遥测遥控/校准降额/低线降额/均流下垂都在；
- 属「讲道理层」，缺的是把道理变成 verdict 步骤的方法条目。

## 四、非知识缺口的待办（保持既有 blocked 清单）

1. **PENDING 45 → 需签字**：含 CI 长红的 `review_annotation --check`（SR-PA601-D54A-1213 未签字，自 9b66d11）。
   这是流程动作，任何人不得代码侧代签。
2. **跨仓基准 82 vs 91 skip**：待 ATEStudio 侧同步（红线，勿动）。
3. **五张点位表 0 行**（信号类通道阻塞）：需产品侧提供信号表结构化数据。
4. **fixture/test_station/instrument_ledger 0 行**：产品侧工装/仪器台账决策。
5. **spec 矛盾待澄清**: SR-1309（过流 12A vs 8.1~18A 备注）、SR-1211（变速率量级 139×）、SR-2503（湿度自相矛盾）、
   两轨额定 и 599.75W > 400W 档上限（verify_board_rag 如实暴露）。

## 五、本轮之后建议的后续动作
- ① 增加 load_share / tempco / battery 三条方法（配 assess 段，零词汇代码纪律不变）
- ② assess 把空 spec 条件分流到 unnecessary（守 GAP 语义）
- ③ 需求推导层为温度类 kind 传播 thermal_chamber 能力需求
- ④ rail-vs-supplies 一致性告警（ingest 侧 lint）
