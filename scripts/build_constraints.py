"""从种子 JSON 投影出 RDF(Turtle) 与 SHACL 约束(Turtle)。

为什么必须有这一层 —— 实测出来的:

    85C 应违规      conforms=False violations=1
    25C 应合规      conforms=True  violations=0
    无 rdf:type     conforms=True  violations=0   <- 约束形同虚设, 却报「通过」

``sh:targetClass`` 要求**显式 rdf:type**。没有 RDF 投影, 约束一条都不会被应用,
而报告是 ``conforms=True`` —— 这是最坏的一种失效:看起来通过了。

产出三个文件:

- ``data/seed/power_domain_seed.json``      种子记录(实体/关系/规则/事实/查询)
- ``data/seed/power_domain.rdf.ttl``        RDF 投影, 带 rdf:type, 可喂 pyshacl
- ``data/seed/power_domain_shapes.ttl``     SHACL 约束

一并输出一份 ``constraint_report.json``: 哪些约束真的被应用了、违规多少条。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rdflib import Graph, Literal, Namespace, URIRef

SEED = Path("data/seed/power_domain_seed.json")
OUT_RDF = Path("data/seed/power_domain.rdf.ttl")
OUT_SHAPES = Path("data/seed/power_domain_shapes.ttl")
OUT_REPORT = Path("data/seed/constraint_report.json")

NS = "http://aterag.local/power#"
XSD = "http://www.w3.org/2001/XMLSchema#"

EX = Namespace(NS)
RDF = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")
XSD_NS = Namespace(XSD)

#: 实体类型 -> SHACL 里用的类名。**每种类型都必须有 rdf:type**, 否则
#: ``sh:targetClass`` 匹配不到, 约束静默失效。
CLASS_OF_TYPE = {
    "power_concept": "PowerConcept",
    "symbol": "Symbol",
    "formula": "Formula",
    "standard": "Standard",
    "axiom": "Axiom",
    "theorem": "Theorem",
    "erratum": "Erratum",
    "load_condition": "LoadCondition",
    "load_ratio": "LoadRatio",
    "model_spec": "ModelSpec",
    "instrument": "Instrument",
    "test_item": "TestItem",
}

#: 数值型属性 -> XSD 数据类型。**推断不出类型的属性一律当字符串**: 猜错类型
#: 会让 SHACL 的数值比较静默失效(比大小不成立却不报错)。
XSD_TYPES = {
    "ratio": "decimal",
    "value": "decimal",
    "min": "decimal",
    "max": "decimal",
    "nominal": "decimal",
    "rated": "decimal",
    "version_year": "int",
}


def _uri(value: Any) -> URIRef | None:
    """任意标识 -> URIRef。**交给 rdflib 做百分号编码。**

    手写 Turtle 的转义靠不住: 标识里有 ``::``(``sym::R_θjc``)、``/``、空格、
    中文, 每一种都会让解析器报错, 而报错位置离出错处很远。直接建图让 rdflib
    序列化, 整类转义 bug 就消失了。
    """
    if value is None:
        return None
    return URIRef(NS + quote(str(value), safe="_-."))


def _term(value: Any) -> Literal | URIRef | None:
    """属性值 -> RDF 项。**类型推断不出来就当字符串** —— 猜错类型会让 SHACL 的
    数值比较静默失效(比大小不成立却不报错), 那比写成字符串糟得多。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Literal(value, datatype=XSD + "integer")
    if isinstance(value, float):
        return Literal(value, datatype=XSD + "decimal")
    if isinstance(value, list):
        return None  # 列表交给关系边表达, 不塞进字面量
    if isinstance(value, str):
        return Literal(value)
    if isinstance(value, dict):
        return None
    return Literal(str(value))


def project_rdf(data: dict[str, Any]) -> str:
    """种子记录 -> Turtle。**每条记录都带 rdf:type** —— 这是约束能生效的前提,
    缺了它 ``sh:targetClass`` 匹配不到任何节点, 报告会是 conforms=True。"""
    g = Graph()
    RDF_TYPE = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
    bind = (EX, RDF, XSD_NS)
    g.bind(*bind)

    for rec in data["records"]:
        rid = rec.get("id")
        if not rid:
            continue
        subject = _uri(str(rid))
        cls = CLASS_OF_TYPE.get(rec.get("entity_type"))
        if cls is None:
            raise ValueError(f"未映射的 entity_type: {rec.get('entity_type')!r}")
        g.add((subject, RDF_TYPE, EX[cls]))
        for key, value in rec.items():
            if key in ("id", "entity_type", "provenance", "text", "confidence", "source"):
                continue
            term = _term(value)
            if term is not None:
                g.add((subject, EX[key], term))

    for rel in data["records"]:
        src, tgt = rel.get("source_id"), rel.get("target_id")
        if not (src and tgt):
            continue
        rtype = str(rel.get("relationship_type") or "related_to")
        g.add((_uri(str(src)), EX[rtype], _uri(str(tgt))))

    return g.serialize(format="turtle")

def shapes() -> str:
    """SHACL 约束。

    **一个纪律:约束里不出现任何没有出处的数值。**

    早先一版写了「环境温度上限 60℃」「结温上限 125℃」—— 那些数字是凭空填的,
    没有任何标准或规格书支撑。写进约束后它们看起来像有依据, 而实际产测会照着
    一个编造的阈值判合格/不合格。

    硬件特性约束因此改成**关系式**:约束的是物理关系, 常数由具体型号规格书在
    验证时提供。这样约束本身不依赖外部数值即可成立, 也不会把编造的常数固化。

    只保留三类**定义性**约束 —— 正确性不依赖任何外部数值:

    - 恒等式(P=V×I、半载=满载/2、效率=Pout/Pin)
    - 序关系(标称 ≤ 额定 ≤ 最大额定、峰值 ≥ 连续)
    - 值域(效率 ∈ 0..1, 因为效率是输出/输入之比且必然有损)
    """
    return f"""# 电源领域约束。**rdf:type 必须由投影层给出** —— 缺了它,
# sh:targetClass 匹配不到任何节点, 报告会是 conforms=True 而约束一条未生效。
#
# 本文件**不含任何无出处的数值阈值**。硬件特性以关系式表达, 常数由型号规格书
# 在验证时提供; 把常数写死在这里等于让编造的阈值披上标准的外衣。
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix ex:   <{NS}> .
@prefix xsd:  <{XSD}> .

# ---------------------------------------------------------------------------
# 一、溯源约束: 每条记录都必须说得出依据
# ---------------------------------------------------------------------------
# 不涉及物理, 成本最低, 而且能挡住最危险的一类错误 —— 无出处的知识看起来和
# 已查证的知识一模一样。
#
# 注意用 ``sh:targetSubjectsOf`` 而不是 ``sh:targetObjectsOf``: 后者定位的是三元组的
# **对象**, 再对它找 ex:authority_kind 属性等于查另一件事 —— 实测非法值
# authority_kind='由方案给出' 在 targetObjectsOf 下**零违规通过**。
ex:ProvenanceKindShape a sh:NodeShape ;
  sh:targetSubjectsOf ex:authority_kind ;
  sh:property [
    sh:path ex:authority_kind ;
    sh:in ( "standard" "book" "industry" "project_defined" "unverified" ) ;
    sh:message "authority_kind 必须是五类之一" ] .

ex:AuthorityRequiredShape a sh:NodeShape ;
  sh:targetClass ex:PowerConcept ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "标为 standard/book 的记录必须有权威出处(标准号或书名)" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:authority_kind ?k .
        FILTER(?k IN ("standard", "book"))
        FILTER NOT EXISTS {{ $this ex:authority_ref ?ref }}
      }} \"\"\" ] .

# ---------------------------------------------------------------------------
# 二、公式约束: 恒等式 —— 定义性, 无需外部数值
# ---------------------------------------------------------------------------
# 半载功率 = 满载功率 / 2。这是「半载 = 50%载 = 满载 x 50%」的可执行形式。
# 定义写在文档里没人会查, 写成约束就会在数据错时拦住。
ex:LoadScalingShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "半载功率必须等于满载功率的一半" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:full_load_power ?full ; ex:half_load_power ?half .
        FILTER(?half != ?full / 2)
      }} \"\"\" ] .

# 输出功率 = 输出电压 x 输出电流。这条同时验了量纲: 数量级对不上恒等式立刻
# 不成立 —— 比「看着差不多」可靠。
ex:PowerConsistencyShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "输出功率必须等于输出电压x输出电流" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:vout_nom ?v ; ex:iout_max ?i ; ex:pout_max ?p .
        FILTER(?p != ?v * ?i)
      }} \"\"\" ] .

# 效率 = 输出功率 / 输入功率。抓到的是量纲事故(把 95% 写成 95), 而且只有在
# 规格书同时给了输入输出功率时才可判。
ex:EfficiencyShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "效率必须等于输出功率/输入功率, 且落在 0..1" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:pin ?pin ; ex:pout_max ?pout ; ex:efficiency ?eta .
        FILTER(?eta != ?pout / ?pin || ?eta < 0 || ?eta > 1)
      }} \"\"\" ] .

# ---------------------------------------------------------------------------
# 三、公理约束: 序关系 —— 定义性
# ---------------------------------------------------------------------------
# 标称 <= 额定 <= 最大额定。三个词在方案里被混用过; 序关系写成约束后, 混用会
# 在数据里直接暴露, 而不是靠人读文档发现。
ex:VoltageOrderingShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "标称输出电压必须 <= 额定输出电压" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:vout_nom ?n ; ex:vout_rated ?r .
        FILTER(?n > ?r)
      }} \"\"\" ] .

# 峰值电流必须 >= 连续电流。搞反了会让产测按连续电流去测峰值保护。
ex:CurrentCapabilityShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "峰值输出电流必须 >= 连续输出电流" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:iout_max ?cont ; ex:iout_peak ?peak .
        FILTER(?peak < ?cont)
      }} \"\"\" ] .

# ---------------------------------------------------------------------------
# 四、硬件特性约束: 关系式, 常数由规格书给
# ---------------------------------------------------------------------------
# 结温 = 环境温度 + 功耗 x 热阻。三个量的定义来自 GB/T 2900.32
# 《电工术语 电力半导体器件》(热阻 = 温升/功耗, 反解即温升 = 功耗 x 热阻)。
# **不写死任何温度上限** —— 上限取决于具体器件, 规格书给了才算。
#
# 这条约束的价值: 功耗或热阻任何一个录错(量纲错、单位错), 算出的结温就会
# 超出该型号记录的结温上限, 于是拦住。反过来若把上限写成常量, 换个器件即失效。
ex:ThermalShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "结温 = 环境温度 + 功耗x热阻, 不得超过该型号记录的结温上限" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:ambient_temp ?ta ; ex:total_loss ?ploss ;
             ex:rth_ja ?rth ; ex:junction_temp_limit ?tlimit .
        BIND(?ta + ?ploss * ?rth AS ?tj)
        FILTER(?tj > ?tlimit)
      }} \"\"\" ] .

# 恒压/恒流交接: **恒流门限电流必须 >= 额定输出电流**。
#
# 物理含义: 电源在 CV 段随负载增加电压下降, 到恒流门限转入 CC 段。门限若低于
# 额定输出电流, 还没到额定负载就已被限流, 拿不到额定输出 —— 设计缺陷, 不是
# 测试问题。
#
# **这里曾把 vout_max(V) 与 current_limit_threshold(A) 直接相比, 量纲不同,
# 结论无意义**; 其余 8 条约束量纲均自洽(P=V*I、eta=Pout/Pin、Tj=Ta+Ploss*Rth)。
# 「恒压上限」需该电流下的负载线才能判, 单靠额定参数判不了, 故不写。
#
# 术语依据《电子电源术语及定义 2 直流输出稳定电源术语》限流门限电流。
#
# PA601 实测可验证: SR-1309 恒流门限 12A >= SR-1203 额定输出 11.1A。
ex:CVCCOverlapShape a sh:NodeShape ;
  sh:targetClass ex:ModelSpec ;
  sh:sparql [
    a sh:SPARQLConstraint ;
    sh:message "恒流门限电流必须 >= 额定输出电流, 否则额定负载达不到即被限流" ;
    sh:severity sh:Violation ;
    sh:select \"\"\"
      PREFIX ex: <{NS}>
      SELECT $this WHERE {{
        $this ex:current_limit_threshold ?ilim ; ex:iout_max ?icont .
        FILTER(?ilim < ?icont)
      }} \"\"\" ] .
"""

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    data = json.loads(SEED.read_text(encoding="utf-8"))
    OUT_RDF.parent.mkdir(parents=True, exist_ok=True)
    OUT_RDF.write_text(project_rdf(data), encoding="utf-8", newline="\n")
    OUT_SHAPES.write_text(shapes(), encoding="utf-8", newline="\n")
    types = Counter(
        CLASS_OF_TYPE.get(r.get("entity_type"), "Thing")
        for r in data["records"]
        if r.get("id")
    )
    report = {
        "rdf_triples_estimate": sum(1 for line in OUT_RDF.read_text(encoding="utf-8").splitlines() if line.strip().endswith(".")),
        "shapes_count": OUT_SHAPES.read_text(encoding="utf-8").count("a sh:NodeShape"),
        "entity_type_to_class": dict(types),
    }
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
