"""Encryption at rest of the registration tokens of Keycloak machine clients.

Keycloak answers a client's registration token with the client's secret, so the
token is as sensitive as the secret itself (#698). It is sealed with Fernet
(AES-128-CBC with HMAC-SHA256) under ``KEYCLOAK_REGISTRATION_TOKEN_KEYS`` before
it is written to ``machine_credentials`` and opened only to delete the client.

The setting is a comma-separated list of Fernet keys (``Fernet.generate_key()``).
The first seals; every one opens, so a new key goes first and the old one
stays listed while any row is still sealed under it. Neither the keys nor the
tokens are ever logged: errors name the variable, never a value.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.knobs import knob

KEYS_VARIABLE = "KEYCLOAK_REGISTRATION_TOKEN_KEYS"


class RegistrationTokenKeysNotConfigured(RuntimeError):
    """``KEYCLOAK_REGISTRATION_TOKEN_KEYS`` is unset, empty or holds a value that is not a Fernet key."""


class RegistrationTokenUnreadable(RuntimeError):
    """A stored registration token does not open under any configured key."""


class RegistrationTokenCipher:
    """Seals and opens registration tokens. An empty token (none stored) stays empty.

    The first key seals; every key opens (MultiFernet). The cipher keeps its
    MultiFernet instance and the primary ``Fernet`` so helpers can use the
    first key directly without relying on MultiFernet internals.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise RegistrationTokenKeysNotConfigured(f"{KEYS_VARIABLE} is not set")
        try:
            fernets = [Fernet(key) for key in keys]
        except ValueError as exc:
            raise RegistrationTokenKeysNotConfigured(
                f"{KEYS_VARIABLE} holds a value that is not a Fernet key (32 url-safe base64-encoded bytes)"
            ) from exc
        self._fernet = MultiFernet(fernets)
        self._primary = fernets[0]

    def __repr__(self) -> str:
        return "RegistrationTokenCipher(<keys hidden>)"

    def seal(self, token: str) -> str:
        """Seal with the configured MultiFernet (first key seals; all keys open)."""
        return self._fernet.encrypt(token.encode()).decode() if token else ""

    def primary_seal(self, token: str) -> str:
        """Seal with the first configured key only.

        Used by data-migration tools that need to place every token under the
        current primary key without depending on MultiFernet internals.
        """
        return self._primary.encrypt(token.encode()).decode() if token else ""

    def opens_with_primary(self, sealed: str) -> bool:
        """Whether the sealed value can be opened by the first configured key alone."""
        if not sealed:
            return False
        try:
            self._primary.decrypt(sealed.encode())
        except InvalidToken:
            return False
        return True

    def open(self, sealed: str) -> str:
        if not sealed:
            return ""
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken as exc:
            raise RegistrationTokenUnreadable(
                f"a stored registration token does not open under any key in {KEYS_VARIABLE}"
            ) from exc


def registration_token_cipher() -> RegistrationTokenCipher:
    """The cipher of ``KEYCLOAK_REGISTRATION_TOKEN_KEYS``, or ``RegistrationTokenKeysNotConfigured``."""
    raw = knob("KEYCLOAK_REGISTRATION_TOKEN_KEYS") or ""
    return RegistrationTokenCipher([key.strip() for key in raw.split(",") if key.strip()])
