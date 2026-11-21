#!/usr/bin/env python3
"""
CI checks of the running Compose stack's observability (stdlib only):

    python3 deploy/ci/check_stack.py prometheus --replicas 2
    python3 deploy/ci/check_stack.py grafana --password ...
    python3 deploy/ci/check_stack.py langfuse --public-key pk --secret-key sk

Each check retries for up to --timeout seconds (scrapes and trace ingestion are asynchronous).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any


def get(url: str, auth: tuple[str, str] | None = None) -> Any:
    request = urllib.request.Request(url)  # noqa: S310 - fixed http://localhost URLs
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read())


def retry(name: str, check: Callable[[], tuple[bool, str]], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    detail = ""
    while time.monotonic() < deadline:
        try:
            ok, detail = check()
            if ok:
                print(f"[PASS] {name}: {detail}", flush=True)
                return True
        except Exception as exc:  # service still starting
            detail = repr(exc)
        time.sleep(5)
    print(f"[FAIL] {name}: {detail}", flush=True)
    return False


def prometheus(base: str, replicas: int, timeout: float) -> bool:
    def query(expr: str) -> list[dict[str, Any]]:
        out = get(f"{base}/api/v1/query?query={urllib.parse.quote(expr)}")
        return list(out["data"]["result"])

    def targets() -> tuple[bool, str]:
        up = query('up{job="lexai-api"}')
        healthy = [r for r in up if r["value"][1] == "1"]
        return len(healthy) == replicas, f"{len(healthy)}/{len(up)} API targets up (want {replicas})"

    def ready() -> tuple[bool, str]:
        values = [r["value"][1] for r in query("lexai_ready")]
        return bool(values) and all(v == "1" for v in values), f"lexai_ready = {values}"

    def requests() -> tuple[bool, str]:
        total = sum(float(r["value"][1]) for r in query("sum(lexai_http_requests_total)"))
        return total > 0, f"{total:.0f} requests recorded"

    def rules() -> tuple[bool, str]:
        groups = get(f"{base}/api/v1/rules")["data"]["groups"]
        names = sorted(r["name"] for g in groups for r in g["rules"])
        return "LexAINotReady" in names, f"{len(names)} alert rules loaded"

    return all([
        retry("prometheus targets", targets, timeout),
        retry("prometheus readiness metric", ready, timeout),
        retry("prometheus request metrics", requests, timeout),
        retry("prometheus alert rules", rules, timeout),
    ])


def grafana(base: str, password: str, timeout: float) -> bool:
    auth = ("admin", password)

    def dashboard() -> tuple[bool, str]:
        found = get(f"{base}/api/search?query=LexAI", auth)
        titles = [d["title"] for d in found if d.get("type") == "dash-db"]
        return bool(titles), f"dashboards {titles}"

    def datasource() -> tuple[bool, str]:
        health = get(f"{base}/api/datasources/uid/prometheus/health", auth)
        return health.get("status") == "OK", str(health.get("message"))

    def panel_data() -> tuple[bool, str]:
        # Query Prometheus through Grafana, as the dashboard does.
        out = get(f"{base}/api/datasources/proxy/uid/prometheus/api/v1/query?query=lexai_ready", auth)
        return bool(out["data"]["result"]), f"{len(out['data']['result'])} series via Grafana"

    return all([
        retry("grafana dashboard provisioned", dashboard, timeout),
        retry("grafana data source healthy", datasource, timeout),
        retry("grafana sees LexAI metrics", panel_data, timeout),
    ])


def langfuse(base: str, public_key: str, secret_key: str, timeout: float) -> bool:
    # Langfuse v4 reads through the Observations API v2 (GET /api/public/traces returns 404).
    def traces() -> tuple[bool, str]:
        now = datetime.now(UTC)
        query = urllib.parse.urlencode({
            "fromStartTime": (now - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "toStartTime": (now + timedelta(minutes=5)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "limit": 100,
        })
        out = get(f"{base}/api/public/v2/observations?{query}", (public_key, secret_key))
        rows = out.get("data", [])
        trace_ids = {r.get("traceId") for r in rows}
        names = sorted({r.get("name") or "" for r in rows})
        return bool(rows), f"{len(rows)} observations in {len(trace_ids)} traces, names {names[:8]}"

    return retry("langfuse received LexAI traces", traces, timeout)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="what", required=True)
    p = sub.add_parser("prometheus")
    p.add_argument("--url", default="http://localhost:9095")
    p.add_argument("--replicas", type=int, default=1)
    g = sub.add_parser("grafana")
    g.add_argument("--url", default="http://localhost:3001")
    g.add_argument("--password", required=True)
    lf = sub.add_parser("langfuse")
    lf.add_argument("--url", default="http://localhost:3000")
    lf.add_argument("--public-key", required=True)
    lf.add_argument("--secret-key", required=True)
    for s in (p, g, lf):
        s.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if args.what == "prometheus":
        ok = prometheus(args.url, args.replicas, args.timeout)
    elif args.what == "grafana":
        ok = grafana(args.url, args.password, args.timeout)
    else:
        ok = langfuse(args.url, args.public_key, args.secret_key, args.timeout)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
