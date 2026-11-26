"""The profile of a first-login user, read from the provider's UserInfo endpoint.

For a provider whose access tokens carry no email (``ProviderTokenConfig.profile_from_userinfo``,
Zitadel), just-in-time provisioning takes ``email``, ``email_verified`` and the
names from UserInfo (OIDC Core 1.0 §5.3), through py-identity-model. UserInfo's
``sub`` must equal the verified token's (§5.3.2): the library refuses any
other, and a response without one.

Both fetches, discovery and UserInfo, go through one client with an explicit
timeout and no retries (conventions §6), and discovery honours the provider's
``require_https``.
"""

from __future__ import annotations

from dataclasses import dataclass

from expression import Error, Ok, Result
from opentelemetry import trace
from py_identity_model import DiscoveryDocumentRequest, DiscoveryPolicy, HTTPConfig
from py_identity_model.aio import (
    AsyncHTTPClient,
    UserInfoErrorKind,
    UserInfoRequest,
    get_discovery_document,
    get_userinfo,
)

from app.errors.identity import Forbidden, IdentityError, ProviderError
from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig

tracer = trace.get_tracer(__name__)
logger = logbook(__name__)

USERINFO_TIMEOUT_SECONDS = 5.0
# A UserInfo answer whose subject is not the token's: the profile belongs to no one we know.
_SUBJECT_REFUSALS = frozenset(
    {UserInfoErrorKind.SUB_MISMATCH, UserInfoErrorKind.MISSING_SUB, UserInfoErrorKind.UNVERIFIABLE_SUB}
)


@dataclass(frozen=True)
class FirstLoginProfile:
    """What provisioning takes from UserInfo. ``email_verified`` is true only for the JSON ``true``."""

    email: str
    email_verified: bool
    given_name: str
    family_name: str


def _string(claims: dict, name: str) -> str:
    value = claims.get(name)
    return value if isinstance(value, str) else ""


async def userinfo_profile(
    provider: ProviderTokenConfig, access_token: str, sub: str
) -> Result[FirstLoginProfile, IdentityError]:
    """The UserInfo profile of the caller of ``access_token``, whose verified ``sub`` is ``sub``.

    ``Forbidden`` when UserInfo answers for another subject or none;
    ``ProviderError`` when discovery or UserInfo cannot be read.
    """
    with tracer.start_as_current_span(
        "first_login_profile.userinfo_profile", attributes={"identity.provider": provider.name}
    ):
        config = HTTPConfig(http_timeout=USERINFO_TIMEOUT_SECONDS, http_retry_max_attempts=0)
        async with AsyncHTTPClient(config=config) as client:
            disco = await get_discovery_document(
                DiscoveryDocumentRequest(
                    issuer=provider.discovery_issuer,
                    policy=DiscoveryPolicy(require_https=provider.require_https),
                ),
                http_client=client,
            )
            endpoint = disco.userinfo_endpoint if disco.is_successful else None
            if not endpoint:
                logger.warning("first login: provider %s advertises no usable UserInfo endpoint", provider.name)
                return Error(ProviderError("The identity provider's UserInfo endpoint could not be found"))
            response = await get_userinfo(
                UserInfoRequest(address=endpoint, token=access_token, expected_sub=sub),
                http_client=client,
            )
        if not response.is_successful:
            if response.error_kind in _SUBJECT_REFUSALS:
                logger.warning(
                    "first login: provider %s UserInfo refused (%s)", provider.name, response.error_kind.value
                )
                return Error(Forbidden("UserInfo does not answer for the token's subject"))
            logger.warning("first login: provider %s UserInfo could not be read", provider.name)
            return Error(ProviderError("The identity provider's UserInfo could not be read"))
        claims = response.claims or {}
        return Ok(
            FirstLoginProfile(
                email=_string(claims, "email"),
                email_verified=claims.get("email_verified") is True,
                given_name=_string(claims, "given_name"),
                family_name=_string(claims, "family_name"),
            )
        )
