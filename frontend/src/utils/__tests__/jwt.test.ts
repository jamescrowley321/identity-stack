import { describe, it, expect } from "vitest";
import { jwtDecode } from "../jwt";

function makeJwt(claims: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const payload = btoa(JSON.stringify(claims));
  return `${header}.${payload}.fake-signature`;
}

describe("jwtDecode", () => {
  it("decodes a valid JWT payload", () => {
    const token = makeJwt({ sub: "user-1", email: "a@b.com" });
    const result = jwtDecode(token);
    expect(result).toEqual({ sub: "user-1", email: "a@b.com" });
  });

  it("decodes nested objects", () => {
    const token = makeJwt({ tenants: { t1: { roles: ["admin"] } } });
    const result = jwtDecode<{ tenants: Record<string, { roles: string[] }> }>(token);
    expect(result.tenants.t1.roles).toEqual(["admin"]);
  });

  it("throws on invalid JWT format (no dots)", () => {
    expect(() => jwtDecode("not-a-jwt")).toThrow("Invalid JWT format");
  });

  it("throws on JWT with wrong number of parts", () => {
    expect(() => jwtDecode("a.b")).toThrow("Invalid JWT format");
    expect(() => jwtDecode("a.b.c.d")).toThrow("Invalid JWT format");
  });

  it("handles URL-safe base64 characters", () => {
    // Create a payload with characters that differ between standard and URL-safe base64
    const claims = { data: "test+value/with=padding" };
    const token = makeJwt(claims);
    const result = jwtDecode<typeof claims>(token);
    expect(result.data).toBe("test+value/with=padding");
  });
});
