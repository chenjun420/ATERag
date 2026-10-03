# ADR-019 型号 schema 三张推导表（`doc`/`clause`/`trace`）与「时间列不得脱离版本轴」

- 状态: Accepted
- 日期: 2026-10-04
- 关联: V6.0 §18.5 第 2 分区、§18.6 Step 10/17、§3.5.2、§3.9.3、§17.4、§1.6；ADR-018 决策 2

## 背景

§18.5 第 2 分区点名型号 schema 要有 `doc / clause / fact / graph_node /
graph_edge / trace`，§18.6 的装载顺序依赖其中三张：

```
Step 10  文档与条款：doc / clause（clause_uid 体系）
Step 17  trace（五级可追溯链，§1.6）
```

**但方案全文 59 处 `CREATE TABLE` 里没有 `doc`、`clause`、`trace`。**
`doc_chunk`/`fact`/`provenance`/`conflict` 有（§3.5.2/§3.5.3），`graph_node`/
`graph_edge` 也没有（LightRAG 抽取结果，落库由 A-W2 的 `retrieval.graph`
负责）。

这不是可以绕过的问题：`clause_uid` 出现在 `yx_point`、`yc_point`、
`yk_command`、`yt_parameter`、`protection_setting`、`fixture_channel_map`、
`comm_protocol` **七张表**里，而 §17.4 的硬约束是「不得编造条款号：
`clause_uid` 必须来自 clause 表」。不建 `clause`，这七列全是无约束的自由文本，
§1.6 的六层追溯链在第三级就断了。

同时 §18.5 的验收判据是「每型号表数 ≥ 30」，而章节里有权威 DDL 的表只有
26 张（含 `yx_soe`/`yc_trend`）。两条路：推导三张表的列集，或下调验收判据。
本 ADR 选前者。

## 决策

### 1. 建三张表，列集逐列追溯到方案用途

| 表 | 用途出处 | 关键列的推导依据 |
|---|---|---|
| `doc` | §18.6 Step 10 | PK `(doc_id, rev)` —— §3.5.2 的 `doc_chunk` 以这两列为事实来源；`object_id` 是**可选**外键指向 `public.document_object`（§3.9 物理附件台账与逻辑文档台账不是一回事） |
| `clause` | §18.6 Step 10 + §17.4 | PK `clause_uid` 单列；`is_normative` 标出图注/目录/页眉这类非条款行，让「不得编造条款号」有作用范围 |
| `trace` | §18.6 Step 17 + §1.6 | `layer` 六值有序枚举 + `ref_id`，自引用表达上下游 |

### 2. 时间列不得脱离版本轴（装饰列判据）

三张表里只有 `doc` 带 `valid_from`/`valid_until`。`clause` 与 `trace`
**刻意不带**。

判据：**一张表的主键若已包含版本轴，再加时间列就是装饰列**。装饰列比没有
更坏——它让人以为这张表能时间旅行，实际写一次就冻结。

- `clause`：`clause_uid` 本身已含修订版（约定形如
  `SR-PA601-D54A-1213@B#3.5.2`），换版产生新 uid，不覆盖旧的。
- `trace`：时间历史归 `provenance`（那里已有 `valid_from`/`valid_until`
  与 `derivation_path`），本表只存当前拓扑。
- `doc`：主键是 `(doc_id, rev)`，**修订版就是版本轴**，所以这里不加时间列
  也会失去历史。折中做法是保留时间列 + `ux_doc_current` partial unique
  index（`WHERE valid_until IS NULL`），与 `storage/bitemporal.py` 的
  `build_partial_unique_index_sql` 同一机制。

同一判据在 ADR-018 决策 4 独立地否决了给 `rule` 表加时间列。

### 3. 七张表的 `clause_uid` 一律外键到 `clause`

`clause` 在 knowledge 批内最先建（`doc` → `clause` 的复合外键决定了顺序），
七张表分布在 telemetry/protection/fixture 批，都在 knowledge 之后，
因此用**列级 inline REFERENCES** 即可，不需要进 `alter_fk` 批。

### 4. 刻意不加的东西

- `clause_uid` **不加格式 CHECK**：编号规则会随客户文档变，§17.4 只说
  「必须来自 clause 表」没规定长什么样。DDL 钉死格式会让第一个不按此格式的
  客户规格书直接建不出条款行。
- `trace` 用 `layer`+`ref_id` 分层表示而非六个独立外键列：六个列的 CHECK
  要写成「恰有一个非空」，换一级就要改约束。
- `trace` 加 `ck_trace_self_ref`：自环的推导链会让「反查上游」死循环。

## 理由

| 备选 | 否决理由 |
|---|---|
| 下调验收判据到 26 张表 | 判据下调掩盖的是「追溯链缺一环」。`clause_uid` 无外键时，格式对了的编号照样可能指向不存在的条款——形式合规、实质失效 |
| `clause` 改用 `doc_chunk.doc_id + chunk_index` 代替 | `doc_chunk` 是**分块**产物，一条款可能跨块被切开，也可能被合进一块（表格）。条款号是人工/规则抽取的语义单元，不是分块的副产物。溯源要指向条款，不是指向块 |
| `trace` 按 §1.6 六级拆成六张表 | 六张表 + 六套 RLS + 六次装载，`docgen` 要导六份 CSV。分层表示的代价是一条 CHECK，收益是加一级不改 schema |
| `clause`/`trace` 也带时间列，与 `doc` 统一 | 「统一」不是理由。第 2 条的判据是逐表判断的结果：`doc` 的版本轴在 PK 外，`clause`/`trace` 的在 PK 内。为形式一致给两表加装饰列，正是本 ADR 要防的失败模式 |
| `trace` 不建表，改为跨 schema 视图 | 追溯要反查 `l0_term.formula` 与 `l0_term.rule`，跨 schema 视图在 §18.2 的模型下不可行；且视图无法承载 `verified`/`verified_by` 这些人工复审状态 |

## 影响

- 正面:
  - 表数 26 → 29，达 §18.5 的 ≥30 门槛（`doc`/`clause`/`trace`）。
  - §17.4 的「不得编造条款号」从约定变成外键约束：写入不存在的条款号直接失败。
  - §1.6 六级链有落点，P1-18 的追溯矩阵（≥120 行）有数据源。
  - 「装饰列」判据成为可复用规则，未来加表时有明确判据而非逐表争论。
- 负面 / 代价:
  - 三张表的列集是推导的，与方案原文无逐字对应。审阅者若认为某列多余或
    缺失，改的是本 ADR。
  - 七张表新增对 `clause` 的外键，插入顺序上条款必须先于点位/定值落库。
    §18.6 Step 10（doc/clause）先于 Step 12（四遥点表）、Step 13
    （protection_setting），顺序本身已满足。
  - `doc.object_id` 指向 `public.document_object`，故 `cmd_init` 必须
    **先**建 public 附件表再建型号表（已改 `storage/cli.py` 的顺序）。
- 后续需要做的:
  - `graph_node`/`graph_edge` 仍无 DDL。待 A-W2 的 `retrieval.graph` 定稿
    时一并处理——它们是 LightRAG 抽取结果的落地形状，应由那个模块定义，
    不宜在 W1 凭推测先建。
  - `clause_uid` 的编号约定（`SR-<model>-<sr>@<rev>#<path>`）需在 P1-10
    装载时落到 `seed/`，并由 G 门禁校验格式一致性（不在 DDL 校验）。
