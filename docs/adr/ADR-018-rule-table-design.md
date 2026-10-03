# ADR-018 `rule` 表列集由三处交叉推导，三处主键追加版本维

- 状态: Accepted
- 日期: 2026-10-04
- 关联: V6.0 §18.6 Step 6、附录B.2/B.3/B.4、§17.6.1、§18.2.2、§7.4.1、§7.7.1、§5.12.1、§10.8.1、§6.5.3

## 背景

方案 §18.6 Step 6 要求装载「`rule` 表（R1–R20 + P1–P18）」，§18.4.1 把
「M13 `rule` 表 + 附录B 参数装载」列为 W1 的交付物，§18.5 第 1 分区也把
rule 列进 L0 共享层。

**但方案从未给出 `CREATE TABLE rule`。** 全文 59 处 `CREATE TABLE` 里没有它
（已逐条核对，见 `grep -n "CREATE TABLE"`，命中行 845/901/936/959/987/1024/
1055/1076/1091/1103/1324/1376/2325/2356/2392/2415/2433/2459/2483/2828/
3050/3069/3530/4664/5105/6700/6804/7555/7733/9246/11502/12206/12288/12470/
12575/12659/12699/12777/12817/12915/13038/13079/13108/13242/13556/13622/
13667/13822/13864/14611/14658/15106/15125/15352/15568/15586/17890/24602/24634）。

与此同时，§7.8 有一条不能绕过的要求：「公式依据 | 每条规则标注公式编号 |
五级可追溯」，§18.2.2 契约 ① 写「加载规则前必须校验 `formula_ref` 存在于
`formula` 表（否则启动失败）」。也就是说这张表是**六层追溯链的枢纽**（§1.6：
公理→定理→公式→规则→测试→判据），却只有一个表名。

同批要建的另五张表，方案都给了 DDL，但**主键设计有共同缺陷**：
`rule_parameter`（§7.4.1）、`borrow_rule`（§5.12.1）、`jev_threshold`（§10.8.1）
三张表都带 `valid_from`/`valid_until`，主键却都不含时间维。

## 决策

### 1. `rule` 表列集由三处交叉推导

无 spec 原文可抄，故逐列追溯到出处，不引入无出处的列：

| 列 | 出处 |
|---|---|
| `rule_id` | §18.2.2 `Rule.rule_id`（「R1~R20 / P1~P18」）、附录B.3/B.4 的「#」列 |
| `rule_type` | 追溯矩阵 CSV 表头 `rule_type`，取值 `hard`/`soft`（附录B.3=硬、B.4=软） |
| `rule_name` / `formal_criterion` | 附录B.3/B.4 与 §17.6.1 的「规则」「形式化判据」列 |
| `body` / `expr` | §18.2.2 `Rule.body`（「Datalog 规则体」） |
| `formula_ref` / `formula_section` | 附录B.3/B.4「公式依据」列、追溯矩阵 CSV 的 `formula_id`/`formula_section` |
| `severity` | §18.2.2 `Literal["reject","hold","degrade","hint"]` |
| `fail_action` | 附录B.3/B.4「失败动作」列、§17.6.1「失败动作」列 |
| `domain` | 追溯矩阵 CSV 表头 `domain` |
| `used_by_test` | 追溯矩阵 CSV 表头 `test_id`；与 `formula.used_by_test` 同构，构成反向索引 |
| `standard_ref` | 追溯矩阵 CSV 表头 `standard_ref` |
| `owner` / `review_cycle` | §7.8「规则owner」「复审周期不超过一个季度」 |
| `no_formula_reason` | §18.10 注 2「不可追溯的值记 UNKNOWN，绝不猜」的 DDL 落点（见决策 3） |

`rule_id` 加了格式 `CHECK (rule_id ~ '^(R[1-9]|R1[0-9]|R20|P[1-9]|P1[0-8])$')`：
§17.6.1 末尾明说「R1–R20（20 条）+ P1–P18（18 条）= 38 条」，闭集已知，
在 DDL 层挡住越界编号比在门禁层查便宜。

### 2. `fail_action` 只 NOT NULL，不加 CHECK

附录B.3/B.4/§17.6.1 的取值是中文动作短语：`拒`、`拒或标冲突`、`提示扩容`、
`插入前置步`、`改写时长`、`换仪器/标不可信`、`改工装`、`强制前置`、
`告警/降级`、`降级`、`挂起`、`阻断工装转 qualified`、
`工位置 maintenance + 使受影响用例失效`、`禁止生产`、`阻断测试`。

这是自然语言描述而非闭集枚举，且条目本身还在长（P13–P18 是本版新增，
§17.6.1 末尾已预告「需同步登记到附录B」）。给它加 `CHECK (fail_action IN (...))`
会造出一个**假闭集**：下一次加规则又得改 DDL，而漏改的表现是 `ALTER TABLE`
失败——在生产库上执行 DDL 失败比存一条没校验的动作描述糟得多。

### 3. `formula_ref` 可空，但可空必须给理由

附录B 里 R4（条件可复现）、R11（保护功能可用）、P11（遥信点表完整性）的
「公式依据」列本来就是空的——它们是流程性/完整性判据，本来就没有对应公式。
§17.6.1 的 P16 依据写「L7 三方一致」，也不是公式 ID。

但 §18.10 注 2 要求「不可追溯的值记 `UNKNOWN`，绝不猜」。裸的可空列满足不了
这条：留空与「确实没有公式」无法区分。所以加

```sql
CONSTRAINT ck_formula_ref_reason CHECK (
    (formula_ref IS NOT NULL AND no_formula_reason IS NULL)
    OR (formula_ref IS NULL AND no_formula_reason IS NOT NULL))
```

即：要么给出可解析的公式外键，要么写明为什么没有，不允许悄悄留空。

### 4. `rule` 表**刻意不带** `valid_from`/`valid_until`

主键是 `rule_id`，与时间列并存会让版本历史无法表达（同一 `rule_id` 只能有一行），
两个时间列于是退化成写一次就冻结的装饰列，反而给人「这张表能时间旅行」的错觉。
版本历史归 §7.7.1 的 `rule_version`（PK `(rule_id, version)`）。

同理**不建** `(valid_from, valid_until)` 索引、不建 partial unique index。

### 5. 三处 PK 追加 `valid_from`

`rule_parameter` / `borrow_rule` / `jev_threshold` 的 spec 主键不含时间维，
但表里有 `valid_from`/`valid_until`。反例：§7.4.3 的型号 B 示例把型号 A 的 P2
`bw_limit` 从 `20MHz` 改成 `100MHz`。按 spec 主键，这只能 `UPDATE` 覆盖，
于是「某历史时刻该用哪个 `bw_limit`」永远无法回答——而 §5.8.3 纪律 3 要求
溯源走双时态视图，§7.8 也明说「规则版本双时态」。

追加 `valid_from` 进 PK 后，「至多一条当前版本」由 partial unique index
（`WHERE valid_until IS NULL`）保证，与 `storage/bitemporal.py` 的
`build_partial_unique_index_sql` 是同一机制，不引入第二套判定。

### 6. 另两处刻意偏离

- `borrow_rule` **不加** `concept_id` 外键：`borrow_type` 有五个取值
  （`term`/`rule`/`ontology`/`template`/`formula`），借用对象横跨术语/规则/
  公式三个命名空间，外键无处指向。加 `ck_borrow_not_self` 挡住自我借用。
- `disambiguation_log` **不带**时间列：它是运行日志不是知识条目，双时态会让
  审计者误以为可时间旅行。它自带 `created_at`，够了。另加
  `ck_reviewed_pair`（`reviewed_by`/`reviewed_at` 同有同无）。

## 理由

| 备选 | 否决理由 |
|---|---|
| 只建 spec 明确给了 DDL 的表，`rule` 等 P1-17/P1-18 再补 | §18.6 的装载顺序 Step 6 依赖 `rule`，Step 17 的 `trace` 要引 `rule_ref`；跳过它则 W1 的追溯链从第三级断开，比列集推导的风险大得多 |
| `rule` 表照抄最接近的 `rule_version`（§7.7.1） | `rule_version` 存的是「规则文本的历史版本」，`rule` 要的是「规则本体 + 公式依据 + 追溯字段」。两者列集差一半，且 `rule_version` 有 `version` 列而 `rule` 没有 |
| `fail_action` 归一成枚举（`reject`/`block_load`/…） | 方案给的是中文短语；归一就是发明词汇。且 §17.6 的 `poka_yoke_event.fail_action` 是**另一套**闭集（`block_load`/`stop_line`/…），两套混用会让「同一列名两种取值」无法排查 |
| 三处 PK 照抄 spec，靠应用层保证只留一条当前版本 | 应用层的保证在并发下不成立；partial unique index 是唯一能挡住「两个事务同时插入当前版本」的机制。且装饰时间列本身就是缺陷信号 |
| `rule` 表加 `valid_from`/`valid_until` 并把 `version` 进主键 | 那就是 `rule_version` 的定义。同一份数据两张表、两个真相源，与 §7.8「单一真值源」冲突 |

## 影响

- 正面:
  - §18.6 Step 6 有落点，W1 的「规则带 `formula_ref`」（P1-17）可实现。
  - `ck_formula_ref_reason` 把 §18.10 注 2 从约定变成 DDL 约束——「没有公式
    依据」必须书面解释，追溯矩阵里这一栏不会是空的。
  - `ck_formula_ref_reason` + `formula_ref` 外键共同实现 §18.2.2 契约 ①，
    且失败点在装载时而不是启动时。
  - 三处 PK 追加 `valid_from` 后，`rule_parameter` 真正能回答
    「某历史时刻该用哪个 `bw_limit`」。
- 负面 / 代价:
  - `rule` 表列集是推导的，与方案原文不存在逐字对应关系。审阅者若认为某列
    多余或缺失，改的是本 ADR 而不是「按 spec 修」。
  - 三张表的 PK 偏离 spec，`psql -f seed/schema_full.sql` 与直接执行方案
    示例 `INSERT` 的语句形状不同（示例 INSERT 不带 `valid_from`，靠 DEFAULT）。
- 后续需要做的:
  - `fail_action` 的自然语言取值需要一份受控词表，由 P1-17 落库时统一，
    在 `docgen.validate` 里检查是否越出词表（而不是 DDL CHECK）。
  - `R4`/`R11`/`P11`/`P16` 四条规则的 `no_formula_reason` 需在 seed 装载时
    写明，并同步回方案的附录B（当前 spec 未记录，属方案的文档缺口）。
