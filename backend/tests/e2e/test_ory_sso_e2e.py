"""E2E validation of the Ory SSO provider against the real running backend (FR-21).

The integration capstone (``tests/integration/test_ory_end_to_end.py``) drives a
real Ory JWT through the middleware in-process, against an app assembled by the
test. This suite asks the question that one cannot: does the **shipped** app —
started by uvicorn from ``app.main``, with its real startup wiring, its real
provider list built from the environment, its real rate limiter and error
handlers, reached over a real socket — accept an Ory principal and authorize it
correctly?

That distinction has teeth. ``app.main`` builds its provider list at import time
from ``ORY_ISSUER_URL``; the integration suite works around that by constructing
the middleware directly, so a break in ``app.middleware.factory`` — the code that
turns the environment into providers — would not show up there. Here it would.

Tiers, matching the rest of this suite:
  1. Unauthenticated → 401
  2. Authenticated as an Ory principal, no matching role → 403
  3. Authenticated with the canonical role → 200

Requirements, all supplied by the E2E CI job:
  * ``E2E_ORY_ISSUER_URL`` — the issuer the backend under test was started with.
    Absent, the backend cannot validate an Ory token at all and every test here
    would 401 for a reason that says nothing about the code, so the module skips.
    It is listed in ``_CI_REQUIRED_ENV`` so that skip cannot happen in CI.
  * a reachable Ory provider — the local ``hydra-test`` container by default.
  * ``E2E_DATABASE_URL`` (or ``DATABASE_URL``) — the tier-3 role grant is written
    straight to the canonical model. There is no API for it: ``POST
    /api/roles/assign`` depends on ``get_tenant_id``, which reads the Descope
    ``dct`` claim, so an Ory principal cannot reach it. Granting a role is a
    precondition of the test, not the thing under test.

Running this locally: the backend under test **commits**, and several integration
tests assert against an empty canonical database (``test_get_status_empty_state``,
``test_distinct_subjects_share_default_tenant_and_role``, …). Pointing this suite
at the same Postgres that ``make test-integration`` uses will therefore leave that
suite red until the stack is recycled with ``make test-down && make test-up``. CI
gives the two jobs separate stacks, so the collision is local-only.
"""

import asyncio
import os
import threading
import uuid

import pytest
from playwright.sync_api import APIRequestContext, Playwright
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from tests.helpers.ory import (
    OryTokenSourceMisconfigured,
    OryTokenSourceUnavailable,
    build_ory_token_source,
)

ORY_ISSUER_URL = os.environ.get("E2E_ORY_ISSUER_URL", "")
DATABASE_URL = os.environ.get("E2E_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not ORY_ISSUER_URL,
    reason="E2E_ORY_ISSUER_URL is not set — the backend under test has no Ory provider configured",
)

# The role-gated route with no `get_tenant_id` dependency of its own, so a 403
# here is an authorization decision about the principal rather than a Descope
# claim the principal was never going to carry.
GATED_ROUTE = "/api/sync/status"
GATED_ROLE = "operator"

# An unauthenticated route pair: one canonical, one role-gated. Both must reject.
UNAUTHENTICATED_ROUTES = ("/api/identity", GATED_ROUTE)


@pytest.fixture(scope="module")
def ory_tokens():
    """A real Ory token source, verified to match the issuer the backend trusts."""
    try:
        source = build_ory_token_source()
    except OryTokenSourceMisconfigured as exc:
        # Half a credential group set is never intentional — loud everywhere.
        pytest.fail(str(exc))
    except OryTokenSourceUnavailable as exc:
        if os.environ.get("CI"):
            pytest.fail(
                f"{exc}. In CI this fails rather than skips: a skip here would let the "
                "E2E job report green with the Ory path unexercised."
            )
        pytest.skip(str(exc))

    if source.issuer != ORY_ISSUER_URL.rstrip("/"):
        pytest.fail(
            f"the backend under test trusts issuer {ORY_ISSUER_URL!r} but the token "
            f"source issues {source.issuer!r}. Every assertion below would fail on the "
            "issuer allow-list and tell you nothing about the code under test."
        )
    return source


@pytest.fixture
def bearer_context(playwright: Playwright, backend_url: str):
    """Build API request contexts that present a given bearer token."""
    contexts: list[APIRequestContext] = []

    def _make(token: str) -> APIRequestContext:
        context = playwright.request.new_context(
            base_url=backend_url,
            extra_http_headers={"Authorization": f"Bearer {token}"},
        )
        contexts.append(context)
        return context

    yield _make
    for context in contexts:
        context.dispose()


def _new_principal() -> tuple[str, str]:
    """A subject and email no earlier run has used.

    The E2E database is not rolled back between tests, and the JIT provisioner
    keys on the token's subject, so a reused one would resolve an existing user
    and quietly stop testing first-login provisioning.
    """
    unique = uuid.uuid4()
    return f"ory-e2e-{unique}", f"ory-e2e-{unique}@example.com"


def _run_async(coroutine):
    """Run a coroutine to completion from inside a synchronous Playwright test.

    ``asyncio.run`` is not available here: this suite is sync (Playwright's sync
    API cannot be driven from a running loop) while pytest-asyncio's
    ``asyncio_default_test_loop_scope = "session"`` keeps a loop running for the
    whole session. A worker thread gets a loop of its own and satisfies both.
    """
    outcome: list = []

    def _target() -> None:
        try:
            outcome.append((True, asyncio.run(coroutine)))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            outcome.append((False, exc))

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()
    succeeded, value = outcome[0]
    if not succeeded:
        raise value
    return value


def _grant_gated_role(email: str) -> None:
    """Grant ``GATED_ROLE`` to the canonical user, in the tenant JIT put them in."""
    _run_async(_grant_gated_role_async(email))


async def _grant_gated_role_async(email: str) -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            user = await UserRepository(session).get_by_email(email)
            assert user is not None, f"expected JIT provisioning to have created a user for {email}"
            assignments = await UserTenantRoleRepository(session).list_by_user(user.id)
            assert assignments, "expected JIT provisioning to have placed the user in a tenant"
            tenant_id = assignments[0].tenant_id

            role_repository = RoleRepository(session)
            role = await role_repository.get_by_name(GATED_ROLE, tenant_id=tenant_id)
            if role is None:
                # The default tenant is shared, and (name, tenant_id) is unique —
                # so this creates the role on the first run and reuses it after.
                role = Role(name=GATED_ROLE, description="granted by the Ory E2E suite", tenant_id=tenant_id)
                session.add(role)
                await session.flush()

            session.add(UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=role.id))
            await session.commit()
    finally:
        await engine.dispose()


# =============================================================================
# Tier 1 — unauthenticated
# =============================================================================


@pytest.mark.parametrize("route", UNAUTHENTICATED_ROUTES)
def test_unauthenticated_request_is_rejected(api_context: APIRequestContext, backend_url: str, route: str):
    """Configuring a second provider must not open a route to anonymous callers."""
    assert api_context.get(f"{backend_url}{route}").status == 401


# =============================================================================
# Tier 2 — a real Ory principal
# =============================================================================


def test_ory_jwt_is_accepted_and_jit_provisions_a_canonical_identity(ory_tokens, bearer_context):
    """A real Ory JWT reaches the running backend and becomes a canonical identity.

    This is the whole chain in one assertion set: the shipped provider list built
    from the environment, signature verified against Ory's JWKS, issuer and
    audience checked, first-login JIT provisioning, canonical payload returned.
    """
    sub, email = _new_principal()
    token = ory_tokens.mint(sub=sub, email=email, email_verified=True, given_name="Ory", family_name="Capstone")
    context = bearer_context(token)

    response = context.get("/api/identity")
    assert response.status == 200, response.text()
    body = response.json()

    assert body["user"]["email"] == email
    assert body["user"]["given_name"] == "Ory"
    assert body["user"]["family_name"] == "Capstone"
    assert {"provider_name": "ory", "external_sub": sub} in body["linked_idps"]
    assert body["tenant_memberships"], "JIT provisioning must place the user in a tenant"
    assert {role["role_name"] for role in body["roles"]} == {"member"}

    # Second call is idempotent — the same canonical user, not a second one.
    again = context.get("/api/identity")
    assert again.status == 200
    assert again.json()["user"]["id"] == body["user"]["id"]


def test_ory_principal_without_the_role_is_denied_the_gated_route(ory_tokens, bearer_context):
    """Authenticated is not authorized. A `member` must not reach an operator route.

    The failure mode this guards is the tempting one: making the canonical
    fallback in ``require_role`` grant *something* rather than deny when the
    token carries no authorization claims.
    """
    sub, email = _new_principal()
    context = bearer_context(ory_tokens.mint(sub=sub, email=email, email_verified=True))

    assert context.get("/api/identity").status == 200  # provisions as `member`
    assert context.get(GATED_ROUTE).status == 403


def test_unprovisioned_ory_principal_is_denied_the_gated_route(ory_tokens, bearer_context):
    """A valid token for a subject with no canonical identity at all → 403, not 500.

    Hitting the gated route first means ``require_role`` resolves against a
    subject the canonical model has never seen. It must fail closed, and it must
    not surface as a server error.
    """
    sub, email = _new_principal()
    context = bearer_context(ory_tokens.mint(sub=sub, email=email, email_verified=True))

    assert context.get(GATED_ROUTE).status == 403


# =============================================================================
# Tier 3 — a real Ory principal holding the canonical role
# =============================================================================


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="E2E_DATABASE_URL/DATABASE_URL is not set — the role grant this test needs cannot be written",
)
def test_ory_principal_with_the_canonical_role_reaches_the_gated_route(ory_tokens, bearer_context):
    """The FR-21 endpoint of the chain: an Ory principal authorized by the canonical model.

    Ory tokens carry no ``dct``/``tenants`` claims, so a 200 here is only possible
    because ``require_role`` resolves roles from the canonical model for providers
    that put no authorization in the token. Before that change this returned 403
    "No tenant context" for every Ory principal on every role-gated route.
    """
    sub, email = _new_principal()
    context = bearer_context(ory_tokens.mint(sub=sub, email=email, email_verified=True))

    assert context.get("/api/identity").status == 200
    assert context.get(GATED_ROUTE).status == 403, "precondition: `member` alone must not pass"

    _grant_gated_role(email)

    response = context.get(GATED_ROUTE)
    assert response.status == 200, response.text()
    assert "providers" in response.json()


# =============================================================================
# Adversarial — the controls that separate a valid token from any other token
# =============================================================================


def test_tampered_ory_jwt_is_rejected(ory_tokens, bearer_context):
    """One flipped signature byte → 401. Proves the running backend verifies, not decodes."""
    sub, email = _new_principal()
    header, payload, signature = ory_tokens.mint(sub=sub, email=email, email_verified=True).split(".")
    flipped = "A" if signature[-1] != "A" else "B"
    context = bearer_context(f"{header}.{payload}.{signature[:-1]}{flipped}")

    assert context.get("/api/identity").status == 401


def test_ory_token_minted_for_another_audience_is_rejected(ory_tokens, bearer_context):
    """The confused-deputy case: genuine issuer, genuine signature, wrong audience.

    Same Ory project, same signing key, a token issued for some other relying
    party. Audience is the only thing that binds a token to *this* API, and this
    asserts the running backend enforces it rather than the in-process one.
    """
    other_api = "a-different-relying-party"
    # A client may only request an audience it is registered for, so the other
    # relying party needs a client of its own — asking the suite's client for a
    # foreign audience is refused by the provider before a token is ever issued.
    client_id, client_secret = ory_tokens.register_client(audience=[other_api])
    try:
        sub, email = _new_principal()
        token = ory_tokens.mint(
            sub=sub,
            email=email,
            email_verified=True,
            audience=other_api,
            client_id=client_id,
            client_secret=client_secret,
        )
        context = bearer_context(token)

        assert context.get("/api/identity").status == 401
    finally:
        # Hydra's store is in-memory and discarded with the container, but a live
        # Ory project is not — leaving a client behind there would be litter.
        ory_tokens.delete_client(client_id)


def test_a_structurally_valid_token_from_no_provider_is_rejected(bearer_context):
    """A well-formed JWT nobody in the provider list issued → 401.

    The issuer allow-list runs before any crypto; this asserts it holds against
    the shipped provider list, not a test-constructed one.
    """
    forged = (
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6ImZvcmdlZCJ9"
        ".eyJpc3MiOiJodHRwczovL2F0dGFja2VyLmV4YW1wbGUiLCJzdWIiOiJhdHRhY2tlciIsImF1ZCI6ImlkZW50aXR5LXN0YWNrLWFwaSJ9"
        ".c2lnbmF0dXJl"
    )
    context = bearer_context(forged)

    assert context.get("/api/identity").status == 401
