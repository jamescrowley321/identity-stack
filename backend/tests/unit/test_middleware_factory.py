"""Unit tests for the middleware factory (Story 3.1).

Tests DEPLOYMENT_MODE validation, default behavior, startup logging,
and middleware registration via configure_middleware().
"""

import importlib
import logging
import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI


@pytest.fixture(autouse=True)
def _restore_factory_module():
    """Restore factory module to a clean state after each test.

    Tests that reload the module with invalid DEPLOYMENT_MODE leave
    it in a broken state. This fixture ensures a clean reload after
    every test so subsequent tests aren't affected.
    """
    yield
    # Restore with a known-good value so later imports work
    with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
        import app.middleware.factory as factory_mod

        importlib.reload(factory_mod)


class TestDeploymentModeDefault:
    """FR-18: defaults to 'standalone' when DEPLOYMENT_MODE is unset."""

    def test_defaults_to_standalone_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "DEPLOYMENT_MODE"}
        with patch.dict(os.environ, env, clear=True):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)
            assert factory_mod.DEPLOYMENT_MODE == "standalone"


class TestDeploymentModeValidValues:
    """FR-18: accepts exactly 'standalone' and 'gateway'."""

    def test_accepts_standalone(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)
            assert factory_mod.DEPLOYMENT_MODE == "standalone"

    def test_accepts_gateway(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)
            assert factory_mod.DEPLOYMENT_MODE == "gateway"


class TestDeploymentModeInvalidValues:
    """FR-18: invalid values raise ValueError at startup."""

    def test_rejects_invalid_value(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "production"}):
            import app.middleware.factory as factory_mod

            with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                importlib.reload(factory_mod)

    def test_rejects_empty_string(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": ""}):
            import app.middleware.factory as factory_mod

            with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                importlib.reload(factory_mod)

    def test_rejects_wrong_case_gateway(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "Gateway"}):
            import app.middleware.factory as factory_mod

            with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                importlib.reload(factory_mod)

    def test_rejects_wrong_case_standalone(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "STANDALONE"}):
            import app.middleware.factory as factory_mod

            with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                importlib.reload(factory_mod)

    def test_error_message_lists_valid_values(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "bad"}):
            import app.middleware.factory as factory_mod

            with pytest.raises(ValueError, match="standalone.*gateway"):
                importlib.reload(factory_mod)


class TestDeploymentModeImportTime:
    """FR-19: evaluated once at import time, not per-request."""

    def test_module_level_constant(self):
        """DEPLOYMENT_MODE is a module-level attribute, not a function."""
        import app.middleware.factory as factory_mod

        # It's a plain str attribute, not callable
        assert isinstance(factory_mod.DEPLOYMENT_MODE, str)

    def test_does_not_change_after_env_update(self):
        """Changing env var after import does not change DEPLOYMENT_MODE."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)
            assert factory_mod.DEPLOYMENT_MODE == "standalone"

            # Change env without reloading — value should remain "standalone"
            with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
                assert factory_mod.DEPLOYMENT_MODE == "standalone"


class TestDeploymentModeWhitespace:
    """Edge case: whitespace around DEPLOYMENT_MODE value is stripped."""

    def test_strips_trailing_whitespace(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone  "}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)
            assert factory_mod.DEPLOYMENT_MODE == "standalone"

    def test_strips_leading_whitespace(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "  gateway"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)
            assert factory_mod.DEPLOYMENT_MODE == "gateway"


class TestConfigureMiddleware:
    """configure_middleware() registers the full middleware stack on the app."""

    def test_registers_middleware_on_app(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)

            test_app = FastAPI()
            factory_mod.configure_middleware(test_app)

            # FastAPI stores user-added middleware in app.user_middleware
            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware]
            assert "CORSMiddleware" in middleware_classes
            assert "TokenValidationMiddleware" in middleware_classes
            assert "SlowAPIMiddleware" in middleware_classes
            assert "SecurityHeadersMiddleware" in middleware_classes
            assert "CorrelationIdMiddleware" in middleware_classes
            assert "ProxyHeadersMiddleware" in middleware_classes

    def test_middleware_count(self):
        """Story 3.1 registers all 6 middleware layers regardless of mode."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)

            test_app = FastAPI()
            factory_mod.configure_middleware(test_app)

            assert len(test_app.user_middleware) == 6


class TestDoubleCallProtection:
    """configure_middleware() is idempotent — second call is a no-op."""

    def test_double_call_does_not_duplicate_middleware(self):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)

            test_app = FastAPI()
            factory_mod.configure_middleware(test_app)
            first_count = len(test_app.user_middleware)

            factory_mod.configure_middleware(test_app)
            assert len(test_app.user_middleware) == first_count


class TestStartupLogging:
    """FR-20: INFO log at startup with mode and middleware list."""

    def test_logs_mode_and_stack_at_info(self, caplog):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)

            test_app = FastAPI()
            with caplog.at_level(logging.INFO, logger="app.middleware.factory"):
                factory_mod.configure_middleware(test_app)

            assert any("mode=standalone" in record.message for record in caplog.records)
            assert any("ProxyHeaders" in record.message for record in caplog.records)

    def test_logs_gateway_mode_with_exclusions(self, caplog):
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory_mod

            importlib.reload(factory_mod)

            test_app = FastAPI()
            with caplog.at_level(logging.INFO, logger="app.middleware.factory"):
                factory_mod.configure_middleware(test_app)

            assert any("mode=gateway" in record.message for record in caplog.records)
            assert any("excluded_in_story_2.2" in record.message for record in caplog.records)
            assert any("TokenValidationMiddleware" in record.message for record in caplog.records)


class TestV2UpgradeComment:
    """FR-22: module contains v2 upgrade path comment."""

    def test_module_docstring_contains_openfeature_comment(self):
        import app.middleware.factory as factory_mod

        assert "OpenFeature" in factory_mod.__doc__
        assert "deployment_mode" in factory_mod.__doc__
