"""The committed dashboard and alert rules only query metrics the API exports."""

from __future__ import annotations

import json
import re

import yaml
from prometheus_client import REGISTRY

import lexai.core.metrics  # noqa: F401  (registers the metrics)
from lexai.core.settings import REPO_ROOT


def exported() -> set[str]:
    names: set[str] = set()
    suffixes = {"counter": ("_total", "_created"), "histogram": ("_bucket", "_count", "_sum", "_created")}
    for metric in REGISTRY.collect():
        if metric.name.startswith("lexai_"):
            names.add(metric.name)
            names.update(metric.name + s for s in suffixes.get(metric.type, ()))
    return names


def referenced(expr: str) -> set[str]:
    return set(re.findall(r"\blexai_[a-z_]+\b", expr))


def test_dashboard_queries_known_metrics() -> None:
    dash = json.loads((REPO_ROOT / "deploy" / "grafana" / "lexai.json").read_text(encoding="utf-8"))
    names = exported()
    exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
    assert len(exprs) >= 15
    for expr in exprs:
        missing = referenced(expr) - names
        assert not missing, f"dashboard queries unknown metrics {missing}: {expr}"
    assert len({p["id"] for p in dash["panels"]}) == len(dash["panels"])


def test_alert_rules_query_known_metrics() -> None:
    rules = yaml.safe_load((REPO_ROOT / "deploy" / "prometheus" / "alerts.yml").read_text(encoding="utf-8"))
    alerts = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    assert {"LexAINotReady", "LexAISlowFirstToken", "LexAIHighErrorRate"} <= set(alerts)
    assert alerts["LexAINotReady"]["for"] == "2m"
    assert "> 0.02" in alerts["LexAIHighErrorRate"]["expr"]
    names = exported()
    for name, rule in alerts.items():
        missing = referenced(rule["expr"]) - names
        assert not missing, f"alert {name} queries unknown metrics {missing}"
