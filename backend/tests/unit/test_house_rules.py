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


# --- §20: every public service method opens its own span ----------------------

# (module, qualified name) -> why it opens no span of its own.
UNTRACED_SERVICE_CALLABLES = {
    ("services/descope.py", "DescopeManagementClient.close"): "lifecycle: closes the owned HTTP client",
    ("services/canonical.py", "canonical_user"): "pure dict-shaping helper, no I/O",
    ("services/canonical.py", "canonical_tenant"): "pure dict-shaping helper, no I/O",
    ("services/identity_resolution.py", "run_cache_invalidation_subscriber"): "a background loop for the process "
    "lifetime; a span around it would never end",
}


def _span_names(func: ast.AST) -> set[str]:
    """Names passed to ``tracer.start_as_current_span("...")`` in a ``with`` inside ``func``."""
    names = set()
    for node in ast.walk(func):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "start_as_current_span"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "tracer"
                and call.args
                and isinstance(call.args[0], ast.Constant)
            ):
                names.add(call.args[0].value)
    return names


def _has_module_tracer(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "tracer" for t in node.targets)
        and ast.unparse(node.value) == "trace.get_tracer(__name__)"
        for node in tree.body
    )


def _is_abstract(func: ast.AST) -> bool:
    return any(ast.unparse(d).endswith("abstractmethod") for d in func.decorator_list)


def _service_modules() -> list[Path]:
    return [path for path in _modules("services") if not path.is_relative_to(APP / "services" / "adapters")]


def test_public_service_methods_open_their_own_span():
    missing, untraced = [], set()
    for path in _service_modules():
        tree = _tree(path)
        module = _rel(path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                members = [
                    (f"{node.name}.{f.name}", f)
                    for f in node.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not f.name.startswith("_")
                    and not _is_abstract(f)
                ]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                members = [(node.name, node)]
            else:
                continue
            for qualname, func in members:
                if (module, qualname) in UNTRACED_SERVICE_CALLABLES:
                    untraced.add((module, qualname))
                    continue
                expected = qualname if "." in qualname else f"{path.stem}.{qualname}"
                if expected not in _span_names(func):
                    missing.append(f"{module}::{qualname} (expected span {expected!r})")
                elif not _has_module_tracer(tree):
                    missing.append(f"{module} has spans but no module-level tracer = trace.get_tracer(__name__)")
    stale = UNTRACED_SERVICE_CALLABLES.keys() - untraced
    assert not missing, f"§20: {missing}"
    assert not stale, f"§20: stale exceptions: {sorted(stale)}"


def test_logout_strategies_open_a_span_per_logout_kind():
    """The logout strategies are private functions reached through LOGOUT_STRATEGIES."""
    from app.services.logout import LOGOUT_STRATEGIES

    tree = _tree(APP / "services" / "logout.py")
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert _has_module_tracer(tree)
    for kind, strategy in LOGOUT_STRATEGIES.items():
        assert f"logout.{kind}" in _span_names(functions[strategy.__name__]), f"§20: {strategy.__name__}"


def test_abstract_identity_service_has_no_implementation_to_trace():
    """§20 skips abstract methods; if IdentityService gains a concrete subclass, that subclass is traced."""
    from app.services.identity import IdentityService

    assert IdentityService.__abstractmethods__


# --- §4: a handler that catches UpstreamStatusError also catches the transport error ---

# (module, handler) -> why a transport failure still surfaces as 500 here.
STATUS_ONLY_UPSTREAM_HANDLERS = {
    ("routers/users.py", name): "predates #512; answering 502 is a follow-up behaviour change"
    for name in ("list_members", "invite_member", "deactivate_member", "activate_member", "remove_member")
} | {
    ("routers/attributes.py", name): "predates #512; answering 502 is a follow-up behaviour change"
    for name in ("update_profile_attribute", "get_tenant_settings", "update_tenant_settings")
}


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {node.id for node in nodes if isinstance(node, ast.Name)}


def test_handlers_that_catch_upstream_status_also_catch_transport():
    found = set()
    for path in _modules("routers"):
        tree = _tree(path)
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            caught = set().union(*(_caught_names(h) for h in node.handlers)) if node.handlers else set()
            if "UpstreamStatusError" in caught and "UpstreamTransportError" not in caught:
                found.add((_rel(path), owner[node]))
    expected = STATUS_ONLY_UPSTREAM_HANDLERS.keys()
    assert found == expected, (
        f"§4: catch UpstreamTransportError too and answer 502. New: {sorted(found - expected)}; "
        f"stale exceptions: {sorted(expected - found)}"
    )


# Router modules that call Descope without catching any upstream failure.
UNHANDLED_UPSTREAM_ROUTERS = {
    "routers/accesskeys.py": "predates #512; every Descope failure surfaces as 500 (follow-up)",
}

_UPSTREAM_CATCHES = {"UpstreamStatusError", "UpstreamTransportError", "Exception"}


def test_routers_that_call_descope_handle_upstream_failures():
    """§4: catches the module the status-only check cannot see, one that catches nothing at all."""
    found = set()
    for path in _modules("routers"):
        source = path.read_text()
        if "descope_client" not in source:
            continue
        caught = set()
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ExceptHandler):
                caught |= _caught_names(node) if node.type is not None else {"Exception"}
        if not caught & _UPSTREAM_CATCHES:
            found.add(_rel(path))
    expected = UNHANDLED_UPSTREAM_ROUTERS.keys()
    assert found == expected, (
        f"§4: handle upstream failures. New: {sorted(found - expected)}; stale exceptions: {sorted(expected - found)}"
    )


def test_descope_client_spans_leave_status_to_the_caller():
    """§20: every DescopeManagementClient span passes record_exception=False and set_status_on_exception=False."""
    offenders = []
    for node in ast.walk(_tree(APP / "services" / "descope.py")):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "start_as_current_span"
        ):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        for name in ("record_exception", "set_status_on_exception"):
            value = kwargs.get(name)
            if not (isinstance(value, ast.Constant) and value.value is False):
                offenders.append(f"line {node.lineno}: {name}")
    assert not offenders, f"§20: {offenders}"
