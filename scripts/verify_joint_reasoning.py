"""通用知识库 + PA601 型号知识库 的联合推理验证。

**要证明什么**
------------
1. 领域侧的 shape 能在**型号侧真实数据**上判定, 且判定结果正确
   (PA601: 恒流门限 12.0A >= 额定输出 11.1A -> conform, 正是 shapes 文件里
   自己写明「PA601 实测可验证」的那一条);
2. shape **不是恒真** —— 故意把门槛改到 10.0A 必须报出违规。这一步是必须的:
   shapes 文件开头就警告过「缺 rdf:type 时 sh:targetClass 匹配不到任何节点,
   报告会是 conforms=True 而约束一条未生效」, 所以「conforms」本身不能当
   证据, 「该违规时确实违规」才是;
3. 领域侧的公式能在型号侧真实数值上求值, 且量纲自洽 (P=V*I 用 额定输出电压
   x 输出电流);
4. Datalog 规则能拿型号侧的满载值推出别的负载点 (load-scaling);
5. 推理结论**可追溯** —— 能说出它用了哪些型号事实与哪条领域规则。

**明确不做的事**
----------------
不把「型号侧数值」硬凑成 shape 需要的属性名去骗过约束。有一处
(PowerConsistencyShape 用 600W vs -54V x 11.1A = -599.4W) 会报违规, 但那是
**映射与取整的产物** —— 规格书写的 600W 是「176~286Vac 时的上限」且是圆整值,
而 -54V 是负轨, 乘出来必然是负数。那种「违规」不是缺陷, 是我把两个不同语义
的量硬接在一起的结果, 所以本脚本把它单列并标注, 不混进「shape 正常工作」的
结论里。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "src")

OUT: list[str] = []


def say(m: str = "") -> None:
    OUT.append(m)


# DSN 从环境读: 硬编码口令曾经把生产 PG 密码写进仓库 —— 凭据属于环境,
# 不属于源码。本地跑: $env:POSTGRES_DSN='...'; 缺省回落到 .env (get_settings)。
from aterag.config import get_settings  # noqa: E402

DSN = os.environ.get("POSTGRES_DSN") or get_settings().postgres_dsn
SHAPES = "data/seed/power_domain_shapes.ttl"

# =====================================================================
# 第 0 步: 从板卡 PG 读 PA601 真实值 (型号知识库)
# =====================================================================
import psycopg  # noqa: E402

VALUES: dict[str, float] = {}
SOURCES: dict[str, str] = {}
NOTES: dict[str, str] = {}

with psycopg.connect(DSN) as c, c.cursor() as cur:
    cur.execute(
        "SELECT eid, props->>'title', props->>'min', props->>'max', props->>'rail', "
        "       props->>'unit', props->>'notes', props->>'req_id' "
        "FROM public.aterag_entities WHERE model_id = 'PA601-D54A' "
        "  AND etype IN ('Requirement','Protection')"
    )
    for eid, title, mn, mx, rail, unit, notes, req in cur.fetchall():
        VALUES[eid] = None  # type: ignore[assignment]
        SOURCES[eid] = f"spec:PA601-D54A {req}"
        NOTES[eid] = str(notes or "")

    def one(eid: str, col: str) -> float:
        """按**显式指定的列**取值。

        刻意不用「min 优先, 空了取 max」这种启发式: 实测 SR-1204(输出功率)是
        ``min=0.0 / typ=600.0``, 启发式会取到 0.0 —— 而 0 是「不适用」的占位,
        不是数量值。启发式取数出的错会一路传到除法里变成 ZeroDivisionError,
        或者更糟: 取到别的数而不报错。所以列在这里写死。
        """
        assert col in ("min", "typ", "max"), col
        cur.execute("SELECT props->%s::text, props->>'min', props->>'typ', "
                    "props->>'max' FROM public.aterag_entities WHERE eid = %s",
                    (col, eid))
        val, mn, ty, mx = cur.fetchone()
        if val is None:
            raise ValueError(f"{eid} 的 {col} 为空 (min={mn} typ={ty} max={mx})")
        return float(val)

    # 显式点名每一条 + 取哪一列。不靠标题模糊匹配 —— 标题会变, id 不会。
    PICK = {
        # prop: (eid, 列)
        "vout_nom": ("SR-PA601-D54A-1200@-54V", "min"),          # 额定输出电压 -54V
        "iout_max": ("SR-PA601-D54A-1203@-54V", "max"),           # 输出电流 max 11.1A
        "current_limit_threshold": ("SR-PA601-D54A-1309@-54V", "min"),  # 过流保护 min 12.0A
        "pout_max": ("SR-PA601-D54A-1204@W", "max"),              # 输出功率 max 600W
        "efficiency_pct": ("SR-PA601-D54A-1210@%#3", "min"),      # 整机效率 min 93%
        "ovp_static_min": ("SR-PA601-D54A-1310@-54V", "min"),     # 静态过压 min 58V
        "ovp_dynamic_min": ("SR-PA601-D54A-1311@-54V", "min"),    # 动态过压 min 59V
    }
    for prop, (eid, col) in PICK.items():
        VALUES[prop] = one(eid, col)
        SOURCES[prop] = SOURCES.get(eid, eid)

say("=== 0. 型号知识库(板卡 PG)读到的 PA601 事实 ===")
for k, v in VALUES.items():
    if v is not None:
        say(f"  {k:26s} = {v:<10} <- {SOURCES.get(k, '')}")

# =====================================================================
# 第 1 步: SHACL —— 领域约束 x 型号数据
# =====================================================================
from pyshacl import validate  # noqa: E402

say("")
say("=== 1. SHACL: 领域 shape 在型号真实数据上判定 ===")

PREFIXES = """
@prefix ex: <http://aterag.local/power#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def build_instance(threshold: float, efficiency: float | None) -> str:
    """按 shapes 需要的属性名构造一个 ex:ModelSpec 实例 (Turtle 文本)。

    **用 rdflib 构图再序列化**, 不手拼字符串: 手写的 Turtle 一旦少个分号就是
    BadSyntax, 而那种失败会被误读成「约束没生效」—— 恰好是 shapes 文件开头
    警告的那个陷阱。构图则保证语法合法, 报出来的只可能是约束本身的问题。

    阈值可调 —— 负向对照要用。``efficiency`` 为 None 时不给该属性, 于是
    EfficiencyShape 因前提不齐而不判(SHACL 语义: SPARQL 约束里任一模式不
    匹配即不违反)。
    """
    from rdflib import RDF, Graph, Literal, Namespace, URIRef

    EX = Namespace("http://aterag.local/power#")
    g = Graph()
    spec = URIRef("http://aterag.local/power#PA601-D54A")
    g.add((spec, RDF.type, EX.ModelSpec))
    g.add((spec, EX.model_id, Literal("PA601-D54A")))
    g.add((spec, EX.vout_nom, Literal(VALUES["vout_nom"])))
    g.add((spec, EX.iout_max, Literal(VALUES["iout_max"])))
    g.add((spec, EX.current_limit_threshold, Literal(threshold)))
    g.add((spec, EX.pout_max, Literal(VALUES["pout_max"])))
    if efficiency is not None:
        g.add((spec, EX.efficiency, Literal(efficiency)))
    return g.serialize(format="turtle")


def run_shacl(data_ttl: str, label: str) -> tuple[bool, list[str]]:
    """跑 pyshacl 并把违规消息**从结果图里取出来**。

    不去解析文本报告 —— 文本格式是给人看的, 版本间会变; 结果图里的
    ``sh:resultMessage`` / ``sh:sourceConstraintComponent`` 才是契约。
    """
    from rdflib import RDF, Namespace

    SH = Namespace("http://www.w3.org/ns/shacl#")
    conforms, g, _text = validate(
        data_graph=data_ttl,
        shacl_graph=Path(SHAPES).read_text(encoding="utf-8"),
        shacl_files=None,
        ont_graph=None,
        inference="none",
        abort_on_first=False,
        meta_shacl=False,
        debug=False,
    )
    msgs: list[str] = []
    for res in g.subjects(RDF.type, SH.ValidationResult):
        parts = []
        for pred, label_ in ((SH.resultMessage, "Message"),
                             (SH.sourceConstraintComponent, "Component"),
                             (SH.focusNode, "Focus")):
            for o in g.objects(res, pred):
                parts.append(f"{label_}={str(o).split('#')[-1]}")
        msgs.append("; ".join(parts))
    say(f"  [{label}] conforms={conforms}  违规 {len(msgs)} 条")
    for m in msgs:
        say(f"      {m}")
    return conforms, msgs


# 1a. 正向: 真实数据
real = build_instance(VALUES["current_limit_threshold"], None)
say(f"  实例三元组 (真实值, CVCC: 门限 {VALUES['current_limit_threshold']} "
    f"vs 额定输出 {VALUES['iout_max']}):")
for line in real.splitlines():
    if line.strip() and not line.startswith("@prefix"):
        say(f"    {line}")
ok_real, _ = run_shacl(real, "1a 真实数据")

# 1b. 负向对照: 把门槛改到 10.0 -> 必须报违规
broken = build_instance(10.0, None)
ok_broken, msgs_broken = run_shacl(broken, "1b 故意改坏(门槛 10.0 < 额定 11.1)")

say("")
say("  判定:")
say(f"    真实数据 conforms            = {ok_real}   "
    f"{'符合 (12.0 >= 11.1, 与 shapes 文件的预测一致)' if ok_real else '**不符合**'}")
say(f"    改坏后 conforms             = {ok_broken}  "
    f"{'**仍然符合 -> 该 shape 是恒真的, 验证无效**' if ok_broken else '报出违规 -> 门禁确实咬人'}")

# 1c. EfficiencyShape: 真实数据是百分数, shape 要求 0..1
say("")
say("  EfficiencyShape: shape 要求 eta = Pout/Pin 且落在 0..1")
say(f"    PA601 记录的是百分数: {VALUES['efficiency_pct']}% "
    f"(SR-1210, 额定输入/最大输出负载)")
eff_bad = build_instance(VALUES["current_limit_threshold"], VALUES["efficiency_pct"])
ok_eff, msgs_eff = run_shacl(eff_bad, "1c 效率按百分数喂进去(93), 但未给 ex:pin")
say("    注意: EfficiencyShape 需要 ex:pin + ex:pout_max + ex:efficiency 三个量齐全,")
say("          而上面这个实例**没有 ex:pin**(PA601 的 SR-1604 只给了条件式判据")
say("          「功率<50W不要求精度; 50W<=功率<100W 精度±10W; >=100W 精度±5%」,")
say("          没有单一数值), 所以该 shape **根本没判** —— 不能拿它证明量纲检查有效。")
say(f"    conforms={ok_eff}, 违规里是否含 EfficiencyShape: "
    f"{'含' if any('效率' in m for m in msgs_eff) else '**不含(该 shape 未参与判定)**'}")

# 单独验证 EfficiencyShape 会咬人: 构造一个领域侧的自足样例
say("")
say("  单独验证 EfficiencyShape 本身会咬人(领域侧自足样例, 非 PA601 数据):")
from rdflib import RDF, Graph, Literal, Namespace, URIRef  # noqa: E402

EX2 = Namespace("http://aterag.local/power#")
g3 = Graph()
s3 = URIRef("http://aterag.local/power#SYNTH")
g3.add((s3, RDF.type, EX2.ModelSpec))
g3.add((s3, EX2.pin, Literal(650)))        # 输入功率 650W
g3.add((s3, EX2.pout_max, Literal(600)))   # 输出功率 600W
g3.add((s3, EX2.efficiency, Literal(93)))  # 效率写成 93 (百分数没除 100)
run_shacl(g3.serialize(format="turtle"), "效率写成 93 而非 0.92")

# 1d. 序关系: 动态过压 > 静态过压 (VoltgeOrderingShape 的同类)
say("")
say("  保护点序关系(领域公理: 动态过压门限必须高于静态):")
say(f"    静态过压 min = {VALUES['ovp_static_min']} V (SR-1310)")
say(f"    动态过压 min = {VALUES['ovp_dynamic_min']} V (SR-1311)")
say(f"    判定: {VALUES['ovp_dynamic_min']} > {VALUES['ovp_static_min']} -> "
    f"{'成立' if VALUES['ovp_dynamic_min'] > VALUES['ovp_static_min'] else '**不成立**'}")

# =====================================================================
# 第 2 步: 公式求值 —— 领域公式 x 型号数值
# =====================================================================
say("")
say("=== 2. 领域公式在型号真实数值上求值 ===")
import yaml  # noqa: E402

rules = (yaml.safe_load(Path("domain_rules/power/rules.yaml").read_text(encoding="utf-8"))
         or {}).get("rules") or []
k_e1 = next((r for r in rules if r.get("id") == "K-ELEC-001"), None)
say(f"  取规则 {k_e1.get('id') if k_e1 else '?'}: "
    f"{(k_e1 or {}).get('statement')!r}")
say(f"    derive: {(k_e1 or {}).get('derive')}")

v, i = VALUES["vout_nom"], VALUES["iout_max"]
p = v * i
say("  求值 power = voltage * current")
say(f"    voltage  = {v} V      (SR-1200@-54V 额定输出电压)")
say(f"    current  = {i} A      (SR-1203@-54V 输出电流 max)")
say(f"    power    = {v} x {i} = {p} W")
say(f"    规格书给的 输出功率 max = {VALUES['pout_max']} W (SR-1204, "
    f"备注 176~286Vac 时为 600W)")
say(f"    量纲自洽: |{p}| = {abs(p)} W 与 600 W 同量级 -> "
    f"{'量纲对' if 0.5 < abs(p) / VALUES['pout_max'] < 2 else '**量纲可疑**'}")
say("")
say("  **单列为映射产物, 不算 shape 违规**: PowerConsistencyShape 要求")
say("  ?pout = ?vout_nom * ?iout_max 精确相等, 而 600 != -599.4。原因有二:")
say("    (a) 600W 是规格书的**取整上限**且只在 176~286Vac 成立, 不是 V*I 的积;")
say("    (b) -54V 是负轨, 与正的额定电流相乘必为负 —— 该 shape 隐含「轨为正」,")
say("        对负轨型号不适用。把这两点当「shape 抓到了真缺陷」是误报。")

# =====================================================================
# 第 3 步: Datalog —— 领域规则 x 型号事实
# =====================================================================
say("")
say("=== 3. Datalog: 领域规则在型号事实上推导 ===")
seed = json.loads(Path("data/seed/power_domain_seed.json").read_text(encoding="utf-8"))
say(f"  领域规则 {len(seed.get('rules') or [])} 条, 事实 {len(seed.get('facts') or [])} 条")
for r in seed.get("rules") or []:
    say(f"    RULE {r['rule_id']}: {r['rule_str']}")

from semantica.reasoning.datalog_reasoner import DatalogReasoner  # noqa: E402

dr = DatalogReasoner()
for f in seed.get("facts") or []:
    dr.add_fact(f["fact_str"])
for r in seed.get("rules") or []:
    dr.add_rule(r["rule_str"])
# 型号侧的满载输出功率, 作为领域规则的前提
dr.add_fact(f"value_at_full_load(pout_max, {VALUES['pout_max']})")
dr.add_fact("load_alias(50pct_load, half_load)")

# derive_all() 返回的是**全部**事实(不是仅新增), 所以新增量要用差集算。
# query 的变量语法是 ?X(不是 _), 返回 [{"X": "v"}] 形态。
n_before = len(set(dr.derive_all()))
dr.add_fact(f"value_at_full_load(pout_max, {VALUES['pout_max']})")
dr.add_fact("load_alias(50pct_load, half_load)")
after_facts = set(dr.derive_all())

say(f"  加入型号事实: value_at_full_load(pout_max, {VALUES['pout_max']})  <- SR-1204")
say(f"  fixpoint 后事实总数 {len(after_facts)} 条")
say("  查询 value_at_load(pout_max, ?load, ?value, ?ratio):")
rows = dr.query("value_at_load(pout_max, ?load, ?value, ?ratio)")
for r in rows:
    say(f"    {r}")

half = VALUES["pout_max"] / 2
say(f"  规则产出的四元组: {rows}")
say("")
say("  注意第 3 位是**满载基准值**, 不是该工况下的值 —— 这是设计, 不是缺陷:")
say("    build_seed_data.py:95-98 写明「引擎是纯合一, 没有算术……规则只推出")
say("    (量, 工况, 满载基准值, 比例) 四元组, **乘法交给消费方** —— 那样乘法")
say("    步骤本身才是可审计的, 而不是一个藏在引擎里、无法复核的中间值」。")
say("    下面由 aterag.reasoning.load_scaling 补上这个消费方。")

from aterag.reasoning.load_scaling import scale_bindings  # noqa: E402

scaled = scale_bindings(rows, quantity="pout_max")
say("")
say("  消费方(aterag.reasoning.scale_bindings)算出的值:")
for s in scaled:
    say(f"    {s.as_fact()}")

got = [s for s in scaled if s.value == half]
say("")
say(f"  人工核对: 满载 {VALUES['pout_max']}W x 0.5 = {half}W -> "
    f"{'与消费方结果一致' if got else '**不一致**'}")

# 让 SHACL 独立判定消费方的结果 —— 判对判错不归本模块, 归 shape
from rdflib import RDF, Graph, Literal, Namespace, URIRef  # noqa: E402

EX = Namespace("http://aterag.local/power#")


def _spec(name: str, full: float, halfv: float) -> str:
    g = Graph()
    s = URIRef(f"http://aterag.local/power#{name}")
    g.add((s, RDF.type, EX.ModelSpec))
    g.add((s, EX.full_load_power, Literal(full)))
    g.add((s, EX.half_load_power, Literal(halfv)))
    return g.serialize(format="turtle")


say("")
say("  让 SHACL 独立判定 (LoadScalingShape: 半载功率 = 满载功率 / 2):")
ok_scaled, _ = run_shacl(_spec("PA601-D54A-scaled", VALUES["pout_max"], half),
                         "消费方算出的半载功率")
say(f"    -> conforms={ok_scaled}  "
    f"{'**通过: 消费方的乘法与领域约束一致**' if ok_scaled else '**仍违规**'}")

say("")
say("  对照组: 把规则的原始输出(未缩放的满载值)喂给同一个 shape:")
ok_raw, _ = run_shacl(_spec("PA601-D54A-unscaled", VALUES["pout_max"], VALUES["pout_max"]),
                      "未缩放的值")
say(f"    -> conforms={ok_raw}  "
    f"{'通过(意外)' if ok_raw else '报违规 -> shape 确实在判缩放, 消费方的乘法是必要的'}")

# 别名归一: 50pct_load 应被规则解析成 half_load, 两者比例相同
rows2 = dr.query("load_ratio(?alias, ?ratio)")
say("")
say(f"  别名归一(这部分是对的): 50pct_load 经 load-alias 解析到 half_load, "
    f"比例 {[(r.get('alias'), r.get('ratio')) for r in rows2]}")

# =====================================================================
# 第 4 步: 谱系 —— 推理结论能不能说出出处
# =====================================================================
say("")
say("=== 4. 谱系: 推理用到的每个量的出处是否可查 ===")
from aterag.provenance.seed_loader import build_manager  # noqa: E402

mgr = build_manager(DSN)
for prop in ("vout_nom", "iout_max", "current_limit_threshold", "pout_max"):
    src = SOURCES.get(prop, "")
    say(f"  {prop:26s} <- {src}")
say("")
say("  领域侧同一批概念的谱系(板卡 l0_term.provenance):")
# OCP_PROTECTION 已于 2026-10-07 移除 —— 库里存在同名的 PROT_OCP, 两条都叫
# 「输出过流保护」, 而 credibility 分级 unverified 0.2 恒低于 counterpart,
# 于是这条赢不了冲突消解却仍能被检索命中(两个可改入口, 改错的不报错)。
for cid in ("PROT_OCP", "HYSTERESIS"):
    e = mgr.get_provenance(cid)
    if e is None:
        say(f"    {cid}: <无谱系>")
        continue
    say(f"    {cid}: source={e.get('source_document')!r} "
        f"credibility={e.get('credibility')} authority={e.get('metadata', {}).get('authority_kind')!r}")
chain = mgr.verify_chain()
say(f"    谱系链 valid={chain['valid']} total={chain['total_entries']} "
    f"broken={len(chain['broken_links'])}")

Path(r"C:\Users\chenp\AppData\Local\Temp\opencode\joint_reasoning.txt").write_text(
    "\n".join(OUT), encoding="utf-8"
)
print("\n".join(OUT))
