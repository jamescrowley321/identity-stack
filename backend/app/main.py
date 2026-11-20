import asyncio
from contextlib import asynccontextmanager

import sqlalchemy
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from scalar_fastapi import get_scalar_api_reference
from slowapi.errors import RateLimitExceeded

from app.dependencies import identity as identity_dependencies
from app.dependencies.keycloak_pull import KeycloakPullNotConfigured, keycloak_pull_settings, run_keycloak_pull
from app.dependencies.zitadel_sync import zitadel_admin_settings
from app.errors.identity import IdentityErrorRaised
from app.errors.problem_detail import identity_error_raised_handler, request_validation_error_handler
from app.knobs import knob, required_knob
from app.logbook import logbook
from app.logging_config import setup_logging
from app.middleware.factory import configure_middleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_engine
from app.outbound import OutboundClient, outbound_client
from app.repositories.registration_tokens import RegistrationTokenKeysNotConfigured, registration_token_cipher
from app.routers import (
    access_checks,
    accesskeys,
    attributes,
    auth,
    canonical_users,
    capabilities,
    documents,
    fga,
    health,
    idp_links,
    internal,
    permissions,
    protected,
    providers,
    reconciliation,
    roles,
    sync_status,
    tenants,
    users,
)
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.descope import DescopeManagementClient
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    REALM_MANAGEMENT_CLIENT,
    REQUIRED_ROLES,
    KeycloakAdminError,
    KeycloakAdminNotConfigured,
    KeycloakAdminRolesMissing,
    KeycloakApiClientNotManaged,
    connect_keycloak_admin,
)
from app.telemetry import init_telemetry, shutdown_telemetry

logger = logbook(__name__)


def _warn_missing_secrets() -> None:
    """Log warnings for internal endpoint secrets missing at startup."""
    if not knob("DESCOPE_WEBHOOK_SECRET"):
        logger.warning("DESCOPE_WEBHOOK_SECRET not set — webhook endpoint will reject all requests")
    if not knob("DESCOPE_FLOW_SYNC_SECRET"):
        logger.warning("DESCOPE_FLOW_SYNC_SECRET not set — flow sync endpoint will reject all requests")
    if not knob("INTERNAL_IDENTITY_KEY"):
        logger.warning("INTERNAL_IDENTITY_KEY not set — identity resolution endpoint will reject all requests")


KEYCLOAK_ADMIN_TIMEOUT_SECONDS = ADMIN_TIMEOUT_SECONDS


def _admin_check_failure(status: int | None) -> str:
    """Why startup stopped, by what Keycloak answered the admin client's credential check."""
    if status is None:
        return (
            f"Keycloak gave the admin client's credential check no readable answer: "
            f"check {ISSUER_VARIABLE} and that Keycloak is up"
        )
    if status in (400, 401, 403):
        return (
            f"Keycloak refused the admin client's credential (status {status}): "
            f"check {ADMIN_CLIENT_ID_VARIABLE} and {ADMIN_CREDENTIAL_VARIABLE}"
        )
    return (
        f"Keycloak answered the admin client's credential check with status {status}: "
        f"check {ISSUER_VARIABLE} names a realm with organizations enabled"
    )


async def _start_sync_provider(app: FastAPI) -> OutboundClient | None:
    """Open what the configured sync adapter needs, or stop startup.

    ``SYNC_PROVIDER=keycloak`` builds the Keycloak admin client's one HTTP client
    and checks its credential, the roles of its service account and its access to
    the API client against the realm before any request is served. ``SYNC_PROVIDER=zitadel`` reads the Zitadel
    adapter's settings (``app.state.zitadel_admin``); an incomplete configuration
    raises ``ZitadelSyncNotConfigured``. That adapter opens a client per operation.
    Returns the Keycloak HTTP client for the lifespan to close, or ``None``.
    """
    provider = identity_dependencies.SYNC_PROVIDER
    known = sorted(identity_dependencies.SYNC_ADAPTERS)
    if provider not in known:
        raise RuntimeError(f"SYNC_PROVIDER {provider!r} is not one of {known}")
    app.state.keycloak_admin = None
    app.state.keycloak_pull = None
    app.state.zitadel_admin = zitadel_admin_settings() if provider == "zitadel" else None
    if provider != "keycloak":
        return None
    try:
        app.state.keycloak_pull = keycloak_pull_settings()
    except KeycloakPullNotConfigured as exc:
        raise RuntimeError(f"SYNC_PROVIDER is keycloak but {exc}") from exc
    try:
        registration_token_cipher()
    except RegistrationTokenKeysNotConfigured as exc:
        raise RuntimeError(f"SYNC_PROVIDER is keycloak but {exc}") from exc
    http = outbound_client(timeout=KEYCLOAK_ADMIN_TIMEOUT_SECONDS)
    try:
        app.state.keycloak_admin = await connect_keycloak_admin(http)
    except KeycloakAdminNotConfigured as exc:
        await http.aclose()
        raise RuntimeError(f"SYNC_PROVIDER is keycloak but {exc}") from exc
    except KeycloakAdminRolesMissing as exc:
        await http.aclose()
        raise RuntimeError(
            f"The Keycloak admin client's service account lacks {REALM_MANAGEMENT_CLIENT} roles "
            f"{sorted(exc.missing)}: grant it {sorted(REQUIRED_ROLES)} directly (roles reached through a group "
            f"or a composite realm role are not read) or name another client "
            f"in {ADMIN_CLIENT_ID_VARIABLE}"
        ) from exc
    except KeycloakApiClientNotManaged as exc:
        await http.aclose()
        raise RuntimeError(
            f"The Keycloak admin client's service account {'cannot manage' if exc.found else 'cannot find'} "
            f"the API client named by {API_CLIENT_VARIABLE}: grant it query-clients and the fine-grained "
            f"permissions view, manage and map-roles-composite on that client (keycloak/fixtures/README.md)"
        ) from exc
    except KeycloakAdminError as exc:
        await http.aclose()
        raise RuntimeError(_admin_check_failure(exc.status)) from exc
    logger.info("Keycloak admin client connected: canonical identities, roles and assignments sync to Keycloak")
    return http


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    _warn_missing_secrets()
    engine = get_engine()

    # Verify database connectivity before accepting requests
    try:
        async with engine.connect() as conn:
            await conn.execute(sqlalchemy.text("SELECT 1"))
    except Exception as exc:
        logger.critical("Database connectivity check failed: %s", exc)
        raise RuntimeError("Cannot connect to database — verify DATABASE_URL and that the database is running") from exc

    init_telemetry(engine=engine)
    try:
        project_id = required_knob("DESCOPE_PROJECT_ID")
        management_key = required_knob("DESCOPE_MANAGEMENT_KEY")
    except KeyError as exc:
        raise RuntimeError(
            f"Required environment variable {exc} is not set — "
            "ensure DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY are configured"
        ) from exc
    # Built after the reads above so a missing credential cannot leave it unclosed.
    http_client = outbound_client(timeout=30.0)
    base_url = knob("DESCOPE_BASE_URL", "https://api.descope.com")
    app.state.descope_client = DescopeManagementClient(project_id, management_key, base_url, http_client=http_client)
    sync_http_client: OutboundClient | None = None
    try:
        # A RELATIONSHIP_STORE that names no store stops startup (RelationshipStoreNotAvailable).
        relationship_store = identity_dependencies.relationship_store_kind()
        logger.info("Relationship store: %s", relationship_store)
        sync_http_client = await _start_sync_provider(app)
        stop_relationship_store = await identity_dependencies.start_relationship_store(app.state, relationship_store)
    except Exception:
        # Startup stops here: close what was opened before it, each close whatever the one before did.
        try:
            if sync_http_client is not None:
                await sync_http_client.aclose()
            await http_client.aclose()
        finally:
            try:
                await asyncio.get_running_loop().run_in_executor(None, shutdown_telemetry)
            finally:
                await engine.dispose()
        raise

    # Redis for cache invalidation pub/sub — optional, degrades gracefully
    redis_client = None
    redis_url = knob("REDIS_URL")
    if redis_url:
        from urllib.parse import urlparse

        parsed = urlparse(redis_url)
        if parsed.scheme not in ("redis", "rediss"):
            logger.warning("REDIS_URL has unsupported scheme '%s' — cache invalidation disabled", parsed.scheme)
        else:
            try:
                import redis.asyncio as aioredis

                redis_client = aioredis.from_url(redis_url)
                await redis_client.ping()
                logger.info("Redis connected for cache invalidation")
            except Exception:
                logger.warning("Redis connection failed — cache invalidation disabled", exc_info=True)
                if redis_client is not None:
                    await redis_client.aclose()
                redis_client = None
    else:
        logger.info("REDIS_URL not set — cache invalidation disabled")
    app.state.cache_publisher = CacheInvalidationPublisher(redis_client=redis_client)
    app.state.redis_client = redis_client

    # Start cache invalidation subscriber (AC-4.3.3) — background task
    subscriber_task = None
    if redis_client is not None:
        from app.services.identity_resolution import run_cache_invalidation_subscriber

        subscriber_task = asyncio.create_task(run_cache_invalidation_subscriber(redis_client))

    # Keycloak event pull — background task while the Keycloak adapter syncs (docs/keycloak.md#event-pull)
    pull_task = None
    pull_settings = app.state.keycloak_pull
    if app.state.keycloak_admin is not None and pull_settings is not None and pull_settings.enabled:
        pull_task = asyncio.create_task(
            run_keycloak_pull(app.state.keycloak_admin, pull_settings, app.state.cache_publisher)
        )

    try:
        yield
    finally:
        # Stop the Keycloak pull before the clients and the engine it uses are closed
        if pull_task is not None:
            pull_task.cancel()
            try:
                await pull_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Keycloak pull shutdown error", exc_info=True)
        # Cancel cache invalidation subscriber before closing Redis
        if subscriber_task is not None:
            subscriber_task.cancel()
            try:
                await subscriber_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Cache invalidation subscriber shutdown error", exc_info=True)
        try:
            app.state.redis_client = None
            app.state.cache_publisher = None
        except Exception:
            logger.warning("Cache publisher shutdown failed", exc_info=True)
        try:
            if redis_client is not None:
                await redis_client.aclose()
        except Exception:
            logger.warning("Redis shutdown failed", exc_info=True)
        finally:
            try:
                # Run synchronous shutdown in executor to avoid blocking the event loop
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, shutdown_telemetry)
            except Exception:
                logger.warning("Telemetry shutdown failed", exc_info=True)
            finally:
                try:
                    app.state.descope_client = None
                except Exception:
                    logger.warning("Descope client shutdown failed", exc_info=True)
                finally:
                    try:
                        await http_client.aclose()
                    except Exception:
                        logger.warning("HTTP client close failed", exc_info=True)
                    finally:
                        try:
                            app.state.keycloak_admin = None
                            if sync_http_client is not None:
                                await sync_http_client.aclose()
                            if stop_relationship_store is not None:
                                await stop_relationship_store()
                        except Exception:
                            logger.warning("Sync provider HTTP client close failed", exc_info=True)
                        finally:
                            await engine.dispose()


app = FastAPI(title="Identity Stack API", docs_url=None, redoc_url="/redoc", lifespan=lifespan)

# Rate limiter state and exception handler — must remain registered even in
# gateway mode to prevent import errors from @limiter decorators on routes.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
# Lets routing-layer guards raise a domain error instead of returning a response
# the caller has to remember to propagate. See IdentityErrorRaised.
app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)
# FastAPI's 422, but able to echo a NaN or infinity a body sent (attribute values).
app.add_exception_handler(RequestValidationError, request_validation_error_handler)

# Middleware stack — configured by deployment mode (standalone vs gateway)
configure_middleware(app)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(protected.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")
app.include_router(roles.router, prefix="/api")
app.include_router(attributes.router, prefix="/api")
app.include_router(accesskeys.router, prefix="/api")
app.include_router(permissions.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(fga.router, prefix="/api")
app.include_router(internal.router, prefix="/api")
app.include_router(reconciliation.router, prefix="/api")
app.include_router(idp_links.router, prefix="/api")
app.include_router(providers.router, prefix="/api")
app.include_router(sync_status.router, prefix="/api")
app.include_router(canonical_users.router, prefix="/api")
app.include_router(access_checks.router, prefix="/api")
app.include_router(capabilities.router, prefix="/api")


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title=app.title,
    )
