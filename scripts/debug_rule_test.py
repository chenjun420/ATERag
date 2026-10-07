"""调试单条 SHACL 规则的 test 块: python scripts/debug_rule_test.py <rule_id>"""

from __future__ import annotations

import os
import sys

import yaml

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

os.environ.setdefault("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x")
os.environ.setdefault("LLM_BASE", "http://x/v1")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBED_BASE", "http://x")
os.environ.setdefault("EMBED_MODEL", "x")

from rules_selftest import build_ttl

rid = sys.argv[1]
for f in ("domain_rules/power/rules.yaml",):
    data = yaml.safe_load(open(f, encoding="utf-8").read()) or {}
    for r in data.get("rules", []):
        if r["id"] == rid:
            ttl = build_ttl(rid, r.get("scope", ""), r["test"]["given"])
            print("--- data ttl ---")
            print(ttl)
            print("--- shape ---")
            print(r["constraint"]["shape"])
            from pyshacl import validate

            conforms, g, txt = validate(
                data_graph=ttl, shacl_graph=r["constraint"]["shape"], inference="rdfs"
            )
            print("conforms:", conforms)
            print(txt[:2000])
            raise SystemExit(0)
raise SystemExit(f"rule not found: {rid}")
