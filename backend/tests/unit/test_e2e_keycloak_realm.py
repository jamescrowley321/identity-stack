"""The Keycloak e2e set-up's reconciliation passes (``tests/e2e_keycloak/realm.py``)."""

import httpx
import pytest

from tests.e2e_keycloak import realm

BACKEND = "http://backend.test"


def _stats(applied: int = 0, failed: int = 0, push_failed: int = 0) -> dict:
    return {
        "applied": applied,
        "skipped": 0,
        "failed": failed,
        "kept_pending": 0,
        "pushed": 0,
        "push_failed": push_failed,
        "pending": 0,
    }


@pytest.fixture
def answers(monkeypatch):
    """The passes the backend answers, in order; the calls it received."""
    queue: list[httpx.Response] = []
    calls: list[tuple[str, str | None]] = []

    def post(url, *, headers, timeout):
        calls.append((url, headers.get("X-Flow-Secret")))
        return queue.pop(0)

    monkeypatch.setattr(realm.httpx, "post", post)
    monkeypatch.setenv("DESCOPE_FLOW_SYNC_SECRET", "flow-secret")
    return queue, calls


def _pass(stats: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"status": "completed", "stats": stats, "providers": {"keycloak": {"stats": stats}}}
    )


def test_passes_run_until_one_applies_nothing(answers):
    queue, calls = answers
    queue.extend([_pass(_stats(applied=26)), _pass(_stats(applied=8)), _pass(_stats())])

    realm.take_in_the_realm(BACKEND)

    assert calls == [(f"{BACKEND}{realm.RECONCILIATION}", "flow-secret")] * 3


def test_passes_that_keep_applying_fail_the_run(answers):
    queue, _calls = answers
    queue.extend([_pass(_stats(applied=1))] * realm.MAX_RECONCILIATION_PASSES)

    with pytest.raises(pytest.fail.Exception, match=f"did not converge in {realm.MAX_RECONCILIATION_PASSES} passes"):
        realm.take_in_the_realm(BACKEND)


@pytest.mark.parametrize("stats", [_stats(failed=1), _stats(push_failed=1)], ids=["pull-half", "push-half"])
def test_a_failed_pass_fails_the_run(answers, stats):
    queue, calls = answers
    queue.append(_pass(stats))

    with pytest.raises(pytest.fail.Exception, match="a reconciliation pass with Keycloak failed"):
        realm.take_in_the_realm(BACKEND)
    assert len(calls) == 1


def test_a_refused_pass_names_what_the_backend_needs(answers):
    queue, _calls = answers
    queue.append(httpx.Response(409, json={"detail": "no reconciliation for descope"}))

    with pytest.raises(pytest.fail.Exception, match="answered 409.*SYNC_PROVIDER=keycloak"):
        realm.take_in_the_realm(BACKEND)


@pytest.mark.parametrize(
    "body",
    [
        {"status": "completed", "stats": {}, "providers": {"descope": {"stats": _stats()}}},
        {"status": "completed", "stats": {}, "providers": {}},
        {
            "status": "partial",
            "stats": {},
            "providers": {"keycloak": {"status": "failed", "error": "realm unreadable"}},
        },
    ],
    ids=["descope-only", "no-pass", "keycloak-pass-failed"],
)
def test_an_answer_without_a_completed_keycloak_pass_names_what_the_backend_needs(answers, body):
    queue, _calls = answers
    queue.append(httpx.Response(200, json=body))

    with pytest.raises(pytest.fail.Exception, match="ran no completed Keycloak pass.*SYNC_PROVIDER=keycloak"):
        realm.take_in_the_realm(BACKEND)


def test_no_answer_names_what_the_backend_needs(monkeypatch):
    def post(url, *, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(realm.httpx, "post", post)

    with pytest.raises(pytest.fail.Exception, match="got no answer.*SYNC_PROVIDER=keycloak"):
        realm.take_in_the_realm(BACKEND)
