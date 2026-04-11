# Tyk Gateway Configuration

This directory holds the file-based Tyk configuration the gateway profile
mounts into the `tyk-gateway` container. Tyk runs in [file-based mode]
(`use_db_app_configs: false`), so editing files here and running
`make dev-gateway` (or `docker compose --profile gateway up --build`) is
the canonical way to change gateway behavior.

[file-based mode]: https://tyk.io/docs/tyk-self-managed/install/install-tyk-gateway/configure-tyk-gateway/

## Layout

```
tyk/
├── README.md           ← this file
├── tyk.conf            ← gateway-level config (listen port, Redis, policies, app_path)
├── apps/               ← API definition templates (read by tyk-init sidecar)
│   └── saas-backend.json
├── policies/           ← shared OpenID/key policies
│   └── policies.json
└── middleware/         ← Tyk JS plugins (currently empty placeholder)
```

The base directory layout is mounted into the container at boot:

| Host path             | Container path                            | Mode | Notes                                                   |
| --------------------- | ----------------------------------------- | ---- | ------------------------------------------------------- |
| `tyk/tyk.conf`        | `/opt/tyk-gateway/tyk.conf`               | rw   | Gateway config — substituted at startup, edit freely    |
| `tyk/apps/`           | (mounted into `tyk-init`'s `/in`, not gateway) | ro   | API definition **templates** (with `${VAR}` placeholders) |
| `tyk-apps` (volume)   | `/opt/tyk-gateway/apps`                   | ro   | Substituted API definitions (written by `tyk-init`)     |
| `tyk/middleware/`     | `/opt/tyk-gateway/middleware`             | rw   | JS plugin directory                                     |
| `tyk/policies/`       | `/opt/tyk-gateway/policies`               | rw   | Policy JSON                                             |

## How env-var substitution works (`tyk-init` sidecar)

The `tykio/tyk-gateway:v5.3` image is **scratch-based** — it ships only
the `tyk` binary and has no shell. That means a shebang script (the
old `tyk/entrypoint.sh` approach) cannot run inside the container at
all; it crashes with `exec: no such file or directory` because the
kernel can't find `/bin/sh`. This was discovered in [issue #240]
during the CI wire-up of the profile integration tests, after the
broken setup had been silently sitting in `main` for four days.

[issue #240]: https://github.com/jamescrowley321/identity-stack/issues/240

The fix is the **`tyk-init` init sidecar** defined in
`docker-compose.yml`:

```yaml
tyk-init:
  image: alpine:3.20
  environment:
    - DESCOPE_PROJECT_ID=${DESCOPE_PROJECT_ID:?...}
  volumes:
    - ./tyk/apps:/in:ro          # template source
    - tyk-apps:/out              # substituted output
  entrypoint:
    - /bin/sh
    - -eu
    - -c
    - |
      # validate DESCOPE_PROJECT_ID
      # rm -f /out/*.json
      # for f in /in/*.json; do sed substitute → /out/$(basename $f)
  profiles: [gateway, full]

tyk-gateway:
  image: tykio/tyk-gateway:v5.3
  volumes:
    - ./tyk/tyk.conf:/opt/tyk-gateway/tyk.conf
    - tyk-apps:/opt/tyk-gateway/apps:ro    # mounts substituted output
    - ./tyk/middleware:/opt/tyk-gateway/middleware
    - ./tyk/policies:/opt/tyk-gateway/policies
  depends_on:
    tyk-init: { condition: service_completed_successfully }
    tyk-redis: { condition: service_started }
    backend:   { condition: service_started }
```

`tyk-gateway` uses its image default entrypoint (`/opt/tyk-gateway/tyk`)
and waits on `tyk-init` to exit successfully via
`service_completed_successfully`. The `tyk-apps` named volume is the
shared substitution output directory, mounted read-only into the
gateway. Adding a new template:

  1. Drop `your-api.json` into `tyk/apps/`.
  2. Use `${DESCOPE_PROJECT_ID}` (or any other env var) anywhere you
     want compose-time substitution.
  3. `make dev-gateway` (or `docker compose --profile gateway up --build`)
     — `tyk-init` will sed-substitute it on next boot.

## API definition: `tyk/apps/saas-backend.json`

Defines the proxy from Tyk's `/api/` to the backend. Key fields:

| Field                  | Value                          | Why                                                            |
| ---------------------- | ------------------------------ | -------------------------------------------------------------- |
| `listen_path`          | `/api/`                        | Tyk listens for requests under this prefix                     |
| `target_url`           | `http://backend:8000/`         | Backend root — Tyk forwards the full incoming path verbatim    |
| `strip_listen_path`    | `false`                        | Keep the `/api/` prefix when forwarding (it's part of FastAPI's routes)|
| `preserve_host_header` | `true`                         | Backend sees the original `Host` header                        |
| `strip_auth_data`      | `false`                        | Authorization header is forwarded so FastAPI can read tenant claims |
| `use_openid`           | `true`                         | Validate JWTs against Descope's OpenID Connect discovery       |
| `openid_options.providers` | dual issuer                | Descope emits two issuer formats (OIDC and session/access-key) |
| `extended_paths.ignored` | `/api/health` GET            | Health endpoint bypasses JWT validation for readiness probes   |
| `extended_paths.rate_limit` | `/api/validate-id-token` 10/min POST | Stricter rate limit on a token-handling route        |

The `target_url` is the **backend root**, not `http://backend:8000/api/`.
With `strip_listen_path: false`, Tyk appends the full incoming path
(`/api/<x>`) to the target URL. If the target included `/api/`, the
backend would see `/api/api/<x>` and 404. The unit test
`test_api_definition_required_keys` pins this to prevent the
duplicated-prefix regression that was uncovered alongside #240.

## tyk.conf

| Key                  | Value                                               | Notes                                                            |
| -------------------- | --------------------------------------------------- | ---------------------------------------------------------------- |
| `listen_port`        | `8080`                                              | Compose maps to host `127.0.0.1:8080`                            |
| `secret`             | `REPLACE_AT_RUNTIME`                                | Sentinel — overridden by `TYK_GW_SECRET` env var (no hardcoded secrets) |
| `app_path`           | `/opt/tyk-gateway/apps`                             | Tyk reads API definitions from here — populated by `tyk-init`    |
| `use_db_app_configs` | `false`                                             | File-based mode, no Tyk Dashboard required                       |
| `storage`            | Redis (`tyk-redis:6379`)                            | Separate Redis instance from the app's `redis` service           |

## Operating procedures

| Command                                | Effect                                                          |
| -------------------------------------- | --------------------------------------------------------------- |
| `make dev-gateway`                     | Spin up gateway profile + backend in `DEPLOYMENT_MODE=gateway`  |
| `make test-integration-gateway`        | Lifecycle-managed end-to-end test (CI gate)                     |
| `docker compose logs tyk-init`         | Inspect substitution output (template count + filenames)        |
| `docker compose logs tyk-gateway`      | Inspect Tyk's startup, API loading, and request logs            |

## Troubleshooting

**`tyk-init` exits 1 with `FATAL: DESCOPE_PROJECT_ID contains invalid characters`**

The substitution script enforces `[A-Za-z0-9_-]` on the project id to
prevent shell-injection-style placeholder values. Set `DESCOPE_PROJECT_ID`
in `.env` (or your CI env) to a real project id.

**`tyk-gateway` healthcheck fails / 404 from `/api/health`**

Confirm `tyk-init` exited cleanly and the `tyk-apps` volume contains
the substituted JSON: `docker run --rm -v identity-stack_tyk-apps:/data alpine:3.20 ls -la /data/`.
If the volume is empty, the init sidecar didn't run — check
`docker compose logs tyk-init`.

If the volume is populated but the gateway returns 404 on `/api/<x>`
that the backend should serve, verify `target_url` is `http://backend:8000/`
(NOT `http://backend:8000/api/`) — the duplicated-prefix bug is
specifically what `test_api_definition_required_keys` guards against.
