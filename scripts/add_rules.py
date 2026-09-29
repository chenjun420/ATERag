"""追加 K-FIX-008 (探针间距) 与 K-COV-001 (强制需求覆盖) 到 power 规则."""
from pathlib import Path

import yaml

EXTRA = '''

  - id: K-FIX-008
    statement: "探针排布间距约束: 相邻探针中心间距不小于探针外径的2倍, 防止插拔干涉与短路"
    category: fixture
    scope: probe
    constraint:
      shape: |
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix ps: <https://aterag.example.org/power#> .
        ps:ProbeSpacingShape a sh:NodeShape ;
            sh:targetClass ps:Probe ;
            sh:sparql [
                sh:message "探针 {?this} 间距 {?spacing} 小于外径 {?dia} 的2倍" ;
                sh:select """
                    SELECT $this ?spacing ?dia WHERE {
                        $this ps:diameter ?dia .
                        $this ps:neighborSpacing ?spacing .
                        FILTER (?spacing < 2 * ?dia)
                    }
                """ ;
            ] .
    source:
      name: "工装探针排布惯例 + 规格方案 §5.3.3"
      url: https://fixturfab.com/shop/components/pogo-pins
      retrieved: 2026-09-29
    confidence: 0.9
    test: {given: {diameter: 2.5, spacing: 3}, expect: {violation: true}}

  - id: K-COV-001
    statement: "强制等级(强制)需求必须具备测试用例覆盖 (测试覆盖率约束)"
    category: coverage
    scope: universal-in-power
    constraint:
      shape: |
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        @prefix ps: <https://aterag.example.org/power#> .
        ps:CoverageShape a sh:NodeShape ;
            sh:targetClass ps:Requirement ;
            sh:sparql [
                sh:message "强制需求 {?this} 缺少测试用例覆盖" ;
                sh:select """
                    SELECT $this WHERE {
                        $this ps:priority '强制' .
                        FILTER NOT EXISTS { $this ps:hasTestCase ?tc . }
                    }
                """ ;
            ] .
    source:
      name: "规格方案 §5.3.4 强制需求测试覆盖约束"
      url: local://spec-plan-5.3.4
      retrieved: 2026-09-29
    confidence: 1.0
    test: {given: {priority: "强制", has_test_case: false}, expect: {violation: true}}
'''

path = Path("domain_rules/power/rules.yaml")
text = path.read_text(encoding="utf-8").rstrip()
path.write_text(text + EXTRA + "\n", encoding="utf-8")
d = yaml.safe_load(path.read_text(encoding="utf-8"))
print("RULES_OK count=", len(d["rules"]))
