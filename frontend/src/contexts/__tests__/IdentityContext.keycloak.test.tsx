import { describe, it, expect, vi, beforeAll, beforeEach, afterEach, afterAll } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "react-oidc-context";
import App from "../../App";
import { IdentityProvider, type CanonicalIdentity } from "../IdentityContext";
import { buildOidcConfig, type OidcEnv } from "../../config/oidc";
import { TENANT_SELECTION_KEY } from "../tenantSelection";

// The whole SPA session against a Keycloak realm: the real AuthProvider
// (oidc-client-ts), IdentityProvider, useApiClient, App, TenantSwitcher and
// UserMenu, built from the `frontend_environment` output of infra/keycloak.
// Only the network and the browser's navigation are stubbed.

const ISSUER = "http://keycloak.localhost:8180/realms/identity-stack";
const SPA_CLIENT_ID = "identity-stack-spa";
const ORIGIN = "http://localhost:3000";
const PLAIN_SCOPE = "openid profile email organization";

const keycloakEnv: OidcEnv = {
  VITE_OIDC_AUTHORITY: ISSUER,
  VITE_OIDC_CLIENT_ID: SPA_CLIENT_ID,
  VITE_OIDC_REDIRECT_URI: ORIGIN,
  VITE_OIDC_SCOPE: PLAIN_SCOPE,
};

const AUTHORIZE_ENDPOINT = `${ISSUER}/protocol/openid-connect/auth`;
const TOKEN_ENDPOINT = `${ISSUER}/protocol/openid-connect/token`;
const END_SESSION_ENDPOINT = `${ISSUER}/protocol/openid-connect/logout`;

const SUBJECT = "kc-sub-bob";
const AUTH_TIME = 1_900_000_000;

const bob: CanonicalIdentity = {
  user: {
    id: "user-bob",
    email: "bob@acme.test",
    user_name: "bob",
    given_name: "Bob",
    family_name: "Builder",
    status: "active",
  },
  roles: [
    { tenant_id: "tenant-acme", role_name: "member", permissions: [] },
    { tenant_id: "tenant-globex", role_name: "member", permissions: [] },
  ],
  tenant_memberships: [
    { tenant_id: "tenant-acme", tenant_name: "Acme", external_org_id: "acme", status: "active" },
    { tenant_id: "tenant-globex", tenant_name: "Globex", external_org_id: "globex", status: "active" },
  ],
  linked_idps: [{ provider_name: "keycloak", external_sub: SUBJECT }],
};

/** One request the SPA sent to the backend. */
interface ApiCall {
  method: string;
  path: string;
  bearer: string | null;
  tenant: string | null;
  body: string | null;
}

/** One request the SPA sent to Keycloak's token endpoint, as its form fields. */
type TokenRequest = Record<string, string>;

let apiCalls: ApiCall[];
let tokenRequests: TokenRequest[];
let navigations: URL[];
/** Lifetime of the next access token Keycloak issues, in seconds. */
let accessTokenLifetime: number;
let issued: number;
let nonce: string | undefined;
/** The organization bob signed in through (the ID token's `organization`). */
let organization: string;

function base64url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A Keycloak ID token for bob (oidc-client-ts reads its claims; the backend checks signatures). */
function idToken(): string {
  const claims = {
    iss: ISSUER,
    sub: SUBJECT,
    aud: SPA_CLIENT_ID,
    azp: SPA_CLIENT_ID,
    typ: "ID",
    auth_time: AUTH_TIME,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + 300,
    email: "bob@acme.test",
    name: "Bob Builder",
    organization: [organization],
    ...(nonce === undefined ? {} : { nonce }),
  };
  return `${base64url({ alg: "RS256", typ: "JWT" })}.${base64url(claims)}.signature`;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function keycloak(url: URL, init?: RequestInit): Response {
  if (url.pathname.endsWith("/.well-known/openid-configuration")) {
    return json(200, {
      issuer: ISSUER,
      authorization_endpoint: AUTHORIZE_ENDPOINT,
      token_endpoint: TOKEN_ENDPOINT,
      end_session_endpoint: END_SESSION_ENDPOINT,
      jwks_uri: `${ISSUER}/protocol/openid-connect/certs`,
      code_challenge_methods_supported: ["S256"],
    });
  }
  if (url.href === TOKEN_ENDPOINT && init?.method === "POST") {
    tokenRequests.push(Object.fromEntries(new URLSearchParams(String(init.body))));
    issued += 1;
    const lifetime = accessTokenLifetime;
    accessTokenLifetime = 3600;
    return json(200, {
      access_token: `kc-access-${issued}`,
      token_type: "Bearer",
      expires_in: lifetime,
      refresh_token: `kc-refresh-${issued}`,
      id_token: idToken(),
      scope: PLAIN_SCOPE,
    });
  }
  return json(404, { error: "not_found" });
}

function backend(url: URL, init?: RequestInit): Response {
  const headers = new Headers(init?.headers);
  const authorization = headers.get("Authorization");
  apiCalls.push({
    method: init?.method ?? "GET",
    path: url.pathname,
    bearer: authorization?.replace(/^Bearer /, "") ?? null,
    tenant: headers.get("X-Tenant-ID"),
    body: typeof init?.body === "string" ? init.body : null,
  });
  if (url.pathname === "/api/identity") return json(200, bob);
  if (url.pathname === "/api/auth/logout") {
    const logout = new URL(END_SESSION_ENDPOINT);
    logout.searchParams.set("client_id", SPA_CLIENT_ID);
    logout.searchParams.set("post_logout_redirect_uri", ORIGIN);
    return json(200, { status: "logout_redirect", logout_url: logout.href });
  }
  if (url.pathname === "/api/health") return json(200, { status: "ok" });
  if (url.pathname === "/api/tenants") {
    return json(200, { tenants: bob.tenant_memberships.map((m) => ({ id: m.tenant_id, name: m.tenant_name })) });
  }
  if (/^\/api\/tenants\/[^/]+\/resources$/.test(url.pathname)) return json(200, { resources: [] });
  return json(200, {});
}

function stubNetwork() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input), ORIGIN);
    return url.origin === new URL(ISSUER).origin ? keycloak(url, init) : backend(url, init);
  }) as typeof fetch;
}

/** The browser location the app and oidc-client-ts read and navigate with. */
function setLocation(href: string) {
  const url = new URL(href);
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      href: url.href,
      origin: url.origin,
      protocol: url.protocol,
      host: url.host,
      hostname: url.hostname,
      port: url.port,
      pathname: url.pathname,
      search: url.search,
      hash: url.hash,
      assign: (to: string) => navigations.push(new URL(to, ORIGIN)),
      replace: (to: string) => navigations.push(new URL(to, ORIGIN)),
      reload: () => undefined,
    },
  });
}

function renderSpa(path: string) {
  return render(
    <AuthProvider {...buildOidcConfig(keycloakEnv, ORIGIN)}>
      <MemoryRouter initialEntries={[path]}>
        <IdentityProvider>
          <App />
        </IdentityProvider>
      </MemoryRouter>
    </AuthProvider>,
  );
}

/** Every navigation to Keycloak's authorization endpoint so far. */
function authorizeRequests(): URL[] {
  return navigations.filter((n) => `${n.origin}${n.pathname}` === AUTHORIZE_ENDPOINT);
}

function callsTo(path: string): ApiCall[] {
  return apiCalls.filter((c) => c.path === path);
}

/** Bearer token and tenant of every call to the tenant's resources since `from`. */
function resourceCalls(from: number): Set<string> {
  return new Set(
    apiCalls
      .slice(from)
      .filter((c) => c.path.startsWith("/api/tenants/") && c.path.endsWith("/resources"))
      .map((c) => `${c.bearer} ${c.tenant} ${c.path}`),
  );
}

const realLocation = window.location;

/**
 * Sign in as the SPA does: "Sign In" sends the browser to Keycloak's
 * authorization endpoint, and Keycloak's redirect back with a code loads the
 * app, which exchanges the code and loads the identity.
 */
async function signIn(): Promise<URL> {
  setLocation(`${ORIGIN}/login`);
  const login = renderSpa("/login");
  fireEvent.click(await screen.findByRole("button", { name: "Sign In" }));
  await waitFor(() => expect(authorizeRequests()).toHaveLength(1));
  const [authorize] = authorizeRequests();
  nonce = authorize.searchParams.get("nonce") ?? undefined;
  login.unmount();

  const callback = new URL(ORIGIN);
  callback.searchParams.set("code", "keycloak-code");
  callback.searchParams.set("state", authorize.searchParams.get("state") ?? "");
  callback.searchParams.set("session_state", "keycloak-session");
  callback.searchParams.set("iss", ISSUER);
  setLocation(callback.href);
  renderSpa("/");
  await waitFor(() => expect(callsTo("/api/identity")).toHaveLength(1));
  await screen.findByRole("combobox");
  return authorize;
}

async function chooseTenant(name: string) {
  fireEvent.click(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByRole("option", { name }));
}

describe("a Keycloak session", () => {
  beforeAll(() => {
    // Radix primitives jsdom lacks.
    Element.prototype.scrollIntoView = vi.fn();
    Element.prototype.hasPointerCapture = vi.fn(() => false);
    Element.prototype.releasePointerCapture = vi.fn();
  });

  beforeEach(() => {
    apiCalls = [];
    tokenRequests = [];
    navigations = [];
    accessTokenLifetime = 3600;
    issued = 0;
    nonce = undefined;
    organization = "acme";
    window.sessionStorage.clear();
    window.localStorage.clear();
    stubNetwork();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  afterAll(() => {
    Object.defineProperty(window, "location", { configurable: true, value: realLocation });
  });

  it("logs in with the plain organization scope and names the first tenant on every call", async () => {
    const authorize = await signIn();

    expect(`${authorize.origin}${authorize.pathname}`).toBe(AUTHORIZE_ENDPOINT);
    expect({
      client_id: authorize.searchParams.get("client_id"),
      redirect_uri: authorize.searchParams.get("redirect_uri"),
      response_type: authorize.searchParams.get("response_type"),
      scope: authorize.searchParams.get("scope"),
      code_challenge_method: authorize.searchParams.get("code_challenge_method"),
    }).toEqual({
      client_id: SPA_CLIENT_ID,
      redirect_uri: ORIGIN,
      response_type: "code",
      scope: PLAIN_SCOPE,
      code_challenge_method: "S256",
    });
    expect(tokenRequests).toEqual([
      expect.objectContaining({
        grant_type: "authorization_code",
        code: "keycloak-code",
        client_id: SPA_CLIENT_ID,
        redirect_uri: ORIGIN,
      }),
    ]);
    // The identity spans every tenant and names none; the page's calls name the first.
    expect(callsTo("/api/identity")).toEqual([
      { method: "GET", path: "/api/identity", bearer: "kc-access-1", tenant: null, body: null },
    ]);
    expect(screen.getByRole("combobox")).toHaveTextContent("Acme");
    await waitFor(() =>
      expect(resourceCalls(0)).toEqual(new Set(["kc-access-1 tenant-acme /api/tenants/tenant-acme/resources"])),
    );
  });

  it("opens the tenant of the organization the sign-in named, not one chosen before", async () => {
    window.localStorage.setItem(TENANT_SELECTION_KEY, "tenant-acme");
    organization = "globex";
    await signIn();

    expect(screen.getByRole("combobox")).toHaveTextContent("Globex");
    await waitFor(() =>
      expect(resourceCalls(0)).toEqual(new Set(["kc-access-1 tenant-globex /api/tenants/tenant-globex/resources"])),
    );
  });

  it("refreshes with the refresh token, keeping the identity and the active tenant", async () => {
    // Bob signed in through Globex; the first access token is renewed a second
    // after sign-in (oidc-client-ts renews 60 s before expiry).
    organization = "globex";
    accessTokenLifetime = 61;
    await signIn();
    expect(screen.getByRole("combobox")).toHaveTextContent("Globex");

    await waitFor(() => expect(tokenRequests).toHaveLength(2), { timeout: 5000 });

    expect(tokenRequests[1]).toEqual(
      expect.objectContaining({ grant_type: "refresh_token", refresh_token: "kc-refresh-1", client_id: SPA_CLIENT_ID }),
    );
    expect(tokenRequests[1].scope ?? PLAIN_SCOPE).toBe(PLAIN_SCOPE);
    expect(authorizeRequests()).toHaveLength(1);
    // A renewal changes neither the identity nor the tenant: the identity is not
    // fetched again, and the next calls carry the new token in the same tenant.
    expect(callsTo("/api/identity")).toHaveLength(1);
    expect(screen.getByRole("combobox")).toHaveTextContent("Globex");
    await waitFor(() =>
      expect(resourceCalls(0)).toEqual(
        new Set([
          "kc-access-1 tenant-globex /api/tenants/tenant-globex/resources",
          "kc-access-2 tenant-globex /api/tenants/tenant-globex/resources",
        ]),
      ),
    );
  }, 10_000);

  it("switches tenant with the X-Tenant-ID selector: no new authorize request, no new token", async () => {
    await signIn();
    const beforeSwitch = apiCalls.length;

    await chooseTenant("Globex");

    expect(screen.getByRole("combobox")).toHaveTextContent("Globex");
    await waitFor(() =>
      expect(resourceCalls(beforeSwitch)).toEqual(
        new Set(["kc-access-1 tenant-globex /api/tenants/tenant-globex/resources"]),
      ),
    );
    expect(authorizeRequests()).toHaveLength(1);
    expect(tokenRequests.map((r) => r.grant_type)).toEqual(["authorization_code"]);
    expect(callsTo("/api/identity")).toHaveLength(1);
    expect(apiCalls.slice(beforeSwitch).filter((c) => c.tenant === "tenant-acme")).toEqual([]);
    expect(window.localStorage.getItem(TENANT_SELECTION_KEY)).toBe("tenant-globex");
  });

  it("logs out through the backend and Keycloak's end-session endpoint", async () => {
    await signIn();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "BB" }));
    await act(async () => {
      await user.click(await screen.findByText("Sign out"));
    });

    await waitFor(() =>
      expect(navigations.map((n) => `${n.origin}${n.pathname}`)).toEqual([AUTHORIZE_ENDPOINT, END_SESSION_ENDPOINT]),
    );
    const [logout] = callsTo("/api/auth/logout");
    expect({ method: logout.method, bearer: logout.bearer }).toEqual({ method: "POST", bearer: "kc-access-1" });
    expect(JSON.parse(logout.body ?? "{}").id_token.split(".")).toHaveLength(3);
    // The local session is gone: oidc-client-ts keeps no user for this client.
    expect(window.sessionStorage.getItem(`oidc.user:${ISSUER}:${SPA_CLIENT_ID}`)).toBeNull();
    expect(authorizeRequests()).toHaveLength(1);
  });
});
