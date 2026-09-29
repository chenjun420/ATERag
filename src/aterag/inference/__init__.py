"""推理层: 分域规则装配的计算/验证/溯源."""
from aterag.inference.engine import InferenceEngine
from aterag.inference.rules import load_domain_rules

__all__ = ["InferenceEngine", "load_domain_rules"]
