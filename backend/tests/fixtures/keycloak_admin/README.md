# Recorded Keycloak Admin API answers

`responses.json` holds the answers `quay.io/keycloak/keycloak:26.8.2` gave in the compose
`keycloak-test` container, realm `identity-stack` imported from
[`keycloak/realm/identity-stack-realm.json`](../../../../keycloak/realm/identity-stack-realm.json),
to the admin adapter's service account `identity-stack-admin` (secret `local-only-admin-client`,
a local fixture). Taken on 2026-11-19 by `record.py` in this directory, run from `backend/` with
`keycloak-test` up and freshly created:

```bash
python tests/fixtures/keycloak_admin/record.py > tests/fixtures/keycloak_admin/responses.json
```

Each entry is one request and its answer: method, URL (`{base}` for
`http://keycloak-test.localhost:18180`), status, JSON body and, for a 201, the `Location`
header. The access token in the `token` answer is replaced by `recorded-access-token` and its
refresh token dropped, and a client's `secret` is left out of client listings; nothing else is
edited. The recording creates and deletes one organization (alias
`7e5a0000-0000-4000-8000-000000000586`), one user (username
`05e40000-0000-4000-8000-000000000586`), one permission client role (`recorded:read`) and one role
client role (`recorded-role`) of the API client `identity-stack-api`, and one group of the
organization. It records the startup check (the service account's own `realm-management` roles,
then the API client lookup), the role's composites added and removed, the group created, renamed,
given the member and emptied, the member's organizations, and refusals: a wrong secret, an unknown
client, a token the Admin API does not accept, a taken name, email, domain, role name and group
name, an API client the service account cannot see, and the answers for objects already there or
already gone (among them the generic 404 Keycloak gives a membership that is not there, and the
same 404 for a route that does not match, for removing a user the realm lacks from an organization,
and for the organizations of a user the realm lacks; a missing organization's own 404).

`tests/helpers/keycloak_admin_recorded.py` replays them, one answer per request, for
`tests/unit/test_keycloak_sync_adapter.py` and `tests/unit/test_keycloak_admin_api.py`.
