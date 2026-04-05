"""OpenTelemetry configuration — initialized once during app startup.

Configures OTLP exporter, auto-instrumentors for FastAPI, httpx, SQLAlchemy,
and logging. Gracefully degrades when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
"""

import logging
import os

logger = logging.getLogger(__name__)


def init_telemetry(*, engine=None) -> None:
    """Initialize OTel SDK with auto-instrumentors and OTLP exporter.

    Skips initialization when OTEL_EXPORTER_OTLP_ENDPOINT is empty/unset.
    Each instrumentor is wrapped in a try/except so a single failure
    doesn't prevent the others from loading.

    Args:
        engine: Optional SQLAlchemy async engine for query tracing.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("OTel disabled: OTEL_EXPORTER_OTLP_ENDPOINT is not set")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OTel SDK packages not installed — telemetry disabled")
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "identity-stack")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _instrument_fastapi()
    _instrument_httpx()
    _instrument_sqlalchemy(engine)
    _instrument_logging()

    logger.info("OTel initialized: endpoint=%s, service=%s", endpoint, service_name)


def shutdown_telemetry() -> None:
    """Flush and shut down the TracerProvider if active."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        logger.debug("OTel shutdown skipped (not initialized or already shut down)")


def _instrument_fastapi() -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument()
        logger.debug("OTel: FastAPI instrumentor registered")
    except Exception:
        logger.warning("OTel: FastAPI instrumentor failed", exc_info=True)


def _instrument_httpx() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.debug("OTel: httpx instrumentor registered")
    except Exception:
        logger.warning("OTel: httpx instrumentor failed", exc_info=True)


def _instrument_sqlalchemy(engine=None) -> None:
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        if engine is not None:
            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        else:
            SQLAlchemyInstrumentor().instrument()
        logger.debug("OTel: SQLAlchemy instrumentor registered")
    except Exception:
        logger.warning("OTel: SQLAlchemy instrumentor failed", exc_info=True)


def _instrument_logging() -> None:
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)
        logger.debug("OTel: logging instrumentor registered")
    except Exception:
        logger.warning("OTel: logging instrumentor failed", exc_info=True)
