"""Structured JSON logging, configured via dictConfig.

Contextual fields (customer_id, case_id) are threaded through via contextvars
so any log call inside a request/agent-node scope picks them up automatically,
without every call site having to pass them explicitly.
"""

import json
import logging
import logging.config
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_customer_id: ContextVar[str | None] = ContextVar("customer_id", default=None)
_case_id: ContextVar[str | None] = ContextVar("case_id", default=None)


def bind_context(*, customer_id: str | None = None, case_id: str | None = None) -> None:
    """Set contextual IDs for the current async task / request. Pass None to leave unset."""
    if customer_id is not None:
        _customer_id.set(customer_id)
    if case_id is not None:
        _case_id.set(case_id)


def clear_context() -> None:
    _customer_id.set(None)
    _case_id.set(None)


class ContextFilter(logging.Filter):
    """Injects the current customer_id/case_id (if any) onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.customer_id = _customer_id.get()
        record.case_id = _case_id.get()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        customer_id = getattr(record, "customer_id", None)
        case_id = getattr(record, "case_id", None)
        if customer_id is not None:
            payload["customer_id"] = customer_id
        if case_id is not None:
            payload["case_id"] = case_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_level: str = "INFO") -> None:
    """Applies structured JSON logging to root, uvicorn, and uvicorn.access loggers."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": ContextFilter}},
            "formatters": {"json": {"()": JSONFormatter}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "filters": ["context"],
                }
            },
            "root": {"handlers": ["console"], "level": log_level},
            "loggers": {
                "uvicorn": {"handlers": ["console"], "level": log_level, "propagate": False},
                "uvicorn.access": {"handlers": ["console"], "level": log_level, "propagate": False},
                "uvicorn.error": {"handlers": ["console"], "level": log_level, "propagate": False},
            },
        }
    )
