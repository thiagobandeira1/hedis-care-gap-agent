"""Deny-by-default structured logging (P6's pattern): node names, ids, durations, statuses,
error classes — never content, outreach text, display strings, or dates."""

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "level",
        "logger",
        "timestamp",
        "request_id",
        "run_id",
        "thread_id",
        "patient_id",
        "node",
        "measure_id",
        "status",
        "action",
        "duration_ms",
        "attempt",
        "model_id",
        "prompt_version",
        "case_key",
        "error_class",
        "schema",
        "usage_input_tokens",
        "usage_output_tokens",
        "count",
        "method",
        "path",
        "status_code",
        "service_version",
        "exc_info",
    }
)


def _allowlist_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {k: v for k, v in event_dict.items() if k in ALLOWED_KEYS}


def configure_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _allowlist_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
