# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

identity-stack: a reference identity platform with a Python backend, a React
frontend and Terraform-managed providers. Start with `README.md`, then
`CONTRIBUTING.md` and `docs/`.

## Git

- Work on a `<type>/<short-description>` branch: in its own git worktree when
  local, in the session's own checkout in a Claude Code cloud session.
- Conventional commits. Every change lands through a squash-merged PR to
  `main`; the PR title becomes the commit.
- Never force-push. Others push to the same branches, so fetch before you push.
- James merges. Never merge your own PR.

## Rules for every artifact

- Name no client or outside project in commits, PRs, issues or docs. Say
  "another project".
- Fill in the PR template's AI provenance block: tick both boxes, give the
  harness (`Claude Code`) and the model ID. James has delegated the tick of
  "A human has reviewed this change and is accountable for it" for agent PRs.
- Commit no secrets, keys or tokens.

## What CI runs, and what you can run

Before every push, run what CI runs where you can:

- Lint: `cd backend && ruff check . && ruff format --check .`
- Frontend: `cd frontend && npm ci && npm test && npx tsc -b && npm run lint`
- Backend unit tests: `make test-unit`. It brings up `docker-compose.test.yml`,
  so it needs Docker.

Integration, E2E and profile tests need credentials or the full stack: they run
in CI only. If Docker isn't available, run the unit tests that don't need it
and say in the PR which ones ran.

## Working an issue end to end

This is how an agent, local or in a Claude Code cloud session, takes one issue
from open to a reviewed PR.

1. **Pick.** Take the next open issue labelled `cloud-ready`, in the order the
   pinned queue issue gives. Read it, its "Done when" list and any code it
   names.
2. **Plan.** Write the plan by hand in the PR body under `## Plan`.
3. **Build test-first.** Write the failing test, then the change. Run the
   section above before every push.
4. **Open the PR against `main`.** Use a conventional title and put
   `Closes #<n>`, the issue's "Done when" list and the AI provenance block in
   the body.
5. **Read the feedback.** CI, the lens reviews and the Merge Gate's verdict
   come back on the PR. Fix each MUST FIX at its cause. Lenses see only diff
   hunks, so read the whole file before fixing a "missing" line; for a false
   positive, reply on the PR saying why. Fix a SHOULD FIX when it's cheap.
6. **Stop after three review rounds** and summarise what's left in a PR
   comment.
7. **Never merge.** James merges.

Keep to one file area per session so parallel sessions don't collide. If `gh`
isn't logged in, use the GitHub connector tools instead.
