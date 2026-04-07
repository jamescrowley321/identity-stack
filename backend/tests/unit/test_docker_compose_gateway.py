"""Unit tests for docker-compose gateway profile (Story 1.3).

Validates that the docker-compose.yml defines tyk-gateway and tyk-redis services
correctly under the gateway profile, and that default compose behavior is unchanged.
"""

import json
from pathlib import Path

import yaml

# Repo root is two levels up from tests/unit/
REPO_ROOT = Path(__file__).resolve().parents[2].parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
TYK_DIR = REPO_ROOT / "tyk"
ENV_EXAMPLE = REPO_ROOT / "backend" / ".env.example"


def _load_compose() -> dict:
    """Load and parse docker-compose.yml."""
    return yaml.safe_load(COMPOSE_FILE.read_text())


class TestTykGatewayService:
    """AC1: tyk-gateway service configuration."""

    def test_tyk_gateway_service_exists(self):
        compose = _load_compose()
        assert "tyk-gateway" in compose["services"]

    def test_tyk_gateway_image(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        assert svc["image"] == "tykio/tyk-gateway:v5.3"

    def test_tyk_gateway_port(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        assert "8080:8080" in svc["ports"]

    def test_tyk_gateway_volume_tyk_conf(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/tyk.conf" in v for v in volumes)

    def test_tyk_gateway_volume_apps(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/apps" in v for v in volumes)

    def test_tyk_gateway_volume_middleware(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/middleware" in v for v in volumes)

    def test_tyk_gateway_volume_policies(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/policies" in v for v in volumes)


class TestTykRedisService:
    """AC2: tyk-redis service configuration."""

    def test_tyk_redis_service_exists(self):
        compose = _load_compose()
        assert "tyk-redis" in compose["services"]

    def test_tyk_redis_image(self):
        svc = _load_compose()["services"]["tyk-redis"]
        assert svc["image"] == "redis:7-alpine"

    def test_tyk_redis_named_volume(self):
        svc = _load_compose()["services"]["tyk-redis"]
        volumes = svc.get("volumes", [])
        assert any("tyk-redis-data" in v for v in volumes)

    def test_tyk_redis_volume_declared(self):
        compose = _load_compose()
        assert "tyk-redis-data" in compose.get("volumes", {})


class TestTykGatewayDependencies:
    """AC3: tyk-gateway depends on tyk-redis and backend."""

    def test_depends_on_tyk_redis(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        deps = svc.get("depends_on", [])
        # depends_on can be a list or dict
        if isinstance(deps, dict):
            assert "tyk-redis" in deps
        else:
            assert "tyk-redis" in deps

    def test_depends_on_backend(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        deps = svc.get("depends_on", [])
        if isinstance(deps, dict):
            assert "backend" in deps
        else:
            assert "backend" in deps


class TestGatewayProfiles:
    """AC4: Both tyk services under gateway profile — not started by default."""

    def test_tyk_gateway_has_gateway_profile(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        assert "gateway" in svc.get("profiles", [])

    def test_tyk_redis_has_gateway_profile(self):
        svc = _load_compose()["services"]["tyk-redis"]
        assert "gateway" in svc.get("profiles", [])

    def test_existing_services_have_no_profile(self):
        """Default services (frontend, backend, postgres, etc.) have no profiles."""
        compose = _load_compose()
        default_services = ["frontend", "backend", "postgres", "aspire-dashboard", "redis"]
        for name in default_services:
            svc = compose["services"].get(name)
            if svc is not None:
                assert "profiles" not in svc, f"Service '{name}' should not have profiles"


class TestTykGatewaySecret:
    """AC5: TYK_GATEWAY_SECRET sourced from env var substitution, not hardcoded."""

    def test_tyk_gateway_secret_env_var(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        env_list = svc.get("environment", [])
        secret_entries = [e for e in env_list if "TYK_GW_SECRET" in str(e)]
        assert len(secret_entries) == 1
        entry = secret_entries[0]
        # Must reference ${TYK_GATEWAY_SECRET}, not a hardcoded value
        assert "${TYK_GATEWAY_SECRET}" in str(entry)

    def test_tyk_conf_secret_placeholder(self):
        """tyk.conf uses a placeholder (not a real secret)."""
        conf = json.loads((TYK_DIR / "tyk.conf").read_text())
        secret = conf.get("secret", "")
        assert secret == "${TYK_GATEWAY_SECRET}"


class TestEnvExample:
    """AC6: .env.example has TYK_GATEWAY_SECRET placeholder."""

    def test_env_example_has_tyk_gateway_secret(self):
        content = ENV_EXAMPLE.read_text()
        assert "TYK_GATEWAY_SECRET" in content


class TestDefaultComposeUnchanged:
    """AC7: Default compose (no profile) starts only original services."""

    def test_only_default_services_without_profile(self):
        """Services without profiles are the 'default' set."""
        compose = _load_compose()
        default_services = []
        for name, svc in compose["services"].items():
            if "profiles" not in svc:
                default_services.append(name)
        # Original set: frontend, backend, postgres, aspire-dashboard, redis
        expected = {"frontend", "backend", "postgres", "aspire-dashboard", "redis"}
        assert set(default_services) == expected


class TestTykConfigJsonValidity:
    """Validate Tyk JSON config files are syntactically valid with required keys."""

    def test_tyk_conf_valid_json(self):
        conf = json.loads((TYK_DIR / "tyk.conf").read_text())
        assert isinstance(conf, dict)

    def test_tyk_conf_required_keys(self):
        conf = json.loads((TYK_DIR / "tyk.conf").read_text())
        assert conf["listen_port"] == 8080
        assert conf["use_db_app_configs"] is False
        assert conf["storage"]["type"] == "redis"
        assert conf["storage"]["host"] == "tyk-redis"

    def test_api_definition_valid_json(self):
        api_def = json.loads((TYK_DIR / "apps" / "saas-backend.json").read_text())
        assert isinstance(api_def, dict)

    def test_api_definition_required_keys(self):
        api_def = json.loads((TYK_DIR / "apps" / "saas-backend.json").read_text())
        assert api_def["api_id"] == "saas-backend"
        assert api_def["use_openid"] is True
        assert api_def["target_url"] == "http://backend:8000/api/"
        assert api_def["active"] is True


class TestServiceIsolation:
    """tyk-redis and app redis are separate services."""

    def test_two_redis_services(self):
        compose = _load_compose()
        assert "redis" in compose["services"]
        assert "tyk-redis" in compose["services"]

    def test_separate_redis_volumes(self):
        """tyk-redis uses its own named volume, not the app redis volume."""
        compose = _load_compose()
        app_redis = compose["services"]["redis"]
        tyk_redis = compose["services"]["tyk-redis"]
        # App redis has no named volume (no volumes key or different volume)
        app_volumes = app_redis.get("volumes", [])
        tyk_volumes = tyk_redis.get("volumes", [])
        # tyk-redis-data should only appear in tyk-redis
        assert any("tyk-redis-data" in str(v) for v in tyk_volumes)
        assert not any("tyk-redis-data" in str(v) for v in app_volumes)
