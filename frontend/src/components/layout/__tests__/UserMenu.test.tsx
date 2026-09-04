import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { UserMenu } from "../UserMenu"

const mockRemoveUser = vi.fn()
const mockNavigate = vi.fn()
const mockApiFetch = vi.fn()

let mockUser: { id_token?: string; profile?: { name?: string; email?: string } } = {
  id_token: "the-id-token",
  profile: { name: "Test User", email: "test@example.com" },
}

vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ user: mockUser, removeUser: mockRemoveUser }),
}))

vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}))

vi.mock("@/hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}))

const mockToastError = vi.fn()
vi.mock("sonner", () => ({
  toast: { error: (...args: unknown[]) => mockToastError(...args) },
}))

const assignMock = vi.fn()

// jsdom lacks these Radix-required primitives (see the shared vitest Radix note).
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
  // jsdom's window.location.assign is not directly redefinable, but the
  // window.location property itself is — swap in a copy with a mock assign.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, assign: assignMock },
  })
})

beforeEach(() => {
  mockUser = { id_token: "the-id-token", profile: { name: "Test User", email: "test@example.com" } }
})

afterEach(() => {
  vi.clearAllMocks()
})

/** Open the dropdown and click "Sign out". */
async function clickSignOut() {
  const user = userEvent.setup()
  await user.click(screen.getByRole("button"))
  await user.click(await screen.findByText("Sign out"))
}

describe("UserMenu logout", () => {
  it("redirects to the RP-initiated logout_url when the backend returns one (Ory)", async () => {
    const url = "https://inspiring-nash-yli2uiwmcw.projects.oryapis.com/oauth2/sessions/logout?id_token_hint=x"
    mockApiFetch.mockResolvedValue({ ok: true, json: async () => ({ status: "logout_redirect", logout_url: url }) })

    render(<UserMenu />)
    await clickSignOut()

    await waitFor(() => expect(assignMock).toHaveBeenCalledWith(url))
    expect(mockRemoveUser).toHaveBeenCalled()
    expect(mockNavigate).not.toHaveBeenCalled()
  })

  it("posts the id_token in the request body", async () => {
    mockApiFetch.mockResolvedValue({ ok: true, json: async () => ({ status: "logged_out" }) })

    render(<UserMenu />)
    await clickSignOut()

    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled())
    const [path, options] = mockApiFetch.mock.calls[0]
    expect(path).toBe("/api/auth/logout")
    expect(options.method).toBe("POST")
    expect(JSON.parse(options.body)).toEqual({ id_token: "the-id-token" })
  })

  it("navigates to /login when the backend returns no logout_url (Descope)", async () => {
    mockApiFetch.mockResolvedValue({ ok: true, json: async () => ({ status: "logged_out", sub: "u" }) })

    render(<UserMenu />)
    await clickSignOut()

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login"))
    expect(mockRemoveUser).toHaveBeenCalled()
    expect(assignMock).not.toHaveBeenCalled()
  })

  it("fails closed on a backend error: keeps the session, surfaces an error, does not navigate", async () => {
    // Ory fail-closed 500 (no end_session_endpoint). Reporting success and
    // navigating to /login would strand a live provider (OP) session.
    mockApiFetch.mockResolvedValue({ ok: false, status: 500, json: async () => ({ detail: "no end_session_endpoint" }) })

    render(<UserMenu />)
    await clickSignOut()

    await waitFor(() => expect(mockToastError).toHaveBeenCalledWith(expect.stringMatching(/sign out failed/i)))
    expect(mockRemoveUser).not.toHaveBeenCalled()
    expect(mockNavigate).not.toHaveBeenCalled()
    expect(assignMock).not.toHaveBeenCalled()
  })

  it("still clears the session and navigates when the logout call fails", async () => {
    mockApiFetch.mockRejectedValue(new Error("network"))

    render(<UserMenu />)
    await clickSignOut()

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login"))
    expect(mockRemoveUser).toHaveBeenCalled()
    expect(assignMock).not.toHaveBeenCalled()
  })
})
