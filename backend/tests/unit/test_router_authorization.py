"""Every tenant-scoped route declares its authorization (docs/conventions.md §15).

A route is *tenant-scoped* when its dependency tree contains ``get_tenant_id`` or
``get_tenant_claims``. Such a route MUST also depend on ``require_role``,
``require_permission`` or ``require_fga``, unless it is listed in
``MEMBER_ROUTES`` below: routes any member of the caller's tenant may call, where
tenant membership *is* the authorization decision.

Adding a route to ``MEMBER_ROUTES`` is a reviewed decision, not a way to make this
test pass. The reason string is the justification a reviewer reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.dependencies.fga import require_fga
from app.dependencies.rbac import require_permission, require_role
from app.dependencies.tenant import get_tenant_claims, get_tenant_id

TENANT_DEPENDENCIES = (get_tenant_id, get_tenant_claims)
AUTHORIZATION_FACTORIES = (require_role, require_permission, require_fga)

# (method, path) -> why tenant membership alone authorizes the route.
MEMBER_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/tenants"): "lists the caller's own memberships from the token",
    ("GET", "/api/tenants/current"): "reads the caller's current tenant",
    ("GET", "/api/tenants/{tenant_id}/resources"): "_verify_tenant_membership checks the path tenant",
    ("POST", "/api/tenants/{tenant_id}/resources"): "_verify_tenant_membership checks the path tenant",
    ("GET", "/api/roles/me"): "reads the caller's own roles",
    ("GET", "/api/tenants/current/settings"): "any member may read settings; PATCH requires admin/owner",
    ("POST", "/api/documents"): "any member may create a document; the creator becomes its FGA owner",
    ("GET", "/api/documents"): "lists only the documents FGA reports the caller can view",
}


def _dependency_calls(dependant: Dependant) -> Iterator[Callable]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _dependency_calls(sub)


def _is_authorization(call: Callable) -> bool:
    """True for the closure a ``require_*`` factory returns."""
    qualname = getattr(call, "__qualname__", "")
    return any(qualname.startswith(f"{factory.__name__}.<locals>.") for factory in AUTHORIZATION_FACTORIES)


def _unauthorized_tenant_routes(app: FastAPI) -> set[tuple[str, str]]:
    """(method, path) of every tenant-scoped route with no authorization dependency."""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = list(_dependency_calls(route.dependant))
        if not any(call in TENANT_DEPENDENCIES for call in calls):
            continue
        if any(_is_authorization(call) for call in calls):
            continue
        found.update((method, route.path) for method in route.methods)
    return found


@pytest.fixture(scope="module")
def application() -> FastAPI:
    from app.main import app

    return app


def test_every_tenant_scoped_route_declares_authorization(application):
    unexpected = _unauthorized_tenant_routes(application) - MEMBER_ROUTES.keys()
    assert not unexpected, (
        "Tenant-scoped routes without require_role/require_permission/require_fga "
        f"(docs/conventions.md §15): {sorted(unexpected)}"
    )


def test_member_routes_register_has_no_stale_entries(application):
    """An entry for a route that is gone, or now declares authorization, is removed."""
    stale = MEMBER_ROUTES.keys() - _unauthorized_tenant_routes(application)
    assert not stale, f"Remove from MEMBER_ROUTES: {sorted(stale)}"


def test_checker_flags_a_tenant_route_without_authorization():
    app = FastAPI()

    @app.get("/bad")
    async def bad(tenant_id: str = Depends(get_tenant_id)):
        return {}

    @app.get("/good")
    async def good(
        tenant_id: str = Depends(get_tenant_id),
        _roles: list[str] = Depends(require_role("admin")),
    ):
        return {}

    @app.get("/nested")
    async def nested(_user: str = Depends(require_fga("document", "can_view"))):
        return {}

    @app.get("/untenanted")
    async def untenanted():
        return {}

    assert _unauthorized_tenant_routes(app) == {("GET", "/bad")}


def test_checker_sees_decorator_level_dependencies():
    app = FastAPI()

    @app.get("/decorated", dependencies=[Depends(get_tenant_claims)])
    async def decorated():
        return {}

    @app.get("/decorated-ok", dependencies=[Depends(get_tenant_claims), Depends(require_permission("x"))])
    async def decorated_ok():
        return {}

    assert _unauthorized_tenant_routes(app) == {("GET", "/decorated")}
