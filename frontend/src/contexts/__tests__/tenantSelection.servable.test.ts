import { describe, expect, it } from "vitest";
import { initialTenantId, servableMemberships } from "../tenantSelection";

const memberships = [
  { tenant_id: "tenant-suspended", external_org_id: "acme", status: "suspended" },
  { tenant_id: "tenant-active", external_org_id: "globex", status: "active" },
];

describe("servableMemberships (#689)", () => {
  it("leaves out a tenant that is not active", () => {
    expect(servableMemberships(memberships).map((m) => m.tenant_id)).toEqual(["tenant-active"]);
  });

  it("never starts the selector on a suspended tenant, selected or named by the sign-in", () => {
    expect(initialTenantId(servableMemberships(memberships), "tenant-suspended", "acme")).toBe("tenant-active");
  });
});
