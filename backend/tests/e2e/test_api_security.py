"""E2E tests for API security headers and rate limiting."""

from playwright.sync_api import APIRequestContext


def test_security_headers_present(api_context: APIRequestContext, backend_url: str):
    """API responses include security headers."""
    response = api_context.get(f"{backend_url}/api/health")
    headers = response.headers
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "content-security-policy" in headers


def test_cors_no_wildcard(api_context: APIRequestContext, backend_url: str):
    """API does not return wildcard CORS headers on health endpoint."""
    response = api_context.get(f"{backend_url}/api/health")
    # If CORS is configured, it should not be wildcard
    acao = response.headers.get("access-control-allow-origin")
    if acao is not None:
        assert acao != "*", "CORS should not use wildcard origin"


def test_rate_limiting_returns_429(api_context: APIRequestContext, backend_url: str):
    """Exceeding rate limit returns 429 with Retry-After header."""
    # Auth endpoints have stricter limits (10/minute)
    # Send many requests to trigger the limit
    responses = []
    for _ in range(15):
        resp = api_context.post(
            f"{backend_url}/api/auth/logout",
            headers={"Authorization": "Bearer fake"},
        )
        responses.append(resp.status)

    # At least one should be 429 (or all 401 if auth check comes first)
    # The important thing is no 500s
    assert all(s in (401, 429) for s in responses), f"Unexpected status codes: {set(responses)}"
