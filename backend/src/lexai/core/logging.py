"""
Structured logging. Human-readable console output locally; one JSON object per
line everywhere else. `request_id` and `session_id` are bound through
contextvars so every log line inside a request carries them.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from lexai.core.settings import APISettings

# Personal data that users type into questions ("my number is 98xxxxxxxx") must not
# reach the logs. Applied to every string value of every log line.
_PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[pan]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[aadhaar]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), "[phone]"),
]


def mask_pii(text: str) -> str:
    for pattern, label in _PII:
        text = pattern.sub(label, text)
    return text


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {k: _mask_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask_value(v) for v in value]
    return value


def _mask_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: _mask_value(v) for k, v in event_dict.items()}


def configure_logging(settings: APISettings) -> None:
    level = getattr(logging, settings.log_level)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _mask_processor,
    ]
    renderer: Any
    if settings.environment == "local":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


def bind_request_context(**values: str) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
