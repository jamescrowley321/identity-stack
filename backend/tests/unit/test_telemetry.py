"""Unit tests for OpenTelemetry configuration and graceful degradation."""

import builtins
from unittest.mock import MagicMock, patch

import pytest

from app.telemetry import init_telemetry, shutdown_telemetry

_original_import = builtins.__import__


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
        try:
            from opentelemetry import trace

            # Reset to default (no-op) provider
            trace._TRACER_PROVIDER = None
            trace._TRACER_PROVIDER_SET_ONCE._done = False
        except Exception:  # noqa: BLE001, S110
            pass

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

    def test_get_trace_id_returns_hex_trace_id_within_active_span(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        try:
            tracer = trace.get_tracer("test")
            with tracer.start_as_current_span("test-span") as span:
                from app.errors.problem_detail import _get_trace_id

                trace_id = _get_trace_id()
                expected = format(span.get_span_context().trace_id, "032x")
                assert trace_id == expected
                assert len(trace_id) == 32
                assert trace_id != "0" * 32
        finally:
            trace._TRACER_PROVIDER = None
            trace._TRACER_PROVIDER_SET_ONCE._done = False

    def test_get_trace_id_returns_empty_without_active_span(self):
        from app.errors.problem_detail import _get_trace_id

        trace_id = _get_trace_id()
        assert trace_id == ""


def _block_otel_imports(name, *args, **kwargs):
    """Raise ImportError for opentelemetry packages, allow everything else."""
    if isinstance(name, str) and name.startswith("opentelemetry"):
        raise ImportError(f"No module named '{name}'")
    return _original_import(name, *args, **kwargs)
