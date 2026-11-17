import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useTenants } from "../useTenants";
import type { CanonicalIdentity } from "../../contexts/IdentityContext";

const mockUseIdentityContext = vi.fn();
vi.mock("../../contexts/IdentityContext", () => ({
  useIdentityContext: () => mockUseIdentityContext(),
}));

const identity: CanonicalIdentity = {
  user: {
    id: "user-1",
    email: "carol@example.test",
    user_name: "carol",
    given_name: null,
    family_name: null,
    status: "active",
  },
  roles: [],
  tenant_memberships: [
    { tenant_id: "acme", tenant_name: "Acme" },
    { tenant_id: "globex", tenant_name: "Globex" },
  ],
  linked_idps: [],
  token_tenant: { tenant_id: "globex", permissions: ["documents.read", "documents.read", "members.read"] },
};

describe("useTenants with a tenant that comes from the token", () => {
  it("gives the token's tenant the token's permissions and every other membership none", () => {
    mockUseIdentityContext.mockReturnValue({ identity, currentTenantId: "globex" });

    const { result } = renderHook(() => useTenants());
    const byId = Object.fromEntries(result.current.tenants.map((t) => [t.id, t]));

    expect(result.current.currentTenantId).toBe("globex");
    expect(byId).toEqual({
      acme: { id: "acme", name: "Acme", roles: [], permissions: [] },
      globex: {
        id: "globex",
        name: "Globex",
        roles: [],
        permissions: ["documents.read", "members.read"],
      },
    });
  });
});
