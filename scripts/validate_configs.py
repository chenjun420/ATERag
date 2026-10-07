"""校验项目内所有 YAML 配置的合法性 (提交前必跑).

背景: 本项目已经两次因"未加引号的裸标量里含 ': '"被 YAML 判定为
  mapping values are not allowed here 而失败 —— 一次是 GitHub Actions 工作流
  (导致流水线从未执行), 一次是 doc_profiles.yaml 的章节说明文字。
  两次都是"人眼看不出来、只有解析器能发现"的错误, 且都在推送后才暴露。

本脚本把这些配置一次性解析 + 做结构自检, 挡在本地:
  config/table_schemas.yaml        表头 -> 规范字段映射 (白名单 + 策略名校验)
  config/doc_profiles.yaml          档案/剔除词/章节先验
  config/condition_patterns.yaml   条件类型封闭词表 + 规则自洽性
  data/annotations/*.yaml           人工注记 (运行时数据, 缺失属正常)
  data/registry.yaml                产品注册表
  domain_rules/*/rules.yaml         领域规则

用法: .venv\\Scripts\\python.exe scripts/validate_configs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.extract.configs import validate_extraction_configs  # noqa: E402

FAIL = "❌"
PASS = "✅"

# 需要引号包裹却漏了的场景: value 里含 ": " 的未加引号标量。
# PyYAML 本身就会报错, 这里只负责把报错信息翻译成人能定位的行号。
CONFIG_GLOBS = (
    "config/*.yaml",
    "data/annotations/*.yaml",
    "domain_rules/*/rules.yaml",
    "data/registry.yaml",
)


def parse_one(path: Path) -> tuple[bool, str]:
    src = path.read_text(encoding="utf-8")
    try:
        yaml.safe_load(src)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        lines = src.splitlines()
        detail = ""
        if mark is not None:
            n = mark.line + 1
            detail = f"第 {n} 行: {lines[n - 1].strip()[:90]}"
        hint = ""
        if mark is not None:
            line = lines[mark.line]
            # 典型成因: 未加引号的说明文字里出现冒号+空格
            if ": " in line and not line.strip().startswith("#"):
                hint = " -> 疑似未加引号的裸标量含 ': ', 需给该值加引号"
        return False, f"YAML 解析失败 | {detail}{hint}"
    return True, ""


def main() -> int:
    files: list[Path] = []
    for g in CONFIG_GLOBS:
        files.extend(sorted(Path(".").glob(g)))
    if not files:
        print("未找到任何配置文件")
        return 1

    bad: list[tuple[Path, str]] = []
    print(f"=== 1. YAML 解析 ({len(files)} 个文件) ===")
    for f in files:
        ok, detail = parse_one(f)
        print(f"  {PASS if ok else FAIL} {f} {detail if not ok else ''}")
        if not ok:
            bad.append((f, detail))

    # ---- 2. 结构自检 (解析过了还要看字段是否符合约定) ----
    print("\n=== 2. 结构自检 ===")
    problems: list[str] = []

    ts_path = Path("config/table_schemas.yaml")
    if ts_path.exists():
        try:
            from aterag.ingest.table_schema import SchemaRegistry

            reg = SchemaRegistry.load(ts_path)
            print(
                f"  {PASS} 表结构档案: {len(reg.schemas)} 个 schema, 白名单 {len(reg.known_fields)} 字段"
            )
            if not reg.schemas:
                problems.append("table_schemas.yaml 未定义任何 schema")
            if reg.grouping_protection is None:
                problems.append("table_schemas.yaml 缺 grouping.protection (保护功能将不会聚合)")
        except Exception as e:  # noqa: BLE001
            print(f"  {FAIL} 表结构档案加载失败: {e}")
            problems.append(f"table_schemas.yaml: {e}")
    else:
        problems.append("缺少 config/table_schemas.yaml")

    dp_path = Path("config/doc_profiles.yaml")
    # 档案角色词表 —— 供 test_methods.yaml 交叉校验 (角色是档案知识, 不硬编码)
    pb_roles: frozenset[str] = frozenset()
    if dp_path.exists():
        try:
            from aterag.extract import ProfileBook

            pb = ProfileBook.load(dp_path)
            pb_roles = frozenset(
                pr.role for p in pb.profiles.values() for pr in p.section_priors.values()
            )
            print(f"  {PASS} 文档档案: {len(pb.profiles)} 个 profile, 默认 {pb.default_profile}")
            for name, p in pb.profiles.items():
                if not p.section_keywords:
                    problems.append(f"profile {name} 未定义 section_keywords")
                for sec, pr in p.section_priors.items():
                    if pr.limits_to not in {"input", "output", "both"}:
                        problems.append(f"profile {name} 的 {sec}.limits_to 非法: {pr.limits_to}")
            if pb.default_profile not in pb.profiles:
                problems.append(f"default_profile 指向未定义档案: {pb.default_profile}")
        except Exception as e:  # noqa: BLE001
            print(f"  {FAIL} 文档档案加载失败: {e}")
            problems.append(f"doc_profiles.yaml: {e}")
    else:
        problems.append("缺少 config/doc_profiles.yaml")

    cp_path = Path("config/condition_patterns.yaml")
    book = None
    if cp_path.exists():
        try:
            from aterag.extract import PatternBook

            book = PatternBook.load(cp_path)
            print(
                f"  {PASS} 条件规则库: {len(book.rules)} 短语规则 / "
                f"{len(book.title_rules)} 标题规则 / {len(book.kinds)} 词表项"
            )
            if not book.rules:
                problems.append("condition_patterns.yaml 未定义任何短语规则")
        except Exception as e:  # noqa: BLE001
            print(f"  {FAIL} 条件规则库加载失败: {e}")
            problems.append(f"condition_patterns.yaml: {e}")
    else:
        problems.append("缺少 config/condition_patterns.yaml")

    # ---- 业界方法库 (缝⑤) + 抽取侧配置自洽 (A20) ----
    # 交叉校验**只有一份实现**(aterag.extract.configs): 校验规则曾经有两份
    # (这里一份、抽取内部一份), 两份必然漂 —— 而漂掉的那份不会报错, 只会在
    # 另一个入口放行坏配置。
    tm_path = Path("config/test_methods.yaml")
    if tm_path.exists():
        try:
            from aterag.extract.assess import RuleBook
            from aterag.extract.quantity_aliases import QuantityAliasBook
            from aterag.extract.scenarios import ScenarioRules
            from aterag.extract.supplement import MethodBook

            mbook = MethodBook.load(tm_path)
            rules = RuleBook.load(str(tm_path))
            scen = ScenarioRules.load()
            aliases = QuantityAliasBook.load("config/quantity_aliases.yaml")
            if book is not None and pb_roles:
                validate_extraction_configs(pb, book, mbook, rules, scen, aliases)
                print(
                    f"  {PASS} 业界方法库: {len(mbook.methods)} 条方法 / "
                    f"{len(mbook.templates)} 个描述模板 / {len(rules.rules)} 条评估规则 / "
                    f"{len(scen.dimensions)} 个场景维度 / "
                    f"{len(aliases.facts)} 个事实别名"
                )
                print(
                    f"       交叉校验通过: kind ⊆ 词表({len(book.kinds)}), "
                    f"role ∈ 档案({len(pb_roles)})"
                )
            else:
                problems.append("test_methods.yaml 交叉校验跳过: 词表或档案角色未就绪")
        except Exception as e:  # noqa: BLE001
            print(f"  {FAIL} 业界方法库校验失败: {e}")
            problems.append(f"test_methods.yaml: {e}")
    else:
        problems.append("缺少 config/test_methods.yaml")

    # 注册表在 {data/}, 不在仓库根 —— d5ab36b 把它移进 data/ 时漏改这里,
    # 于是本脚本恒报「缺少 registry.yaml」并退出 1, 而 CI 第 32 行跑的就是它。
    reg_path = Path("data/registry.yaml")
    if reg_path.exists():
        reg = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
        prods = reg.get("products") or {}
        print(f"  {PASS} 注册表: {len(prods)} 个型号")
        for mid, p in prods.items():
            if not p.get("domain"):
                problems.append(f"data/registry.yaml: 型号 {mid} 缺 domain")
    else:
        problems.append("缺少 data/registry.yaml")

    for p in problems:
        print(f"  {FAIL} {p}")

    if bad or problems:
        print(f"\nCONFIG_VALIDATE FAIL ({len(bad)} 个解析错误 + {len(problems)} 个结构问题)")
        return 1
    print(f"\nCONFIG_VALIDATE PASS ({len(files)} 个文件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
