# AGENTS.md

Guidance for coding agents (and people) working in this repository.

## What this is

identity-stack: a reference identity platform with a Python backend, a React
frontend and Terraform-managed providers. Start with `README.md`, then
`CONTRIBUTING.md` and `docs/`.

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

## Git

- Work on a `<type>/<short-description>` branch: in its own git worktree when
  local, in the session's own checkout in a Claude Code cloud session.
- Conventional commits. Every change lands through a PR to `main`, merged by
  rebase.
- Don't force-push a branch someone else is pushing to; fetch before you push.
- James merges. Never merge your own PR.

## Rules for every artifact

- Name no client or outside project in commits, PRs, issues or docs. Say
  "another project".
- Fill in the PR template's AI provenance block: tick both boxes, give the
  harness (`Claude Code`) and the model ID. James has delegated the tick of
  "A human has reviewed this change and is accountable for it" for agent PRs.
- Commit no secrets, keys or tokens.

## Before you push

Run what CI runs where you can:

- `make lint`: Ruff, including the house-rule bans (TID251) and lazy logging (G004/G010).
- `make test-unit`: includes `tests/unit/test_house_rules.py` and
  `tests/unit/test_router_authorization.py`, which enforce the rules Ruff cannot.
  It brings up `docker-compose.test.yml`, so it needs Docker.
- Frontend: `cd frontend && npm ci && npm test && npx tsc -b && npm run lint`.
- `make test-integration` when Docker is available.

E2E and profile tests need credentials or the full stack: they run in CI only.
If Docker isn't available, run the checks that don't need it and say in the PR
which ones ran.

## Working an issue end to end

This is how an agent, local or in a Claude Code cloud session, takes one issue
from open to a reviewed PR.

1. **Pick.** Take the issue James names, or else the next open issue labelled
   `cloud-ready`, in the order the pinned queue issue gives. Read it, its
   "Done when" list and any code it names.
2. **Plan.** Write the plan by hand in the PR body under `## Plan`.
3. **Build test-first.** Write the failing test, then the change. Run the
   section above before every push.
4. **Open the PR against `main`.** Use a conventional title and put
   `Closes #<n>`, the issue's "Done when" list and the AI provenance block in
   the body.
5. **Read the feedback.** CI and the blind peer review (`claude.yml`) come
   back on the PR. Fix each MUST FIX at its cause. Lenses see only diff hunks,
   so read the whole file before fixing a "missing" line; for a false
   positive, reply on the PR saying why. Fix a SHOULD FIX when it's cheap.
   After pushing fixes, comment `@claude review this PR` for a fresh review.
6. **Stop after three review rounds** and summarise what's left in a PR
   comment.
7. **Never merge.** James merges.

Keep to one file area per session so parallel sessions don't collide. If `gh`
isn't logged in, use the GitHub connector tools instead.

## Everything else

- Contribution process, commit style and AI-provenance requirements: [`CONTRIBUTING.md`](CONTRIBUTING.md).
- How the system fits together: [`docs/architecture.md`](docs/architecture.md).
- Every environment variable and its default: [`docs/environment-variables.md`](docs/environment-variables.md).
