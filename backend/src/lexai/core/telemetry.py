"""
OpenTelemetry tracing.

One trace per request: the HTTP span (middleware), a `lexai.chat` span, and one
child span per stage (`lexai.condense`, `lexai.retrieve`, `lexai.context`,
`lexai.generate`, `lexai.verify`) carrying the prompt version, retrieved chunk ids
with scores, token usage and the verification result.

Export: OTLP/HTTP when `LEXAI_OBS_OTLP_ENDPOINT` is set (Langfuse, Jaeger, Tempo,
any collector), and/or JSON lines to `LEXAI_OBS_TRACE_FILE` (the laptop, with no
collector; read with `scripts/show_trace.py`). With neither, the no-op tracer is
used and tracing costs nothing.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from lexai import __version__
from lexai.core.logging import get_logger
from lexai.core.settings import ObservabilitySettings

logger = get_logger(__name__)
tracer = trace.get_tracer("lexai", __version__)
_configured: TracerProvider | None = None


class JsonlSpanExporter(SpanExporter):
    """Finished spans as one JSON object per line (append-only, thread-safe)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = [json.dumps(span_record(s), ensure_ascii=False, default=str) for s in spans]
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def span_record(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    start, end = span.start_time or 0, span.end_time or 0
    return {
        "trace_id": f"{ctx.trace_id:032x}" if ctx else "",
        "span_id": f"{ctx.span_id:016x}" if ctx else "",
        "parent_id": f"{span.parent.span_id:016x}" if span.parent else None,
        "name": span.name,
        "start_ns": start,
        "duration_ms": round((end - start) / 1e6, 1),
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
    }


def _headers(raw: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def configure_tracing(obs: ObservabilitySettings) -> TracerProvider | None:
    """Install the global tracer provider once per process (no-op when nothing is configured)."""
    global _configured
    if _configured is not None or not (obs.otlp_endpoint or obs.trace_file):
        return _configured
    provider = TracerProvider(
        resource=Resource.create({"service.name": obs.service_name, "service.version": __version__})
    )
    if obs.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        headers = _headers(obs.otlp_headers.get_secret_value() if obs.otlp_headers else None)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=obs.otlp_endpoint, headers=headers))
        )
    if obs.trace_file:
        provider.add_span_processor(BatchSpanProcessor(JsonlSpanExporter(obs.trace_file)))
    trace.set_tracer_provider(provider)
    _configured = provider
    logger.info("tracing_enabled", otlp=bool(obs.otlp_endpoint), trace_file=str(obs.trace_file or ""))
    return provider


def flush() -> None:
    if _configured is not None:
        _configured.force_flush(timeout_millis=5000)
