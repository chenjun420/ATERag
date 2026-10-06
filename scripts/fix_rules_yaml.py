"""修复领域规则 YAML: 给含 ': ' 的未加引号标量值统一加双引号."""

import re
from pathlib import Path

KEY_ALLOWLIST = {
    "rules",
    "id",
    "category",
    "scope",
    "derive",
    "output",
    "expr",
    "inputs",
    "variables",
    "source",
    "url",
    "retrieved",
    "confidence",
    "test",
    "given",
    "expect",
    "violation",
    "constraint",
    "shape",
}

LINE_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*): (.+)$")


def fix(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    out, n = [], 0
    for line in lines:
        m = LINE_RE.match(line)
        if m:
            indent, key, val = m.group(1), m.group(2), m.group(3).strip()
            if (
                ": " in val
                and not val.startswith(('"', "{", "[", "|", ">", "&", "*"))
                and key not in KEY_ALLOWLIST
                and not key.startswith("sh")
                and not key.startswith("ps")
            ):
                val = val.replace('"', '\\"')
                line = f'{indent}{key}: "{val}"'
                n += 1
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return n


if __name__ == "__main__":
    total = 0
    for p in [Path("domain_rules/power/rules.yaml")]:
        n = fix(p)
        print(f"{p}: fixed {n} lines")
        total += n
    import yaml

    yaml.safe_load(Path("domain_rules/power/rules.yaml").read_text(encoding="utf-8"))
    print("YAML_OK total_fixed =", total)
