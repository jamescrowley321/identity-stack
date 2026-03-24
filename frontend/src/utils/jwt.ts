/**
 * Decode a JWT payload without verifying the signature.
 * Only use client-side for reading claims — never for authorization decisions.
 */
export function jwtDecode<T = Record<string, unknown>>(token: string): T {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Invalid JWT format");
  const payload = parts[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(atob(payload));
}
