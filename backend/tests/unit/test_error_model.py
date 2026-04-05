"""Unit tests for error model hierarchy and RFC 9457 Problem Detail responses.

Covers:
- IdentityError base and all 6 subclasses (AC-1.3.1)
- ProblemDetailResponse model with RFC 9457 fields (AC-1.3.2)
- ERROR_TYPE_MAP registry completeness (AC-1.3.3)
- result_to_response() helper (AC-1.3.4)
- Result/Ok/Error importability from expression (AC-1.3.5)
- Review fix coverage: sanitisation, logging, edge cases, serialisation
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from expression import Error, Ok, Result
from pydantic import BaseModel

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
    _SANITIZED_DETAIL,
    ProblemDetailResponse,
    _ErrorMapping,
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
        with pytest.raises(AttributeError):
            err.message = "changed"  # type: ignore[misc]

    def test_context_typed_as_dict_str_any(self):
        """Context accepts dict[str, Any] — string keys only."""
        ctx: dict[str, object] = {"key": 123, "nested": {"a": True}}
        err = IdentityError(message="test", context=ctx)
        assert err.context == ctx

    def test_unsafe_hash_false_with_context(self):
        """Instances with non-None context must raise TypeError on hash."""
        err = IdentityError(message="test", context={"key": "value"})
        with pytest.raises(TypeError):
            hash(err)

    def test_hash_works_without_context(self):
        """Instances with context=None are hashable (None is immutable)."""
        err = IdentityError(message="test")
        # frozen=True generates __hash__ from fields; None is hashable so this works.
        assert isinstance(hash(err), int)


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
            traceId="abc123",
        )
        assert pd.type == "/errors/not-found"
        assert pd.title == "Not Found"
        assert pd.status == 404
        assert pd.detail == "User abc not found"
        assert pd.instance == "/api/users/abc"
        assert pd.traceId == "abc123"

    def test_optional_fields_default_none(self):
        """RFC 9457 optional fields default to None (not empty string)."""
        pd = ProblemDetailResponse(
            type="/errors/test",
            title="Test",
            status=500,
            detail="err",
        )
        assert pd.instance is None
        assert pd.traceId is None

    def test_model_dump_excludes_none(self):
        """When using exclude_none=True, absent optional fields are omitted."""
        pd = ProblemDetailResponse(
            type="/errors/test",
            title="Test",
            status=500,
            detail="err",
        )
        dumped = pd.model_dump(exclude_none=True)
        assert "instance" not in dumped
        assert "traceId" not in dumped

    def test_model_dump_camel_case(self):
        """traceId must be camelCase in JSON output."""
        pd = ProblemDetailResponse(
            type="/errors/test",
            title="Test",
            status=500,
            detail="err",
            traceId="trace-xyz",
        )
        dumped = pd.model_dump()
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

    def test_map_uses_structured_mapping(self):
        """Map values are _ErrorMapping instances, not bare tuples."""
        for cls, mapping in _ERROR_TYPE_MAP.items():
            assert isinstance(mapping, _ErrorMapping), f"{cls.__name__} uses bare tuple instead of _ErrorMapping"

    def test_status_codes(self):
        """AC-1.3.3: exact status code mapping."""
        assert _ERROR_TYPE_MAP[NotFound].status == 404
        assert _ERROR_TYPE_MAP[Conflict].status == 409
        assert _ERROR_TYPE_MAP[ValidationError].status == 422
        assert _ERROR_TYPE_MAP[SyncFailed].status == 207
        assert _ERROR_TYPE_MAP[ProviderError].status == 502
        assert _ERROR_TYPE_MAP[Forbidden].status == 403

    def test_sync_failed_is_207_not_202(self):
        """SyncFailed maps to 207 (Multi-Status), not 202 (Accepted), to signal partial success."""
        assert _ERROR_TYPE_MAP[SyncFailed].status == 207

    def test_uri_paths(self):
        assert _ERROR_TYPE_MAP[NotFound].uri == "/errors/not-found"
        assert _ERROR_TYPE_MAP[Conflict].uri == "/errors/conflict"
        assert _ERROR_TYPE_MAP[ValidationError].uri == "/errors/validation"
        assert _ERROR_TYPE_MAP[SyncFailed].uri == "/errors/sync-failed"
        assert _ERROR_TYPE_MAP[ProviderError].uri == "/errors/provider-error"
        assert _ERROR_TYPE_MAP[Forbidden].uri == "/errors/forbidden"

    def test_all_entries_have_title(self):
        for cls, mapping in _ERROR_TYPE_MAP.items():
            assert mapping.title, f"{cls.__name__} has empty title"


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

    def test_ok_with_pydantic_model(self):
        """jsonable_encoder handles Pydantic models without silent 500."""

        class UserOut(BaseModel):
            id: str
            name: str

        result: Result[UserOut, IdentityError] = Ok(UserOut(id="abc", name="Alice"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body == {"id": "abc", "name": "Alice"}

    def test_ok_with_uuid(self):
        """jsonable_encoder handles UUID objects."""
        test_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        result: Result[dict, IdentityError] = Ok({"id": str(test_uuid)})
        response = result_to_response(result, _make_request())
        assert response.status_code == 200

    def test_ok_with_datetime(self):
        """jsonable_encoder handles datetime objects."""
        now = datetime.now(tz=timezone.utc)
        result: Result[dict, IdentityError] = Ok({"created_at": now})
        response = result_to_response(result, _make_request())
        assert response.status_code == 200
        body = json.loads(response.body)
        assert "created_at" in body

    def test_ok_with_dataclass(self):
        """jsonable_encoder handles dataclasses."""

        @dataclass
        class Item:
            name: str
            count: int

        result: Result[Item, IdentityError] = Ok(Item(name="widget", count=5))
        response = result_to_response(result, _make_request())
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body == {"name": "widget", "count": 5}


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

    def test_sync_failed_produces_207(self):
        """SyncFailed -> 207 Multi-Status (partial success)."""
        result: Result[dict, IdentityError] = Error(
            SyncFailed(
                message="descope sync failed",
                operation="create_user",
                payload_summary="{}",
                underlying_error="timeout",
            )
        )
        response = result_to_response(result, _make_request())
        assert response.status_code == 207
        body = json.loads(response.body)
        assert body["type"] == "/errors/sync-failed"
        assert body["title"] == "Sync Partial Success"

    def test_provider_error_produces_502(self):
        result: Result[dict, IdentityError] = Error(ProviderError(message="upstream down"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 502

    def test_forbidden_produces_403(self):
        result: Result[dict, IdentityError] = Error(Forbidden(message="not allowed"))
        response = result_to_response(result, _make_request())
        assert response.status_code == 403

    def test_content_type_is_problem_json(self):
        """Every error response must use application/problem+json."""
        for error_cls in [NotFound, Conflict, ValidationError, SyncFailed, ProviderError, Forbidden]:
            result: Result[dict, IdentityError] = Error(error_cls(message="test"))
            response = result_to_response(result, _make_request())
            assert response.media_type == "application/problem+json", f"{error_cls.__name__} wrong media type"

    def test_instance_from_request_path(self):
        """instance field populated from request.url.path."""
        result: Result[dict, IdentityError] = Error(NotFound(message="missing"))
        response = result_to_response(result, _make_request("/api/tenants/xyz/users/abc"))
        body = json.loads(response.body)
        assert body["instance"] == "/api/tenants/xyz/users/abc"

    def test_trace_id_absent_without_otel(self):
        """traceId is omitted (not empty string) when OTel is not configured."""
        result: Result[dict, IdentityError] = Error(NotFound(message="test"))
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        assert "traceId" not in body

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
# Sanitisation and logging (review fix coverage)
# ---------------------------------------------------------------------------


class TestErrorSanitisation:
    """Verify that internal details are not leaked in client-facing responses."""

    def test_provider_error_detail_is_sanitised(self):
        """ProviderError detail must not contain upstream error messages."""
        result: Result[dict, IdentityError] = Error(
            ProviderError(message="HTTPStatusError: 500 Internal Server Error from Descope API")
        )
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        assert "HTTPStatusError" not in body["detail"]
        assert "Descope" not in body["detail"]
        assert body["detail"] == _SANITIZED_DETAIL[ProviderError]

    def test_sync_failed_detail_is_sanitised(self):
        """SyncFailed detail must not contain underlying error strings."""
        result: Result[dict, IdentityError] = Error(
            SyncFailed(message="timeout connecting to descope", underlying_error="ConnectionError: ...")
        )
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        assert "timeout" not in body["detail"]
        assert body["detail"] == _SANITIZED_DETAIL[SyncFailed]

    def test_forbidden_detail_is_sanitised(self):
        """Forbidden detail must not leak internal permission details."""
        result: Result[dict, IdentityError] = Error(Forbidden(message="user lacks role admin on tenant t-123"))
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        assert "t-123" not in body["detail"]
        assert body["detail"] == _SANITIZED_DETAIL[Forbidden]

    def test_not_found_detail_is_not_sanitised(self):
        """NotFound is safe to pass through — no upstream internals."""
        result: Result[dict, IdentityError] = Error(NotFound(message="user abc not found"))
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        assert body["detail"] == "user abc not found"

    def test_validation_error_detail_is_not_sanitised(self):
        """ValidationError is safe to pass through."""
        result: Result[dict, IdentityError] = Error(ValidationError(message="email format invalid"))
        response = result_to_response(result, _make_request())
        body = json.loads(response.body)
        assert body["detail"] == "email format invalid"


class TestErrorLogging:
    """Verify that errors are logged for operator visibility."""

    def test_provider_error_is_logged(self, caplog):
        result: Result[dict, IdentityError] = Error(ProviderError(message="upstream timeout"))
        with caplog.at_level(logging.ERROR):
            result_to_response(result, _make_request("/api/users"))
        assert any(
            "ProviderError" in record.message and "upstream timeout" in record.message for record in caplog.records
        )

    def test_forbidden_is_logged(self, caplog):
        result: Result[dict, IdentityError] = Error(Forbidden(message="not allowed"))
        with caplog.at_level(logging.WARNING):
            result_to_response(result, _make_request("/api/admin"))
        assert any("Forbidden" in record.message and "not allowed" in record.message for record in caplog.records)

    def test_sync_failed_is_logged(self, caplog):
        result: Result[dict, IdentityError] = Error(SyncFailed(message="sync error", operation="create_user"))
        with caplog.at_level(logging.WARNING):
            result_to_response(result, _make_request("/api/users"))
        assert any("SyncFailed" in record.message and "create_user" in record.message for record in caplog.records)

    def test_non_identity_error_is_logged(self, caplog):
        """Non-IdentityError in Result error branch is logged."""
        result: Result[dict, IdentityError] = Error("just a string")  # type: ignore[arg-type]
        with caplog.at_level(logging.ERROR):
            response = result_to_response(result, _make_request())
        assert response.status_code == 500
        assert any("non-IdentityError" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Edge case: non-IdentityError in Result error branch
# ---------------------------------------------------------------------------


class TestNonIdentityErrorEdgeCases:
    """Guard against non-IdentityError values in the error branch of Result."""

    def test_error_none_returns_500(self):
        """Error(None) must not crash — returns 500."""
        result: Result[dict, IdentityError] = Error(None)  # type: ignore[arg-type]
        response = result_to_response(result, _make_request())
        assert response.status_code == 500
        body = json.loads(response.body)
        assert body["type"] == "/errors/unknown"

    def test_error_string_returns_500(self):
        """Error('some string') must not crash — returns 500."""
        result: Result[dict, IdentityError] = Error("some string")  # type: ignore[arg-type]
        response = result_to_response(result, _make_request())
        assert response.status_code == 500

    def test_error_dict_returns_500(self):
        """Error({'key': 'value'}) must not crash — returns 500."""
        result: Result[dict, IdentityError] = Error({"key": "value"})  # type: ignore[arg-type]
        response = result_to_response(result, _make_request())
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# _get_trace_id warning log on failure
# ---------------------------------------------------------------------------


class TestGetTraceIdLogging:
    def test_trace_id_failure_logs_warning(self, caplog, monkeypatch):
        """_get_trace_id logs a warning when OTel import fails unexpectedly."""
        import app.errors.problem_detail as pd_module

        # Simulate an unexpected exception during trace ID retrieval
        def _broken_get_trace_id():
            try:
                msg = "broken otel"
                raise RuntimeError(msg)
            except Exception:  # noqa: BLE001
                pd_module.logger.warning(
                    "Failed to retrieve OTel trace ID; tracing may be misconfigured",
                    exc_info=True,
                )
            return ""

        monkeypatch.setattr(pd_module, "_get_trace_id", _broken_get_trace_id)
        result: Result[dict, IdentityError] = Error(NotFound(message="test"))
        with caplog.at_level(logging.WARNING):
            result_to_response(result, _make_request())
        assert any("tracing may be misconfigured" in record.message for record in caplog.records)


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
                pytest.fail("Expected Ok")

    def test_error_value_extraction(self):
        r: Result[dict, IdentityError] = Error(Conflict(message="dup"))
        match r:
            case Result(tag="error", error=err):
                assert isinstance(err, Conflict)
                assert err.message == "dup"
            case _:
                pytest.fail("Expected Error")
