#!/usr/bin/env bash
# SessionStart hook: ready a Claude Code cloud session to build and check
# identity-stack. Local sessions skip it; a local machine is already set up.
#
# Never blocks the session: each step reports and moves on. Its output lands in
# the session's context, so it says what is missing rather than failing.
set -uo pipefail
[[ "${CLAUDE_CODE_REMOTE:-}" == true ]] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
root=$PWD

note() { echo "cloud-setup: $*"; }

# uv runs the backend's Python.
if ! command -v uv > /dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh 2> /dev/null | sh > /dev/null 2>&1
  export PATH="$HOME/.local/bin:$PATH"
fi
if command -v uv > /dev/null; then
  note "uv $(uv --version 2>&1)"
else
  note "uv is missing and could not be installed; check the environment's network access"
fi

# CI runs the backend on Python 3.12.
if uv python install 3.12 > /dev/null 2>&1; then
  note "python 3.12 installed"
else
  note "python 3.12 could not be installed"
fi

if (cd backend && uv sync --locked --python 3.12 > /dev/null 2>&1); then
  note "backend deps synced (backend/.venv)"
  # Put ruff and pytest on PATH for the whole session.
  if [[ -n "${CLAUDE_ENV_FILE:-}" ]]; then
    echo "export PATH=\"$root/backend/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
  fi
else
  note "uv sync failed; backend deps are missing"
fi

# CI runs the frontend on Node 22.
node_major() { node --version 2> /dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
if [[ "$(node_major)" != 22 ]]; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  [[ -s "$NVM_DIR/nvm.sh" ]] && . "$NVM_DIR/nvm.sh" > /dev/null 2>&1
  if command -v nvm > /dev/null; then
    nvm install 22 > /dev/null 2>&1 && nvm use 22 > /dev/null 2>&1
  elif command -v npm > /dev/null; then
    npm install -g n > /dev/null 2>&1 && n 22 > /dev/null 2>&1 && hash -r
  fi
fi
if [[ "$(node_major)" == 22 ]]; then
  note "node $(node --version)"
else
  note "node 22 is not available (found: $(node --version 2>&1)); frontend checks may differ from CI"
fi

if (cd frontend && npm ci > /dev/null 2>&1); then
  note "frontend deps installed"
else
  note "npm ci failed; frontend deps are missing"
fi

if docker info > /dev/null 2>&1; then
  note "docker is available: make test-unit can run"
else
  note "no Docker: unit tests that need the test stack run in CI only"
fi

# CI and the lens reviews come back through GitHub. Say which route works.
if gh auth status > /dev/null 2>&1; then
  note "gh is authenticated"
else
  note "gh is not authenticated: read PRs, checks and reviews with the GitHub connector tools"
fi
exit 0
