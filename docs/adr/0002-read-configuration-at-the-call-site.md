# ADR-0002: Read configuration at the call site

- **Status:** Superseded by [ADR-0005](0005-central-settings-module.md)
- **Date:** 2026-10-28 (recorded retroactively. It describes the tree up to `fc01974`.)

## Context

The backend has no configuration object. It started as a single FastAPI app whose
settings were a handful of Descope credentials, and each module that needed a value read
it where it was used.

## Decision

Each module reads the environment variables it needs with `os.getenv(NAME, default)`
or `os.environ[NAME]`, at the point of use. Some reads are module-level constants
(`RATE_LIMIT_AUTH`, `DEPLOYMENT_MODE`, the internal-endpoint secrets). Others run inside
the function that uses the value (`DESCOPE_MANAGEMENT_KEY` in logout,
`OTEL_EXPORTER_OTLP_ENDPOINT` in `init_telemetry`).

## Consequences

- Adding a variable is one line, next to the code that uses it.
- At `fc01974` there were 42 reads across `backend/app`, with no single list of what the
  app reads. `docs/environment-variables.md` had drifted and was missing eight of them.
- The same variable was parsed in several places with copies of the same logic. For
  example, `ORY_REQUIRE_AUDIENCE`'s "only `false`/`0`/`no` turn it off" rule appeared in
  both `middleware/factory.py` and `routers/auth.py`.
- An agent or a new contributor copies whichever read it finds first.

## Superseded by

[ADR-0005](0005-central-settings-module.md) keeps the read timing this ADR produced and
moves every read behind `app.knobs`.
