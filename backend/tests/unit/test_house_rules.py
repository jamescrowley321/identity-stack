"""Checks for the house rules in docs/conventions.md that Ruff cannot express.

Each test names the section it enforces. A rule's named exceptions are listed
here exactly as the rule names them, and a stale exception fails the test too, so
the list cannot outlive the code it excuses.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from pathlib import Path

from app.errors import identity as identity_errors
from app.errors.identity import IdentityError
from app.errors.problem_detail import _ERROR_TYPE_MAP

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
ENV_DOC = BACKEND.parent / "docs" / "environment-variables.md"


def _modules(*parts: str) -> list[Path]:
    return sorted(APP.joinpath(*parts).rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(APP).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, str]:
    """Map every node to the name of the innermost function that contains it."""
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            owner[child] = child_name
            visit(child, child_name)

    visit(tree, "<module>")
    return owner


# --- §1: only app/knobs.py reads the environment ------------------------------


def test_only_knobs_reads_the_environment():
    """The issue's acceptance check, ``rg 'os\\.(environ|getenv)' backend/app``, on code only.

    Ruff TID251 bans the same names, but its exemptions are per file and per rule
    code; this test pins the exemption to the one module the rule names.
    """
    offenders = []
    for path in _modules():
        if path == APP / "knobs.py":
            continue
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in ("environ", "getenv", "putenv", "unsetenv")
            ):
                offenders.append(f"{_rel(path)}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                if {alias.name for alias in node.names} & {"environ", "getenv", "putenv", "unsetenv"}:
                    offenders.append(f"{_rel(path)}:{node.lineno}")
    assert not offenders, f"Read configuration through app.knobs (§1): {offenders}"


# --- §2: every variable the app reads is documented ---------------------------

_KNOB_CALL = re.compile(
    r"\b(knob|required_knob|knob_flag|knob_set)\(\s*\"([A-Z][A-Z0-9_]*)\"(?:,\s*\"([^\"]*)\"\s*\))?"
)


def _knob_reads() -> list[tuple[str, str, str | None, str]]:
    """(helper, name, literal default or None, location) for every knob read in app/."""
    reads = []
    for path in _modules():
        if path == APP / "knobs.py":
            continue
        for match in _KNOB_CALL.finditer(path.read_text()):
            helper, name, default = match.groups()
            reads.append((helper, name, default, _rel(path)))
    return reads


def _documented_rows() -> dict[str, str]:
    rows = {}
    for line in ENV_DOC.read_text().splitlines():
        match = re.match(r"\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", line)
        if match:
            rows[match.group(1)] = line
    return rows


def test_knob_reads_are_found():
    """Guard the regex: if it silently matched nothing, the doc check below would pass vacuously."""
    names = {name for _, name, _, _ in _knob_reads()}
    assert {"DATABASE_URL", "DEPLOYMENT_MODE", "ORY_REQUIRE_AUDIENCE", "JIT_ENABLED_PROVIDERS"} <= names


def test_every_knob_is_documented_with_its_default():
    rows = _documented_rows()
    problems = []
    for helper, name, default, where in _knob_reads():
        row = rows.get(name)
        if row is None:
            problems.append(f"{name} ({where}) is not in docs/environment-variables.md")
            continue
        default_cell = row.split("|")[2].strip()
        if helper == "required_knob" or default is None:
            continue
        expected = f"`{default}`" if default else '`""`'
        if expected not in default_cell:
            problems.append(f"{name} ({where}) defaults to {expected}, doc says {default_cell}")
    assert not problems, "§2: " + "; ".join(problems)


# --- §10: service classes return Result ---------------------------------------

# (module, class, method) -> why it returns a plain value.
NON_RESULT_SERVICE_METHODS = {
    ("services/sync_status.py", "SyncStatusService", "record_event"): "appends inside the caller's transaction",
    ("services/user.py", "UserService", "list_canonical_users"): "an unfiltered repository read with no domain error",
}


def test_service_methods_return_result():
    found = set()
    for path in _modules("services"):
        for cls in (node for node in _tree(path).body if isinstance(node, ast.ClassDef)):
            if not cls.name.endswith("Service"):
                continue
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name.startswith("_"):
                    continue
                returns = ast.unparse(method.returns) if method.returns else ""
                if "Result" not in returns:
                    found.add((_rel(path), cls.name, method.name))
    assert found == NON_RESULT_SERVICE_METHODS.keys(), (
        f"§10: public *Service methods must return Result. New: {sorted(found - NON_RESULT_SERVICE_METHODS.keys())}; "
        f"stale exceptions: {sorted(NON_RESULT_SERVICE_METHODS.keys() - found)}"
    )


# --- §11: services and repositories do not import FastAPI ---------------------

FASTAPI_IN_SERVICES = {"services/logout.py"}


def test_services_and_repositories_do_not_import_fastapi():
    found = set()
    for path in _modules("services") + _modules("repositories"):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("fastapi", "starlette"):
                found.add(_rel(path))
            if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] in ("fastapi", "starlette") for a in node.names
            ):
                found.add(_rel(path))
    assert found == FASTAPI_IN_SERVICES, (
        f"§11: new: {sorted(found - FASTAPI_IN_SERVICES)}; stale exceptions: {sorted(FASTAPI_IN_SERVICES - found)}"
    )


# --- §13: every IdentityError kind has a problem-detail mapping ---------------


def test_every_identity_error_is_a_registered_frozen_dataclass():
    kinds = [
        obj
        for _, obj in inspect.getmembers(identity_errors, inspect.isclass)
        if issubclass(obj, IdentityError) and obj is not IdentityError
    ]
    assert kinds, "no IdentityError subclasses found"
    unmapped = [kind.__name__ for kind in kinds if kind not in _ERROR_TYPE_MAP]
    assert not unmapped, f"§13: add to _ERROR_TYPE_MAP in app/errors/problem_detail.py: {unmapped}"
    unfrozen = [kind.__name__ for kind in kinds if not dataclasses.fields(kind) or not kind.__dataclass_params__.frozen]
    assert not unfrozen, f"§13: IdentityError subclasses must be frozen dataclasses: {unfrozen}"


# --- §19: queries live in app/repositories ----------------------------------

QUERY_BUILDERS_OUTSIDE_REPOSITORIES = {
    "routers/tenants.py": "the demo TenantResource table",
    "routers/documents.py": "the demo Document table",
    "dependencies/identity.py": "the reconciliation advisory lock",
    "main.py": "the startup connectivity probe, SELECT 1",
}

_QUERY_CALLS = {"select", "insert", "update", "delete", "text", "execute", "exec"}


def test_queries_live_in_repositories():
    found = set()
    for path in _modules():
        if path.is_relative_to(APP / "repositories") or path.is_relative_to(APP / "models"):
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            # session.execute(...) / conn.execute(...) — not a Redis pipeline's execute() —
            # or a bare select()/text() imported from sqlalchemy/sqlmodel.
            if (
                name in ("execute", "exec")
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in ("session", "conn", "connection")
            ):
                found.add(_rel(path))
            elif name in _QUERY_CALLS - {"execute", "exec"} and isinstance(func, ast.Name):
                found.add(_rel(path))
    expected = QUERY_BUILDERS_OUTSIDE_REPOSITORIES.keys()
    assert found == expected, (
        f"§19: build statements in app/repositories. New: {sorted(found - expected)}; "
        f"stale exceptions: {sorted(expected - found)}"
    )


# --- §16: router code does not read request.state -----------------------------

# (module, function) -> why it reads request.state itself.
REQUEST_STATE_READERS = {
    ("routers/auth.py", "logout"): "needs the principal or None, to fall back to the first configured provider",
    ("routers/protected.py", "canonical_identity"): "checks principal, identity and claims together for one 401",
    ("routers/idp_links.py", "_require_user_in_tenant"): "a raising guard; answers 404, not 401/403, by design",
}


def test_router_code_does_not_read_request_state():
    found = set()
    for path in _modules("routers"):
        tree = _tree(path)
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "state"
                and isinstance(node.value, ast.Name)
                and node.value.id == "request"
            ):
                found.add((_rel(path), owner[node]))
    assert found == REQUEST_STATE_READERS.keys(), (
        "§16: take claims from get_claims/get_current_user/get_tenant_id. "
        f"New: {sorted(found - REQUEST_STATE_READERS.keys())}; "
        f"stale exceptions: {sorted(REQUEST_STATE_READERS.keys() - found)}"
    )
