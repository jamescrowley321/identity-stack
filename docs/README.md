# identity-stack documentation

Deeper reference for the identity-stack platform. Start with the [project README](../README.md) for the overview and quickstart; use these docs when you need the detail behind it.

| Doc | What it covers |
|-----|----------------|
| [architecture.md](architecture.md) | Canonical-identity-store model, components, deployment modes, the Descope claim/issuer model, and the two sync flows |
| [local-development.md](local-development.md) | The `make` workflow, Compose profiles, and the isolated test stack |
| [deployment.md](deployment.md) | Standalone vs. gateway deployment, the Terraform `infra/` apply flow, and the required-secrets matrix |
| [environment-variables.md](environment-variables.md) | Every environment variable the backend and frontend read, with defaults |
| [security.md](security.md) | Token validation, RBAC/FGA authorization, security headers, rate limiting, and the CI scanning stack |

The API gateway component has its own reference: [`tyk/README.md`](../tyk/README.md).
