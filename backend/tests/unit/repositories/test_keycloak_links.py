"""Which ``keycloak`` IdP link the Keycloak adapter follows to an account (``RepositoryKeycloakLinks``), on Postgres.

As the Zitadel adapter: a link a sign-in made, and one the backend made (an
account the adapter created, or one ``seed_keycloak --link-users`` names). An
admin-made link names whatever account the admin typed in and is not followed.
"""

import uuid

import pytest

from app.dependencies.keycloak_sync import FOLLOWED_LINK_ORIGINS, RepositoryKeycloakLinks
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.user import LINK_ORIGIN, LINKED_AT_SIGN_IN, LINKED_BY_BACKEND, IdPLink, User, UserStatus
from app.services.adapters.keycloak import PROVIDER_NAME

pytestmark = pytest.mark.asyncio

ACCOUNT_ID = "3cb3c52a-0000-4000-8000-000000000b0b"


async def _linked_user(db_session, metadata: dict | None) -> User:
    provider = Provider(
        name=PROVIDER_NAME,
        type=ProviderType.oidc,
        issuer_url="http://keycloak.test/realms/identity-stack",
        base_url="http://keycloak.test/realms/identity-stack",
        capabilities=["sso"],
    )
    user = User(email=f"bob-{uuid.uuid4().hex[:8]}@globex.test", user_name="bob", status=UserStatus.active)
    db_session.add_all([provider, user])
    await db_session.flush()
    db_session.add(IdPLink(user_id=user.id, provider_id=provider.id, external_sub=ACCOUNT_ID, metadata_=metadata))
    await db_session.flush()
    return user


def test_the_followed_origins_are_a_sign_ins_and_the_backends():
    assert FOLLOWED_LINK_ORIGINS == {LINKED_AT_SIGN_IN, LINKED_BY_BACKEND}


@pytest.mark.parametrize(
    "metadata",
    [{LINK_ORIGIN: LINKED_AT_SIGN_IN}, {LINK_ORIGIN: LINKED_BY_BACKEND}],
    ids=["sign-in", "backend-or-seed"],
)
async def test_a_link_a_sign_in_or_the_backend_made_is_followed(db_session, metadata):
    user = await _linked_user(db_session, metadata)

    assert await RepositoryKeycloakLinks(db_session).linked_subject(user.id) == ACCOUNT_ID


@pytest.mark.parametrize("metadata", [None, {}, {LINK_ORIGIN: "admin"}], ids=["admin-route", "empty", "other-origin"])
async def test_an_admin_made_link_is_not_followed(db_session, metadata):
    user = await _linked_user(db_session, metadata)
    links = RepositoryKeycloakLinks(db_session)

    assert (await links.linked_subject(user.id), await links.has_link(user.id)) == (None, True)
