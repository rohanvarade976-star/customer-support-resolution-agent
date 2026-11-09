"""Structured JSON logging.

Each log line is one JSON object, so logs are easy to search and filter.
If a workflow `run_id` is set for the current task, it is added to every line,
which lets us pull up all logs for one complaint together.

Usage:
    configure_logging("INFO")
    log = logging.getLogger(__name__)
    log.info("refund reserved", extra={"amount_paise": 49900})
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# A ContextVar is like a global, but each async task sees its own value.
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

# Attributes every LogRecord has. Anything else came from `extra=` and is logged as a field.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = run_id_var.get()
        if run_id is not None:
            entry["run_id"] = run_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # default=str keeps logging from crashing on values json can't encode.
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Send all logs to stdout as JSON. Call once at startup."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
