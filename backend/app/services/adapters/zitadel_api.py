"""ZitadelAdminApi — the Zitadel calls ZitadelSyncAdapter makes, with a machine user's token.

One method per call, against the v2 resource APIs where Zitadel has one:

- organizations and human users: the v2 REST paths (``/v2/organizations``, ``/v2/users``);
- project roles and grants: the v2 ``ProjectService``, which Zitadel serves over Connect
  only (``/zitadel.project.v2.ProjectService/<Method>``, JSON bodies);
- an organization's login policy: management v1. Zitadel has no v2 call that sets login
  settings (``/v2/settings/login`` only reads them).

A non-2xx answer raises ``UpstreamStatusError`` and no answer ``UpstreamTransportError``
(``app.outbound``); the adapter turns both into ``SyncError``. The token goes in the
``Authorization`` header only and is never logged (conventions §9).
"""

from __future__ import annotations

from urllib.parse import quote

from app.outbound import OutboundClient, OutboundResponse

_PROJECT_SERVICE = "/zitadel.project.v2.ProjectService"
_LOGIN_POLICY = "/management/v1/policies/login"
_NOT_FOUND = 404

# The fields of an organization's login policy (management v1 AddCustomLoginPolicy),
# as Zitadel answers them in the policy an organization has. Zitadel zeroes every field
# a new policy leaves out, so a policy that named only allowRegister would also turn
# off password login. The policy's linked identity providers (``idps``) are not
# copied: their shape differs between the answer and the request.
LOGIN_POLICY_FIELDS = frozenset(
    {
        "allowUsernamePassword",
        "allowRegister",
        "allowExternalIdp",
        "forceMfa",
        "forceMfaLocalOnly",
        "passwordlessType",
        "hidePasswordReset",
        "ignoreUnknownUsernames",
        "defaultRedirectUri",
        "passwordCheckLifetime",
        "externalLoginCheckLifetime",
        "mfaInitSkipLifetime",
        "secondFactorCheckLifetime",
        "multiFactorCheckLifetime",
        "secondFactors",
        "multiFactors",
        "allowDomainDiscovery",
        "disableLoginWithEmail",
        "disableLoginWithPhone",
    }
)


def login_policy_without_registration(effective: dict) -> dict:
    """The organization's own policy: the one it has now, with self-registration off."""
    policy = {field: value for field, value in effective.items() if field in LOGIN_POLICY_FIELDS}
    policy["allowRegister"] = False
    return policy


def _segment(value: str) -> str:
    """An id as one path segment: nothing in it can name another endpoint."""
    return quote(value, safe="")


class ZitadelAdminApi:
    """The Zitadel Admin calls of the sync adapter, over a client its caller owns."""

    def __init__(self, http: OutboundClient, *, base_url: str, token: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _send(
        self, method: str, path: str, *, body: dict | None = None, org_id: str | None = None
    ) -> OutboundResponse:
        headers = dict(self._headers)
        if org_id is not None:
            headers["x-zitadel-orgid"] = org_id
        return await self._http.request(method, self._base_url + path, json=body, headers=headers)

    async def _call(self, method: str, path: str, *, body: dict | None = None, org_id: str | None = None) -> dict:
        response = await self._send(method, path, body=body, org_id=org_id)
        response.raise_for_status()
        return response.json()

    async def add_organization(self, name: str) -> str:
        """Create an organization; its id."""
        answer = await self._call("POST", "/v2/organizations", body={"name": name})
        return answer["organizationId"]

    async def effective_login_policy(self, org_id: str) -> dict:
        """The login policy the organization has: its own, or the instance default it inherits."""
        answer = await self._call("GET", _LOGIN_POLICY, org_id=org_id)
        return answer["policy"]

    async def add_login_policy(self, org_id: str, policy: dict) -> None:
        """Give the organization a login policy of its own."""
        await self._call("POST", _LOGIN_POLICY, body=policy, org_id=org_id)

    async def project_role_keys(self, project_id: str) -> list[str]:
        """The keys of the project's roles, sorted."""
        answer = await self._call("POST", f"{_PROJECT_SERVICE}/ListProjectRoles", body={"projectId": project_id})
        return sorted(role["key"] for role in answer.get("projectRoles", []))

    async def grant_project(self, project_id: str, org_id: str, role_keys: list[str]) -> None:
        """Grant the project, with those roles, to the organization."""
        await self._call(
            "POST",
            f"{_PROJECT_SERVICE}/CreateProjectGrant",
            body={"projectId": project_id, "grantedOrganizationId": org_id, "roleKeys": role_keys},
        )

    async def add_human_user(
        self, *, user_id: str, org_id: str, username: str, given_name: str, family_name: str, email: str
    ) -> None:
        """Create a human user with that id in the organization, its email marked verified."""
        await self._call(
            "POST",
            "/v2/users/human",
            body={
                "userId": user_id,
                "organization": {"orgId": org_id},
                "username": username,
                "profile": {"givenName": given_name, "familyName": family_name},
                "email": {"email": email, "isVerified": True},
            },
        )

    async def user_state(self, user_id: str) -> str | None:
        """The user's state (``USER_STATE_ACTIVE``, ``USER_STATE_INACTIVE``, ...), None if Zitadel has no such user."""
        response = await self._send("GET", f"/v2/users/{_segment(user_id)}")
        if response.status_code == _NOT_FOUND:
            return None
        response.raise_for_status()
        return response.json()["user"]["state"]

    async def deactivate_user(self, user_id: str) -> None:
        await self._call("POST", f"/v2/users/{_segment(user_id)}/deactivate", body={})

    async def reactivate_user(self, user_id: str) -> None:
        await self._call("POST", f"/v2/users/{_segment(user_id)}/reactivate", body={})

    async def delete_user(self, user_id: str) -> bool:
        """Remove the user. False when Zitadel had no such user."""
        response = await self._send("DELETE", f"/v2/users/{_segment(user_id)}")
        if response.status_code == _NOT_FOUND:
            return False
        response.raise_for_status()
        return True
