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
        ports = svc["ports"]
        assert any("8080" in str(p) for p in ports)

    def test_tyk_gateway_port_localhost_binding(self):
        """Gateway port must bind to 127.0.0.1, matching other services."""
        svc = _load_compose()["services"]["tyk-gateway"]
        ports = svc["ports"]
        assert any("127.0.0.1:8080:8080" in str(p) for p in ports)

    def test_tyk_gateway_volume_tyk_conf(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/tyk.conf" in v for v in volumes)

    def test_tyk_gateway_mounts_apps_volume(self):
        """tyk-gateway mounts the tyk-apps named volume (populated by tyk-init
        sidecar) at /opt/tyk-gateway/apps. It does NOT bind-mount tyk/apps
        directly — the tykio image has no shell for envsubst."""
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk-apps:/opt/tyk-gateway/apps" in v for v in volumes)

    def test_tyk_gateway_volume_middleware(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/middleware" in v for v in volumes)

    def test_tyk_gateway_volume_policies(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        volumes = svc["volumes"]
        assert any("tyk/policies" in v for v in volumes)

    def test_tyk_gateway_restart_policy(self):
        """Gateway should have a restart policy for crash recovery."""
        svc = _load_compose()["services"]["tyk-gateway"]
        assert "restart" in svc


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
        assert "tyk-redis" in deps

    def test_depends_on_backend(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        deps = svc.get("depends_on", [])
        assert "backend" in deps


class TestGatewayProfiles:
    """AC4: Both tyk services under gateway and full profiles — not started by default."""

    def test_tyk_gateway_has_gateway_profile(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        assert "gateway" in svc.get("profiles", [])

    def test_tyk_gateway_has_full_profile(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        assert "full" in svc.get("profiles", [])

    def test_tyk_redis_has_gateway_profile(self):
        svc = _load_compose()["services"]["tyk-redis"]
        assert "gateway" in svc.get("profiles", [])

    def test_tyk_redis_has_full_profile(self):
        svc = _load_compose()["services"]["tyk-redis"]
        assert "full" in svc.get("profiles", [])

    def test_default_services_have_no_profile(self):
        """Minimum-functional default services (frontend, backend, postgres) have no profiles.

        postgres stays in the default set because backend's lifespan does a
        SELECT 1 connectivity check at startup and crashes if the database
        is unreachable. redis and aspire-dashboard moved to the infra
        profile in story 4.1 because backend degrades gracefully without
        them.
        """
        compose = _load_compose()
        default_services = ["frontend", "backend", "postgres"]
        for name in default_services:
            svc = compose["services"].get(name)
            assert svc is not None, f"Service '{name}' must exist"
            assert "profiles" not in svc, f"Service '{name}' must be in the default profile"


class TestTykGatewaySecret:
    """AC5: TYK_GATEWAY_SECRET sourced from env var substitution with fail-fast guard."""

    def test_tyk_gateway_secret_env_var(self):
        svc = _load_compose()["services"]["tyk-gateway"]
        env = svc.get("environment", [])
        # Handle both list and dict forms of environment
        if isinstance(env, dict):
            secret_value = env.get("TYK_GW_SECRET", "")
            assert "TYK_GATEWAY_SECRET" in secret_value
        else:
            secret_entries = [e for e in env if "TYK_GW_SECRET" in str(e)]
            assert len(secret_entries) == 1
            assert "TYK_GATEWAY_SECRET" in str(secret_entries[0])

    def test_tyk_gateway_secret_has_fail_fast_guard(self):
        """TYK_GATEWAY_SECRET must use :? syntax to fail if unset."""
        svc = _load_compose()["services"]["tyk-gateway"]
        env = svc.get("environment", [])
        if isinstance(env, dict):
            secret_value = env.get("TYK_GW_SECRET", "")
        else:
            secret_entries = [e for e in env if "TYK_GW_SECRET" in str(e)]
            secret_value = str(secret_entries[0]) if secret_entries else ""
        assert ":?" in secret_value, "TYK_GATEWAY_SECRET must use :? syntax to abort if unset"

    def test_tyk_conf_secret_is_sentinel(self):
        """tyk.conf uses a sentinel value (not a shell-like variable reference)."""
        conf = json.loads((TYK_DIR / "tyk.conf").read_text())
        secret = conf.get("secret", "")
        # Must not look like a real secret or a shell variable reference
        assert secret == "REPLACE_AT_RUNTIME"


class TestEnvExample:
    """AC6: .env.example has TYK_GATEWAY_SECRET placeholder."""

    def test_env_example_has_tyk_gateway_secret(self):
        content = ENV_EXAMPLE.read_text()
        assert "TYK_GATEWAY_SECRET" in content


class TestDefaultComposeMinimumFunctional:
    """Story 4.1: Default compose (no profile) starts only the minimum
    functional services. redis and aspire-dashboard are gated behind
    --profile infra; tyk-* services are gated behind --profile gateway."""

    def test_only_default_services_without_profile(self):
        """Services without profiles are exactly the minimum-functional set."""
        compose = _load_compose()
        default_services = {name for name, svc in compose["services"].items() if "profiles" not in svc}
        expected = {"frontend", "backend", "postgres"}
        assert default_services == expected

    def test_exactly_three_default_services(self):
        """Default profile starts exactly 3 containers — the minimum functional dev stack."""
        compose = _load_compose()
        default_services = [name for name, svc in compose["services"].items() if "profiles" not in svc]
        assert len(default_services) == 3


class TestDeploymentModeWiring:
    """Story 3.2: DEPLOYMENT_MODE is wired through docker compose.

    The base file sets standalone; the gateway override flips it to gateway.
    Both `make dev-gateway` and `make test-integration-gateway` use the
    multi-file invocation so backend lands in gateway mode whenever Tyk is
    in front of it.
    """

    def test_base_compose_sets_standalone_default(self):
        compose = _load_compose()
        env = compose["services"]["backend"]["environment"]
        assert any("DEPLOYMENT_MODE=standalone" in str(e) for e in env), (
            "backend service must declare DEPLOYMENT_MODE=standalone in the base compose file"
        )

    def test_gateway_override_file_exists(self):
        override = REPO_ROOT / "docker-compose.gateway.yml"
        assert override.exists(), "docker-compose.gateway.yml override file must exist"

    def test_gateway_override_flips_deployment_mode(self):
        override = yaml.safe_load((REPO_ROOT / "docker-compose.gateway.yml").read_text())
        env = override["services"]["backend"]["environment"]
        assert any("DEPLOYMENT_MODE=gateway" in str(e) for e in env), (
            "docker-compose.gateway.yml must override DEPLOYMENT_MODE=gateway"
        )

    def test_makefile_dev_gateway_uses_override(self):
        makefile = (REPO_ROOT / "Makefile").read_text()
        assert "docker-compose.gateway.yml" in makefile, (
            "Makefile must reference docker-compose.gateway.yml so dev-gateway flips DEPLOYMENT_MODE"
        )


class TestInfraProfile:
    """Story 4.1: redis + aspire-dashboard are opt-in via --profile infra."""

    def test_redis_in_infra_profile(self):
        svc = _load_compose()["services"]["redis"]
        assert "infra" in svc.get("profiles", []), "redis must be in the infra profile"

    def test_aspire_dashboard_in_infra_profile(self):
        svc = _load_compose()["services"]["aspire-dashboard"]
        assert "infra" in svc.get("profiles", []), "aspire-dashboard must be in the infra profile"

    def test_postgres_NOT_in_infra_profile(self):
        """postgres stays in the default profile because backend lifespan
        requires it for the SELECT 1 connectivity check at startup."""
        svc = _load_compose()["services"]["postgres"]
        assert "profiles" not in svc, "postgres must remain in the default profile (backend requires it)"

    def test_postgres_password_fail_fast_guard_preserved(self):
        """POSTGRES_PASSWORD must still use :? syntax to prevent silent
        defaults in production. Story 4.1's first iteration weakened this
        to :- changeme — explicitly reverted because it's a security
        regression for any non-dev deployment."""
        compose = _load_compose()
        env = compose["services"]["postgres"]["environment"]
        if isinstance(env, dict):
            password_value = env.get("POSTGRES_PASSWORD", "")
        else:
            entries = [e for e in env if "POSTGRES_PASSWORD" in str(e)]
            password_value = str(entries[0]) if entries else ""
        assert ":?" in password_value, "POSTGRES_PASSWORD must use :? guard, not :- default"


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
        # target_url is the backend root (not /api/) so the full incoming
        # path /api/<x> — which Tyk forwards verbatim with strip_listen_path
        # = false — does not get duplicated to /api/api/<x>.
        assert api_def["target_url"] == "http://backend:8000/"
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
        app_volumes = app_redis.get("volumes", [])
        tyk_volumes = tyk_redis.get("volumes", [])
        # tyk-redis-data should only appear in tyk-redis
        assert any("tyk-redis-data" in str(v) for v in tyk_volumes)
        assert not any("tyk-redis-data" in str(v) for v in app_volumes)
