import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useTenants } from "../useTenants";

// Mock react-oidc-context
const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

// Helper to create a JWT with given claims
function makeJwt(claims: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const payload = btoa(JSON.stringify(claims));
  return `${header}.${payload}.fake-signature`;
}

describe("useTenants", () => {
  it("returns null tenant and empty tenants when no access token", () => {
    mockUseAuth.mockReturnValue({ user: null });
    const { result } = renderHook(() => useTenants());
    expect(result.current.currentTenantId).toBeNull();
    expect(result.current.tenants).toEqual([]);
  });

  it("extracts dct claim as currentTenantId", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({ dct: "tenant-abc", tenants: { "tenant-abc": { roles: ["admin"] } } }),
      },
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.currentTenantId).toBe("tenant-abc");
  });

  it("extracts tenant list with roles and permissions", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: {
            t1: { roles: ["admin", "member"], permissions: ["docs.read", "docs.write"] },
            t2: { roles: ["viewer"], permissions: ["docs.read"] },
          },
        }),
      },
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.tenants).toHaveLength(2);
    expect(result.current.tenants[0]).toEqual({
      id: "t1",
      roles: ["admin", "member"],
      permissions: ["docs.read", "docs.write"],
    });
    expect(result.current.tenants[1]).toEqual({
      id: "t2",
      roles: ["viewer"],
      permissions: ["docs.read"],
    });
  });

  it("handles missing tenants claim gracefully", () => {
    mockUseAuth.mockReturnValue({
      user: { access_token: makeJwt({ sub: "user-1" }) },
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.currentTenantId).toBeNull();
    expect(result.current.tenants).toEqual([]);
  });

  it("handles malformed JWT gracefully", () => {
    mockUseAuth.mockReturnValue({
      user: { access_token: "not-a-jwt" },
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.currentTenantId).toBeNull();
    expect(result.current.tenants).toEqual([]);
  });

  it("handles missing roles/permissions in tenant info", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: { t1: {} },
        }),
      },
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.tenants[0].roles).toEqual([]);
    expect(result.current.tenants[0].permissions).toEqual([]);
  });
});
