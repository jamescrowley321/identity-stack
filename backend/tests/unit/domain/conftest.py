"""Domain tests are pure: no app state, no database, no event loop, no mocks."""

import pytest


@pytest.fixture(autouse=True)
def _init_app_state():
    """Replace the unit suite's app-state fixture, which imports the FastAPI app."""
    return
