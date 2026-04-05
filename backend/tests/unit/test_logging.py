"""Unit tests for structured logging configuration."""

import logging
from unittest.mock import patch

from app.logging_config import _OTelAwareJsonFormatter, get_logger, setup_logging


class TestGetLogger:
    def test_returns_logger_with_name(self):
        logger = get_logger("test.module")
        assert logger.name == "test.module"


class TestSetupLogging:
    def test_configures_root_logger(self):
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) >= 1

    def test_production_uses_otel_aware_formatter(self):
        with patch.dict("os.environ", {"ENVIRONMENT": "production"}):
            setup_logging()
        root = logging.getLogger()
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, _OTelAwareJsonFormatter)

    def test_development_uses_standard_formatter(self):
        with patch.dict("os.environ", {"ENVIRONMENT": "development"}):
            setup_logging()
        root = logging.getLogger()
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, logging.Formatter)
        assert not isinstance(formatter, _OTelAwareJsonFormatter)


class TestOTelAwareJsonFormatter:
    """Verify that zero-value OTel fields are suppressed."""

    def test_suppresses_zero_trace_id(self):
        formatter = _OTelAwareJsonFormatter()
        record = {"message": "test", "otelTraceID": "0", "otelSpanID": "0"}
        result = formatter.process_log_record(record)
        assert "otelTraceID" not in result
        assert "otelSpanID" not in result

    def test_suppresses_32_zero_trace_id(self):
        formatter = _OTelAwareJsonFormatter()
        record = {"message": "test", "otelTraceID": "00000000000000000000000000000000"}
        result = formatter.process_log_record(record)
        assert "otelTraceID" not in result

    def test_suppresses_empty_trace_id(self):
        formatter = _OTelAwareJsonFormatter()
        record = {"message": "test", "otelTraceID": "", "otelSpanID": ""}
        result = formatter.process_log_record(record)
        assert "otelTraceID" not in result
        assert "otelSpanID" not in result

    def test_preserves_real_trace_id(self):
        formatter = _OTelAwareJsonFormatter()
        record = {
            "message": "test",
            "otelTraceID": "4bf92f3577b34da6a3ce929d0e0e4736",
            "otelSpanID": "00f067aa0ba902b7",
        }
        result = formatter.process_log_record(record)
        assert result["otelTraceID"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert result["otelSpanID"] == "00f067aa0ba902b7"

    def test_handles_missing_otel_fields(self):
        formatter = _OTelAwareJsonFormatter()
        record = {"message": "test"}
        result = formatter.process_log_record(record)
        assert "otelTraceID" not in result
        assert "otelSpanID" not in result
