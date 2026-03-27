import logging
import os
from contextvars import ContextVar

from pythonjsonlogger.json import JsonFormatter

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class CorrelationIdFilter(logging.Filter):
    """Injects the current request's correlation ID into every log record."""

    def filter(self, record):
        record.correlation_id = correlation_id_var.get("-")
        return True


def setup_logging() -> None:
    """Configure structured logging. JSON in production, human-readable in development."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    environment = os.getenv("ENVIRONMENT", "development")

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on reload
    root.handlers.clear()

    handler = logging.StreamHandler()
    handler.addFilter(CorrelationIdFilter())

    if environment == "production":
        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s %(correlation_id)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger that automatically includes the correlation ID."""
    return logging.getLogger(name)
