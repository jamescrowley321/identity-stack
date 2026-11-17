import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { useApiClient } from "../useApiClient";
import { TENANT_SELECTOR_HEADER, TenantSelectorContext } from "../../contexts/TenantSelector";

const mockUseAuth = vi.fn();
const mockNavigate = vi.fn();

vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

/**
 * Every request the hook sent, in order, with its headers as a plain object.
 * Header names are case-insensitive, so they are compared lower-cased.
 */
function sentHeaders(fetchMock: ReturnType<typeof vi.fn>): Record<string, string>[] {
  return fetchMock.mock.calls.map(([, init]) => Object.fromEntries(new Headers((init as RequestInit).headers)));
}

function withTenant(tenantId: string | null) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <TenantSelectorContext.Provider value={tenantId}>{children}</TenantSelectorContext.Provider>;
  };
}

describe("useApiClient tenant selector", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockUseAuth.mockReset();
    mockNavigate.mockReset();
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("names the selected tenant in the X-Tenant-ID header", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant("tenant-acme") });
    await result.current.apiFetch("/api/roles", { headers: { "Content-Type": "application/json" } });

    expect(TENANT_SELECTOR_HEADER).toBe("X-Tenant-ID");
    expect(sentHeaders(fetchMock)).toEqual([
      { "content-type": "application/json", "x-tenant-id": "tenant-acme", authorization: "Bearer token-a" },
    ]);
  });

  it("sends no tenant header when no tenant is selected", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant(null) });
    await result.current.apiFetch("/api/identity");

    expect(sentHeaders(fetchMock)).toEqual([{ authorization: "Bearer token-a" }]);
  });

  it("sends no tenant a caller names when no tenant is selected", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant(null) });
    await result.current.apiFetch("/api/roles", { headers: { "x-tenant-id": "tenant-globex" } });

    expect(sentHeaders(fetchMock)).toEqual([{ authorization: "Bearer token-a" }]);
  });

  it.each([
    ["the same case", { "X-Tenant-ID": "tenant-globex" }],
    ["lower case", { "x-tenant-id": "tenant-globex" }],
    ["a Headers instance", new Headers({ "x-tenant-id": "tenant-globex" })],
    ["an entry list", [["x-tenant-id", "tenant-globex"]] as [string, string][]],
  ])("the selected tenant wins over a header the caller set in %s", async (_form, headers: HeadersInit) => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant("tenant-acme") });
    await result.current.apiFetch("/api/roles", { headers });

    expect(sentHeaders(fetchMock)).toEqual([{ "x-tenant-id": "tenant-acme", authorization: "Bearer token-a" }]);
  });

  it("keeps the caller's other headers when they come as a Headers instance", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant("tenant-acme") });
    await result.current.apiFetch("/api/roles", { headers: new Headers({ "Content-Type": "application/json" }) });

    expect(sentHeaders(fetchMock)).toEqual([
      { "content-type": "application/json", "x-tenant-id": "tenant-acme", authorization: "Bearer token-a" },
    ]);
  });

  it("keeps the tenant header on the retry after a silent renewal", async () => {
    mockUseAuth.mockReturnValue({
      user: { access_token: "stale" },
      signinSilent: vi.fn().mockResolvedValue({ access_token: "fresh" }),
      removeUser: vi.fn(),
    });
    fetchMock
      .mockResolvedValueOnce(new Response("unauthorized", { status: 401 }))
      .mockResolvedValueOnce(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant("tenant-acme") });
    await result.current.apiFetch("/api/roles");

    expect(sentHeaders(fetchMock)).toEqual([
      { "x-tenant-id": "tenant-acme", authorization: "Bearer stale" },
      { "x-tenant-id": "tenant-acme", authorization: "Bearer fresh" },
    ]);
  });

  it("gives a new apiFetch when the tenant changes, so pages keyed on it reload", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockImplementation(() => Promise.resolve(new Response("ok", { status: 200 })));
    let tenant: string | null = "tenant-acme";
    function Wrapper({ children }: { children: ReactNode }) {
      return <TenantSelectorContext.Provider value={tenant}>{children}</TenantSelectorContext.Provider>;
    }

    const { result, rerender } = renderHook(() => useApiClient(), { wrapper: Wrapper });
    const before = result.current.apiFetch;
    rerender();
    expect(result.current.apiFetch).toBe(before);

    tenant = "tenant-globex";
    rerender();
    expect(result.current.apiFetch).not.toBe(before);
    await result.current.apiFetch("/api/roles");

    expect(sentHeaders(fetchMock)).toEqual([{ "x-tenant-id": "tenant-globex", authorization: "Bearer token-a" }]);
  });
});
