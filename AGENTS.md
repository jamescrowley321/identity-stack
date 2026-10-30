# AGENTS.md

Guidance for coding agents (and people) working in this repository.

## Conventions live in one place

The backend's house rules are in **[`docs/conventions.md`](docs/conventions.md)**. Each rule
is numbered, uses MUST/SHOULD/MAY, and names its exceptions. Read the whole file before
you change anything under `backend/app/`, and cite rules by section number ("conventions
§15") in commits and reviews. This file does not restate them. If the two ever disagree,
`docs/conventions.md` wins.

The decisions behind the larger rules are in [`docs/adr/`](docs/adr/README.md). The helper
modules that enforce the rules (`backend/app/knobs.py`, `backend/app/outbound.py`,
`backend/app/logbook.py`) carry further rules in their docstrings. Read a helper's
docstring before you call it in a new way.

## Before you push

- `make lint`: Ruff, including the house-rule bans (TID251) and lazy logging (G004/G010).
- `make test-unit`: includes `tests/unit/test_house_rules.py` and
  `tests/unit/test_router_authorization.py`, which enforce the rules Ruff cannot.
- `make test-integration` when Docker is available.

## Everything else

- Contribution process, commit style and AI-provenance requirements: [`CONTRIBUTING.md`](CONTRIBUTING.md).
- How the system fits together: [`docs/architecture.md`](docs/architecture.md).
- Every environment variable and its default: [`docs/environment-variables.md`](docs/environment-variables.md).
