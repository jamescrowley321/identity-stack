"""A loopback OIDC provider for unit tests that run the real library.

Serves a discovery document and one JWKS holding an RS256, an ES256 and a PS256
key, and signs tokens with any of them. The middleware under test fetches both
documents over HTTP and verifies the signature itself: nothing is patched.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

RS256 = "RS256"
ES256 = "ES256"
PS256 = "PS256"
HS256 = "HS256"
UNSIGNED = "none"
TOKEN_LIFETIME_SECONDS = 300
_RSA_KEY_BITS = 2048
_RSA_PUBLIC_EXPONENT = 65537
_WELL_KNOWN = "/.well-known/openid-configuration"
_JWKS = "/jwks"


def _segment(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _published(public_jwk: str, alg: str) -> dict:
    """A JWKS entry for ``alg``, named after it. ``use`` replaces PyJWT's ``key_ops``."""
    jwk = {name: value for name, value in json.loads(public_jwk).items() if name != "key_ops"}
    return {**jwk, "kid": alg, "alg": alg, "use": "sig"}


class LocalProvider:
    """Keys, the documents built from them, and tokens signed by them."""

    def __init__(self, issuer: str) -> None:
        self.issuer = issuer
        self._published_issuer = issuer
        rsa_key = rsa.generate_private_key(public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_BITS)
        pss_key = rsa.generate_private_key(public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_BITS)
        ec_key = ec.generate_private_key(ec.SECP256R1())
        self._private_keys = {RS256: rsa_key, PS256: pss_key, ES256: ec_key}
        self._jwks = {
            "keys": [
                _published(RSAAlgorithm.to_jwk(rsa_key.public_key()), RS256),
                _published(RSAAlgorithm.to_jwk(pss_key.public_key()), PS256),
                _published(ECAlgorithm.to_jwk(ec_key.public_key()), ES256),
            ]
        }
        self._failing: dict[str, int] = {}
        self._rs256_public_pem = rsa_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @property
    def disco_address(self) -> str:
        return f"{self.issuer}{_WELL_KNOWN}"

    @property
    def jwks_path(self) -> str:
        return _JWKS

    @property
    def discovery_path(self) -> str:
        return _WELL_KNOWN

    def fail(self, path: str, status: int) -> None:
        """Answer ``path`` with ``status`` and an error body from now on."""
        self._failing[path] = status

    def publish(self, *, issuer: str | None = None, keys: list[dict] | None = None) -> None:
        """Replace the issuer the discovery document names, or the keys the JWKS holds."""
        if issuer is not None:
            self._published_issuer = issuer
        if keys is not None:
            self._jwks = {"keys": keys}

    def failure(self, path: str) -> int | None:
        return self._failing.get(path)

    def document(self, path: str) -> dict | None:
        if path == _WELL_KNOWN:
            return {
                "issuer": self._published_issuer,
                "jwks_uri": f"{self.issuer}{_JWKS}",
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": sorted(self._private_keys),
            }
        if path == _JWKS:
            return self._jwks
        return None

    def token(self, alg: str, claims: dict) -> str:
        """A token for ``claims`` signed ``alg``.

        RS256, ES256 and PS256 are signed by that algorithm's key in the JWKS.
        HS256 names the RS256 key and uses its public PEM as the HMAC secret.
        ``none`` names the RS256 key and carries no signature.
        """
        now = int(time.time())
        payload = {"iat": now, "exp": now + TOKEN_LIFETIME_SECONDS, **claims}
        if alg in self._private_keys:
            return jwt.encode(payload, self._private_keys[alg], algorithm=alg, headers={"kid": alg})
        signing_input = f"{_segment({'alg': alg, 'typ': 'JWT', 'kid': RS256})}.{_segment(payload)}"
        if alg == HS256:
            digest = hmac.new(self._rs256_public_pem, signing_input.encode(), hashlib.sha256).digest()
            return f"{signing_input}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"
        return f"{signing_input}."

    def token_with_header_alg(self, alg: str, claims: dict) -> str:
        """An unsigned token whose header ``alg`` is ``alg``, whatever text that is."""
        now = int(time.time())
        payload = {"iat": now, "exp": now + TOKEN_LIFETIME_SECONDS, **claims}
        return f"{_segment({'alg': alg, 'typ': 'JWT', 'kid': RS256})}.{_segment(payload)}."


@contextmanager
def local_provider() -> Iterator[LocalProvider]:
    """Serve a ``LocalProvider`` on a free loopback port for the length of the block."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            failure = provider.failure(self.path)
            document = None if failure is not None else provider.document(self.path)
            body = json.dumps(document if document is not None else {"error": "not_served"}).encode()
            self.send_response(200 if document is not None else failure or 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Keep the test output free of access-log lines."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    provider = LocalProvider(f"http://127.0.0.1:{server.server_port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
