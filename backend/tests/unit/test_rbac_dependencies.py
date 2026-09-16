"""Unit tests for RBAC dependency factories."""

from unittest.mock import MagicMock

import pytest
from expression import Error, Ok
from fastapi import HTTPException

from app.dependencies import rbac
from app.dependencies.rbac import get_rbac_session, require_permission, require_role
from app.errors.identity import NotFound


def _make_request(claims, auth_type: str | None = None):
    request = MagicMock()
    request.state.claims = claims
    if auth_type is None:
        request.state.principal = None
    else:
        request.state.principal.identity.authentication_type = auth_type
    return request


CLAIMS_ADMIN = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["projects.create", "projects.read", "members.invite"]},
    },
}

CLAIMS_VIEWER = {
    "sub": "user456",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["viewer"], "permissions": ["projects.read", "documents.read"]},
    },
}

CLAIMS_MULTI_ROLE = {
    "sub": "user789",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin", "member"], "permissions": ["projects.read"]},
    },
}

CLAIMS_NO_TENANT = {
    "sub": "user123",
    "tenants": {"tenant-abc": {"roles": ["admin"], "permissions": []}},
}

CLAIMS_NO_TENANTS_KEY = {
    "sub": "user123",
    "dct": "tenant-abc",
}


class TestRequireRole:
    async def test_allows_matching_role(self):
        dep = require_role("admin")
        result = await dep(_make_request(CLAIMS_ADMIN), session=None)
        assert result == ["admin"]

    async def test_allows_any_of_multiple_roles(self):
        dep = require_role("owner", "admin")
        result = await dep(_make_request(CLAIMS_ADMIN), session=None)
        assert "admin" in result

    async def test_rejects_non_matching_role(self):
        dep = require_role("owner")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(CLAIMS_VIEWER), session=None)
        assert exc_info.value.status_code == 403

    async def test_rejects_without_tenant_context(self):
        dep = require_role("admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(CLAIMS_NO_TENANT), session=None)
        assert exc_info.value.status_code == 403

    async def test_rejects_without_claims(self):
        dep = require_role("admin")
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            await dep(request, session=None)
        assert exc_info.value.status_code == 401

    async def test_handles_missing_tenants_key(self):
        dep = require_role("admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(CLAIMS_NO_TENANTS_KEY), session=None)
        assert exc_info.value.status_code == 403

    async def test_multi_role_user(self):
        dep = require_role("member")
        result = await dep(_make_request(CLAIMS_MULTI_ROLE), session=None)
        assert "member" in result


class TestRequirePermission:
    async def test_allows_matching_permission(self):
        dep = require_permission("projects.create")
        result = await dep(_make_request(CLAIMS_ADMIN), session=None)
        assert "projects.create" in result

    async def test_allows_any_of_multiple_permissions(self):
        dep = require_permission("billing.manage", "projects.read")
        result = await dep(_make_request(CLAIMS_VIEWER), session=None)
        assert "projects.read" in result

    async def test_rejects_non_matching_permission(self):
        dep = require_permission("billing.manage")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(CLAIMS_VIEWER), session=None)
        assert exc_info.value.status_code == 403

    async def test_rejects_without_tenant_context(self):
        dep = require_permission("projects.read")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(CLAIMS_NO_TENANT), session=None)
        assert exc_info.value.status_code == 403

    async def test_rejects_without_claims(self):
        dep = require_permission("projects.read")
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            await dep(request, session=None)
        assert exc_info.value.status_code == 401

    async def test_handles_non_dict_tenant_info(self):
        claims = {"sub": "u1", "dct": "t1", "tenants": {"t1": None}}
        dep = require_permission("projects.read")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(claims), session=None)
        assert exc_info.value.status_code == 403


# ──────────────────────────────────────────────
# Canonical-model path (providers with no authorization claims, e.g. Ory)
# ──────────────────────────────────────────────

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

ORY_CLAIMS = {"sub": "ory-subject-1"}


class _StubResolver:
    """Stands in for IdentityResolutionService with a canned resolve() result."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, str]] = []
        self.constructed_with: dict = {}

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return self._result


@pytest.fixture
def stub_resolver(monkeypatch):
    """Patch in a resolver whose result each test sets via ``install(...)``.

    The keyword arguments the dependency constructs the resolver with are kept on
    ``resolver.constructed_with``: one of them (``redis_client``) is a security
    control in its own right, so it has to be observable.
    """

    def install(result) -> _StubResolver:
        resolver = _StubResolver(result)

        def _factory(**kwargs):
            resolver.constructed_with = kwargs
            return resolver

        monkeypatch.setattr(rbac, "IdentityResolutionService", _factory)
        return resolver

    return install


def _grants(*entries) -> Ok:
    return Ok({"roles": [{"tenant_id": t, "role_name": r, "permissions": list(p)} for t, r, p in entries]})


class TestCanonicalRoleResolution:
    async def test_ory_principal_allowed_by_canonical_role(self, stub_resolver):
        resolver = stub_resolver(_grants((TENANT_A, "operator", ["sync.read"])))
        dep = require_role("operator")
        result = await dep(_make_request(ORY_CLAIMS, auth_type="Ory"), session=MagicMock())
        assert result == ["operator"]
        # The provider name is taken from the principal, lowercased to match the
        # `providers` table, not hardcoded.
        assert resolver.calls == [("ory", "ory-subject-1")]

    async def test_ory_principal_rejected_when_canonical_role_does_not_match(self, stub_resolver):
        stub_resolver(_grants((TENANT_A, "member", [])))
        dep = require_role("owner", "admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(ORY_CLAIMS, auth_type="Ory"), session=MagicMock())
        assert exc_info.value.status_code == 403

    async def test_ory_principal_with_no_canonical_identity_is_rejected(self, stub_resolver):
        stub_resolver(Error(NotFound("no identity")))
        dep = require_role("operator")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(ORY_CLAIMS, auth_type="Ory"), session=MagicMock())
        assert exc_info.value.status_code == 403
        # The *reason* matters, not just the code: a caller with no canonical
        # identity must be denied by the resolution branch. Removing that branch
        # and letting an empty role list fall through to the comparison below
        # also produces a 403, so a status-only assertion cannot tell the two
        # apart — and would keep passing with the fail-closed branch deleted.
        assert exc_info.value.detail == "No tenant context"

    async def test_ory_principal_without_database_fails_closed(self, stub_resolver):
        """No database means no canonical answer — 403, never a 500 or an allow."""
        stub_resolver(_grants((TENANT_A, "operator", [])))
        dep = require_role("operator")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(ORY_CLAIMS, auth_type="Ory"), session=None)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "No tenant context"

    async def test_ory_principal_without_subject_is_unauthenticated(self, stub_resolver):
        stub_resolver(_grants((TENANT_A, "operator", [])))
        dep = require_role("operator")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request({}, auth_type="Ory"), session=MagicMock())
        assert exc_info.value.status_code == 401

    async def test_ambiguous_multi_tenant_membership_is_rejected(self, stub_resolver):
        """Two tenants and nothing in the token to choose — picking one would be a guess.

        Asserting the reason is what makes this test able to fail. Replace the
        deny with "pick one" and the outcome depends on which tenant comes out of
        a set first: sometimes an allow, sometimes a different 403. Pinning the
        detail catches both halves of that coin.
        """
        stub_resolver(_grants((TENANT_A, "operator", []), (TENANT_B, "member", [])))
        dep = require_role("operator")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(ORY_CLAIMS, auth_type="Ory"), session=MagicMock())
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "No tenant context"

    async def test_dct_selects_among_multiple_canonical_tenants(self, stub_resolver):
        stub_resolver(_grants((TENANT_A, "operator", []), (TENANT_B, "member", [])))
        claims = {**ORY_CLAIMS, "dct": TENANT_B}
        dep = require_role("member")
        result = await dep(_make_request(claims, auth_type="Ory"), session=MagicMock())
        assert result == ["member"]

    async def test_dct_naming_an_ungranted_tenant_is_rejected(self, stub_resolver):
        stub_resolver(_grants((TENANT_A, "operator", [])))
        claims = {**ORY_CLAIMS, "dct": TENANT_B}
        dep = require_role("operator")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(claims, auth_type="Ory"), session=MagicMock())
        assert exc_info.value.status_code == 403
        # Denied because the token named a tenant the canonical model does not
        # grant — not merely because the role list came out empty afterwards.
        assert exc_info.value.detail == "No tenant context"

    async def test_roles_from_other_tenants_do_not_leak(self, stub_resolver):
        """An `admin` grant in tenant B must not authorize an admin route in tenant A."""
        stub_resolver(_grants((TENANT_A, "member", []), (TENANT_B, "admin", [])))
        claims = {**ORY_CLAIMS, "dct": TENANT_A}
        dep = require_role("admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(claims, auth_type="Ory"), session=MagicMock())
        assert exc_info.value.status_code == 403

    async def test_canonical_permissions_are_aggregated_for_the_tenant(self, stub_resolver):
        stub_resolver(
            _grants(
                (TENANT_A, "operator", ["sync.read"]),
                (TENANT_A, "auditor", ["events.read"]),
                (TENANT_B, "admin", ["billing.manage"]),
            )
        )
        claims = {**ORY_CLAIMS, "dct": TENANT_A}
        dep = require_permission("events.read")
        result = await dep(_make_request(claims, auth_type="Ory"), session=MagicMock())
        assert sorted(result) == ["events.read", "sync.read"]
        assert "billing.manage" not in result

    async def test_authorization_never_reads_the_identity_cache(self, stub_resolver):
        """Roles for an authz decision are resolved uncached, deliberately.

        ``GET /api/identity`` may serve a cached identity up to IDENTITY_CACHE_TTL
        seconds old. An authorization decision may not: a revoked role has to stop
        authorizing when it is revoked, not when a cache entry expires. Handing the
        resolver a Redis client here would silently reintroduce that window, and
        nothing else in this file would notice.
        """
        resolver = stub_resolver(_grants((TENANT_A, "operator", [])))
        dep = require_role("operator")
        await dep(_make_request(ORY_CLAIMS, auth_type="Ory"), session=MagicMock())
        assert resolver.constructed_with["redis_client"] is None


class TestDescopePathIsUnchanged:
    async def test_descope_principal_never_consults_the_canonical_model(self, stub_resolver):
        """A Descope token with no tenant context still 403s even when the canonical
        model would grant the role. Descope's signed claims stay authoritative —
        this change widens nothing for the provider that was already working."""
        resolver = stub_resolver(_grants((TENANT_A, "admin", ["projects.create"])))
        dep = require_role("admin")
        with pytest.raises(HTTPException) as exc_info:
            await dep(_make_request(CLAIMS_NO_TENANT, auth_type="Descope"), session=MagicMock())
        assert exc_info.value.status_code == 403
        assert resolver.calls == []

    async def test_unknown_provider_uses_the_claims_path(self, stub_resolver):
        resolver = stub_resolver(_grants((TENANT_A, "admin", [])))
        dep = require_role("admin")
        result = await dep(_make_request(CLAIMS_ADMIN, auth_type="SomeOtherIdP"), session=MagicMock())
        assert result == ["admin"]
        assert resolver.calls == []


class TestRbacSession:
    async def test_yields_none_when_no_database_is_configured(self, monkeypatch):
        def _raise() -> None:
            raise RuntimeError("DATABASE_URL environment variable is required.")

        monkeypatch.setattr(rbac, "get_session_factory", _raise)
        agen = get_rbac_session()
        assert await agen.__anext__() is None
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
