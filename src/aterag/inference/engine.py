"""推理引擎: 规则驱动计算 (安全表达式) + SHACL 约束验证 + 决策溯源."""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

from aterag.inference.rules import (
    find_rule,
    formula_type_to_rule_id,
    load_domain_rules,
)

# 表达式环境: 仅暴露安全数学函数
_SAFE_ENV = {
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "sqrt": math.sqrt,
    "ceil": math.ceil,
    "floor": math.floor,
    "pow": pow,
    "log": math.log,
    "exp": math.exp,
    "pi": math.pi,
    "e": math.e,
}

_DENY_RE = re.compile(r"__|import|exec|eval|open|compile|globals|getattr|setattr")

# 输入名别名: 规则输出名 -> 规则入参名的桥接
_INPUT_ALIASES = {
    "output_power": "power",
    "pout": "power",
    "pin": "input_power",
    "loss_power": "loss",
    "i_rms": "ripple_current_rms",
}


@dataclass
class Derivation:
    rule_id: str
    statement: str
    inputs: dict
    output_name: str
    output_value: object
    domain_layer: str
    confidence: float | None  # None = 规则未标注可信度, 属未知, 不得默认顶格
    source: dict


@dataclass
class DecisionTrace:
    decision_id: str
    agent: str
    decision_type: str
    value: str
    rule_id: str
    premises: list[dict] = field(default_factory=list)
    confidence: float | None = None
    section_path: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class InferenceEngine:
    def __init__(self, settings, domain: str, model_facts: dict | None = None):
        """model_facts: 型号事实 (来自 PG 实体), 如 {'voltage': 54, 'current': 11.1}."""
        self.rules, self.shapes = load_domain_rules(settings.domain_rules_dir, domain)
        self.domain = domain
        self.model_facts = model_facts or {}
        self.traces: list[DecisionTrace] = []

    # ---------- 计算 ----------
    def calculate(self, formula_type: str, inputs: dict | None, rule_id: str | None = None) -> dict:
        rid = rule_id or formula_type_to_rule_id(formula_type)
        rule = find_rule(self.rules, rid, None)
        if rule is None:
            raise KeyError(f"rule not found: {rid} (type={formula_type})")
        derive = rule.get("derive") or {}
        expr = derive.get("expr")
        if not expr:
            raise ValueError(f"rule {rid} has no derive.expr")
        if _DENY_RE.search(expr):
            raise ValueError(f"rule {rid} expr contains forbidden tokens")

        given: dict = {}
        given.update(inputs or {})
        var_names = derive.get("inputs", [])
        missing = [v for v in var_names if v not in given]
        if missing:
            # 多步推导: 缺失输入尝试由其他规则递归派生 (Datalog 链式语义)
            for v in missing:
                derived = self._derive_input(v, depth=0, chain=set())
                given[v] = derived

        env = dict(_SAFE_ENV)
        env.update({k: given[k] for k in var_names})
        try:
            value = eval(expr, {"__builtins__": {}}, env)  # 受控规则表达式
        except Exception as e:
            raise ValueError(f"rule {rid} eval failed: {e}") from e

        derivation = Derivation(
            rule_id=rule["id"],
            statement=rule.get("statement", ""),
            inputs={k: given[k] for k in var_names},
            output_name=derive.get("output", "result"),
            output_value=value,
            domain_layer=rule.get("_domain", "common"),
            # 缺 confidence 时为 None (未知) —— 不得默认 1.0: 缺失是"未标注可信度",
            # 顶格等于凭空给出最高可信度证书, 方向反了
            confidence=rule.get("confidence"),
            source=rule.get("source", {}),
        )
        trace = DecisionTrace(
            decision_id=str(uuid.uuid4()),
            agent="mcp_calculate",
            decision_type=formula_type,
            value=repr(value),
            rule_id=rule["id"],
            premises=[
                {
                    "input": k,
                    "value": given[k],
                    "layer": self.domain if k in self.model_facts else "caller",
                }
                for k in var_names
            ],
            confidence=rule.get("confidence"),
        )
        self.traces.append(trace)
        return {
            "rule_id": rule["id"],
            "statement": rule.get("statement", ""),
            "output": derive.get("output", "result"),
            "value": value,
            "inputs": derivation.inputs,
            "domain_layer": derivation.domain_layer,
            "confidence": derivation.confidence,
            # 显式声明"可信度未知", 避免调用方把 None 当 0 或当缺字段忽略
            "confidence_unknown": derivation.confidence is None,
            "source": derivation.source,
            "decision_id": trace.decision_id,
        }

    def _derive_input(self, name: str, depth: int, chain: set[str]) -> object:
        """解析输入: 型号事实优先, 否则找产出该输出的规则递归推导 (含别名桥接)。"""
        if name in self.model_facts:
            return self.model_facts[name]
        lookup = _INPUT_ALIASES.get(name, name)
        if lookup != name and lookup in self.model_facts:
            return self.model_facts[lookup]
        if depth > 5 or name in chain:
            raise KeyError(f"cannot derive input {name!r} (depth={depth}, chain={sorted(chain)})")
        for rule in self.rules:
            d = rule.get("derive") or {}
            out_name = d.get("output")
            if out_name and (out_name == name or out_name == lookup) and d.get("expr"):
                sub = self._eval_rule(rule, {}, depth + 1, chain | {name})
                return sub["value"]
        raise KeyError(
            f"input {name!r} not in model facts and no rule derives it; "
            f"available rules: {[r['id'] for r in self.rules if (r.get('derive') or {}).get('output')]}"
        )

    def _eval_rule(self, rule: dict, inputs: dict, depth: int, chain: set[str]) -> dict:
        """calculate 的内部形态 (不记 trace, 用于链式推导)。"""
        derive = rule["derive"]
        given: dict = {}
        var_names = derive.get("inputs", [])
        for v in var_names:
            if v in inputs:
                given[v] = inputs[v]
            elif v in self.model_facts:
                given[v] = self.model_facts[v]
            else:
                given[v] = self._derive_input(v, depth, chain)
        env = dict(_SAFE_ENV)
        env.update({k: given[k] for k in var_names})
        value = eval(derive["expr"], {"__builtins__": {}}, env)
        return {"value": value, "inputs": given}

    # ---------- SHACL 约束验证 ----------
    def validate(self, data_graph_ttl: str) -> dict:
        from pyshacl import validate

        shapes_turtle = "\n".join(self.shapes)
        # pyshacl 返回三元组 (conforms, results_graph, results_text);
        # results_graph 是 rdflib Graph 对象, 本处只需文本报告, 故丢弃
        conforms, _results_graph, results_text = validate(
            data_graph=data_graph_ttl,
            shacl_graph=shapes_turtle,
            inference="rdfs",
        )
        trace = DecisionTrace(
            decision_id=str(uuid.uuid4()),
            agent="mcp_validate_constraints",
            decision_type="shacl",
            value="conforms" if conforms else "violations",
            rule_id=",".join(r.get("id", "") for r in self.rules if r.get("constraint")),
            premises=[{"shapes": len(self.shapes), "domain": self.domain}],
        )
        self.traces.append(trace)
        return {
            "conforms": bool(conforms),
            "report": results_text,
            "shapes_count": len(self.shapes),
            "decision_id": trace.decision_id,
        }

    # ---------- 溯源 ----------
    def explain(self, decision_id: str) -> dict:
        for t in self.traces:
            if t.decision_id == decision_id:
                return t.to_dict()
        raise KeyError(f"decision not found: {decision_id}")

    def all_traces(self) -> list[dict]:
        return [t.to_dict() for t in self.traces]
