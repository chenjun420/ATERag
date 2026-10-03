# ADR-016 八层结构置于单一发行包 `aterag` 之下

- 状态: Accepted
- 日期: 2026-10-03
- 关联: V6.0 §18.1.3

## 背景

方案 §18.1.3（`定制电源…V6.0.md:14258-14319`）给出的目录结构把八个层
直接放在 `src/` 下：

```
src/storage/  src/retrieval/  src/reasoning/  src/solver/
src/jev/      src/generation/  src/fixture/    src/docgen/
```

八个目录都将成为 **Python 顶层包名**。

## 决策

八个层置于既有发行包之下，即 `src/aterag/{storage,solver,reasoning,...}`。
层名与 §18.1.3 完全一致，只是多一层 `aterag` 前缀。

## 理由

方案原文写的是「**建议**目录结构」，不是强制。而按字面实现会引入一个
具体的、可预见的安装期故障：

| 顶层包名 | 冲突对象 |
|---|---|
| `fixture` | pytest 生态的 `pytest-fixture`、`_pytest.fixtures` |
| `solver` | `z3-solver`、`cvxpy` 生态的 `solver` 子模块 |
| `generation` | HuggingFace `transformers.generation`、`diffusers` |
| `storage` | `google-cloud-storage`、`localstack` 一类 |
| `docgen` | sphinx 生态 |
| `jev` | 无冲突，但无前缀会让「这是个电力工程模块」这件事完全丢失 |

这些都不是假想冲突：`fixture` 和 `generation` 在同一环境里与
HuggingFace / pytest 生态共存的概率很高，而依赖解析冲突的报错信息
（「cannot import name X from partially initialized module」之类）
**不会**指向本项目，只会指向第三方包。排障成本极高。

保留 `aterag` 前缀还带来两个附带收益：

1. `aterag.storage.schema` / `aterag.solver.symbolic` 在日志和 traceback 里
   自带「这是 ATERag」的信息，与 §18.2 契约里写的模块名
   （`storage.bitemporal`、`solver.symbolic`）一一对应且无歧义。
2. 旧代码现存 8 处 `from aterag.config import ...` 之类的 import 与
   `mcp_server`、`workbench` 两个部署入口不需要因目录重构而改包名。

## 影响

- 正面:
  - 避开安装期命名冲突。
  - 契约对照仍可机械完成：§18.2 写 `solver.symbolic`，
    代码是 `aterag.solver.symbolic`，逐段替换即可。
  - 发行名 `aterag` 与包名统一，`uv sync` / wheel / `PYTHONPATH` 一套口径。
- 负面 / 代价:
  - 与 §18.1.3 的目录树字面不一致。文档与代码的对照需要知道这条偏离。
  - 若将来 ATERag 要被拆成多个发行包，这个前缀要重写一遍。
- 后续需要做的:
  - 若 §18.1.3 后续小修订把目录树改成带前缀形式，本 ADR 可直接废止。
  - 部署脚本与 systemd unit 中的模块路径（`python -m aterag.mcp_server.server`
    之类）保持不变，无需改。