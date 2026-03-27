import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useRBAC } from "../useRBAC";

// Helper to create a JWT with given claims
function makeJwt(claims: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const payload = btoa(JSON.stringify(claims));
  return `${header}.${payload}.fake`;
}

const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

describe("useRBAC", () => {
  it("returns empty roles when no tenant context", () => {
    mockUseAuth.mockReturnValue({ user: null });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.roles).toEqual([]);
    expect(result.current.permissions).toEqual([]);
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isOwner).toBe(false);
  });

  it("identifies admin role", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: { t1: { roles: ["admin"], permissions: ["docs.read"] } },
        }),
      },
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.isAdmin).toBe(true);
    expect(result.current.isOwner).toBe(false);
    expect(result.current.roles).toEqual(["admin"]);
  });

  it("identifies owner role (also counts as admin)", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: { t1: { roles: ["owner"], permissions: [] } },
        }),
      },
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.isOwner).toBe(true);
    expect(result.current.isAdmin).toBe(true);
  });

  it("viewer is not admin", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: { t1: { roles: ["viewer"], permissions: ["docs.read"] } },
        }),
      },
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isOwner).toBe(false);
  });

  it("hasRole returns true for matching role", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: { t1: { roles: ["member", "admin"], permissions: [] } },
        }),
      },
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.hasRole("admin")).toBe(true);
    expect(result.current.hasRole("member")).toBe(true);
    expect(result.current.hasRole("owner")).toBe(false);
  });

  it("hasPermission returns true for matching permission", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "t1",
          tenants: { t1: { roles: [], permissions: ["docs.read", "docs.write"] } },
        }),
      },
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.hasPermission("docs.read")).toBe(true);
    expect(result.current.hasPermission("billing.manage")).toBe(false);
  });

  it("returns currentTenantId", () => {
    mockUseAuth.mockReturnValue({
      user: {
        access_token: makeJwt({
          dct: "my-tenant",
          tenants: { "my-tenant": { roles: ["member"], permissions: [] } },
        }),
      },
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.currentTenantId).toBe("my-tenant");
  });
});
