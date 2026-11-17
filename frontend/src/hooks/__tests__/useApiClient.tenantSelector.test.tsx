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

/** Every request the hook sent, keyed by attempt, with its headers as a plain object. */
function sentHeaders(fetchMock: ReturnType<typeof vi.fn>): Record<string, string>[] {
  return fetchMock.mock.calls.map(([, init]) => ({ ...((init as RequestInit).headers as Record<string, string>) }));
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
      { "Content-Type": "application/json", "X-Tenant-ID": "tenant-acme", Authorization: "Bearer token-a" },
    ]);
  });

  it("sends no tenant header when no tenant is selected", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant(null) });
    await result.current.apiFetch("/api/identity");

    expect(sentHeaders(fetchMock)).toEqual([{ Authorization: "Bearer token-a" }]);
  });

  it("the selected tenant wins over a header the caller set", async () => {
    mockUseAuth.mockReturnValue({ user: { access_token: "token-a" }, signinSilent: vi.fn(), removeUser: vi.fn() });
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient(), { wrapper: withTenant("tenant-acme") });
    await result.current.apiFetch("/api/roles", { headers: { "X-Tenant-ID": "tenant-globex" } });

    expect(sentHeaders(fetchMock)).toEqual([{ "X-Tenant-ID": "tenant-acme", Authorization: "Bearer token-a" }]);
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
      { "X-Tenant-ID": "tenant-acme", Authorization: "Bearer stale" },
      { "X-Tenant-ID": "tenant-acme", Authorization: "Bearer fresh" },
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

    expect(sentHeaders(fetchMock)).toEqual([{ "X-Tenant-ID": "tenant-globex", Authorization: "Bearer token-a" }]);
  });
});
