import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

// Mock auth user for testing
export const mockAuthUser = {
  access_token: "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEyMyIsImVtYWlsIjoidGVzdEB0ZXN0LmNvbSIsIm5hbWUiOiJUZXN0IFVzZXIiLCJkY3QiOiJ0ZW5hbnQtMSIsInRlbmFudHMiOnsidGVuYW50LTEiOnsicm9sZXMiOlsiYWRtaW4iXSwicGVybWlzc2lvbnMiOlsiZG9jdW1lbnRzLnJlYWQiLCJkb2N1bWVudHMud3JpdGUiXX19LCJpYXQiOjE3MTEwMDAwMDAsImV4cCI6OTk5OTk5OTk5OX0.fake",
  id_token: "fake-id-token",
  profile: {
    sub: "user-123",
    email: "test@test.com",
    name: "Test User",
  },
  expires_in: 3600,
  token_type: "Bearer",
  scope: "openid profile email",
  expired: false,
};

// Default mock for useAuth
export const createMockAuth = (overrides = {}) => ({
  user: mockAuthUser,
  isAuthenticated: true,
  isLoading: false,
  error: null,
  signinRedirect: vi.fn(),
  signoutRedirect: vi.fn(),
  removeUser: vi.fn(),
  events: {
    addAccessTokenExpired: vi.fn(),
    removeAccessTokenExpired: vi.fn(),
    addSilentRenewError: vi.fn(),
    removeSilentRenewError: vi.fn(),
  },
  ...overrides,
});

// Mock apiFetch that returns configurable responses
export const createMockApiFetch = (responses: Record<string, unknown> = {}) => {
  return vi.fn((url: string) => {
    const body = responses[url];
    if (body !== undefined) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    });
  });
};

// Wrapper with router for components that use react-router
export function TestWrapper({ children, initialEntries = ["/"] }: { children: ReactNode; initialEntries?: string[] }) {
  return (
    <MemoryRouter initialEntries={initialEntries}>
      {children}
    </MemoryRouter>
  );
}
