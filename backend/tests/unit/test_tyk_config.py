"""Unit tests for Tyk gateway configuration directory (Story 1.1)."""

import json
from pathlib import Path

import pytest

# Repo root is three levels up from this test file
REPO_ROOT = Path(__file__).resolve().parents[3]
assert (REPO_ROOT / "Makefile").is_file(), f"REPO_ROOT resolved incorrectly: {REPO_ROOT}"
TYK_DIR = REPO_ROOT / "tyk"


class TestTykDirectoryStructure:
    """AC-1: tyk/ directory exists with expected subdirectories."""

    def test_tyk_directory_exists(self):
        assert TYK_DIR.is_dir()

    def test_tyk_conf_exists(self):
        assert (TYK_DIR / "tyk.conf").is_file()

    def test_apps_directory_exists(self):
        assert (TYK_DIR / "apps").is_dir()

    def test_policies_directory_exists(self):
        assert (TYK_DIR / "policies").is_dir()

    def test_middleware_directory_exists(self):
        assert (TYK_DIR / "middleware").is_dir()


class TestTykConf:
    """AC-2: tyk.conf has required configuration keys."""

    @pytest.fixture
    def tyk_conf(self):
        with open(TYK_DIR / "tyk.conf") as f:
            return json.load(f)

    def test_valid_json(self, tyk_conf):
        assert isinstance(tyk_conf, dict)

    def test_file_based_mode(self, tyk_conf):
        assert tyk_conf["use_db_app_configs"] is False

    def test_redis_host(self, tyk_conf):
        assert tyk_conf["storage"]["host"] == "tyk-redis"

    def test_redis_port(self, tyk_conf):
        assert tyk_conf["storage"]["port"] == 6379

    def test_policy_source_is_file(self, tyk_conf):
        assert tyk_conf["policies"]["policy_source"] == "file"

    def test_policy_record_path(self, tyk_conf):
        assert tyk_conf["policies"]["policy_record_name"] == "/opt/tyk-gateway/policies/policies.json"


class TestNoHardcodedSecret:
    """AC-3: Gateway secret uses env var, not a hardcoded value."""

    def test_secret_uses_env_var(self):
        raw = (TYK_DIR / "tyk.conf").read_text()
        conf = json.loads(raw)
        # Tyk expands ${VAR} in config files via os.ExpandEnv at load time.
        # Must be the exact expected env var reference.
        assert conf["secret"] == "${TYK_GW_SECRET}", f"secret must be '${{TYK_GW_SECRET}}', got '{conf['secret']}'"

    def test_no_literal_secret_value(self):
        raw = (TYK_DIR / "tyk.conf").read_text()
        # Ensure no common hardcoded secret patterns
        lower = raw.lower()
        for bad in ["password", "changeme", "admin", "default", "secret1", "example", "test"]:
            assert bad not in lower, f"tyk.conf contains suspicious literal: {bad}"


class TestPoliciesJson:
    """AC-4: policies.json is valid JSON with default skeleton."""

    @pytest.fixture
    def policies(self):
        with open(TYK_DIR / "policies" / "policies.json") as f:
            return json.load(f)

    def test_valid_json(self, policies):
        assert isinstance(policies, dict)

    def test_has_data_key(self, policies):
        assert "data" in policies
        assert isinstance(policies["data"], list)


class TestAppsGitkeep:
    """AC-1: .gitkeep preserves empty apps directory for future API definitions."""

    def test_gitkeep_exists(self):
        assert (TYK_DIR / "apps" / ".gitkeep").is_file()


class TestMiddlewareGitkeep:
    """AC-5: .gitkeep preserves empty middleware directory."""

    def test_gitkeep_exists(self):
        assert (TYK_DIR / "middleware" / ".gitkeep").is_file()


class TestTykIsolation:
    """AC-6: Removing tyk/ has no impact on backend or frontend code."""

    def test_no_backend_imports_reference_tyk(self):
        """No Python file in backend/app/ imports or references the tyk/ directory."""
        app_dir = REPO_ROOT / "backend" / "app"
        if not app_dir.exists():
            pytest.skip("backend/app not found")
        py_files = list(app_dir.rglob("*.py"))
        assert py_files, f"No .py files found in {app_dir} — vacuous pass"
        for py_file in py_files:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            assert "tyk/" not in content and "tyk\\" not in content, f"{py_file.relative_to(REPO_ROOT)} references tyk/"

    def test_no_frontend_references_tyk(self):
        """No source file in frontend/src/ references the tyk/ directory."""
        frontend_src = REPO_ROOT / "frontend" / "src"
        if not frontend_src.exists():
            pytest.skip("frontend/src not found")
        for src_file in frontend_src.rglob("*"):
            if src_file.is_file() and src_file.suffix in (".ts", ".tsx", ".js", ".jsx"):
                content = src_file.read_text(encoding="utf-8", errors="replace")
                assert "tyk/" not in content and "tyk\\" not in content, (
                    f"{src_file.relative_to(REPO_ROOT)} references tyk/"
                )
