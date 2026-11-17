# Recorded Keycloak Admin API answers

`responses.json` holds the answers `quay.io/keycloak/keycloak:26.8.2` gave in the compose
`keycloak-test` container, realm `identity-stack` imported from
[`keycloak/realm/identity-stack-realm.json`](../../../../keycloak/realm/identity-stack-realm.json),
to the admin adapter's service account `identity-stack-admin` (secret `local-only-admin-client`,
a local fixture). Taken on 2026-11-16 by `record.py` in this directory, run from `backend/` with
`keycloak-test` up and freshly created:

```bash
python tests/fixtures/keycloak_admin/record.py > tests/fixtures/keycloak_admin/responses.json
```

Each entry is one request and its answer: method, URL (`{base}` for
`http://keycloak-test.localhost:18180`), status, JSON body and, for a 201, the `Location`
header. The access tokens in the `token` and `directory_token` answers are replaced by
`recorded-access-token` and `recorded-directory-token` and their refresh tokens dropped; nothing
else is edited. The recording creates and deletes one
organization (alias `7e5a0000-0000-4000-8000-000000000586`) and one user (username
`05e40000-0000-4000-8000-000000000586`), and records refusals: a wrong secret, an unknown
client, a token the Admin API does not accept, a write by the read-only directory client (403),
a taken name, email and domain, and the 404s of objects already gone. It also records the startup
role check for both clients: each finds its own service account, reads its role mappings and, where
they name `realm-management`, that client's effective roles. The directory client is shown no
client mappings (`{}`).

`tests/helpers/keycloak_admin_recorded.py` replays them, one answer per request, for
`tests/unit/test_keycloak_sync_adapter.py` and `tests/unit/test_keycloak_admin_api.py`.
