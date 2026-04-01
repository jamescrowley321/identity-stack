"""Unit tests for the middleware factory (DEPLOYMENT_MODE logic)."""

import importlib
import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class TestDeploymentModeValidation:
    """Test DEPLOYMENT_MODE environment variable validation at import time."""

    def test_default_standalone_when_unset(self):
        """DEPLOYMENT_MODE defaults to 'standalone' when not set."""
        env = os.environ.copy()
        env.pop("DEPLOYMENT_MODE", None)
        with patch.dict(os.environ, env, clear=True):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)
            assert factory.DEPLOYMENT_MODE == "standalone"

    def test_accepts_standalone(self):
        """DEPLOYMENT_MODE='standalone' is valid."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)
            assert factory.DEPLOYMENT_MODE == "standalone"

    def test_accepts_gateway(self):
        """DEPLOYMENT_MODE='gateway' is valid."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)
            assert factory.DEPLOYMENT_MODE == "gateway"

    def test_rejects_invalid_value(self):
        """Invalid DEPLOYMENT_MODE raises ValueError at import time."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "invalid"}):
            with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                import app.middleware.factory as factory

                importlib.reload(factory)

    def test_rejects_empty_string(self):
        """Empty string DEPLOYMENT_MODE raises ValueError (not treated as unset)."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": ""}):
            with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                import app.middleware.factory as factory

                importlib.reload(factory)

    def test_rejects_wrong_case(self):
        """Case-sensitive: 'Gateway' and 'STANDALONE' are invalid."""
        for bad_value in ("Gateway", "STANDALONE", "GATEWAY", "Standalone"):
            with patch.dict(os.environ, {"DEPLOYMENT_MODE": bad_value}):
                with pytest.raises(ValueError, match="Invalid DEPLOYMENT_MODE"):
                    import app.middleware.factory as factory

                    importlib.reload(factory)


class TestConfigureMiddlewareStandalone:
    """Test middleware stack in standalone mode."""

    def test_standalone_includes_all_middleware(self):
        """Standalone mode includes TokenValidation and SlowAPI middleware."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "TokenValidationMiddleware" in middleware_classes
            assert "SlowAPIMiddleware" in middleware_classes
            assert "CORSMiddleware" in middleware_classes
            assert "SecurityHeadersMiddleware" in middleware_classes
            assert "CorrelationIdMiddleware" in middleware_classes
            assert "ProxyHeadersMiddleware" in middleware_classes

    def test_standalone_middleware_order(self):
        """Standalone middleware is added innermost-first (CORS first, ProxyHeaders last)."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_names = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            cors_idx = middleware_names.index("CORSMiddleware")
            proxy_idx = middleware_names.index("ProxyHeadersMiddleware")
            # FastAPI user_middleware stores last-added (outermost) first in the list
            # ProxyHeaders is outermost (added last) → lower index than CORS (innermost)
            assert proxy_idx < cors_idx


class TestConfigureMiddlewareGateway:
    """Test middleware stack in gateway mode."""

    def test_gateway_excludes_token_validation(self):
        """Gateway mode skips TokenValidationMiddleware (Tyk handles JWT)."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "TokenValidationMiddleware" not in middleware_classes

    def test_gateway_excludes_slowapi(self):
        """Gateway mode skips SlowAPIMiddleware (Tyk handles rate limiting)."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "SlowAPIMiddleware" not in middleware_classes

    def test_gateway_keeps_cors(self):
        """Gateway mode still includes CORS middleware."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "CORSMiddleware" in middleware_classes

    def test_gateway_keeps_security_headers(self):
        """Gateway mode still includes SecurityHeadersMiddleware."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "SecurityHeadersMiddleware" in middleware_classes

    def test_gateway_keeps_correlation_id(self):
        """Gateway mode still includes CorrelationIdMiddleware."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "CorrelationIdMiddleware" in middleware_classes

    def test_gateway_keeps_proxy_headers(self):
        """Gateway mode still includes ProxyHeadersMiddleware."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_classes = [m.cls.__name__ for m in test_app.user_middleware if hasattr(m, "cls")]
            assert "ProxyHeadersMiddleware" in middleware_classes

    def test_gateway_has_four_middleware(self):
        """Gateway mode should have exactly 4 middleware (CORS, Security, Correlation, Proxy)."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            factory.configure_middleware(test_app)

            middleware_with_cls = [m for m in test_app.user_middleware if hasattr(m, "cls")]
            assert len(middleware_with_cls) == 4


class TestConfigureMiddlewareLogging:
    """Test that configure_middleware logs the expected messages."""

    def test_standalone_logs_included_middleware(self, caplog):
        """Standalone mode logs all middleware as included."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "standalone"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            with caplog.at_level("INFO", logger="app.middleware.factory"):
                factory.configure_middleware(test_app)

            assert "Middleware included: TokenValidationMiddleware" in caplog.text
            assert "Middleware included: SlowAPIMiddleware" in caplog.text
            assert "Deployment mode: standalone" in caplog.text

    def test_gateway_logs_excluded_middleware(self, caplog):
        """Gateway mode logs TokenValidation and SlowAPI as excluded."""
        with patch.dict(os.environ, {"DEPLOYMENT_MODE": "gateway"}):
            import app.middleware.factory as factory

            factory = importlib.reload(factory)

            test_app = FastAPI()
            with caplog.at_level("INFO", logger="app.middleware.factory"):
                factory.configure_middleware(test_app)

            assert "Middleware excluded: TokenValidationMiddleware (gateway mode)" in caplog.text
            assert "Middleware excluded: SlowAPIMiddleware (gateway mode)" in caplog.text
            assert "Deployment mode: gateway" in caplog.text


class TestMainAppIntegration:
    """Test that main.py correctly wires up the factory."""

    def test_main_app_has_limiter_state(self):
        """app.state.limiter must be set even when factory configures middleware."""
        from app.main import app

        assert hasattr(app.state, "limiter")
        assert app.state.limiter is not None

    def test_main_app_has_rate_limit_exception_handler(self):
        """RateLimitExceeded handler must be registered regardless of deployment mode."""
        from slowapi.errors import RateLimitExceeded

        from app.main import app

        assert RateLimitExceeded in app.exception_handlers

    @pytest.mark.anyio
    async def test_health_endpoint_works(self):
        """Health endpoint should work through the factory-configured middleware stack."""
        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/health")
            assert response.status_code == 200
