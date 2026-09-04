import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useRBAC } from "../useRBAC";
import type { CanonicalIdentity } from "../../contexts/IdentityContext";

// useRBAC consumes useTenants, which sources the canonical `GET /api/identity`
// payload from IdentityContext. We mock only the context so the real
// useTenants -> useRBAC chain runs — proving the public RBAC API is unchanged
// (AC-2) now that the data source is canonical, not the Descope token claim.
const mockUseIdentityContext = vi.fn();
vi.mock("../../contexts/IdentityContext", () => ({
  useIdentityContext: () => mockUseIdentityContext(),
}));

/** Canonical identity with a configurable role set on the active tenant t1. */
function identityWithRoles(
  roles: string[],
  permissions: string[] = [],
  provider = "ory",
): CanonicalIdentity {
  return {
    user: {
      id: "user-1",
      email: "user@example.test",
      user_name: "user",
      given_name: null,
      family_name: null,
      status: "active",
    },
    roles: roles.map((role_name) => ({ tenant_id: "t1", role_name, permissions })),
    tenant_memberships: [{ tenant_id: "t1", tenant_name: "Acme" }],
    linked_idps: [{ provider_name: provider, external_sub: `${provider}|1` }],
  };
}

describe("useRBAC", () => {
  beforeEach(() => {
    mockUseIdentityContext.mockReset();
  });

  it("returns empty roles when identity is null (no tenant context)", () => {
    mockUseIdentityContext.mockReturnValue({ identity: null, currentTenantId: null });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.roles).toEqual([]);
    expect(result.current.permissions).toEqual([]);
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isOwner).toBe(false);
  });

  it("identifies admin role", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["admin"], ["docs.read"]),
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.isAdmin).toBe(true);
    expect(result.current.isOwner).toBe(false);
    expect(result.current.roles).toEqual(["admin"]);
  });

  it("identifies owner role (also counts as admin)", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["owner"]),
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.isOwner).toBe(true);
    expect(result.current.isAdmin).toBe(true);
  });

  it("viewer is not admin", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["viewer"], ["docs.read"]),
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.isOwner).toBe(false);
  });

  it("hasRole returns true for matching role", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["member", "admin"]),
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.hasRole("admin")).toBe(true);
    expect(result.current.hasRole("member")).toBe(true);
    expect(result.current.hasRole("owner")).toBe(false);
  });

  it("hasPermission returns true for matching permission", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["member"], ["docs.read", "docs.write"]),
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.hasPermission("docs.read")).toBe(true);
    expect(result.current.hasPermission("billing.manage")).toBe(false);
  });

  it("returns currentTenantId", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["member"]),
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useRBAC());
    expect(result.current.currentTenantId).toBe("t1");
  });

  it("derives identical RBAC for an Ory and a Descope principal (AC-2 / NFR-8)", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["admin", "member"], ["docs.read", "docs.write"], "ory"),
      currentTenantId: "t1",
    });
    const ory = renderHook(() => useRBAC());

    mockUseIdentityContext.mockReturnValue({
      identity: identityWithRoles(["admin", "member"], ["docs.read", "docs.write"], "descope"),
      currentTenantId: "t1",
    });
    const descope = renderHook(() => useRBAC());

    expect(descope.result.current.roles).toEqual(ory.result.current.roles);
    expect(descope.result.current.permissions).toEqual(ory.result.current.permissions);
    expect(descope.result.current.isAdmin).toBe(ory.result.current.isAdmin);
    expect(descope.result.current.isOwner).toBe(ory.result.current.isOwner);
  });
});
