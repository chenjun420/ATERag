# ADR-017 测试标准跨仓库统一，继承 ATEStudio 档位

- 状态: Accepted
- 日期: 2026-10-03
- 关联: §18.7.1、§18.9；ATERag 与 ATEStudio 的协作关系

## 背景

两个仓库的测试现状相反：

| | ATERag | ATEStudio |
|---|---|---|
| 测试文件 | 3（867 行） | 195 |
| 覆盖率门禁 | 无 | `fail_under = 80`，CI 阻断 |
| 类型门禁 | 无 | mypy strict |
| 安全门禁 | 无 | bandit `-ll`，已改为阻断 |
| 另加 | ~75 个 `verify_*.py` / `board_*.py` 散落脚本承担实际验证 | 无散落脚本，全在 `tests/` |

也就是说 **ATEStudio 的工程基线高于 ATERag**，方向与直觉相反
（ATERag 是知识与推理侧，理论上更该测得细）。

同时 §18.7.1/§18.9 要求 ATERag 交付：单测覆盖率 ≥85%、每接口 ≥5 个契约
用例、≥200 个回归用例、10 道 CI 门禁。这个量级与 ATERag 现状差距很大。

## 决策

1. **不发明新标准**。ATERag 的 W0 测试骨架直接采用 ATEStudio 的档位：
   `.coveragerc`（`fail_under = 80`、`branch = True`、omit `migrations/__init__`）、
   mypy strict、bandit 阻断。
2. **覆盖率门禁分两层**：
   - CI 阻断线 80（与 ATEStudio 同档）
   - §18.7.1 的 85 是 **W7 阶段出口目标**，不是 W0 门禁。
     现在就卡 85 只会靠 `# pragma: no cover` 刷数字，掩盖真实缺口。
3. **mypy strict 只对 W0 起的新八层开启**。旧代码随对应 Wave 退役，
   退役一个开一个。本条不是「旧代码不需要类型」，而是
   「旧代码正在被替换，为即将删除的代码做类型迁移是浪费」。
4. **散落脚本收编**。`scripts/verify_*.py` 中离线的、可断言的
   （`verify_conditions.py`、`verify_bundle.py`、`verify_scenarios.py`、
   `verify_no_hardcoded.py`、`rules_selftest.py`、`validate_configs.py`）
   逐个迁入 `tests/`，每迁一个删一个原脚本。依赖远端板卡
   （`192.168.5.25`）的保留为 `-m integration`，默认不跑。

## 理由

| 备选 | 否决理由 |
|---|---|
| 为 ATERag 定一套「更适合知识系统」的测试标准 | 两个仓库共用同一批缺陷的判定口径才有用。若 ATERag 用 70、ATEStudio 用 80，同一个缺陷在 ATERag 通过、在 ATEStudio 阻断——这种差异无法解释也无法维护。 |
| W0 就卡 §18.7.1 的 85% | 新八层此时只有 storage + solver 两个有实现，覆盖率会被大量 Protocol 抽象体与未接线模块拉到 60% 以下。为了过线只能加 `exclude_lines` 或 `pragma`，而这两者都会**永久**留在文件里，后续新增代码也继承这份宽松。 |
| 保持现状（无覆盖率门禁） | §18.7.1 明确要求覆盖率，且 §18.9 G 系列门禁全部依赖测试。ATEStudio 已证明 80% 档位在本规模下可达成（195 个文件持续通过）。 |

## 影响

- 正面:
  - 覆盖率门禁是 CI 阻断项，不是报告项。
  - mypy strict 守住 §18.2 接口契约与实现的一致性——
    这正是 §18.10 注 5 点名的失败模式。
  - 71 个散落脚本逐步收编后，`scripts/` 从「事实上的测试目录」
    变回「运维脚本目录」。
- 负面 / 代价:
  - 迁移旧脚本期间存在重复覆盖（`tests/` 与 `scripts/` 各一份）。
    收编是逐个进行的，重复窗口不可为零，需在 PR 里逐个标注。
  - `mypy strict` 会让新八层的首次实现成本上升（需写完整类型标注）。
    这是有意的代价：契约型接口不写类型，类型系统就守不住契约。
  - 远端板卡依赖的集成测试默认不跑，CI 上看不到。
    需另设 nightly 任务，否则「默认不跑」会变成「从不跑」。
- 后续需要做的:
  - 新增 nightly CI 任务跑 `-m integration`，连 `192.168.5.25` 的板卡。
  - W7 阶段把 `fail_under` 从 80 提到 85，并同步 ATEStudio。