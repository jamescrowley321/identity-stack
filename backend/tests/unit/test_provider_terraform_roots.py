"""The local provider Terraform roots (``infra/keycloak``, ``infra/zitadel``) share one interface.

Each root takes the same variables (``issuer_url``, ``frontend_url``) and gives the same
outputs: ``backend_environment`` and ``frontend_environment``, maps whose keys are the
environment variables the backend and the SPA read, and ``test_credentials``. A key in an
environment map that docs/environment-variables.md does not list for that side is a variable
the app does not read, and fails here; a ``backend_environment`` key must also be a name the
backend reads through ``app.knobs``, and every setting the backend reads for the root's
provider must be one of its keys. The plain-HTTP rule is written once, in
``infra/modules/provider-urls``.

The roots are read as text: the unit suite runs without Terraform.
"""

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
INFRA = REPO / "infra"
ENV_DOC = REPO / "docs" / "environment-variables.md"
BACKEND_APP = REPO / "backend" / "app"
# The ``app.knobs`` readers: each takes the variable's name as its first argument.
KNOB_READERS = frozenset({"knob", "knob_flag", "knob_set", "required_knob"})
# Settings the backend reads for a root's provider that the root does not set: tuning
# knobs with a default, which an operator sets only to change it.
NOT_FROM_THE_ROOT = {
    # The event pull's tuning, read with defaults; the root emits what names the realm and its clients.
    "keycloak": {"KEYCLOAK_PULL_EVENTS", "KEYCLOAK_PULL_INTERVAL_SECONDS", "KEYCLOAK_PULL_PAGE_SIZE"},
    "zitadel": {"ZITADEL_ADMIN_TIMEOUT_SECONDS", "ZITADEL_POST_LOGOUT_REDIRECT_URI"},
}
URL_MODULE = INFRA / "modules" / "provider-urls"

# Each local root and the prefix of the backend variables it configures.
LOCAL_ROOTS = {"keycloak": "KEYCLOAK_", "zitadel": "ZITADEL_"}
COMMON_OUTPUTS = ("backend_environment", "frontend_environment", "test_credentials")
SENSITIVE_OUTPUTS = ("backend_environment", "test_credentials")
COMMON_VARIABLES = ("issuer_url", "frontend_url")
# The per-provider names the common variables replaced.
RETIRED_VARIABLES = ("keycloak_url", "zitadel_url", "realm")
# The doc's section for each environment map.
DOC_SECTIONS = {"backend_environment": "## Backend", "frontend_environment": "## Frontend"}
# The host part of the plain-HTTP rule: a *.localhost name.
LOOPBACK_MARKER = "\\\\.localhost"


def _root_text(root: str) -> str:
    return "\n".join(path.read_text() for path in sorted((INFRA / root).glob("*.tf")))


def _block(text: str, kind: str, name: str) -> str:
    """The body of ``<kind> "<name>" { ... }``, braces balanced."""
    match = re.search(rf'^{kind} "{re.escape(name)}" \{{', text, re.MULTILINE)
    if match is None:
        raise LookupError(f'no {kind} "{name}"')
    depth = 0
    for index in range(match.end() - 1, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[match.end() : index]
    raise LookupError(f'{kind} "{name}" is not closed')


def _map_keys(output_body: str) -> set[str]:
    """Keys of the object literal assigned to ``value``: the names at its first nesting level."""
    start = re.search(r"^\s*value\s*=\s*\{", output_body, re.MULTILINE)
    if start is None:
        raise LookupError("output value is not an object literal")
    keys = set()
    depth = 1
    line_start = True
    position = start.end()
    while depth and position < len(output_body):
        char = output_body[position]
        if line_start and depth == 1:
            key = re.match(r"[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(?!=)", output_body[position:])
            if key:
                keys.add(key.group(1))
        depth += {"{": 1, "[": 1, "(": 1, "}": -1, "]": -1, ")": -1}.get(char, 0)
        line_start = char == "\n"
        position += 1
    return keys


def _documented(section: str) -> set[str]:
    names = set()
    inside = False
    for line in ENV_DOC.read_text().splitlines():
        if line.startswith("## "):
            inside = line.startswith(section)
            continue
        match = re.match(r"\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", line)
        if inside and match:
            names.add(match.group(1))
    return names


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_root_gives_the_common_outputs(root: str) -> None:
    text = _root_text(root)
    declared = {name: _block(text, "output", name) for name in COMMON_OUTPUTS}
    sensitive = sorted(name for name, body in declared.items() if re.search(r"^\s*sensitive\s*=\s*true", body, re.M))
    assert sensitive == sorted(SENSITIVE_OUTPUTS)


# What tests and set-up read from each root besides the environment maps: the issuer, the SPA's
# client id, another client's id for negative tests, the API audience and the tenants'
# organizations. Zitadel's audience is the project id and its organizations are keyed ids;
# Keycloak's audience is the API client id and a token names organizations by alias.
ROOT_IDENTIFIERS = {
    "keycloak": ("issuer_url", "spa_client_id", "other_client_id", "api_audience", "organization_aliases"),
    "zitadel": ("issuer_url", "spa_client_id", "other_client_id", "project_id", "organization_ids"),
}
# The backend settings that bind a token to the SPA: the issuer, the audience and the client id.
TOKEN_BINDING_SETTINGS = ("ISSUER_URL", "AUDIENCE", "CLIENT_ID")


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_root_names_the_issuer_clients_audience_and_organizations(root: str) -> None:
    declared = set(re.findall(r'^output "([a-z_]+)"', _root_text(root), re.MULTILINE))

    assert sorted(set(ROOT_IDENTIFIERS[root]) - declared) == []


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_backend_environment_binds_tokens_to_the_spa(root: str) -> None:
    keys = _map_keys(_block(_root_text(root), "output", "backend_environment"))
    expected = {f"{LOCAL_ROOTS[root]}{name}" for name in TOKEN_BINDING_SETTINGS}

    assert sorted(expected - keys) == []


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
@pytest.mark.parametrize("output", sorted(DOC_SECTIONS))
def test_environment_keys_are_variables_the_app_reads(root: str, output: str) -> None:
    keys = _map_keys(_block(_root_text(root), "output", output))
    documented = _documented(DOC_SECTIONS[output])

    assert keys, f"infra/{root} output {output} has no keys: the scan matched nothing"
    assert sorted(keys - documented) == []


def _backend_knobs() -> set[str]:
    """Every variable name the backend reads through ``app.knobs``: the string literal its reader is called with."""
    names: set[str] = set()
    for path in sorted(BACKEND_APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in KNOB_READERS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


def test_the_knob_scan_finds_the_backends_settings() -> None:
    """The scan reads the calls it is meant to: a guard against a scan that matches nothing."""
    assert {"KEYCLOAK_ISSUER_URL", "ZITADEL_ISSUER_URL", "CANONICAL_RBAC_PROVIDERS"} <= _backend_knobs()


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_backend_environment_is_exactly_what_the_backend_reads_for_the_provider(root: str) -> None:
    """A key no ``knob(...)`` reads, or a setting the backend reads that the root does not emit, fails here."""
    keys = _map_keys(_block(_root_text(root), "output", "backend_environment"))
    read = {name for name in _backend_knobs() if name.startswith(LOCAL_ROOTS[root])} - NOT_FROM_THE_ROOT[root]

    assert sorted(keys - read) == []
    assert sorted(read - keys) == []


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_backend_environment_configures_only_its_provider(root: str) -> None:
    keys = _map_keys(_block(_root_text(root), "output", "backend_environment"))

    assert sorted(key for key in keys if not key.startswith(LOCAL_ROOTS[root])) == []


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_frontend_environment_is_one_flat_map(root: str) -> None:
    """One SPA build per root: the keys are VITE_* names, not app clients."""
    keys = _map_keys(_block(_root_text(root), "output", "frontend_environment"))

    assert sorted(key for key in keys if not key.startswith("VITE_OIDC_")) == []


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_root_takes_the_common_variables(root: str) -> None:
    text = _root_text(root)
    declared = set(re.findall(r'^variable "([a-z_]+)"', text, re.MULTILINE))

    assert sorted(set(COMMON_VARIABLES) - declared) == []
    assert sorted(declared & set(RETIRED_VARIABLES)) == []


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_plain_http_rule_is_the_shared_module(root: str) -> None:
    text = _root_text(root)
    sources = re.findall(r'^module "[^"]+" \{[^}]*?^\s*source\s*=\s*"([^"]+)"', text, re.MULTILINE | re.DOTALL)

    assert sources == ["../modules/provider-urls"]
    assert LOOPBACK_MARKER not in text


def test_shared_module_holds_the_plain_http_rule() -> None:
    text = "\n".join(path.read_text() for path in sorted(URL_MODULE.glob("*.tf")))

    assert LOOPBACK_MARKER in text


# --- The e2e set-up reads the outputs ---------------------------------------------

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO / "Makefile"
# The CI job and the make target that run each root's e2e suite.
E2E_JOBS = {"keycloak": "e2e-keycloak", "zitadel": "e2e-zitadel"}
E2E_TARGETS = {"keycloak": "test-e2e-keycloak", "zitadel": "test-e2e-zitadel"}
# The admin adapter's credential in backend_environment: it writes to the provider, and the e2e
# backends do not sync to it.
ADMIN_CREDENTIALS = {"keycloak": "KEYCLOAK_ADMIN_CLIENT_", "zitadel": "ZITADEL_ADMIN_TOKEN"}
# The e2e backend that syncs with its provider, and so takes the admin credential:
# the Keycloak suites exercise the push, the event pull and reconciliation.
SYNCING_E2E_ROOTS = {"keycloak"}
BACKEND_STEP = "Start backend"


def _e2e_job(root: str) -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"][E2E_JOBS[root]]


def _job_env_names(job: dict) -> set[str]:
    names = set(job.get("env", {}))
    for step in job["steps"]:
        names |= set(step.get("env", {}))
    return names


def _step_run(job: dict, name: str) -> str:
    runs = {step["name"]: step.get("run", "") for step in job["steps"] if "name" in step}
    return runs[name]


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_e2e_job_applies_the_root(root: str) -> None:
    runs = "\n".join(step.get("run", "") for step in _e2e_job(root)["steps"])

    assert f"terraform -chdir=infra/{root} apply" in runs


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_e2e_job_writes_no_provider_setting_by_hand(root: str) -> None:
    """The provider's backend and VITE_OIDC_* settings come from the outputs, not the job."""
    provider_settings = {
        name
        for name in _documented(DOC_SECTIONS["backend_environment"]) | _documented(DOC_SECTIONS["frontend_environment"])
        if name.startswith((LOCAL_ROOTS[root], "VITE_OIDC_"))
    }

    assert sorted(_job_env_names(_e2e_job(root)) & provider_settings) == []


def _jq(filter_text: str, document: object, *options: str) -> list[str]:
    """Lines printed by ``jq <options> <filter>`` over ``document``; jq is required, as in CI."""
    jq = shutil.which("jq")
    assert jq is not None, "jq is not on PATH: the CI steps and make targets under test run it"
    printed = subprocess.run(
        [jq, *options, filter_text],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        check=True,
    )
    return printed.stdout.splitlines()


def _sample_environment(root: str, output: str) -> dict[str, str]:
    """Every key of the root's output, each with a value naming it."""
    return {key: f"value of {key}" for key in _map_keys(_block(_root_text(root), "output", output))}


@pytest.mark.parametrize("root", sorted(set(LOCAL_ROOTS) - SYNCING_E2E_ROOTS))
def test_e2e_backend_reads_backend_environment_without_the_admin_credential(root: str) -> None:
    """The step's own jq filter, run over every backend_environment key, exports all but the admin credential."""
    environment = _sample_environment(root, "backend_environment")
    expected = sorted(key for key in environment if not key.startswith(ADMIN_CREDENTIALS[root]))

    assert sorted(environment) != expected, f"infra/{root} backend_environment has no admin credential to drop"
    assert _exported_backend_environment(root, environment) == expected
    assert _e2e_job_step_env(root).get("SYNC_PROVIDER", "descope") != root


@pytest.mark.parametrize("root", sorted(SYNCING_E2E_ROOTS))
def test_a_syncing_e2e_backend_reads_all_of_backend_environment(root: str) -> None:
    """The step's own jq filter exports every backend_environment key, the admin credential included."""
    environment = _sample_environment(root, "backend_environment")

    assert any(key.startswith(ADMIN_CREDENTIALS[root]) for key in environment)
    assert _exported_backend_environment(root, environment) == sorted(environment)
    assert _e2e_job_step_env(root)["SYNC_PROVIDER"] == root


def _exported_backend_environment(root: str, environment: dict[str, str]) -> list[str]:
    run = _step_run(_e2e_job(root), BACKEND_STEP)
    filters = [text for text in re.findall(r"jq -er '([^']*)'", run) if "to_entries" in text]
    assert f"terraform -chdir=infra/{root} output -json backend_environment" in run
    assert len(filters) == 1, f"{BACKEND_STEP} should export through one to_entries jq filter: {filters}"
    exported = _jq(filters[0], environment, "-er")
    return sorted(line.split("=", 1)[0].removeprefix("export ") for line in exported)


def _e2e_job_step_env(root: str) -> dict:
    (step,) = [step for step in _e2e_job(root)["steps"] if step.get("name") == BACKEND_STEP]
    return step.get("env", {})


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_e2e_target_reads_the_outputs(root: str) -> None:
    recipe = re.search(rf"^{E2E_TARGETS[root]}:.*\n((?:\t.*\n)+)", MAKEFILE.read_text(), re.MULTILINE)

    assert recipe is not None
    assert f"terraform -chdir=infra/{root} output -json" in recipe.group(1)


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
def test_env_target_prints_both_environments_from_the_outputs(root: str, tmp_path: Path) -> None:
    """``make <root>-env`` reads ``terraform -chdir=infra/<root> output -json`` and prints both maps."""
    outputs = {
        output: {"sensitive": output == "backend_environment", "value": _sample_environment(root, output)}
        for output in ("backend_environment", "frontend_environment")
    }
    expected_call = f"-chdir=infra/{root} output -json"
    fake_terraform = tmp_path / "terraform"
    fake_terraform.write_text(
        "#!/bin/sh\n"
        f'[ "$*" = "{expected_call}" ] || {{ echo "unexpected terraform $*" >&2; exit 2; }}\n'
        f"cat <<'JSON'\n{json.dumps(outputs)}\nJSON\n"
    )
    fake_terraform.chmod(0o755)
    make = shutil.which("make")
    assert make is not None, "make is not on PATH"

    printed = subprocess.run(
        [make, "-s", "-C", str(REPO), f"{root}-env"],
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )

    sections: dict[str, dict[str, str]] = {}
    current = ""
    for line in printed.stdout.splitlines():
        if line.startswith("# "):
            current = line.removeprefix("# ")
            sections[current] = {}
        elif line:
            name, value = line.split("=", 1)
            sections[current][name] = value
    quoted = {output: {name: f"'{value}'" for name, value in outputs[output]["value"].items()} for output in outputs}
    assert sections == {
        "backend/.env": quoted["backend_environment"],
        "frontend/.env": quoted["frontend_environment"],
    }


def _realm_issuer_pattern() -> str:
    """``local.realm_issuer`` of infra/keycloak: the regex that splits issuer_url into base URL and realm."""
    found = re.search(r'^\s*realm_issuer\s*=\s*"([^"]*)"', _root_text("keycloak"), re.MULTILINE)
    assert found is not None, "infra/keycloak has no local.realm_issuer"
    return found.group(1)


KEYCLOAK_DEFAULT_ISSUER = "http://keycloak.localhost:8180/realms/identity-stack-terraform"


def test_keycloak_issuer_splits_into_base_url_and_realm() -> None:
    assert re.fullmatch(_realm_issuer_pattern(), KEYCLOAK_DEFAULT_ISSUER).groups() == (
        "http://keycloak.localhost:8180",
        "identity-stack-terraform",
    )


@pytest.mark.parametrize(
    "issuer_url",
    [
        "http://keycloak.localhost:8180//realms/identity-stack-terraform",
        "http://keycloak.localhost:8180/auth//realms/identity-stack-terraform",
        "http://keycloak.localhost:8180/realms/",
        "http://keycloak.localhost:8180/realms/a/b",
    ],
)
def test_keycloak_issuer_with_a_base_url_ending_in_a_slash_is_refused(issuer_url: str) -> None:
    """Keycloak's iss never holds ``//realms/``: such an issuer_url would never match a token."""
    assert re.fullmatch(_realm_issuer_pattern(), issuer_url) is None


# test-e2e-keycloak's suite settings, as the fake python below prints them.
KEYCLOAK_TEST_SETTINGS = (
    "KEYCLOAK_TEST_BASE_URL",
    "KEYCLOAK_TEST_REALM",
    "KEYCLOAK_TEST_USER_IDS",
    "KEYCLOAK_TEST_USER_PASSWORDS",
    "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET",
)


def _keycloak_outputs() -> dict:
    backend = _sample_environment("keycloak", "backend_environment")
    backend["KEYCLOAK_ISSUER_URL"] = KEYCLOAK_DEFAULT_ISSUER
    backend["KEYCLOAK_ADMIN_CLIENT_SECRET"] = "admin $secret"
    return {
        "backend_environment": {"sensitive": True, "value": backend},
        "test_credentials": {"sensitive": True, "value": {"users": {"alice": "pw 'a'"}, "clients": {}}},
        "user_ids": {"sensitive": False, "value": {"alice": "id-a"}},
    }


def _run_e2e_keycloak_target(outputs: dict, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """``make test-e2e-keycloak`` with a fake terraform answering ``outputs`` and a fake python printing settings."""
    expected_call = "-chdir=infra/keycloak output -json"
    fake_terraform = tmp_path / "terraform"
    fake_terraform.write_text(
        "#!/bin/sh\n"
        f'[ "$*" = "{expected_call}" ] || {{ echo "unexpected terraform $*" >&2; exit 2; }}\n'
        f"cat <<'JSON'\n{json.dumps(outputs)}\nJSON\n"
    )
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n" + "".join(f"printf '%s\\0%s\\0' {name} \"${name}\"\n" for name in KEYCLOAK_TEST_SETTINGS)
    )
    for fake in (fake_terraform, fake_python):
        fake.chmod(0o755)
    make = shutil.which("make")
    assert make is not None, "make is not on PATH"
    environment = {name: value for name, value in os.environ.items() if not name.startswith("KEYCLOAK_TEST_")}
    return subprocess.run(
        [make, "-s", "-C", str(REPO), "test-e2e-keycloak"],
        env={**environment, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_e2e_keycloak_target_gives_the_suite_the_applied_realm(tmp_path: Path) -> None:
    outputs = _keycloak_outputs()

    ran = _run_e2e_keycloak_target(outputs, tmp_path)

    assert ran.returncode == 0, ran.stderr
    fields = ran.stdout.split("\0")[:-1]
    settings = dict(zip(fields[::2], fields[1::2], strict=True))
    assert settings == {
        "KEYCLOAK_TEST_BASE_URL": "http://keycloak.localhost:8180",
        "KEYCLOAK_TEST_REALM": "identity-stack-terraform",
        "KEYCLOAK_TEST_USER_IDS": json.dumps({"alice": "id-a"}, separators=(",", ":")),
        "KEYCLOAK_TEST_USER_PASSWORDS": json.dumps({"alice": "pw 'a'"}, separators=(",", ":")),
        "KEYCLOAK_TEST_ADMIN_CLIENT_SECRET": "admin $secret",
    }


def _without(path: tuple[str, ...]):
    def change(outputs: dict) -> None:
        *parents, last = path
        node = outputs
        for key in parents:
            node = node[key]
        del node[last]

    return change


def _issuer(value: str):
    def change(outputs: dict) -> None:
        outputs["backend_environment"]["value"]["KEYCLOAK_ISSUER_URL"] = value

    return change


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (_without(("backend_environment",)), "backend_environment"),
        (_issuer("http://keycloak.localhost:8180/identity-stack-terraform"), "KEYCLOAK_ISSUER_URL"),
        (_issuer("http://keycloak.localhost:8180//realms/identity-stack-terraform"), "KEYCLOAK_ISSUER_URL"),
        (_without(("backend_environment", "value", "KEYCLOAK_ADMIN_CLIENT_SECRET")), "KEYCLOAK_ADMIN_CLIENT_SECRET"),
        (_without(("user_ids",)), "user_ids"),
        (_without(("test_credentials", "value", "users")), "test_credentials"),
    ],
    ids=["no-backend", "issuer-without-realm", "issuer-double-slash", "no-admin", "no-ids", "no-users"],
)
def test_e2e_keycloak_target_refuses_outputs_it_cannot_read(change, named: str, tmp_path: Path) -> None:
    """A missing output or setting fails the target naming it, before the suite runs with a ``null``."""
    outputs = _keycloak_outputs()
    change(outputs)

    ran = _run_e2e_keycloak_target(outputs, tmp_path)

    assert ran.returncode != 0
    assert ran.stdout == ""
    assert named in ran.stderr


def test_ci_masks_only_values_it_captured_first() -> None:
    """``echo "::add-mask::$(...)"`` drops the command's failure: the step would go on with nothing masked."""
    assert re.findall(r"::add-mask::\$\(.*", CI_WORKFLOW.read_text()) == []
