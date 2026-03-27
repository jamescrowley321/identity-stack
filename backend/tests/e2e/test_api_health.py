"""E2E tests for the health endpoint and API basics."""

from playwright.sync_api import APIRequestContext


def test_health_endpoint_returns_ok(api_context: APIRequestContext, backend_url: str):
    """Health endpoint returns 200 with status ok."""
    response = api_context.get(f"{backend_url}/api/health")
    assert response.status == 200
    body = response.json()
    assert body["status"] == "ok"


def test_health_endpoint_no_auth_required(api_context: APIRequestContext, backend_url: str):
    """Health endpoint does not require authentication."""
    response = api_context.get(f"{backend_url}/api/health")
    assert response.status == 200


def test_protected_endpoint_rejects_no_auth(api_context: APIRequestContext, backend_url: str):
    """Protected endpoints return 401 without auth token."""
    response = api_context.get(f"{backend_url}/api/claims")
    assert response.status == 401


def test_protected_endpoint_rejects_invalid_token(api_context: APIRequestContext, backend_url: str):
    """Protected endpoints return 401 with invalid bearer token."""
    response = api_context.get(
        f"{backend_url}/api/claims",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert response.status == 401


def test_protected_endpoints_all_reject_no_auth(api_context: APIRequestContext, backend_url: str):
    """All protected API endpoints return 401 without auth."""
    endpoints = [
        ("GET", "/api/claims"),
        ("GET", "/api/me"),
        ("GET", "/api/profile"),
        ("GET", "/api/roles/me"),
        ("GET", "/api/tenants"),
        ("GET", "/api/keys"),
        ("GET", "/api/members"),
    ]
    for method, path in endpoints:
        if method == "GET":
            response = api_context.get(f"{backend_url}{path}")
        else:
            response = api_context.post(f"{backend_url}{path}")
        assert response.status == 401, f"{method} {path} returned {response.status}, expected 401"
