"""Validate docker-compose.yml structure for required services (Story 1.1).

These tests parse the docker-compose file to ensure infrastructure services
are configured correctly without needing to spin up containers.
"""

from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose():
    """Load and parse docker-compose.yml."""
    assert COMPOSE_PATH.exists(), f"docker-compose.yml not found at {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text())


class TestPostgresService:
    """AC-1.1.1: Postgres 16-alpine service."""

    def test_postgres_service_exists(self, compose):
        assert "postgres" in compose["services"]

    def test_postgres_image(self, compose):
        assert compose["services"]["postgres"]["image"] == "postgres:16-alpine"

    def test_postgres_port(self, compose):
        ports = compose["services"]["postgres"]["ports"]
        assert "5432:5432" in ports

    def test_postgres_env_vars(self, compose):
        env = compose["services"]["postgres"]["environment"]
        assert env["POSTGRES_DB"] == "identity"
        assert env["POSTGRES_USER"] == "identity"
        assert "POSTGRES_PASSWORD" in str(env)

    def test_postgres_volume(self, compose):
        volumes = compose["services"]["postgres"]["volumes"]
        assert any("pgdata" in v for v in volumes)

    def test_pgdata_named_volume(self, compose):
        assert "pgdata" in compose.get("volumes", {})


class TestAspireDashboardService:
    """AC-1.1.1: Aspire dashboard for OTLP."""

    def test_aspire_service_exists(self, compose):
        assert "aspire-dashboard" in compose["services"]

    def test_aspire_image(self, compose):
        image = compose["services"]["aspire-dashboard"]["image"]
        assert "aspire-dashboard" in image

    def test_aspire_otlp_port(self, compose):
        ports = compose["services"]["aspire-dashboard"]["ports"]
        port_strs = [str(p) for p in ports]
        assert any("18889" in p for p in port_strs)

    def test_aspire_ui_port(self, compose):
        ports = compose["services"]["aspire-dashboard"]["ports"]
        port_strs = [str(p) for p in ports]
        assert any("18888" in p for p in port_strs)

    def test_aspire_unsecured_auth(self, compose):
        env = compose["services"]["aspire-dashboard"]["environment"]
        assert env.get("DASHBOARD__OTLP__AUTHMODE") == "Unsecured"


class TestRedisService:
    """AC-1.1.1: Redis 7-alpine service."""

    def test_redis_service_exists(self, compose):
        assert "redis" in compose["services"]

    def test_redis_image(self, compose):
        assert compose["services"]["redis"]["image"] == "redis:7-alpine"

    def test_redis_port(self, compose):
        ports = compose["services"]["redis"]["ports"]
        assert "6379:6379" in ports


class TestBackendService:
    """Backend service depends on postgres and has correct env."""

    def test_backend_depends_on_postgres(self, compose):
        deps = compose["services"]["backend"].get("depends_on", [])
        if isinstance(deps, list):
            assert "postgres" in deps
        elif isinstance(deps, dict):
            assert "postgres" in deps

    def test_backend_database_url_env(self, compose):
        env = compose["services"]["backend"].get("environment", [])
        env_str = str(env)
        assert "postgresql+asyncpg://" in env_str
        assert "postgres:5432/identity" in env_str

    def test_backend_otel_env(self, compose):
        env = compose["services"]["backend"].get("environment", [])
        env_str = str(env)
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in env_str
        assert "OTEL_SERVICE_NAME" in env_str

    def test_backend_redis_env(self, compose):
        env = compose["services"]["backend"].get("environment", [])
        env_str = str(env)
        assert "REDIS_URL" in env_str
