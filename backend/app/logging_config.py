import logging
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.knobs import knob
from app.logbook import logbook


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
    """Configure structured logging. JSON in production, human-readable in development.

    The only place handlers, formatters and levels are set. Modules obtain their
    logger with ``app.logbook.logbook(__name__)`` and never configure it.
    """
    log_level = knob("LOG_LEVEL", "INFO").upper()
    environment = knob("ENVIRONMENT", "development")

    root = logging.root
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
    logbook("httpx").setLevel(logging.WARNING)
    logbook("httpcore").setLevel(logging.WARNING)
    logbook("uvicorn.access").setLevel(logging.WARNING)
