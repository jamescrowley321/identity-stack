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

import base64
import binascii

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.knobs import knob

KEYS_VARIABLE = "KEYCLOAK_REGISTRATION_TOKEN_KEYS"


class RegistrationTokenKeysNotConfigured(RuntimeError):
    """``KEYCLOAK_REGISTRATION_TOKEN_KEYS`` is unset, empty or holds a value that is not a Fernet key."""


class RegistrationTokenUnreadable(RuntimeError):
    """A stored registration token does not open under any configured key."""


class RegistrationTokenNotSealed(RegistrationTokenUnreadable):
    """A stored registration token is not a Fernet token at all: written before ``011_seal_registration_tokens``.

    It is never read as plain text; sealing it is the migration's job.
    """


def _is_fernet_token(value: str) -> bool:
    """Whether the value has a Fernet token's shape: url-safe base64 opening with version byte 0x80."""
    try:
        return base64.urlsafe_b64decode(value.encode())[:1] == b"\x80"
    except (ValueError, binascii.Error):
        return False


class RegistrationTokenCipher:
    """Seals and opens registration tokens. An empty token (none stored) stays empty."""

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

    def __repr__(self) -> str:
        return "RegistrationTokenCipher(<keys hidden>)"

    def seal(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode() if token else ""

    def open(self, sealed: str) -> str:
        if not sealed:
            return ""
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken as exc:
            if not _is_fernet_token(sealed):
                raise RegistrationTokenNotSealed(
                    "a stored registration token is not sealed: migrate past 011_seal_registration_tokens"
                ) from exc
            raise RegistrationTokenUnreadable(
                f"a stored registration token does not open under any key in {KEYS_VARIABLE}"
            ) from exc


def registration_token_cipher() -> RegistrationTokenCipher:
    """The cipher of ``KEYCLOAK_REGISTRATION_TOKEN_KEYS``, or ``RegistrationTokenKeysNotConfigured``."""
    raw = knob("KEYCLOAK_REGISTRATION_TOKEN_KEYS") or ""
    return RegistrationTokenCipher([key.strip() for key in raw.split(",") if key.strip()])
