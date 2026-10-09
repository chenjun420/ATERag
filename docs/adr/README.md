# 架构决策记录 (ADR)

本目录记录 ATERag 的架构决策。每条 ADR 一旦 Accepted 就不再修改结论——
若结论需要推翻，写一条新 ADR 并在旧 ADR 顶部加 `Superseded by: ADR-XXXX`。

**ADR 记录的是「当时决定了什么」，不是「现在有什么」。** 里面出现的库名、表名、
服务名可能是**已经被移除的东西**——例如 ADR-014 记录的就是移除某个向量库的那次
裁决, 该库在本仓库已不存在。读 ADR 得出「项目现在依赖它」是错的。判断现状看
`pyproject.toml` 的依赖声明与 `src/` 实际代码, ADR 只用来理解「为什么现在是这样」。

## 索引

**`数据政策` 列回答的是「这条决策描述的能力, 数据进没进 PG」** —— 与 `状态`
分开读: `状态: Accepted` 只说明决策已定, 不等于能力已在用 (红线 14)。空表
往往是红线 4（领域知识留在 git 内种子 JSON, 不进 PG）的**正确结果**, 所以
这一列存在本身就是「别把 schema 当成能力」的提醒。表级同一信息写在
`COMMENT ON TABLE`（迁移 `0004_l0_data_policy`）。

| 编号 | 标题 | 状态 | 数据政策 | 来源 |
|---|---|---|---|---|
| ADR-001 | 规格书驱动、抽取全确定性 | Accepted | 已灌 | 仓库既有约定 |
| ADR-002 | 全栈统一 PostgreSQL，单一存储底座 | Accepted | 已灌 | V6.0 §1.7 |
| ADR-003 | 型号物理隔离：`pw_<model_key>` schema | Accepted | 已灌 | V6.0 §5.8 / §5.9 |
| ADR-011 | 附件内容寻址存储 | Accepted | 已灌 | V6.0 §3.9 |
| ADR-012 | 附件写序协议（先落盘、后提交事务） | Accepted | 已灌 | V6.0 §3.9 |
| ADR-013 | 嵌入向量维度统一为 1024 (`halfvec`) | Accepted | 部分灌（机制在，被空表挡住） | 本次 W0 裁决 |
| ADR-014 | 移除 Qdrant，检索层改由 pgvector 承担 | Accepted | 已灌 | 本次 W0 裁决 |
| ADR-015 | 量纲齐次性作为公式入库的硬门禁 | Accepted | 部分灌（门禁完备，`l0_term.formula` 按红线 4 不灌） | V6.0 原则二 / R1 |
| ADR-016 | 八层结构置于单一发行包 `aterag` 之下 | Accepted | 不适用（结构决策） | 对 §18.1.3 的刻意偏离 |
| ADR-017 | 测试标准跨仓库统一，继承 ATEStudio 档位 | Accepted | 已灌 | 本次 W0 裁决 |
| ADR-018 | `rule` 表列集由三处交叉推导，三处主键追加版本维 | Accepted | 不灌（规则权威是 `domain_rules/*/rules.yaml`，ADR-018 只定列集） | V6.0 §18.6 Step 6（方案未给 DDL） |
| ADR-019 | 型号 schema 三张推导表（`doc`/`clause`/`trace`）与「时间列不得脱离版本轴」 | Accepted | 部分灌（`trace` 已灌；`concept` 等按红线 4 不灌） | V6.0 §18.5 第 2 分区（方案未给 DDL） |

取值：`已灌` / `部分灌` / `不灌` / `不适用`。**新增 ADR 时必须填这一列**——
留空会让审计重新落回「Accepted = 已实现」的误读。

## 命名

文件名 `ADR-<编号>-<短横线 slug>.md`，编号只增不改、不复用。
即使删除某条决策，其编号也作废。

## 模板

```markdown
# ADR-XXXX 标题

- 状态: Proposed | Accepted | Superseded by ADR-XXXX
- 日期: YYYY-MM-DD
- 关联: V6.0 §x.y.z / ADR-XXXX

## 背景
问题是什么，为什么现在必须定。

## 决策
一句话说清做了什么。

## 理由
为什么是这个方案，而不是备选方案。备选要列出并说明为何否决。

## 影响
- 正面:
- 负面 / 代价:
- 后续需要做的:
```