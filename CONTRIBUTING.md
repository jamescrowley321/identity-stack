# Contributing to identity-stack

Thanks for your interest in contributing.

## Making changes

- Work on a feature branch with a type prefix (`feat/`, `fix/`, `docs/`, `refactor/`, `test/`, `chore/`, `ci/`).
- Follow [Conventional Commits](https://www.conventionalcommits.org/).
- Keep pull requests focused, explain what and why, and update docs and tests alongside your change.

## AI-Assisted Contributions

Contributions that use AI tools (GitHub Copilot, Claude Code, ChatGPT, Cursor, the Ralph loop, `pi`, etc.) are welcome. We apply the same quality standards to all contributions regardless of how they were authored.

### Requirements for AI-assisted PRs

- **All CI checks must pass** — lint, tests, security scans. No exceptions.
- **Audit disclosure is required.** Every AI-assisted PR must record, in the PR description's **AI provenance** block, the **harness/agent(s)** and the **model(s)** used to produce the change (for example: harness `Claude Code`, model `claude-opus-4-8`; or harness `ralph-orchestrator + pi`, model `z-ai/glm-5.2`).
- **A human is accountable.** A named human must review the change and attest to it. The submitter is responsible for the correctness, security, and quality of the code regardless of whether it was AI-generated.
- **Advisory-only AI.** AI output — including automated review — is advisory until a human attests. A green check is not sign-off.

### Requesting a review from Claude

Comment `@claude review this PR` on a pull request and Claude runs the
[blind-peer-review](https://github.com/jamescrowley321/blind-peer-review) lenses
against the diff — one fresh reviewer per lens (Cold Read, Edge Cases, Acceptance
Criteria, Security Review, Red Team), adjudicated fail-closed into a PASS/BLOCK
verdict. It is the same lens library the maintainers run locally, so CI and the
laptop do not drift apart.

It runs **only when asked**. There is no automatic review on every pull request;
#445 removed that deliberately, because it commented on every push whether or not
anything was found and billed a paid provider each time.

**Only the repository owner can trigger it.** The workflow runs with repository
secrets on a public repo, so the job's `if:` condition requires the commenter's
`author_association` to be `OWNER`. GitHub evaluates that before scheduling
anything, so a mention from anyone else never starts a job at all — it is not
merely rejected after the fact. (`claude-code-action` also requires write access
and refuses bots, but that check runs inside the action, once the job is already
running.)

This means contributors cannot request a review themselves; ask a maintainer to
run it on your PR.

**The verdict is advice, not sign-off.** Two limitations, stated plainly because a
clean PASS is exactly when they matter most:

- The lenses run as subagents of one session. Each starts from a clean context,
  but they share a process, a model and a host — weaker than the upstream CI
  adapter, which runs one job per lens. Upstream measured that batching lenses
  into a single agent's context turned a MUST FIX finding into a clean PASS.
- Claude reviews code that Claude often helped write, so the reviewer shares the
  author's blind spots. Every run prints this caveat; leave it in the posted
  findings rather than trimming it.

Both are why a lens verdict is not a required check here, and why the
**Advisory-only AI** rule above still governs: a named human attests, not a bot.

### What we look for

- No hallucinated APIs, invented SDK methods, or fabricated citations.
- Tests actually run and cover the new functionality.
- Documentation is accurate and complete.

## License

By contributing, you agree that your contributions are licensed under this repository's LICENSE.
