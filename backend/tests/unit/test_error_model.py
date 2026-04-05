"""Unit tests for error model hierarchy and RFC 9457 Problem Detail responses.

Covers:
- IdentityError base and all 6 subclasses (AC-1.3.1)
- ProblemDetailResponse model with RFC 9457 fields (AC-1.3.2)
- ERROR_TYPE_MAP registry completeness (AC-1.3.3)
- result_to_response() helper (AC-1.3.4)
- Result/Ok/Error importability from expression (AC-1.3.5)
"""

import json
from unittest.mock import MagicMock

from expression import Error, Ok, Result

from app.errors.identity import (
    Conflict,
    Forbidden,
    IdentityError,
    NotFound,
    ProviderError,
    SyncFailed,
    ValidationError,
)
from app.errors.problem_detail import (
    _ERROR_TYPE_MAP,
    ProblemDetailResponse,
    result_to_response,
)

# ---------------------------------------------------------------------------
# IdentityError hierarchy (AC-1.3.1)
# ---------------------------------------------------------------------------


class TestIdentityErrorBase:
    def test_base_with_message_only(self):
        err = IdentityError(message="something broke")
        assert err.message == "something broke"
        assert err.context is None

    def test_base_with_context(self):
        ctx = {"user_id": "abc", "field": "email"}
        err = IdentityError(message="invalid", context=ctx)
        assert err.context == ctx

    def test_frozen_immutability(self):
        err = IdentityError(message="test")
        try:
            err.message = "changed"  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass


class TestErrorSubclasses:
    def test_not_found_is_identity_error(self):
        err = NotFound(message="user not found")
        assert isinstance(err, IdentityError)
        assert err.message == "user not found"

    def test_conflict_is_identity_error(self):
        err = Conflict(message="duplicate name")
        assert isinstance(err, IdentityError)

    def test_validation_error_is_identity_error(self):
        err = ValidationError(message="bad email", context={"field": "email"})
        assert isinstance(err, IdentityError)
        assert err.context == {"field": "email"}

    def test_provider_error_is_identity_error(self):
        err = ProviderError(message="upstream timeout")
        assert isinstance(err, IdentityError)

    def test_forbidden_is_identity_error(self):
        err = Forbidden(message="not allowed")
        assert isinstance(err, IdentityError)

    def test_all_six_subclasses_exist(self):
        """AC-1.3.1: 6 concrete subclasses of IdentityError."""
        subclasses = {NotFound, Conflict, ValidationError, SyncFailed, ProviderError, Forbidden}
        for cls in subclasses:
            assert issubclass(cls, IdentityError), f"{cls.__name__} is not a subclass"


class TestSyncFailed:
    def test_extra_fields(self):
        """SyncFailed has operation, payload_summary, underlying_error."""
        err = SyncFailed(
            message="descope sync failed",
            operation="create_user",
            payload_summary='{"email": "a@b.com"}',
            underlying_error="HTTPStatusError: 409",
        )
        assert err.operation == "create_user"
        assert err.payload_summary == '{"email": "a@b.com"}'
        assert err.underlying_error == "HTTPStatusError: 409"

    def test_extra_fields_default_empty(self):
        err = SyncFailed(message="sync failed")
        assert err.operation == ""
        assert err.payload_summary == ""
        assert err.underlying_error == ""

    def test_inherits_context(self):
        err = SyncFailed(message="sync failed", context={"tenant": "t1"})
        assert err.context == {"tenant": "t1"}


# ---------------------------------------------------------------------------
# ProblemDetailResponse model (AC-1.3.2)
# ---------------------------------------------------------------------------


class TestProblemDetailResponse:
    def test_all_rfc_fields(self):
        """RFC 9457: type, title, status, detail, instance, traceId."""
        pd = ProblemDetailResponse(
            type="/errors/not-found",
            title="Not Found",
            status=404,
            detail="User abc not found",
            instance="/api/users/abc",
            trace_id="abc123",
        )
        assert pd.type == "/errors/not-found"
        assert pd.title == "Not Found"
        assert pd.status == 404
        assert pd.detail == "User abc not found"
        assert pd.instance == "/api/users/abc"
        assert pd.trace_id == "abc123"

    def test_accepts_camel_case_alias(self):
        """traceId alias is accepted for input (Pydantic alias)."""
        pd = ProblemDetailResponse(
            type="/errors/test",
            title="Test",
            status=500,
            detail="err",
            traceId="abc123",
        )
        assert pd.trace_id == "abc123"

    def test_optional_fields_default_empty(self):
        pd = ProblemDetailResponse(
            type="/errors/test",
            title="Test",
            status=500,
            detail="err",
        )
        assert pd.instance == ""
        assert pd.trace_id == ""

    def test_model_dump_camel_case(self):
        """traceId must be camelCase in JSON output (serialization alias)."""
        pd = ProblemDetailResponse(
            type="/errors/test",
            title="Test",
            status=500,
            detail="err",
            trace_id="trace-xyz",
        )
        dumped = pd.model_dump(by_alias=True)
        assert "traceId" in dumped
        assert dumped["traceId"] == "trace-xyz"


# ---------------------------------------------------------------------------
# ERROR_TYPE_MAP registry (AC-1.3.3)
# ---------------------------------------------------------------------------


class TestErrorTypeMap:
    def test_all_subclasses_registered(self):
        """Every IdentityError subclass must be in the map."""
        expected = {NotFound, Conflict, ValidationError, SyncFailed, ProviderError, Forbidden}
        assert set(_ERROR_TYPE_MAP.keys()) == expected

    def test_status_codes(self):
        """AC-1.3.3: exact status code mapping."""
        assert _ERROR_TYPE_MAP[NotFound][1] == 404
        assert _ERROR_TYPE_MAP[Conflict][1] == 409
        assert _ERROR_TYPE_MAP[ValidationError][1] == 422
        assert _ERROR_TYPE_MAP[SyncFailed][1] == 202
        assert _ERROR_TYPE_MAP[ProviderError][1] == 502
        assert _ERROR_TYPE_MAP[Forbidden][1] == 403

    def test_uri_paths(self):
        assert _ERROR_TYPE_MAP[NotFound][0] == "/errors/not-found"
        assert _ERROR_TYPE_MAP[Conflict][0] == "/errors/conflict"
        assert _ERROR_TYPE_MAP[ValidationError][0] == "/errors/validation"
        assert _ERROR_TYPE_MAP[SyncFailed][0] == "/errors/sync-failed"
        assert _ERROR_TYPE_MAP[ProviderError][0] == "/errors/provider-error"
        assert _ERROR_TYPE_MAP[Forbidden][0] == "/errors/forbidden"

    def test_all_entries_have_title(self):
        for cls, (uri, status, title) in _ERROR_TYPE_MAP.items():
            assert title, f"{cls.__name__} has empty title"


# ---------------------------------------------------------------------------
# result_to_response() helper (AC-1.3.4)
# ---------------------------------------------------------------------------


def _make_request(path: str = "/api/test") -> MagicMock:
    """Create a mock Request with the given URL path."""
    request = MagicMock()
    request.url.path = path
    return request


class TestResultToResponseOk:
    def test_ok_default_status(self):
        result: Result[dict, IdentityError] = Ok({"id": "123", "name": "test"})
        request = _make_request()
        response = result_to_response(result, request)
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body == {"id": "123", "name": "test"}

    def test_ok_custom_status(self):
        result: Result[dict, IdentityError] = Ok({"created": True})
        request = _make_request()
        response = result_to_response(result, request, status=201)
        assert response.status_code == 201

    def test_ok_empty_dict(self):
        result: Result[dict, IdentityError] = Ok({})
        request = _make_request()
        response = result_to_response(result, request)
        assert response.status_code == 200
        assert json.loads(response.body) == {}

    def test_ok_list_value(self):
        result: Result[list, IdentityError] = Ok([{"id": "1"}, {"id": "2"}])
        request = _make_request()
        response = result_to_response(result, request)
        assert response.status_code == 200
        assert len(json.loads(response.body)) == 2


class TestResultToResponseError:
    def test_not_found_produces_404(self):
        result: Result[dict, IdentityError] = Error(NotFound(message="user not found"))
        request = _make_request("/api/users/abc")
        response = result_to_response(result, request)

        assert response.status_code == 404
        assert response.media_type == "application/problem+json"
        body = json.loads(response.body)
        assert body["type"] == "/errors/not-found"
        assert body["title"] == "Resource Not Found"
        assert body["status"] == 404
        assert body["detail"] == "user not found"
        assert body["instance"] == "/api/users/abc"

    def test_conflict_produces_409(self):
        result: Result[dict, IdentityError] = Error(Conflict(message="duplicate"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 409
        body = json.loads(response.body)
        assert body["type"] == "/errors/conflict"

    def test_validation_error_produces_422(self):
        result: Result[dict, IdentityError] = Error(ValidationError(message="bad input"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 422

    def test_sync_failed_produces_202(self):
        """AC edge case: SyncFailed -> 202, not 5xx."""
        result: Result[dict, IdentityError] = Error(
            SyncFailed(
                message="descope sync failed",
                operation="create_user",
                payload_summary="{}",
                underlying_error="timeout",
            )
        )
        response = result_to_response(result, _make_request())
        assert response.status_code == 202
        body = json.loads(response.body)
        assert body["type"] == "/errors/sync-failed"
        assert body["title"] == "Sync Pending"

    def test_sync_failed_uses_plain_json_not_problem_json(self):
        """SyncFailed (202) must use application/json, not application/problem+json.

        RFC 9457 problem+json is for error responses (4xx/5xx). SyncFailed maps
        to 202 (accepted), so it should use plain JSON.
        """
        result: Result[dict, IdentityError] = Error(SyncFailed(message="sync pending"))
        response = result_to_response(result, _make_request())
        assert response.media_type == "application/json"

    def test_provider_error_produces_502(self):
        result: Result[dict, IdentityError] = Error(ProviderError(message="upstream down"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 502

    def test_forbidden_produces_403(self):
        result: Result[dict, IdentityError] = Error(Forbidden(message="not allowed"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 403

    def test_content_type_is_problem_json_for_errors(self):
        """4xx/5xx error responses must use application/problem+json."""
        for error_cls in [NotFound, Conflict, ValidationError, ProviderError, Forbidden]:
            result: Result[dict, IdentityError] = Error(error_cls(message="test"))
            response = result_to_response(result, _make_request())
            assert response.media_type == "application/problem+json", f"{error_cls.__name__} wrong media type"

    def test_instance_from_request_path(self):
        """instance field populated from request.url.path."""
        result: Result[dict, IdentityError] = Error(NotFound(message="missing"))
        response = result_to_response(result, _make_request("/api/tenants/xyz/users/abc"))
        body = json.loads(response.body)
        assert body["instance"] == "/api/tenants/xyz/users/abc"

    def test_trace_id_empty_without_otel(self):
        """traceId is empty string when OTel is not configured."""
        result: Result[dict, IdentityError] = Error(NotFound(message="test"))
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        # traceId is serialized via alias
        assert body.get("traceId", "") == ""

    def test_unknown_error_type_fallback(self):
        """Unregistered IdentityError subclass -> 500 /errors/unknown."""

        class CustomError(IdentityError):
            pass

        result: Result[dict, IdentityError] = Error(CustomError(message="unexpected"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 500
        body = json.loads(response.body)
        assert body["type"] == "/errors/unknown"
        assert body["title"] == "Internal Error"


# ---------------------------------------------------------------------------
# Expression library integration (AC-1.3.5)
# ---------------------------------------------------------------------------


class TestExpressionIntegration:
    def test_result_ok_error_importable(self):
        """AC-1.3.5: Result, Ok, Error can be imported from expression."""
        assert Result is not None
        assert Ok is not None
        assert Error is not None

    def test_ok_wrapping(self):
        r: Result[str, IdentityError] = Ok("hello")
        assert r.is_ok()

    def test_error_wrapping(self):
        r: Result[str, IdentityError] = Error(NotFound(message="missing"))
        assert r.is_error()

    def test_ok_value_extraction(self):
        r: Result[dict, IdentityError] = Ok({"id": "1"})
        match r:
            case Result(tag="ok", ok=value):
                assert value == {"id": "1"}
            case _:
                assert False, "Expected Ok"

    def test_error_value_extraction(self):
        r: Result[dict, IdentityError] = Error(Conflict(message="dup"))
        match r:
            case Result(tag="error", error=err):
                assert isinstance(err, Conflict)
                assert err.message == "dup"
            case _:
                assert False, "Expected Error"
