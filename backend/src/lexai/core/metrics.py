"""
Prometheus metrics, served at GET /metrics.

Labels are bounded: routes are path templates (never raw paths), outcomes, stages
and services are fixed sets. Dashboards: deploy/grafana/lexai.json; alerts:
deploy/prometheus/alerts.yml.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest

# Answers on the laptop take tens of seconds; on a GPU server a few. Buckets cover both.
_SLOW = (0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 300, 600)
_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQUESTS = Counter(
    "lexai_http_requests_total", "HTTP requests by route template and status", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "lexai_http_request_duration_seconds",
    "Request duration (for streamed answers: until the stream ends)",
    ["method", "route"],
    buckets=_SLOW,
)
TIME_TO_FIRST_TOKEN = Histogram(
    "lexai_time_to_first_token_seconds", "From request to the first answer token", buckets=_SLOW
)
STAGE_DURATION = Histogram(
    "lexai_stage_duration_seconds",
    "Duration of each answer stage (condense, retrieve, context, generate, verify, ...)",
    ["stage"],
    buckets=_SLOW,
)
ANSWERS = Counter(
    "lexai_answers_total",
    "Chat requests by outcome: answered, abstained, refused, out_of_scope, cached, error",
    ["outcome"],
)
VERIFICATION_FAILURES = Counter(
    "lexai_verification_failures_total", "Answers whose citation check failed", ["kind"]
)
CACHE_REQUESTS = Counter("lexai_cache_requests_total", "Answer cache lookups", ["result"])
UPSTREAM_ERRORS = Counter(
    "lexai_upstream_errors_total", "Failures by upstream service and error code", ["service", "code"]
)
RATE_LIMITED = Counter("lexai_rate_limited_total", "Requests refused with 429", ["route"])
FEEDBACK = Counter("lexai_feedback_total", "User feedback", ["rating"])
READY = Gauge("lexai_ready", "1 when model servers and the index are ready (background probe)")
ACTIVE_SESSIONS = Gauge("lexai_active_sessions", "Sessions not yet expired")
TOKENS = Counter("lexai_llm_tokens_total", "LLM tokens", ["kind"])

for _stage in ("condense", "retrieve", "context", "generate", "verify"):
    STAGE_DURATION.labels(stage=_stage)  # export zero series so dashboards show them from the start
for _outcome in ("answered", "abstained", "refused", "out_of_scope", "cached", "error"):
    ANSWERS.labels(outcome=_outcome)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
