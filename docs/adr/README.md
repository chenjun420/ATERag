# 架构决策记录 (ADR)

本目录记录 ATERag 的架构决策。每条 ADR 一旦 Accepted 就不再修改结论——
若结论需要推翻，写一条新 ADR 并在旧 ADR 顶部加 `Superseded by: ADR-XXXX`。

## 索引

| 编号 | 标题 | 状态 | 来源 |
|---|---|---|---|
| ADR-001 | 规格书驱动、抽取全确定性 | Accepted | 仓库既有约定 |
| ADR-002 | 全栈统一 PostgreSQL，单一存储底座 | Accepted | V6.0 §1.7 |
| ADR-003 | 型号物理隔离：`pw_<model_key>` schema | Accepted | V6.0 §5.8 / §5.9 |
| ADR-011 | 附件内容寻址存储 | Accepted | V6.0 §3.9 |
| ADR-012 | 附件写序协议（先落盘、后提交事务） | Accepted | V6.0 §3.9 |
| ADR-013 | 嵌入向量维度统一为 1024 (`halfvec`) | Accepted | 本次 W0 裁决 |
| ADR-014 | 移除 Qdrant，检索层改由 pgvector 承担 | Accepted | 本次 W0 裁决 |
| ADR-015 | 量纲齐次性作为公式入库的硬门禁 | Accepted | V6.0 原则二 / R1 |
| ADR-016 | 八层结构置于单一发行包 `aterag` 之下 | Accepted | 对 §18.1.3 的刻意偏离 |
| ADR-017 | 测试标准跨仓库统一，继承 ATEStudio 档位 | Accepted | 本次 W0 裁决 |

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