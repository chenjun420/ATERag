"""反向测试: 故意注入违规数据, 确认约束**真的拦得住**。

必要性: ``conforms=True violations=0`` 有两种成因 —— 数据真的合规, 或约束
压根没被应用。后者在没有 rdf:type 时就会出现, 而且报告同样是 conforms=True。
不构造反例就无法区分这两者。
"""

from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef

from scripts.build_constraints import CLASS_OF_TYPE, NS

EX = Namespace(NS)
XSD = "http://www.w3.org/2001/XMLSchema#"
SHAPES = Path("data/seed/power_domain_shapes.ttl").read_text(encoding="utf-8")


def build(extra: list[tuple[str, str, object]]) -> str:
    g = Graph()
    g.bind("ex", EX)
    rdf_type = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
    for rid, cls, props in extra:
        s = URIRef(NS + rid)
        g.add((s, rdf_type, EX[cls]))
        for k, v in props:
            g.add((s, EX[k], Literal(v)))
    return g.serialize(format="turtle")


def run(label: str, ttl: str, expect_min: int) -> bool:
    from semantica.ontology.ontology_validator import run_shacl_validation

    rep = run_shacl_validation(ttl, SHAPES)
    got = len(rep.violations)
    ok = got >= expect_min
    print(f"  {label:<44} violations={got:<3} 期望>={expect_min:<3} {'PASS' if ok else '**FAIL**'}")
    for v in rep.violations[:2]:
        print(f"        -> {str(getattr(v, 'message', v))[:62]}")
    return ok


results = []

print("=== 溯源约束: authority_kind 非法值必须被拦 ===")
results.append(run(
    "注入 authority_kind='由方案给出'",
    build([("t1", "PowerConcept", [("authority_kind", "由方案给出")])]),
    1,
))

print("\n=== 溯源约束: 标为 standard 却无权威出处 ===")
results.append(run(
    "authority_kind=standard 但无 authority_ref",
    build([("t2", "PowerConcept", [("authority_kind", "standard")])]),
    1,
))

print("\n=== 公式约束: 半载功率 != 满载功率/2 ===")
results.append(run(
    "满载 1000W, 半载 600W",
    build([("m1", "ModelSpec", [("full_load_power", 1000), ("half_load_power", 600)])]),
    1,
))

print("\n=== 公式约束: 输出功率 != 电压×电流 ===")
results.append(run(
    "12V x 10A = 120W, 却写 150W",
    build([("m2", "ModelSpec", [("vout_nom", 12), ("iout_max", 10), ("pout_max", 150)])]),
    1,
))

print("\n=== 公理约束: 标称 > 额定 ===")
results.append(run(
    "标称 24V > 额定 12V",
    build([("m3", "ModelSpec", [("vout_nom", 24), ("vout_rated", 12)])]),
    1,
))

print("\n=== 公理约束: 峰值 < 连续 ===")
results.append(run(
    "连续 10A, 峰值 8A",
    build([("m4", "ModelSpec", [("iout_max", 10), ("iout_peak", 8)])]),
    1,
))

print("\n=== 硬件约束: 结温超上限 ===")
results.append(run(
    "85C + 100W x 0.5C/W = 135C > 125C",
    build([("m5", "ModelSpec", [("ambient_temp", 85), ("total_loss", 100),
                                ("rth_ja", 0.5), ("junction_temp_limit", 125)])]),
    1,
))

print("\n=== 硬件约束: 恒压恒流交叠区为空 ===")
results.append(run(
    "恒压上限 10V < 恒流门限 12V",
    build([("m6", "ModelSpec", [("vout_max", 10), ("current_limit_threshold", 12)])]),
    1,
))

print("\n=== 效率量纲事故 ===")
results.append(run(
    "效率写成 95 (应为 0.95)",
    build([("m7", "ModelSpec", [("pin", 200), ("pout_max", 190), ("efficiency", 95)])]),
    1,
))

print("\n=== 负例: 数据真的合规时不应报违规 ===")
results.append(run(
    "12V x 10A = 120W, 半载 60W",
    build([("m8", "ModelSpec", [("vout_nom", 12), ("iout_max", 10), ("pout_max", 120),
                                ("full_load_power", 120), ("half_load_power", 60),
                                ("vout_rated", 12), ("iout_peak", 20)])]),
    0,
))

print(f"\n通过 {sum(results)}/{len(results)}")
raise SystemExit(0 if all(results) else 1)