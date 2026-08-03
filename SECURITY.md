# Security Policy

`identity-stack` is an identity platform — a FastAPI backend and Vite/React
frontend that authenticate users and issue/validate security tokens against
Descope. A vulnerability here can compromise the authentication and
authorization decisions of a deployed instance, so reports are taken seriously
and triaged promptly.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **[Private Vulnerability Reporting](https://github.com/jamescrowley321/identity-stack/security/advisories/new)**
(repository **Security** tab → **Report a vulnerability**). This opens a private
advisory visible only to you and the maintainers, where a fix and CVE can be
coordinated.

When you report, please include as much of the following as you can:

- The affected component (`backend/`, `frontend/`, or `infra/`) and the version
  / commit you tested.
- A description of the issue and its security impact (e.g. authentication
  bypass, token/session handling flaw, audience/issuer confusion, privilege
  escalation, injection, secret exposure).
- A minimal reproduction — ideally a failing test, or the request/token/config
  that demonstrates the problem.

## What to expect

- **Acknowledgement** within 3 business days.
- An initial assessment (severity, affected components, likely fix approach)
  within 10 business days.
- Coordinated disclosure: a fix is prepared privately, released, and only then
  is the advisory published — with credit to the reporter unless you prefer to
  remain anonymous.

## Supported versions

This project is evolving and does not yet publish versioned releases. Security
fixes are made against `main`; there is no back-porting to older commits at this
time. Run the latest `main` to stay current on fixes.

## Scope

In scope: the application code that ships and runs — the FastAPI backend
(`backend/`), the React frontend (`frontend/`), and the deployment/infra
definitions (`infra/`) that configure how the platform is exposed.

Out of scope: the test suites (`**/tests/**`, E2E harnesses), local development
fixtures and sample `.env` files, and issues that require a pre-compromised host
or a misconfiguration outside the code in this repository.
