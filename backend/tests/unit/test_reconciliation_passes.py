"""ReconciliationService runs every configured provider's pass and reports each; sync status shows each."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from expression import Error, Ok

from app.errors.identity import ProviderError
from app.repositories.sync_ledger import PendingPushRepository, ReconciliationRunRepository
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.reconciliation import PassOutcome, ReconciliationService
from app.services.sync_status import SyncStatusService


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakePass:
    def __init__(self, provider, result):
        self.provider = provider
        self.result = result
        self.runs = 0

    async def reconcile(self):
        self.runs += 1
        return self.result


def _service(*passes, runs=None, publisher=None):
    return ReconciliationService(
        session=AsyncMock(),
        acquire_lock=AsyncMock(),
        passes=passes,
        run_repository=runs,
        publisher=publisher or AsyncMock(spec=CacheInvalidationPublisher),
    )


@pytest.mark.anyio
class TestEveryProvider:
    async def test_each_pass_runs_and_is_reported(self):
        first = FakePass("keycloak", Ok(PassOutcome(stats={"applied": 2, "pushed": 1}, changes=3)))
        second = FakePass("other", Ok(PassOutcome(stats={"applied": 1}, changes=1)))
        runs = AsyncMock(spec=ReconciliationRunRepository)

        result = await _service(first, second, runs=runs).run()

        assert result.ok == {
            "status": "completed",
            "stats": {"applied": 3, "pushed": 1},
            "providers": {
                "keycloak": {"status": "completed", "stats": {"applied": 2, "pushed": 1}},
                "other": {"status": "completed", "stats": {"applied": 1}},
            },
        }
        assert [call.args[0] for call in runs.record.await_args_list] == ["keycloak", "other"]

    async def test_one_failed_pass_leaves_the_others_reported(self):
        failing = FakePass("keycloak", Error(ProviderError(message="Keycloak could not be read")))
        working = FakePass("other", Ok(PassOutcome(stats={"applied": 0}, changes=0)))
        runs = AsyncMock(spec=ReconciliationRunRepository)

        result = await _service(failing, working, runs=runs).run()

        assert result.ok["status"] == "partial"
        assert result.ok["providers"]["keycloak"] == {"status": "failed", "error": "Keycloak could not be read"}
        assert runs.record.await_args_list[0].kwargs == {
            "status": "failed",
            "stats": {},
            "error": "Keycloak could not be read",
        }
        assert working.runs == 1

    async def test_a_pass_that_raises_is_that_pass_failing(self):
        class Raising:
            provider = "keycloak"

            async def reconcile(self):
                raise RuntimeError("database gone")

        working = FakePass("other", Ok(PassOutcome(stats={}, changes=0)))
        runs = AsyncMock(spec=ReconciliationRunRepository)

        result = await _service(Raising(), working, runs=runs).run()

        assert result.ok["providers"]["keycloak"] == {
            "status": "failed",
            "error": "The keycloak pass failed: RuntimeError",
        }
        assert working.runs == 1

    async def test_every_pass_failed_is_the_first_error(self):
        error = ProviderError(message="Keycloak could not be read")

        result = await _service(FakePass("keycloak", Error(error))).run()

        assert result.error == error

    async def test_a_pass_that_changed_nothing_publishes_nothing(self):
        publisher = AsyncMock(spec=CacheInvalidationPublisher)

        await _service(
            FakePass("keycloak", Ok(PassOutcome(stats={"kept_pending": 1}, changes=0))), publisher=publisher
        ).run()

        publisher.publish_batch.assert_not_awaited()

    async def test_a_pass_that_changed_something_publishes_its_stats(self):
        publisher = AsyncMock(spec=CacheInvalidationPublisher)

        await _service(
            FakePass("keycloak", Ok(PassOutcome(stats={"applied": 1}, changes=1))), publisher=publisher
        ).run()

        publisher.publish_batch.assert_awaited_once_with(operation="reconcile", stats={"applied": 1})


@pytest.mark.anyio
class TestSyncStatus:
    async def test_reports_each_providers_last_pass_and_the_pending_pushes(self):
        finished = datetime(2026, 11, 18, tzinfo=timezone.utc)
        runs = AsyncMock(spec=ReconciliationRunRepository)
        runs.list_all.return_value = [
            SimpleNamespace(
                provider="keycloak", finished_at=finished, status="completed", stats={"pushed": 2}, error=None
            )
        ]
        pending = AsyncMock(spec=PendingPushRepository)
        pending.count.return_value = 3
        providers = AsyncMock()
        providers.list_all.return_value = []
        links = AsyncMock()
        links.count_users_by_provider.return_value = {}
        events = AsyncMock()
        events.latest_per_provider.return_value = {}
        events.latest_overall.return_value = None

        result = await SyncStatusService(
            provider_repository=providers,
            idp_link_repository=links,
            sync_event_repository=events,
            run_repository=runs,
            pending_push_repository=pending,
        ).get_status()

        assert result.ok == {
            "providers": [],
            "last_reconciliation": None,
            "reconciliation": {
                "keycloak": {
                    "finished_at": finished.isoformat(),
                    "status": "completed",
                    "stats": {"pushed": 2},
                    "error": None,
                }
            },
            "pending_pushes": 3,
        }
