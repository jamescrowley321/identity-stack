"""A Descope flow sync never links its user to a canonical user holding an organization's email.

A user a Zitadel organization's first login created holds an email that
organization's administrators chose (``User.email_from_organization``); a
Descope identity with the same email is not shown to be that user.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.errors.identity import EmailLinkRefused
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.user import User
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.user import UserRepository
from app.services.inbound_sync import InboundSyncService

EMAIL = "globex-user@globex.test"


@pytest.mark.anyio
async def test_a_flow_sync_does_not_link_a_user_holding_an_organizations_email():
    user_repo = AsyncMock(spec=UserRepository)
    link_repo = AsyncMock(spec=IdPLinkRepository)
    provider_repo = AsyncMock(spec=ProviderRepository)
    provider_repo.get_by_type.return_value = Provider(id=uuid.uuid4(), name="descope", type=ProviderType.descope)
    link_repo.get_by_provider_and_sub.return_value = None
    user_repo.get_by_email.return_value = User(
        id=uuid.uuid4(), email=EMAIL, user_name=EMAIL, email_from_organization=True
    )
    service = InboundSyncService(
        user_repository=user_repo, idp_link_repository=link_repo, provider_repository=provider_repo
    )

    result = await service.sync_user_from_flow(user_id="descope-user-1", email=EMAIL)

    assert result.is_error()
    assert type(result.error) is EmailLinkRefused
    link_repo.create.assert_not_awaited()
    user_repo.create.assert_not_awaited()
    user_repo.commit.assert_not_awaited()
