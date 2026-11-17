"""The local provider Terraform roots (``infra/keycloak``, ``infra/zitadel``) share one interface.

Each root takes the same variables (``issuer_url``, ``frontend_url``) and gives the same
outputs: ``backend_environment`` and ``frontend_environment``, maps whose keys are the
environment variables the backend and the SPA read, and ``test_credentials``. A key in an
environment map that docs/environment-variables.md does not list for that side is a variable
the app does not read, and fails here. The plain-HTTP rule is written once, in
``infra/modules/provider-urls``.

The roots are read as text: the unit suite runs without Terraform.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INFRA = REPO / "infra"
ENV_DOC = REPO / "docs" / "environment-variables.md"
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


@pytest.mark.parametrize("root", sorted(LOCAL_ROOTS))
@pytest.mark.parametrize("output", sorted(DOC_SECTIONS))
def test_environment_keys_are_variables_the_app_reads(root: str, output: str) -> None:
    keys = _map_keys(_block(_root_text(root), "output", output))
    documented = _documented(DOC_SECTIONS[output])

    assert keys, f"infra/{root} output {output} has no keys: the scan matched nothing"
    assert sorted(keys - documented) == []


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
