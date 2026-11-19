import { jwtDecode } from "../utils/jwt";

/** localStorage key for the persisted active-tenant selection. */
export const TENANT_SELECTION_KEY = "identity.currentTenantId";

/** Keycloak's claim: the aliases of the organizations the token was issued for. */
export const KEYCLOAK_ORGANIZATION_CLAIM = "organization";

/** Zitadel's claim (scope `urn:zitadel:iam:user:resourceowner`): the user's organization id. */
export const ZITADEL_ORGANIZATION_CLAIM = "urn:zitadel:iam:user:resourceowner:id";

export function readStoredTenantId(): string | null {
  try {
    return window.localStorage.getItem(TENANT_SELECTION_KEY);
  } catch {
    return null;
  }
}

export function storeTenantId(tenantId: string): void {
  try {
    window.localStorage.setItem(TENANT_SELECTION_KEY, tenantId);
  } catch {
    // localStorage unavailable — selection persists for this session only.
  }
}

/**
 * Drop the persisted selection. Called on every new sign-in, so a sign-in opens
 * the tenant it named rather than one chosen in an earlier session; a reload or
 * a token renewal keeps the selection.
 */
export function forgetTenantSelection(): void {
  try {
    window.localStorage.removeItem(TENANT_SELECTION_KEY);
  } catch {
    // localStorage unavailable — nothing was persisted.
  }
}

/**
 * The one organization a set of token claims names, or `null`.
 *
 * Keycloak's `organization` is a list of aliases, or a map of alias to its
 * attributes: exactly one alias names an organization, two or more name none.
 * Zitadel's `urn:zitadel:iam:user:resourceowner:id` is the organization id.
 */
export function claimedOrganization(claims: Record<string, unknown>): string | null {
  const keycloak = claims[KEYCLOAK_ORGANIZATION_CLAIM];
  if (keycloak !== null && typeof keycloak === "object") {
    const aliases = Array.isArray(keycloak) ? keycloak : Object.keys(keycloak);
    if (aliases.length === 1 && typeof aliases[0] === "string" && aliases[0]) return aliases[0];
    return null;
  }
  const zitadel = claims[ZITADEL_ORGANIZATION_CLAIM];
  return typeof zitadel === "string" && zitadel ? zitadel : null;
}

function accessTokenClaims(accessToken: string | undefined): Record<string, unknown> {
  if (!accessToken) return {};
  try {
    const claims = jwtDecode(accessToken);
    return claims !== null && typeof claims === "object" ? claims : {};
  } catch {
    // An opaque access token carries no claims the SPA can read.
    return {};
  }
}

/**
 * The organization the sign-in named: from the ID token's (or UserInfo's)
 * claims, else from the access token's (Zitadel puts its organization claim in
 * the access token only). The token chooses nothing: this only picks which
 * membership the selector starts on, and the backend checks the selection.
 */
export function signInOrganization(
  profile: Record<string, unknown> | undefined,
  accessToken: string | undefined,
): string | null {
  return claimedOrganization(profile ?? {}) ?? claimedOrganization(accessTokenClaims(accessToken));
}

/** A membership as the selector reads it: `external_org_id` is absent from an older payload. */
export interface SelectableMembership {
  tenant_id: string;
  external_org_id?: string | null;
}

/**
 * The active tenant: the selection while it is still a membership, else the
 * membership whose organization the sign-in named, else the first.
 */
export function initialTenantId(
  memberships: SelectableMembership[],
  selected: string | null,
  organization: string | null,
): string | null {
  if (memberships.length === 0) return null;
  if (selected && memberships.some((m) => m.tenant_id === selected)) return selected;
  const named = organization ? memberships.find((m) => m.external_org_id === organization) : undefined;
  return (named ?? memberships[0]).tenant_id;
}
