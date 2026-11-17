"""What a Zitadel token must carry to be one for this API, and the one claim read from it.

Audience alone does not bind a Zitadel token to this API. Measured on local
Zitadel v4.20.3 (docs/zitadel.md, ``tests/fixtures/zitadel/``): a human token's
``aud`` lists the client id of every application in the project and then the
project id, and an ID token has the same ``aud``, ``iss`` and header ``typ`` as
an access token. The logout redirect puts the ID token in a URL as
``id_token_hint``. So beyond the issuer, the signature and the project audience,
a token is accepted only when

- its ``client_id`` is the configured SPA client id (access tokens carry
  ``client_id`` and no ``azp``), and
- it carries none of the claims that mark an ID token. The measured ID token
  carries ``azp``, ``at_hash``, ``auth_time`` and ``sid``; ``nonce`` is added
  because an ID token carries it whenever the request did.

Both rest on claim sets observed on v4.20.3, not on a documented contract.

Authorization is canonical: nothing here reads a role. ``organization_id`` is
the one reader of the organization claim, and only first-login provisioning
calls it (through ``ProviderTokenConfig.first_login_organization``).
"""

from __future__ import annotations

CLIENT_ID_CLAIM = "client_id"
ID_TOKEN_CLAIMS = ("azp", "at_hash", "auth_time", "sid", "nonce")
ORGANIZATION_CLAIM = "urn:zitadel:iam:user:resourceowner:id"


def binding_failure(client_id: str, claims: dict) -> str | None:
    """Why ``claims`` are not those of an access token issued to the SPA ``client_id``, or ``None``."""
    token_client = claims.get(CLIENT_ID_CLAIM)
    if not isinstance(token_client, str) or token_client != client_id:
        return f"client_id {token_client!r} is not the SPA client"
    id_token_claims = sorted(claim for claim in ID_TOKEN_CLAIMS if claim in claims)
    if id_token_claims:
        return f"carries ID-token claims {id_token_claims}"
    return None


def organization_id(claims: dict) -> str | None:
    """The id of the organization the token's user belongs to, or ``None`` when the token names none.

    Present only when the token was requested with scope
    ``urn:zitadel:iam:user:resourceowner``. A value that is not a non-empty
    string names no organization.
    """
    organization = claims.get(ORGANIZATION_CLAIM)
    return organization if isinstance(organization, str) and organization else None
