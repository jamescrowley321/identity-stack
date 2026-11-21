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
import sys
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

_KNOB_HELPERS = ("knob", "required_knob", "knob_flag", "knob_set")


def _knob_reads() -> list[tuple[str, str, str | None, str]]:
    """(helper, name, literal default or None, location) for every knob call in app/.

    Parsed with ``ast`` so docstrings and comments that mention a knob are not reads,
    and so a call's formatting (multi-line, trailing comma) cannot hide its default.
    A non-literal name fails loudly instead of being skipped.
    """
    reads = []
    for path in _modules():
        if path == APP / "knobs.py":
            continue
        for node in ast.walk(_tree(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _KNOB_HELPERS):
                continue
            where = f"{_rel(path)}:{node.lineno}"
            name_arg = node.args[0] if node.args else None
            assert isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str), (
                f"§2: {where} passes a non-literal variable name to {node.func.id}"
            )
            default_arg = node.args[1] if len(node.args) > 1 else None
            default = default_arg.value if isinstance(default_arg, ast.Constant) else None
            reads.append((node.func.id, name_arg.value, default, where))
    return reads


def _documented_rows() -> dict[str, str]:
    """Rows of the Backend table only, so a backend variable documented elsewhere does not count."""
    rows = {}
    in_backend = False
    for line in ENV_DOC.read_text().splitlines():
        if line.startswith("## "):
            in_backend = line.strip() == "## Backend"
            continue
        match = re.match(r"\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", line)
        if in_backend and match:
            rows[match.group(1)] = line
    return rows


def test_knob_reads_are_found():
    """Guard the scan: if it silently matched nothing, the doc check below would pass vacuously."""
    names = {name for _, name, _, _ in _knob_reads()}
    assert {"DATABASE_URL", "DEPLOYMENT_MODE", "ORY_REQUIRE_AUDIENCE", "JIT_ENABLED_PROVIDERS", "CSP_POLICY"} <= names


def test_docstring_mentions_are_not_reads():
    """middleware/factory.py's module docstring quotes a knob call; only real calls count."""
    wheres = [where for _, name, _, where in _knob_reads() if name == "DEPLOYMENT_MODE"]
    # One real call; the docstring's quoted knob("DEPLOYMENT_MODE", ...) would make it two.
    assert len(wheres) == 1, wheres


def test_every_knob_is_documented_with_its_default():
    rows = _documented_rows()
    problems = []
    for helper, name, default, where in _knob_reads():
        row = rows.get(name)
        if row is None:
            problems.append(f"{name} ({where}) is not in the Backend table of docs/environment-variables.md")
            continue
        default_cell = row.split("|")[2].strip()
        if helper == "required_knob" or default is None:
            continue
        expected = f"`{default}`" if default else '`""`'
        if expected not in default_cell:
            problems.append(f"{name} ({where}) defaults to {expected}, doc says {default_cell}")
    assert not problems, "§2: " + "; ".join(problems)


# --- §3: knob_flag switches are named for their secure state -------------------

_INSECURE_FLAG_WORDS = ("SKIP", "DISABLE", "ALLOW_INSECURE", "INSECURE", "BYPASS", "NO_VERIFY")


def test_knob_flag_switches_are_named_for_the_secure_state():
    flags = [(name, where) for helper, name, _, where in _knob_reads() if helper == "knob_flag"]
    assert flags, "no knob_flag call sites found"
    offenders = [f"{name} ({where})" for name, where in flags if any(w in name for w in _INSECURE_FLAG_WORDS)]
    assert not offenders, f"§3: a knob_flag switch must be named for its secure state: {offenders}"


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
    "services/keycloak_membership_lock.py": "the Keycloak membership advisory lock",
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
    ("routers/protected.py", "canonical_identity"): (
        "checks principal, identity and claims together for one 401, and hands the provider to first-login provisioning"
    ),
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
    (
        "services/descope.py",
        "DescopeManagementClient.close",
    ): "lifecycle: closes an injected HTTP client; never called in app/",
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


# --- §4: every router function that calls Descope handles both upstream failures ---

# (module, function) -> why it does not catch both UpstreamStatusError and UpstreamTransportError.
UPSTREAM_HANDLING_EXCEPTIONS: dict[tuple[str, str], str] = {}


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    """Exception names an ``except`` clause catches; a bare ``except:`` counts as ``Exception``."""
    if handler.type is None:
        return {"Exception"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    # Last dotted segment, so ``app.outbound.UpstreamStatusError`` reads like the plain name.
    return {ast.unparse(node).rsplit(".", 1)[-1] for node in nodes}


def _handles_both_upstream_failures(func: ast.AST) -> bool:
    caught = set().union(*(_caught_names(h) for h in ast.walk(func) if isinstance(h, ast.ExceptHandler)))
    return "Exception" in caught or {"UpstreamStatusError", "UpstreamTransportError"} <= caught


def test_router_functions_that_call_descope_handle_upstream_failures():
    """Per function, not per module: one compliant handler cannot hide a non-compliant neighbour.

    A function "calls Descope" when it reads ``descope_client`` or calls a function in
    the same module that does.
    """
    found = set()
    for path in _modules("routers"):
        tree = _tree(path)
        functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        direct = {name for name, func in functions.items() if "descope_client" in ast.unparse(func)}
        for name, func in functions.items():
            calls_helper = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in direct - {name}
                for c in ast.walk(func)
            )
            if (name in direct or calls_helper) and not _handles_both_upstream_failures(func):
                found.add((_rel(path), name))
    expected = UPSTREAM_HANDLING_EXCEPTIONS.keys()
    assert found == expected, (
        f"§4: catch UpstreamStatusError and UpstreamTransportError and answer 502. "
        f"New: {sorted(found - expected)}; stale exceptions: {sorted(expected - found)}"
    )


_ADAPTER = "services/descope_management.py"
_ADAPTER_CLASS = "DescopeManagementAdapter"
_ADAPTER_STATUS_ONLY = "predates #512: catches UpstreamStatusError only, so a transport failure is a 500 (#558)"
_ADAPTER_UNHANDLED = "predates #512: catches no upstream failure, so every Descope error is a 500 (#558)"

# method -> why it does not catch both UpstreamStatusError and UpstreamTransportError.
ADAPTER_UPSTREAM_HANDLING_EXCEPTIONS = {
    **{
        name: _ADAPTER_STATUS_ONLY
        for name in (
            "list_members",
            "invite_member",
            "set_member_status",
            "remove_member",
            "update_profile_attribute",
            "load_tenant_settings",
            "update_tenant_settings",
        )
    },
    **{
        name: _ADAPTER_UNHANDLED
        for name in (
            "create_access_key",
            "list_access_keys",
            "load_tenant_access_key",
            "deactivate_access_key",
            "activate_access_key",
            "delete_access_key",
        )
    },
}


def _calls_the_client(func: ast.AST) -> bool:
    """Whether ``func`` reads ``self._client``."""
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "_client"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        for node in ast.walk(func)
    )


def test_adapter_methods_that_call_descope_handle_upstream_failures():
    """§4, for the routes behind the adapter: the adapter makes the calls, so it is checked."""
    cls = next(
        node for node in _tree(APP / _ADAPTER).body if isinstance(node, ast.ClassDef) and node.name == _ADAPTER_CLASS
    )
    methods = [m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name != "__init__"]
    callers = [m for m in methods if _calls_the_client(m)]
    # Guard the scan: load_profile is the compliant method, so it must be seen and must pass.
    assert "load_profile" in {m.name for m in callers}
    found = {m.name for m in callers if not _handles_both_upstream_failures(m)}
    expected = ADAPTER_UPSTREAM_HANDLING_EXCEPTIONS.keys()
    assert found == expected, (
        f"§4: {_ADAPTER_CLASS} methods catch UpstreamStatusError and UpstreamTransportError and return Error. "
        f"New: {sorted(found - expected)}; stale exceptions: {sorted(expected - found)}"
    )


def test_caught_names_reads_dotted_and_bare_handlers():
    tree = ast.parse(
        "try:\n    pass\nexcept app.outbound.UpstreamStatusError:\n    pass\ntry:\n    pass\nexcept:\n    pass\n"
    )
    handlers = [h for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)]
    assert [_caught_names(h) for h in handlers] == [{"UpstreamStatusError"}, {"Exception"}]


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


# --- §5: the pass-through routers reach Descope only through the adapter -------

ADAPTER_ONLY_ROUTERS = ("users.py", "internal.py", "attributes.py", "accesskeys.py", "protected.py")


def test_pass_through_routers_reach_descope_only_through_the_adapter():
    """§5: no client, no upstream error type and no management URL in these routers."""
    offenders = []
    for name in ADAPTER_ONLY_ROUTERS:
        tree = _tree(APP / "routers" / name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("httpx", "app.outbound", "app.services.descope"):
                offenders.append(f"routers/{name}:{node.lineno} imports {node.module}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"routers/{name}:{node.lineno} imports {alias.name}"
                    for alias in node.names
                    if alias.name in ("httpx", "app.outbound", "app.services.descope")
                ]
            if isinstance(node, ast.Attribute) and node.attr == "descope_client":
                offenders.append(f"routers/{name}:{node.lineno} reads descope_client")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "descope_client":
                offenders.append(f"routers/{name}:{node.lineno} reads descope_client")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/v1/mgmt/" in node.value:
                offenders.append(f"routers/{name}:{node.lineno} builds a management URL")
    assert not offenders, f"§5: call app.services.descope_management.DescopeManagementAdapter instead: {offenders}"


# --- §23-§25: the domain is pure, returns Result and is immutable -------------

# Standard-library modules that do I/O, read the clock or produce randomness.
_IMPURE_STDLIB = {
    "asyncio",
    "http",
    "io",
    "logging",
    "os",
    "pathlib",
    "random",
    "secrets",
    "shutil",
    "socket",
    "sqlite3",
    "subprocess",
    "tempfile",
    "threading",
    "time",
    "urllib",
}
_CLOCK_AND_ID_SOURCES = {"now", "utcnow", "today", "uuid1", "uuid4", "uuid6", "uuid7"}


def _impure_domain_lines(tree: ast.Module) -> list[int]:
    """Lines that import outside the standard library and ``expression``, or name a clock or id source."""
    allowed = (set(sys.stdlib_module_names) - _IMPURE_STDLIB) | {"expression"}
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or ""]
            if {alias.name for alias in node.names} & _CLOCK_AND_ID_SOURCES:
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in _CLOCK_AND_ID_SOURCES:
            lines.append(node.lineno)
            continue
        else:
            continue
        for name in imported:
            in_domain = name == "app.domain" or name.startswith("app.domain.")
            if not in_domain and name.split(".")[0] not in allowed:
                lines.append(node.lineno)
    return lines


def test_domain_modules_are_found():
    """Guard the scans below: over an empty package they would pass vacuously."""
    assert "domain/access/decisions.py" in {_rel(path) for path in _modules("domain")}


def test_domain_is_pure():
    offenders = [f"{_rel(path)}:{line}" for path in _modules("domain") for line in _impure_domain_lines(_tree(path))]
    assert not offenders, f"§23: the domain imports stdlib and expression only; time and ids are passed in: {offenders}"


def test_domain_purity_scan_catches_impure_code():
    impure = ast.parse(
        "import logging\n"
        "import sqlalchemy\n"
        "from app.errors import identity\n"
        "from uuid import uuid4\n"
        "stamp = datetime.now(timezone.utc)\n"
        "new_id = uuid.uuid4()\n"
    )
    pure = ast.parse(
        "import uuid\n"
        "from datetime import datetime\n"
        "from expression import Ok\n"
        "from app.domain.access.values import Role\n"
        "def decide(now: datetime) -> datetime:\n"
        "    return now\n"
    )
    assert _impure_domain_lines(impure) == [1, 2, 3, 4, 5, 6]
    assert _impure_domain_lines(pure) == []


def test_domain_does_not_raise():
    offenders = [
        f"{_rel(path)}:{node.lineno}"
        for path in _modules("domain")
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Raise)
    ]
    assert not offenders, f"§24: a domain function returns Error(...) for an expected failure: {offenders}"


def _is_frozen_dataclass(cls: ast.ClassDef) -> bool:
    for decorator in cls.decorator_list:
        if isinstance(decorator, ast.Call) and ast.unparse(decorator.func) in ("dataclass", "dataclasses.dataclass"):
            return any(
                kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in decorator.keywords
            )
    return False


def test_domain_types_are_frozen_dataclasses_or_enums():
    offenders = []
    for path in _modules("domain"):
        for cls in (node for node in ast.walk(_tree(path)) if isinstance(node, ast.ClassDef)):
            is_enum = any(ast.unparse(base).rsplit(".", 1)[-1] in ("Enum", "StrEnum") for base in cls.bases)
            if not is_enum and not _is_frozen_dataclass(cls):
                offenders.append(f"{_rel(path)}:{cls.name}")
    assert not offenders, f"§25: a domain type is a @dataclass(frozen=True) or an Enum: {offenders}"
