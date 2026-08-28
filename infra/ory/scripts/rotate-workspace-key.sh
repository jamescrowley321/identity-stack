#!/usr/bin/env bash
#
# rotate-workspace-key.sh — rotate the Ory Network *workspace* API key used for
# Terraform provider auth (GitHub secret ORY_WORKSPACE_API_KEY on this repo).
# Closes the manual rotation step tracked in identity-stack#375.
#
# Flow (nothing destructive happens until the new key is proven):
#   1. Mint a NEW workspace API key via the Ory CLI.
#   2. Verify the new key authenticates against the Ory API.
#   3. Update the GitHub Actions secret ORY_WORKSPACE_API_KEY.
#   4. Delete the OLD key(s) — only after an explicit y/N confirmation.
#
# Requirements (all run as YOU — an owner/member of the `auth-stack` workspace):
#   - ory   CLI, authenticated:  https://www.ory.sh/docs/guides/cli/installation
#                                run `ory auth` once (opens a browser).
#   - gh    CLI, authenticated:  gh auth login   (needs repo secret write scope).
#   - jq
#   - terraform  (only if VERIFY_TF=1)
#
# The key value is never printed and is piped straight into `gh secret set` via
# stdin. Re-runnable: each run mints a fresh dated key and retires the older ones.
#
# CLI command names/flags are for the current Ory CLI. If a command errors,
# confirm the surface with `ory create workspace-api-key --help` etc. and adjust.
#
# Usage:
#   ./rotate-workspace-key.sh                 # rotate for real (prompts before delete)
#   DRY_RUN=1 ./rotate-workspace-key.sh       # show what it would do, mint nothing
#   VERIFY_TF=1 ./rotate-workspace-key.sh     # also run `terraform plan` with the new key
#
# Overridable env: ORY_WORKSPACE_ID, GH_REPO, OLD_KEY_NAME_MATCH, KEEP_OLD=1
set -euo pipefail

WORKSPACE_ID="${ORY_WORKSPACE_ID:-1a710b61-9aaa-473c-aab5-77b5a5f645ad}"   # auth-stack
REPO="${GH_REPO:-jamescrowley321/identity-stack}"
SECRET_NAME="ORY_WORKSPACE_API_KEY"
NEW_KEY_NAME="terraform-auth-stack-$(date +%Y%m%d-%H%M%S)"
OLD_KEY_NAME_MATCH="${OLD_KEY_NAME_MATCH:-terraform-auth-stack}"  # substring identifying keys to retire
DRY_RUN="${DRY_RUN:-0}"
VERIFY_TF="${VERIFY_TF:-0}"
KEEP_OLD="${KEEP_OLD:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # infra/ory

c_blue='\033[1;34m'; c_red='\033[1;31m'; c_yel='\033[1;33m'; c_off='\033[0m'
log()  { printf "${c_blue}==>${c_off} %s\n" "$*"; }
warn() { printf "${c_yel}WARN:${c_off} %s\n" "$*" >&2; }
die()  { printf "${c_red}ERROR:${c_off} %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight ---
for bin in ory gh jq; do command -v "$bin" >/dev/null 2>&1 || die "missing dependency: $bin"; done
[[ "$VERIFY_TF" == "1" ]] && { command -v terraform >/dev/null 2>&1 || die "VERIFY_TF=1 but terraform not found"; }
gh auth status >/dev/null 2>&1 || die "gh not authenticated — run: gh auth login"
ory list workspaces --format json >/dev/null 2>&1 \
  || die "Ory CLI not authenticated — run: ory auth  (then re-run this script)"

log "Workspace : $WORKSPACE_ID (auth-stack)"
log "Repo      : $REPO"
log "New key   : $NEW_KEY_NAME"

# --------------------------------------------------- list existing (retire) ---
log "Current workspace API keys:"
EXISTING_JSON="$(ory list workspace-api-keys --workspace "$WORKSPACE_ID" --format json 2>/dev/null || echo '[]')"
jq -r '.[]? | "    \(.id)\t\(.name // "-")"' <<<"$EXISTING_JSON" || true

if [[ "$DRY_RUN" == "1" ]]; then
  log "[dry-run] would create workspace-api-key '$NEW_KEY_NAME', update secret $SECRET_NAME, then delete keys whose name contains '$OLD_KEY_NAME_MATCH'."
  exit 0
fi

# ----------------------------------------------------------- 1. mint new key ---
log "Creating new workspace API key…"
NEW_JSON="$(ory create workspace-api-key --workspace "$WORKSPACE_ID" --name "$NEW_KEY_NAME" --format json)" \
  || die "failed to create workspace API key (nothing changed)"
NEW_KEY="$(jq -r '.value // .token // empty' <<<"$NEW_JSON")"
NEW_KEY_ID="$(jq -r '.id // empty' <<<"$NEW_JSON")"
[[ -n "$NEW_KEY" ]]    || die "could not read the new key value from CLI output (check the CLI JSON shape)"
[[ "$NEW_KEY" == ory_wak_* ]] || warn "new key does not start with 'ory_wak_' — continuing, but verify the CLI output shape"
log "Created key id: ${NEW_KEY_ID:-<unknown>}"

# ------------------------------------------ 2. verify BEFORE anything destructive ---
log "Verifying the new key authenticates…"
if ! env -i PATH="$PATH" HOME="$HOME" ORY_WORKSPACE_API_KEY="$NEW_KEY" \
       ory list projects --workspace "$WORKSPACE_ID" --format json >/dev/null 2>&1; then
  die "new key failed to authenticate — leaving the old key in place and the secret unchanged. New key id=${NEW_KEY_ID:-?} exists in the console; delete it manually if abandoning."
fi
log "New key authenticates ✔"

if [[ "$VERIFY_TF" == "1" ]]; then
  log "Running terraform plan with the new key (infra/ory)…"
  ( cd "$ORY_DIR"
    terraform init -input=false -reconfigure >/dev/null
    ORY_WORKSPACE_API_KEY="$NEW_KEY" ORY_WORKSPACE_ID="$WORKSPACE_ID" \
      terraform plan -input=false -lock=false >/dev/null
  ) || die "terraform plan failed with the new key — secret NOT updated, old key NOT deleted."
  log "terraform plan OK with new key ✔"
fi

# -------------------------------------------------------- 3. update GH secret ---
log "Updating GitHub secret $SECRET_NAME on $REPO…"
printf '%s' "$NEW_KEY" | gh secret set "$SECRET_NAME" --repo "$REPO"
log "Secret updated ✔  (ORY_WORKSPACE_ID unchanged — same workspace)"

# ------------------------------------------------------- 4. retire old keys ---
if [[ "$KEEP_OLD" == "1" ]]; then
  log "KEEP_OLD=1 — leaving old keys in place. Delete them manually once CI is confirmed green."
  exit 0
fi

mapfile -t OLD_IDS < <(
  jq -r --arg new "$NEW_KEY_ID" --arg match "$OLD_KEY_NAME_MATCH" \
    '.[]? | select(.id != $new) | select((.name // "") | contains($match)) | .id' <<<"$EXISTING_JSON"
)
if ((${#OLD_IDS[@]} == 0)); then
  log "No older keys matching '$OLD_KEY_NAME_MATCH' to retire."
  log "Done. Rotated $SECRET_NAME."
  exit 0
fi

warn "About to delete ${#OLD_IDS[@]} old key(s): ${OLD_IDS[*]}"
warn "The new key is already live in the GitHub secret. Trigger/confirm a green CI run first if you want extra safety."
read -rp "Delete these old key(s) now? [y/N] " ans
if [[ "${ans:-}" =~ ^[yY]$ ]]; then
  for id in "${OLD_IDS[@]}"; do
    ory delete workspace-api-key --workspace "$WORKSPACE_ID" "$id" && log "deleted $id" || warn "failed to delete $id"
  done
else
  log "Skipped deletion — retire the old key(s) manually in the Ory Console when ready."
fi

log "Done. Rotated $SECRET_NAME."
log "Next: migrate infra/ory state local → HCP so the key lives in HCP/Infisical (identity-stack#376)."
