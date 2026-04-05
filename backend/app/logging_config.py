import logging
import os
from typing import Any

from pythonjsonlogger.json import JsonFormatter


class _OTelAwareJsonFormatter(JsonFormatter):
    """JSON formatter that suppresses zero-value OTel trace/span IDs.

    When OTel is inactive, the logging instrumentor injects "0" as the trace
    and span IDs. This filter removes those noise fields from the JSON output.
    """

    _ZERO_OTEL_VALUES = {"0", "00000000000000000000000000000000", "0000000000000000", ""}

    def process_log_record(self, log_record: dict[str, Any]) -> dict[str, Any]:
        """Remove otelTraceID/otelSpanID when they are zero or empty."""
        for key in ("otelTraceID", "otelSpanID"):
            if str(log_record.get(key, "")) in self._ZERO_OTEL_VALUES:
                log_record.pop(key, None)
        return super().process_log_record(log_record)


def setup_logging() -> None:
    """Configure structured logging. JSON in production, human-readable in development."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    environment = os.getenv("ENVIRONMENT", "development")

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on reload
    root.handlers.clear()

    handler = logging.StreamHandler()

    if environment == "production":
        # OTel logging instrumentor injects otelTraceID / otelSpanID automatically.
        # defaults={} provides fallback values when OTel is not active.
        formatter = _OTelAwareJsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s %(otelTraceID)s %(otelSpanID)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
            defaults={"otelTraceID": "", "otelSpanID": ""},
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger."""
    return logging.getLogger(name)
