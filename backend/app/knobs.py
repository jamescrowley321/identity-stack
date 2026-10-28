"""The one place the backend reads its environment.

Every environment variable the application reads goes through a function in
this module (docs/conventions.md §1). Nothing else in ``app/`` touches
``os.environ`` or ``os.getenv``; Ruff's ``banned-api`` rule (TID251) and
``tests/unit/test_house_rules.py`` both fail the build if something does.

The helpers read the environment **when they are called**, not when this module
is imported. That keeps the read timing of every call site exactly what it was
when the call site read ``os.getenv`` itself: a module-level constant such as
``RATE_LIMIT_AUTH`` is still fixed at import, and a value read inside a function
still sees whatever the environment holds at that moment. A caller that wants a
value fixed for the process lifetime assigns it to a module-level constant; a
caller that must follow the environment reads it inside the function that uses
it. Do not add caching here — tests and operators rely on both behaviours.

Names and defaults are part of the deployment contract. Every name passed to
these helpers MUST appear in ``docs/environment-variables.md`` with the default
given at the call site; ``test_house_rules.py`` checks the names.
"""

from __future__ import annotations

import os
from typing import overload

# The spellings ``knob_flag`` treats as "off". Anything else, including an empty
# string, is "on". That is fail-closed only for a switch whose "on" state is the
# secure one, such as ORY_REQUIRE_AUDIENCE: a typo leaves the check enabled.
_FLAG_OFF = frozenset({"false", "0", "no"})


@overload
def knob(name: str) -> str | None: ...


@overload
def knob(name: str, default: str) -> str: ...


def knob(name: str, default: str | None = None) -> str | None:
    """Return the raw value of environment variable ``name``, or ``default``.

    The value is returned exactly as set: not stripped, not lowercased, and an
    empty string is returned as an empty string rather than replaced by
    ``default``. Callers that treat "set but empty" as unset write
    ``knob(NAME) or fallback`` at the call site, so the choice stays visible.
    """
    return os.environ.get(name, default)


def required_knob(name: str) -> str:
    """Return environment variable ``name``, raising ``KeyError(name)`` if it is unset.

    An empty value counts as set. Only ``main.lifespan`` uses this, for the two
    Descope credentials the app refuses to start without; its error message is
    built from the ``KeyError``.
    """
    return os.environ[name]


def knob_flag(name: str, default: str) -> bool:
    """Parse ``name`` as an on/off switch.

    Rule: a flag is off only when its value, stripped and lowercased, is one of
    ``false``, ``0`` or ``no``. Every other value — ``true``, ``1``, ``yes``,
    ``off``, an empty string, a typo — is on.

    That makes a typo fail closed only when "on" is the secure state, so a switch
    read with this helper MUST be named for the secure behaviour (``REQUIRE_``,
    ``ENFORCE_``), never for the insecure one (``SKIP_``, ``DISABLE_``,
    ``ALLOW_INSECURE_``). See docs/conventions.md §3.
    """
    return knob(name, default).strip().lower() not in _FLAG_OFF


def knob_set(name: str, default: str) -> set[str]:
    """Parse ``name`` as a comma-separated set of lowercased names.

    Entries are stripped and lowercased; empty entries are dropped, so
    ``"ory, ,Descope"`` reads as ``{"ory", "descope"}`` and ``""`` reads as the
    empty set (the feature is off for every provider, not reset to the default).
    """
    return {entry.strip().lower() for entry in knob(name, default).split(",") if entry.strip()}
