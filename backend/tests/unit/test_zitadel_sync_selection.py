"""The sync adapter is chosen by configuration: Descope by default, Zitadel with SYNC_PROVIDER=zitadel.

A Zitadel deployment's adapter is built from ZITADEL_ISSUER_URL, ZITADEL_AUDIENCE (the
project id) and the machine user's ZITADEL_ADMIN_TOKEN, all read through app.knobs.
Startup refuses an unknown provider and an incomplete Zitadel configuration.
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.datastructures import State

from app import main
from app.dependencies import identity, zitadel_sync
from app.dependencies.zitadel_sync import (
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_VARIABLE,
    RepositoryZitadelLinks,
    ZitadelAdminSettings,
    ZitadelSyncNotConfigured,
    zitadel_admin_settings,
)
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.adapters.zitadel_api import ZitadelAdminApi

ISSUER = "http://zitadel.localhost:8480"
PROJECT_ID = "399979368434118659"
TOKEN = "local-test-admin-token"
SERVICE_FACTORIES = [
    identity.get_user_service,
    identity.get_role_service,
    identity.get_permission_service,
    identity.get_tenant_service,
]


@pytest.fixture
def zitadel_environment(monkeypatch):
    monkeypatch.setenv("ZITADEL_ISSUER_URL", ISSUER + "/")
    monkeypatch.setenv("ZITADEL_AUDIENCE", PROJECT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, TOKEN)
    monkeypatch.delenv(ADMIN_TIMEOUT_VARIABLE, raising=False)


class _App:
    def __init__(self, state: State) -> None:
        self.state = state


async def _start(state: State) -> None:
    """Run the one startup check (main._start_sync_provider) against ``state``."""
    assert await main._start_sync_provider(_App(state)) is None


def _request(state: State) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _state(**values) -> State:
    state = State()
    state.descope_client = object()
    state.cache_publisher = None
    for name, value in values.items():
        setattr(state, name, value)
    return state


@pytest.mark.parametrize("factory", SERVICE_FACTORIES)
async def test_a_descope_deployment_gets_the_descope_adapter(factory, monkeypatch):
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")

    service = await factory(_request(_state()), session=object())

    assert type(service._adapter) is DescopeSyncAdapter


@pytest.mark.parametrize("factory", SERVICE_FACTORIES)
async def test_a_zitadel_deployment_gets_the_zitadel_adapter(factory, monkeypatch, zitadel_environment):
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "zitadel")
    state = _state()
    await _start(state)

    service = await factory(_request(state), session=object())

    assert type(service._adapter) is ZitadelSyncAdapter


def test_the_default_provider_is_descope():
    """Read at import: a process started without SYNC_PROVIDER syncs to Descope."""
    environment = {name: value for name, value in os.environ.items() if name != "SYNC_PROVIDER"}
    printed = subprocess.run(
        [sys.executable, "-c", "from app.dependencies import identity; print(identity.SYNC_PROVIDER)"],
        env=environment,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
    )

    assert printed.stdout.strip() == "descope"
    assert set(identity.SYNC_ADAPTERS) == {"descope", "keycloak", "zitadel"}


async def test_startup_refuses_an_unknown_provider(monkeypatch):
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "zitadle")

    with pytest.raises(RuntimeError) as refused:
        await _start(_state())

    assert str(refused.value) == "SYNC_PROVIDER 'zitadle' is not one of ['descope', 'keycloak', 'zitadel']"


async def test_startup_for_descope_reads_no_zitadel_setting(monkeypatch):
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
    monkeypatch.delenv(ADMIN_CREDENTIAL_VARIABLE, raising=False)
    state = _state()

    await _start(state)

    assert state.zitadel_admin is None


async def test_startup_for_zitadel_records_the_settings_and_opens_no_keycloak_client(monkeypatch, zitadel_environment):
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "zitadel")
    state = _state()

    await _start(state)

    assert state.keycloak_admin is None
    assert state.zitadel_admin == ZitadelAdminSettings(
        base_url=ISSUER, project_id=PROJECT_ID, token=TOKEN, timeout_seconds=10.0
    )


@pytest.mark.parametrize("variable", ["ZITADEL_ISSUER_URL", "ZITADEL_AUDIENCE", ADMIN_CREDENTIAL_VARIABLE])
@pytest.mark.parametrize("unset", [True, False], ids=["unset", "empty"])
def test_an_incomplete_zitadel_configuration_is_refused_by_name(variable, unset, monkeypatch, zitadel_environment):
    if unset:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, "")

    with pytest.raises(ZitadelSyncNotConfigured) as refused:
        zitadel_admin_settings()

    assert variable in str(refused.value)
    assert TOKEN not in str(refused.value)


@pytest.mark.parametrize("value", ["0", "-1", "soon", "nan", "inf"])
def test_a_timeout_that_is_not_a_positive_finite_number_is_refused(value, monkeypatch, zitadel_environment):
    monkeypatch.setenv(ADMIN_TIMEOUT_VARIABLE, value)

    with pytest.raises(ZitadelSyncNotConfigured, match=ADMIN_TIMEOUT_VARIABLE):
        zitadel_admin_settings()


def test_the_timeout_is_read_from_its_knob(monkeypatch, zitadel_environment):
    monkeypatch.setenv(ADMIN_TIMEOUT_VARIABLE, "2.5")

    assert zitadel_admin_settings().timeout_seconds == 2.5


def test_the_settings_never_show_the_token():
    settings = ZitadelAdminSettings(base_url=ISSUER, project_id=PROJECT_ID, token=TOKEN, timeout_seconds=10.0)

    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)


async def test_the_adapter_opens_an_outbound_client_with_the_configured_timeout(monkeypatch):
    opened: list[float] = []
    real = zitadel_sync.outbound_client

    def recording_client(*, timeout: float):
        opened.append(timeout)
        return real(timeout=timeout)

    monkeypatch.setattr(zitadel_sync, "outbound_client", recording_client)
    settings = ZitadelAdminSettings(base_url=ISSUER, project_id=PROJECT_ID, token=TOKEN, timeout_seconds=2.5)

    async with zitadel_sync.admin_api_opener(settings)() as api:
        assert isinstance(api, ZitadelAdminApi)

    assert opened == [2.5]


def test_the_adapter_is_built_with_the_project_and_the_request_session(zitadel_environment):
    settings = zitadel_admin_settings()
    session = object()

    adapter = zitadel_sync.build_zitadel_sync_adapter(settings, session)

    assert type(adapter) is ZitadelSyncAdapter
    assert adapter._project_id == PROJECT_ID
    assert type(adapter._links) is RepositoryZitadelLinks


class _Connection:
    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, statement) -> None:
        return None


class _Engine:
    """Answers the lifespan's connectivity check; records its disposal."""

    instances: list["_Engine"] = []

    def __init__(self) -> None:
        self.disposed = False
        _Engine.instances.append(self)

    def connect(self) -> _Connection:
        return _Connection()

    async def dispose(self) -> None:
        self.disposed = True


async def test_the_application_does_not_start_with_an_incomplete_zitadel_configuration(monkeypatch):
    """The lifespan's sync-provider check refuses it before serving and closes what it had opened."""
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "zitadel")
    monkeypatch.delenv(ADMIN_CREDENTIAL_VARIABLE, raising=False)
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "P-local-test")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "local-test-key")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(_Engine, "instances", [])
    monkeypatch.setattr(main, "get_engine", _Engine)
    monkeypatch.setattr(main, "init_telemetry", lambda **_kwargs: None)
    served = []

    with pytest.raises(ZitadelSyncNotConfigured, match=ADMIN_CREDENTIAL_VARIABLE):
        async with main.lifespan(FastAPI()):
            served.append(True)

    assert served == []
    assert [engine.disposed for engine in _Engine.instances] == [True]
