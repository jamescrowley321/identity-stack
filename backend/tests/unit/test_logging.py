"""Unit tests for structured logging configuration."""

import logging

from app.logging_config import get_logger, setup_logging


class TestGetLogger:
    def test_returns_logger_with_name(self):
        logger = get_logger("test.module")
        assert logger.name == "test.module"


class TestSetupLogging:
    def test_configures_root_logger(self):
        setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) >= 1
