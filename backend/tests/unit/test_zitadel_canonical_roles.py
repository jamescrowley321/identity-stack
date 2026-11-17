"""A Zitadel principal's roles and permissions come from the canonical store, never from its token.

The token is the recorded SPA access token (``tests/fixtures/zitadel/``) with
the roles claims that ``accessTokenRoleAssertion`` adds copied from the
recorded roles token, so the token claims a role the canonical store does not
give. The canonical answer is stubbed at ``IdentityResolutionService``, as in
``test_rbac_dependencies.py``.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from expression import Ok
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.dependencies import rbac
from app.dependencies.rbac import require_permission, require_role
from app.middleware.providers import zitadel_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zitadel"
ISSUER = "http://zitadel.localhost:8480"
PROJECT_ID = "399781183140937731"
SPA_CLIENT_ID = "399781187016540163"
TENANT = "11111111-1111-1111-1111-111111111111"
NO_TENANT_CONTEXT = "No tenant context"
ZITADEL = zitadel_config(ISSUER, PROJECT_ID, SPA_CLIENT_ID, require_https=False)


def _claims(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["claims"]


def _token_claiming_roles() -> dict:
    """The SPA token carrying the recorded roles claims, which name ``viewer`` in the user's organization."""
    roles_token = _claims("human-access-token-roles.json")
    role_claims = {name: value for name, value in roles_token.items() if name.endswith(":roles")}
    assert sorted(role_claims) == [
        f"urn:zitadel:iam:org:project:{PROJECT_ID}:roles",
        "urn:zitadel:iam:org:project:roles",
    ]
    return {**_claims("human-access-token-project.json"), **role_claims, "roles": ["admin"]}


CLAIMS = _token_claiming_roles()


def _request(claims: dict):
    request = MagicMock()
    request.headers = Headers()
    request.state.claims = claims
    request.state.provider = ZITADEL
    request.state.principal.identity.authentication_type = ZITADEL.name
    return request


class _StubResolver:
    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, *, provider: str, sub: str):
        self.calls.append((provider, sub))
        return self._result


@pytest.fixture
def canonical(monkeypatch):
    """List ``zitadel`` in ``CANONICAL_RBAC_PROVIDERS`` and stub the canonical answer per test."""
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "zitadel"}))

    def install(role: str, permissions: list[str], status: str = "active") -> _StubResolver:
        resolver = _StubResolver(
            Ok(
                {
                    "user": {"id": "canonical-user-1", "status": status},
                    "roles": [{"tenant_id": TENANT, "role_name": role, "permissions": permissions}],
                }
            )
        )
        monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: resolver)
        return resolver

    return install


class TestTheCanonicalStoreDecides:
    async def test_the_canonical_role_is_granted(self, canonical):
        resolver = canonical("member", ["documents:read"])

        assert await require_role("member")(_request(CLAIMS), session=MagicMock()) == ["member"]
        assert resolver.calls == [("zitadel", CLAIMS["sub"])]

    async def test_a_role_only_the_token_claims_is_refused(self, canonical):
        canonical("member", ["documents:read"])

        for role in ("viewer", "admin"):
            with pytest.raises(HTTPException) as refused:
                await require_role(role)(_request(CLAIMS), session=MagicMock())
            assert refused.value.status_code == 403

    async def test_the_canonical_permissions_are_granted_and_no_other(self, canonical):
        canonical("member", ["documents:read"])

        assert await require_permission("documents:read")(_request(CLAIMS), session=MagicMock()) == ["documents:read"]
        with pytest.raises(HTTPException) as refused:
            await require_permission("settings:manage")(_request(CLAIMS), session=MagicMock())
        assert refused.value.status_code == 403

    async def test_the_answer_is_the_same_without_the_roles_claims(self, canonical):
        canonical("member", ["documents:read"])
        plain = _claims("human-access-token-project.json")

        assert await require_role("member")(_request(plain), session=MagicMock()) == ["member"]
        assert await require_role("member")(_request(CLAIMS), session=MagicMock()) == ["member"]

    @pytest.mark.parametrize("status", ["inactive", "provisioned"])
    async def test_a_deactivated_canonical_user_with_a_valid_token_is_refused(self, canonical, status):
        canonical("admin", ["settings:manage"], status=status)

        with pytest.raises(HTTPException) as refused:
            await require_role("admin")(_request(CLAIMS), session=MagicMock())

        assert refused.value.status_code == 403
        assert refused.value.detail == NO_TENANT_CONTEXT


class TestWithoutTheCanonicalListing:
    async def test_nothing_is_granted(self, monkeypatch):
        """Zitadel has no claims extractor: unlisted, it has no authorization source at all."""
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory"}))

        with pytest.raises(HTTPException) as refused:
            await require_role("viewer")(_request(CLAIMS), session=MagicMock())

        assert refused.value.status_code == 403
