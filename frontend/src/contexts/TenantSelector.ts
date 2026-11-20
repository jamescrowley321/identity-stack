import { createContext } from "react";

/**
 * The request header that names the tenant an API call is for.
 *
 * The backend reads it for a provider whose authorization is canonical (Ory,
 * Zitadel, Keycloak): it chooses among the caller's own memberships and grants
 * nothing by itself. Switching tenant is choosing another value for it; the
 * token is not re-issued. For Descope, whose tenant comes from the token
 * (`dct`), the backend answers in the token's tenant whatever the header says.
 */
export const TENANT_SELECTOR_HEADER = "X-Tenant-ID";

/**
 * The active tenant `useApiClient` names on every call, or `null` for none.
 *
 * `IdentityProvider` supplies it to its children. Its own `GET /api/identity`
 * call sits outside the provider and names no tenant: the identity spans all of
 * the caller's tenants, and it is what the active tenant is chosen from.
 */
export const TenantSelectorContext = createContext<string | null>(null);
