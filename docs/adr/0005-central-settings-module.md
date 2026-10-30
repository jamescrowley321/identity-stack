# ADR-0005: One settings module, `app.knobs`, read at call time

- **Status:** Accepted
- **Date:** 2026-10-28
- **Supersedes:** [ADR-0002](0002-read-configuration-at-the-call-site.md)
- **Rules:** [conventions §1–§3](../conventions.md#configuration)

## Context

[ADR-0002](0002-read-configuration-at-the-call-site.md) left 42 environment reads spread
across `backend/app` (see its Consequences). The usual fix is a settings object, such as a
Pydantic `BaseSettings` instance built once at startup. That changes *when* each value is
read. Module-level constants are already fixed at import. Function-level reads follow the
environment, and the test suite relies on that: tests `monkeypatch.setenv` and then call
the function. A frozen settings object would change behaviour for those reads, and so
would a cached accessor.

## Decision

- `backend/app/knobs.py` is the only module in `app/` that touches `os.environ`. It exposes
  four functions:
  - `knob(name, default=None)` returns the raw value.
  - `required_knob(name)` raises `KeyError(name)` when the variable is unset.
  - `knob_flag(name, default)` returns `False` only for `false`, `0` or `no`.
  - `knob_set(name, default)` parses a comma-separated, lowercased set.
- The functions read the environment on every call and cache nothing. Each call site keeps
  the timing it had. A module-level constant is still read at import, and a function-local
  read still sees the current environment.
- No variable changes name or default. `docs/environment-variables.md` lists every name a
  knob helper is called with, with the call site's default.

## Consequences

- `rg 'os\.(environ|getenv)' backend/app` matches only `app/knobs.py`. Ruff (TID251) and
  `test_house_rules.py` keep it that way.
- The documentation test found eight variables the code read but the documentation did not
  list: `CSP_POLICY`, `ORY_ISSUER_URL`, `ORY_AUDIENCE`, `ORY_REQUIRE_AUDIENCE`,
  `ORY_CLIENT_ID`, `ORY_POST_LOGOUT_REDIRECT_URI`, `JIT_ENABLED_PROVIDERS` and
  `CANONICAL_RBAC_PROVIDERS`. They are documented now.
- There is still no typed settings object. Values arrive as strings and are converted at
  the call site, as `IDENTITY_CACHE_TTL` is. A typed object remains possible later. It
  would need its own ADR, because it changes read timing.
