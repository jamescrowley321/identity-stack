import uuid

import pytest

from app.services.keycloak_membership_lock import hold_keycloak_membership_lock


@pytest.mark.anyio
async def test_membership_lock_setup_failure_is_not_acquired():
    class FailingSession:
        def get_bind(self):
            raise RuntimeError("boom")

    session = FailingSession()

    async with hold_keycloak_membership_lock(session, uuid.uuid4(), uuid.uuid4()) as acquired:
        assert acquired is False
