"""Unit tests for OpenTelemetry configuration and graceful degradation."""

import builtins
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.telemetry import init_telemetry, shutdown_telemetry

_original_import = builtins.__import__


def _reset_tracer_provider():
    """Reset the global OTel tracer provider to the default proxy.

    The OTel SDK enforces set-once semantics. We reset via the internal
    _TRACER_PROVIDER_SET_ONCE guard. This is unavoidable for unit tests —
    the SDK provides no public reset API.
    """
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    # The SDK's set-once guard must be reset for subsequent tests.
    # These are private but stable across opentelemetry-api >=1.12.
    if hasattr(trace, "_TRACER_PROVIDER_SET_ONCE"):
        trace._TRACER_PROVIDER_SET_ONCE._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None


class TestInitTelemetryDisabled:
    """AC-1.4.4: Graceful degradation when OTel is not configured."""

    def test_skips_when_endpoint_empty(self):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": ""}):
            # Should return without error
            init_telemetry()

    def test_skips_when_endpoint_whitespace(self):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "   "}):
            init_telemetry()

    def test_skips_when_endpoint_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove OTEL_EXPORTER_OTLP_ENDPOINT entirely
            import os

            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            init_telemetry()

    def test_logs_disabled_message(self, caplog):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": ""}):
            with caplog.at_level("INFO", logger="app.telemetry"):
                init_telemetry()
            assert "OTel disabled" in caplog.text


class TestInitTelemetryImportFailure:
    """AC-1.4.4: Graceful degradation when OTel packages are missing."""

    def test_handles_import_error_gracefully(self):
        with (
            patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}),
            patch("builtins.__import__", side_effect=_block_otel_imports),
        ):
            # Should not raise
            init_telemetry()

    def test_logs_warning_on_import_error(self, caplog):
        with (
            patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}),
            patch("builtins.__import__", side_effect=_block_otel_imports),
            caplog.at_level("WARNING", logger="app.telemetry"),
        ):
            init_telemetry()
        assert "not installed" in caplog.text


class TestInitTelemetrySdkFailure:
    """Edge case: OTel SDK constructor throws (e.g., gRPC init error)."""

    def test_handles_provider_init_failure(self, caplog):
        with (
            patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}),
            patch(
                "opentelemetry.sdk.trace.TracerProvider",
                side_effect=RuntimeError("gRPC init error"),
            ),
            caplog.at_level("WARNING", logger="app.telemetry"),
        ):
            init_telemetry()
        assert "OTel SDK init failed" in caplog.text


class TestInitTelemetryEnabled:
    """AC-1.4.1: OTel SDK configuration when endpoint is set."""

    @pytest.fixture(autouse=True)
    def _clean_tracer(self):
        """Reset the global tracer provider after each test."""
        yield
        _reset_tracer_provider()

    def test_calls_all_instrumentors(self):
        with (
            patch.dict(
                "os.environ",
                {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"},
            ),
            patch("app.telemetry._instrument_fastapi") as mock_fastapi,
            patch("app.telemetry._instrument_httpx") as mock_httpx,
            patch("app.telemetry._instrument_sqlalchemy") as mock_sqlalchemy,
            patch("app.telemetry._instrument_logging") as mock_logging,
        ):
            init_telemetry()
            mock_fastapi.assert_called_once()
            mock_httpx.assert_called_once()
            mock_sqlalchemy.assert_called_once_with(None)
            mock_logging.assert_called_once()

    def test_passes_engine_to_sqlalchemy_instrumentor(self):
        mock_engine = MagicMock()
        with (
            patch.dict(
                "os.environ",
                {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"},
            ),
            patch("app.telemetry._instrument_fastapi"),
            patch("app.telemetry._instrument_httpx"),
            patch("app.telemetry._instrument_sqlalchemy") as mock_sqlalchemy,
            patch("app.telemetry._instrument_logging"),
        ):
            init_telemetry(engine=mock_engine)
            mock_sqlalchemy.assert_called_once_with(mock_engine)

    def test_uses_custom_service_name(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
                    "OTEL_SERVICE_NAME": "my-custom-service",
                },
            ),
            patch("app.telemetry._instrument_fastapi"),
            patch("app.telemetry._instrument_httpx"),
            patch("app.telemetry._instrument_sqlalchemy"),
            patch("app.telemetry._instrument_logging"),
        ):
            init_telemetry()
            # Verify the provider was set with the custom service name
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            resource_attrs = dict(provider.resource.attributes)
            assert resource_attrs["service.name"] == "my-custom-service"

    def test_uses_default_service_name(self):
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}
        # Ensure OTEL_SERVICE_NAME is not set
        with (
            patch.dict("os.environ", env, clear=False),
            patch("app.telemetry._instrument_fastapi"),
            patch("app.telemetry._instrument_httpx"),
            patch("app.telemetry._instrument_sqlalchemy"),
            patch("app.telemetry._instrument_logging"),
        ):
            import os

            os.environ.pop("OTEL_SERVICE_NAME", None)
            init_telemetry()
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            resource_attrs = dict(provider.resource.attributes)
            assert resource_attrs["service.name"] == "identity-stack"

    def test_logs_initialized_message(self, caplog):
        with (
            patch.dict(
                "os.environ",
                {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"},
            ),
            patch("app.telemetry._instrument_fastapi"),
            patch("app.telemetry._instrument_httpx"),
            patch("app.telemetry._instrument_sqlalchemy"),
            patch("app.telemetry._instrument_logging"),
            caplog.at_level("INFO", logger="app.telemetry"),
        ):
            init_telemetry()
        assert "OTel initialized" in caplog.text
        assert "localhost:4317" in caplog.text


class TestShutdownTelemetry:
    def test_shutdown_calls_provider_shutdown(self):
        mock_provider = MagicMock()
        mock_provider.shutdown = MagicMock()
        with patch("opentelemetry.trace.get_tracer_provider", return_value=mock_provider):
            shutdown_telemetry()
        mock_provider.shutdown.assert_called_once()

    def test_shutdown_no_crash_when_not_initialized(self):
        """Shutdown should be safe even if OTel was never initialized."""
        shutdown_telemetry()

    def test_shutdown_handles_exception(self):
        with patch("opentelemetry.trace.get_tracer_provider", side_effect=RuntimeError("boom")):
            # Should not raise
            shutdown_telemetry()


class TestInstrumentorGracefulDegradation:
    """Each instrumentor wraps its import in try/except — failures are logged, not raised."""

    def test_fastapi_instrumentor_handles_import_error(self):
        from app.telemetry import _instrument_fastapi

        with patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            _instrument_fastapi()

    def test_httpx_instrumentor_handles_import_error(self):
        from app.telemetry import _instrument_httpx

        with patch(
            "opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor",
            side_effect=RuntimeError("boom"),
        ):
            _instrument_httpx()

    def test_sqlalchemy_instrumentor_handles_import_error(self):
        from app.telemetry import _instrument_sqlalchemy

        with patch(
            "opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor",
            side_effect=RuntimeError("boom"),
        ):
            _instrument_sqlalchemy()

    def test_logging_instrumentor_handles_import_error(self):
        from app.telemetry import _instrument_logging

        with patch(
            "opentelemetry.instrumentation.logging.LoggingInstrumentor",
            side_effect=RuntimeError("boom"),
        ):
            _instrument_logging()


class TestGetTraceIdIntegration:
    """AC-1.4.3: traceId populated with current OTel trace ID when span is active."""

    @pytest.fixture(autouse=True)
    def _clean_tracer(self):
        yield
        _reset_tracer_provider()

    def test_get_trace_id_returns_hex_trace_id_within_active_span(self):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("test-span") as span:
            from app.errors.problem_detail import _get_trace_id

            trace_id = _get_trace_id()
            expected = format(span.get_span_context().trace_id, "032x")
            assert trace_id == expected
            assert len(trace_id) == 32
            assert trace_id != "0" * 32

    def test_get_trace_id_returns_empty_without_active_span(self):
        from app.errors.problem_detail import _get_trace_id

        trace_id = _get_trace_id()
        assert trace_id == ""


class TestTraceparentPropagation:
    """AC-1.4.2: W3C traceparent header used for distributed tracing."""

    @pytest.fixture(autouse=True)
    def _clean_tracer(self):
        yield
        _reset_tracer_provider()

    def test_fastapi_instrumentor_propagates_traceparent(self):
        """Verify inbound traceparent header joins the parent trace context."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

        exported_spans = []

        class _CollectorExporter(SpanExporter):
            def export(self, spans):
                exported_spans.extend(spans)
                return SpanExportResult.SUCCESS

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_CollectorExporter()))
        trace.set_tracer_provider(provider)

        test_app = FastAPI()

        @test_app.get("/test")
        async def test_endpoint():
            return {"ok": True}

        FastAPIInstrumentor.instrument_app(test_app)
        try:
            client = TestClient(test_app)
            # W3C traceparent: version-traceId-spanId-flags
            parent_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
            parent_span_id = "00f067aa0ba902b7"
            traceparent = f"00-{parent_trace_id}-{parent_span_id}-01"

            response = client.get("/test", headers={"traceparent": traceparent})
            assert response.status_code == 200

            assert len(exported_spans) >= 1
            server_span = exported_spans[0]
            actual_trace_id = format(server_span.context.trace_id, "032x")
            assert actual_trace_id == parent_trace_id
        finally:
            FastAPIInstrumentor.uninstrument_app(test_app)


def _block_otel_imports(name, *args, **kwargs):
    """Raise ImportError for opentelemetry packages, allow everything else."""
    if isinstance(name, str) and name.startswith("opentelemetry"):
        raise ImportError(f"No module named '{name}'")
    return _original_import(name, *args, **kwargs)
