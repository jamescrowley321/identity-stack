"""Unit tests for auth dependency functions."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.dependencies.auth import get_claims, get_current_user


class TestGetCurrentUser:
    def test_returns_principal_when_present(self):
        request = MagicMock()
        request.state.principal = MagicMock()
        result = get_current_user(request)
        assert result is request.state.principal

    def test_raises_401_when_no_principal(self):
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(request)
        assert exc_info.value.status_code == 401

    def test_raises_401_when_principal_is_none(self):
        request = MagicMock()
        request.state.principal = None
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(request)
        assert exc_info.value.status_code == 401


class TestGetClaims:
    def test_returns_claims_when_present(self):
        request = MagicMock()
        request.state.claims = {"sub": "user123", "email": "test@example.com"}
        result = get_claims(request)
        assert result == {"sub": "user123", "email": "test@example.com"}

    def test_raises_401_when_no_claims(self):
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            get_claims(request)
        assert exc_info.value.status_code == 401

    def test_raises_401_when_claims_is_none(self):
        request = MagicMock()
        request.state.claims = None
        with pytest.raises(HTTPException) as exc_info:
            get_claims(request)
        assert exc_info.value.status_code == 401
