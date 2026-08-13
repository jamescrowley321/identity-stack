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

### What we look for

- No hallucinated APIs, invented SDK methods, or fabricated citations.
- Tests actually run and cover the new functionality.
- Documentation is accurate and complete.

## License

By contributing, you agree that your contributions are licensed under this repository's LICENSE.
