"""Reading the authorization callback out of the navigations a browser login saw.

The browser itself is exercised by the Keycloak e2e suite; these cases cover the
navigations that suite cannot provoke on demand.
"""

import pytest

from tests.e2e_keycloak.browser_login import KeycloakBrowserFlowError, authorization_code, is_callback
from tests.helpers.keycloak import KEYCLOAK_REDIRECT_URI

STATE = "this-request-state"
CALLBACK = f"{KEYCLOAK_REDIRECT_URI}/?state={STATE}&session_state=s&code=the-code"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CALLBACK, True),
        (f"{KEYCLOAK_REDIRECT_URI}?state={STATE}&error=access_denied", True),
        (f"{KEYCLOAK_REDIRECT_URI}/login", False),
        (f"{KEYCLOAK_REDIRECT_URI}/", False),
        (f"{KEYCLOAK_REDIRECT_URI}/?state=another-request&code=c", False),
        (f"{KEYCLOAK_REDIRECT_URI}/?state={STATE}", False),
        (f"http://localhost:30001/?state={STATE}&code=c", False),
    ],
)
def test_only_a_callback_for_this_request_counts(url, expected):
    assert is_callback(url, STATE) is expected


def test_the_one_callback_gives_its_code():
    assert authorization_code([CALLBACK], STATE) == "the-code"


def test_two_callbacks_raise_a_flow_error_naming_both():
    second = f"{KEYCLOAK_REDIRECT_URI}/?state={STATE}&code=another-code"
    with pytest.raises(KeycloakBrowserFlowError) as raised:
        authorization_code([CALLBACK, second], STATE)
    assert CALLBACK in str(raised.value)
    assert second in str(raised.value)


def test_no_callback_raises_a_flow_error():
    with pytest.raises(KeycloakBrowserFlowError):
        authorization_code([], STATE)


def test_an_error_callback_raises_a_flow_error_with_the_error():
    with pytest.raises(KeycloakBrowserFlowError, match="access_denied"):
        authorization_code([f"{KEYCLOAK_REDIRECT_URI}?state={STATE}&error=access_denied"], STATE)
