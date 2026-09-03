import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useTenants } from "../useTenants";
import type { CanonicalIdentity } from "../../contexts/IdentityContext";

// useTenants sources everything from the canonical `GET /api/identity` payload
// via IdentityContext — no more Descope `dct`/`tenants` JWT decoding. We mock
// the context so these tests exercise the pure canonical -> TenantInfo mapping.
const mockUseIdentityContext = vi.fn();
vi.mock("../../contexts/IdentityContext", () => ({
  useIdentityContext: () => mockUseIdentityContext(),
}));

/**
 * Canonical identity for an Ory-authenticated principal. The backend
 * normalizes every provider to this identical shape, so tenant/role rendering
 * must not depend on which IdP issued the session (NFR-8 / AC-3).
 *
 * t1 has two role assignments (admin + member) to exercise per-tenant grouping
 * and permission de-duplication; t2 has a single role.
 */
const oryIdentity: CanonicalIdentity = {
  user: {
    id: "ory-user-1",
    email: "alice@ory.example",
    user_name: "alice",
    given_name: "Alice",
    family_name: "Ory",
    status: "active",
  },
  roles: [
    { tenant_id: "t1", role_name: "admin", permissions: ["docs.read", "docs.write"] },
    { tenant_id: "t1", role_name: "member", permissions: ["docs.read"] },
    { tenant_id: "t2", role_name: "viewer", permissions: ["docs.read"] },
  ],
  tenant_memberships: [
    { tenant_id: "t1", tenant_name: "Acme" },
    { tenant_id: "t2", tenant_name: "Beta" },
  ],
  linked_idps: [{ provider_name: "ory", external_sub: "ory|abc123" }],
};

/**
 * Canonical identity for a Descope-authenticated principal carrying the SAME
 * tenant/role data. Proves the mapping is provider-neutral: identical canonical
 * input -> identical TenantInfo output regardless of `linked_idps` provider.
 */
const descopeIdentity: CanonicalIdentity = {
  user: {
    id: "descope-user-1",
    email: "bob@descope.example",
    user_name: "bob",
    given_name: "Bob",
    family_name: "Descope",
    status: "active",
  },
  roles: [
    { tenant_id: "t1", role_name: "admin", permissions: ["docs.read", "docs.write"] },
    { tenant_id: "t1", role_name: "member", permissions: ["docs.read"] },
    { tenant_id: "t2", role_name: "viewer", permissions: ["docs.read"] },
  ],
  tenant_memberships: [
    { tenant_id: "t1", tenant_name: "Acme" },
    { tenant_id: "t2", tenant_name: "Beta" },
  ],
  linked_idps: [{ provider_name: "descope", external_sub: "descope|xyz789" }],
};

describe("useTenants", () => {
  beforeEach(() => {
    mockUseIdentityContext.mockReset();
  });

  it("returns null tenant and empty tenants when identity is null (unauth/loading/error)", () => {
    mockUseIdentityContext.mockReturnValue({ identity: null, currentTenantId: null });
    const { result } = renderHook(() => useTenants());
    expect(result.current.currentTenantId).toBeNull();
    expect(result.current.tenants).toEqual([]);
  });

  it("maps canonical memberships to TenantInfo, grouping roles by tenant", () => {
    mockUseIdentityContext.mockReturnValue({ identity: oryIdentity, currentTenantId: "t1" });
    const { result } = renderHook(() => useTenants());

    expect(result.current.tenants).toHaveLength(2);
    expect(result.current.tenants[0]).toEqual({
      id: "t1",
      name: "Acme",
      roles: ["admin", "member"],
      permissions: ["docs.read", "docs.write"], // union across both role assignments, de-duped
    });
    expect(result.current.tenants[1]).toEqual({
      id: "t2",
      name: "Beta",
      roles: ["viewer"],
      permissions: ["docs.read"],
    });
  });

  it("passes currentTenantId through from context", () => {
    mockUseIdentityContext.mockReturnValue({ identity: oryIdentity, currentTenantId: "t2" });
    const { result } = renderHook(() => useTenants());
    expect(result.current.currentTenantId).toBe("t2");
  });

  it("renders identically for an Ory and a Descope principal (AC-3 / AC-4, NFR-8)", () => {
    mockUseIdentityContext.mockReturnValue({ identity: oryIdentity, currentTenantId: "t1" });
    const ory = renderHook(() => useTenants());
    const oryTenants = ory.result.current.tenants;

    mockUseIdentityContext.mockReturnValue({ identity: descopeIdentity, currentTenantId: "t1" });
    const descope = renderHook(() => useTenants());

    expect(descope.result.current.tenants).toEqual(oryTenants);
  });

  it("falls back to tenant_id when tenant_name is empty", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: {
        ...oryIdentity,
        tenant_memberships: [{ tenant_id: "t1", tenant_name: "" }],
        roles: [{ tenant_id: "t1", role_name: "admin", permissions: [] }],
      },
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.tenants[0].name).toBe("t1");
  });

  it("returns empty roles/permissions for a membership with no role assignments", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: {
        ...oryIdentity,
        tenant_memberships: [{ tenant_id: "t3", tenant_name: "Gamma" }],
        roles: [],
      },
      currentTenantId: "t3",
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.tenants[0]).toEqual({
      id: "t3",
      name: "Gamma",
      roles: [],
      permissions: [],
    });
  });

  it("returns empty tenants for a principal with zero memberships", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: { ...oryIdentity, tenant_memberships: [], roles: [] },
      currentTenantId: null,
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.tenants).toEqual([]);
    expect(result.current.currentTenantId).toBeNull();
  });

  it("fails closed (no crash) on a malformed 200 payload missing lists", () => {
    // A gateway/cache/API-skew 200 body can omit tenant_memberships/roles or a
    // role's permissions. Without guards these would throw undefined.map in
    // render and — with no ErrorBoundary — blank the whole app. Assert the hook
    // degrades to empty rather than throwing.
    mockUseIdentityContext.mockReturnValue({
      identity: { user: oryIdentity.user, linked_idps: [] } as unknown as CanonicalIdentity,
      currentTenantId: null,
    });
    let missingLists: ReturnType<typeof useTenants> | undefined;
    expect(() => {
      missingLists = renderHook(() => useTenants()).result.current;
    }).not.toThrow();
    expect(missingLists?.tenants).toEqual([]);

    mockUseIdentityContext.mockReturnValue({
      identity: {
        ...oryIdentity,
        tenant_memberships: [{ tenant_id: "t1", tenant_name: "Acme" }],
        roles: [{ tenant_id: "t1", role_name: "admin" }],
      } as unknown as CanonicalIdentity,
      currentTenantId: "t1",
    });
    let missingPerms: ReturnType<typeof useTenants> | undefined;
    expect(() => {
      missingPerms = renderHook(() => useTenants()).result.current;
    }).not.toThrow();
    expect(missingPerms?.tenants[0]).toEqual({
      id: "t1",
      name: "Acme",
      roles: ["admin"],
      permissions: [],
    });
  });

  it("ignores orphan role entries whose tenant has no membership row", () => {
    mockUseIdentityContext.mockReturnValue({
      identity: {
        ...oryIdentity,
        tenant_memberships: [{ tenant_id: "t1", tenant_name: "Acme" }],
        roles: [
          { tenant_id: "t1", role_name: "admin", permissions: ["docs.read"] },
          { tenant_id: "orphan", role_name: "owner", permissions: ["all"] },
        ],
      },
      currentTenantId: "t1",
    });
    const { result } = renderHook(() => useTenants());
    expect(result.current.tenants).toHaveLength(1);
    expect(result.current.tenants[0].id).toBe("t1");
    expect(result.current.tenants[0].roles).toEqual(["admin"]);
  });
});
