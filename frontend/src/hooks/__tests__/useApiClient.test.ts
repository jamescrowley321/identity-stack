import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useApiClient, apiUrl, API_BASE_URL } from "../useApiClient";

const mockUseAuth = vi.fn();
const mockNavigate = vi.fn();

vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

describe("API_BASE_URL constant", () => {
  it("is exported as a string", () => {
    // The actual value depends on VITE_API_BASE_URL at module load time.
    // In the vitest environment that env var is not set, so the default
    // (empty string) applies.
    expect(typeof API_BASE_URL).toBe("string");
  });

  it("defaults to empty string in standalone mode (no env var set)", () => {
    expect(API_BASE_URL).toBe("");
  });
});

describe("apiUrl helper", () => {
  it("returns relative path when API_BASE_URL is empty (standalone)", () => {
    // Verifies the standalone contract: callers see /api/foo, browser
    // resolves it against the current origin, nginx proxies it.
    expect(apiUrl("/api/me")).toBe("/api/me");
    expect(apiUrl("/api/health")).toBe("/api/health");
  });

  it("never produces double-slash even when called with leading slash", () => {
    // Regression: a previous draft of this PR concatenated a hardcoded
    // http://localhost:8000 default in compose with a leading-slash
    // path, producing http://localhost:8000//api/foo. Empty default +
    // single concatenation prevents that even when API_BASE_URL is non-empty.
    const url = apiUrl("/api/something");
    expect(url).not.toContain("//api");
  });
});

describe("Gateway-mode URL construction", () => {
  // The agent flagged that the original PR's tests didn't actually
  // exercise the gateway path. These tests use vi.stubEnv to set
  // VITE_API_BASE_URL and re-import the module so the API_BASE_URL
  // constant is recomputed under the new env, then verify that the
  // url builder produces the expected absolute URL.

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("prepends absolute base URL when VITE_API_BASE_URL=http://localhost:8080", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8080");
    const mod = await import("../useApiClient");
    expect(mod.API_BASE_URL).toBe("http://localhost:8080");
    expect(mod.apiUrl("/api/me")).toBe("http://localhost:8080/api/me");
    expect(mod.apiUrl("/api/health")).toBe("http://localhost:8080/api/health");
  });

  it("strips trailing slash from VITE_API_BASE_URL", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8080/");
    const mod = await import("../useApiClient");
    expect(mod.API_BASE_URL).toBe("http://localhost:8080");
    expect(mod.apiUrl("/api/me")).toBe("http://localhost:8080/api/me");
  });

  it("strips multiple trailing slashes", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8080///");
    const mod = await import("../useApiClient");
    expect(mod.API_BASE_URL).toBe("http://localhost:8080");
  });

  it("treats empty string env var the same as unset", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    const mod = await import("../useApiClient");
    expect(mod.API_BASE_URL).toBe("");
    expect(mod.apiUrl("/api/me")).toBe("/api/me");
  });
});

describe("useApiClient hook", () => {
  beforeEach(() => {
    mockUseAuth.mockReset();
    mockNavigate.mockReset();
    globalThis.fetch = vi.fn();
  });

  it("redirects to /login and rejects when no access token", async () => {
    mockUseAuth.mockReturnValue({ user: null });
    const { result } = renderHook(() => useApiClient());
    await expect(result.current.apiFetch("/api/me")).rejects.toThrow("No access token");
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login"));
  });

  it("calls fetch with apiUrl(path) and Bearer header on success", async () => {
    mockUseAuth.mockReturnValue({
      user: { access_token: "valid-token" },
      signinSilent: vi.fn(),
      removeUser: vi.fn(),
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValue(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient());
    await result.current.apiFetch("/api/me");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    // In the vitest env API_BASE_URL is empty, so url === path.
    expect(url).toBe("/api/me");
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer valid-token");
  });

  it("retries with new token after silent renewal on 401", async () => {
    const signinSilent = vi.fn().mockResolvedValue({ access_token: "fresh-token" });
    mockUseAuth.mockReturnValue({
      user: { access_token: "stale-token" },
      signinSilent,
      removeUser: vi.fn(),
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock
      .mockResolvedValueOnce(new Response("unauthorized", { status: 401 }))
      .mockResolvedValueOnce(new Response("ok", { status: 200 }));

    const { result } = renderHook(() => useApiClient());
    const response = await result.current.apiFetch("/api/me");

    expect(signinSilent).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(response.status).toBe(200);
    const secondCallInit = fetchMock.mock.calls[1][1] as RequestInit;
    const secondHeaders = secondCallInit.headers as Record<string, string>;
    expect(secondHeaders.Authorization).toBe("Bearer fresh-token");
  });

  it("clears session and navigates to /login when silent renewal fails", async () => {
    const removeUser = vi.fn();
    mockUseAuth.mockReturnValue({
      user: { access_token: "stale-token" },
      signinSilent: vi.fn().mockRejectedValue(new Error("renewal failed")),
      removeUser,
    });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValue(new Response("unauthorized", { status: 401 }));

    const { result } = renderHook(() => useApiClient());
    await result.current.apiFetch("/api/me");

    expect(removeUser).toHaveBeenCalled();
    expect(mockNavigate).toHaveBeenCalledWith("/login", { state: { sessionExpired: true } });
  });
});
