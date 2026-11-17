#!/usr/bin/env bash
# Apply this root to a fresh realm in the compose keycloak-test container, check
# that a second plan is empty, compare the result with the imported realm
# (check_realm.py), then destroy it. The container is stopped (with its
# volumes) only when this script started it: a keycloak-test that was already
# running, for another test run or session, is left up.
#
# Run from anywhere in the repo, with Docker, terraform, curl, jq, flock and the
# backend venv (cd backend && uv sync --locked --extra dev). The script holds
# the shared compose lock (~/.cache/identity-stack-compose.lock) for its whole
# run, so no other user of the lock (the Makefile test targets, another session,
# another run of this check) can start using keycloak-test between the moment
# this run decides whether it started the container and the cleanup that stops
# it. It reuses a lock its caller already holds (scripts/with-compose-lock.sh).
set -euo pipefail

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
root="$repo/infra/keycloak"
python="$repo/backend/.venv/bin/python"

missing=()
for command in docker terraform curl jq flock; do
  command -v "$command" >/dev/null || missing+=("$command")
done
[ -x "$python" ] || missing+=("$python (cd backend && uv sync --locked --extra dev)")
if [ "${#missing[@]}" -gt 0 ]; then
  printf 'missing prerequisite: %s\n' "${missing[@]}" >&2
  exit 1
fi

cache="${XDG_CACHE_HOME:-$HOME/.cache}"
compose_lock="$cache/identity-stack-compose.lock"
# Every run's working directory (state and generated secrets) lives here, so a
# later run can find and delete the ones an earlier run kept or was killed with.
runs="$cache/identity-stack-keycloak-terraform-check"
mkdir -p "$cache" "$runs"
chmod 700 "$runs"
touch "$compose_lock"

# Hold the compose lock for the whole run: whether this run started
# keycloak-test (and so may `down -v` it in cleanup) stays true only while no
# one else can bring the container up or start using it. The lock also keeps
# two runs of this check from building the same realm at once. The helper is
# re-entrant: under `make keycloak-terraform-check`, or any caller that already
# holds the lock, it runs this script at once (see scripts/with-compose-lock.sh).
if [ "${IDENTITY_STACK_COMPOSE_LOCK:-}" != "$compose_lock" ]; then
  exec "$repo/scripts/with-compose-lock.sh" "$0" "$@"
fi
base_url="http://keycloak-test.localhost:18180"
import_realm="identity-stack"
terraform_realm="identity-stack-terraform"
compose=(docker compose -f "$repo/docker-compose.test.yml" --profile keycloak)

# The compose bootstrap admin (a local fixture login, see docker-compose.test.yml).
export KEYCLOAK_USER=admin KEYCLOAK_PASSWORD=admin
vars=(-var "keycloak_url=$base_url" -var "realm=$terraform_realm")
work=""
applied=0
started=0

# $work holds the state and outputs.json, with the generated user passwords and
# the directory client secret. It is kept only while the realm it describes may
# still exist: the destroy failed and keycloak-test is still up. The next run
# deletes that realm and then every kept directory (delete_leftover_realm).
cleanup() {
  status=$?
  destroy_failed=0
  if [ "$applied" = 1 ]; then
    if ! terraform -chdir="$work" destroy -auto-approve -input=false "${vars[@]}" >/dev/null; then
      status=1
      destroy_failed=1
    fi
  fi
  stopped=0
  if [ "$started" = 1 ]; then
    if "${compose[@]}" down -v keycloak-test >/dev/null 2>&1; then
      stopped=1
    else
      echo "warning: could not stop keycloak-test (docker compose down -v failed)" >&2
    fi
  fi
  if [ "$destroy_failed" = 1 ] && [ "$stopped" = 0 ]; then
    echo "destroy failed: realm $terraform_realm is still in keycloak-test; state kept in $work (it holds generated secrets); the next run deletes the realm and then this directory" >&2
  elif [ -n "$work" ]; then
    [ "$destroy_failed" = 1 ] && echo "destroy failed, but keycloak-test was stopped with its volumes, so realm $terraform_realm is gone" >&2
    rm -rf "$work"
  fi
  exit "$status"
}
trap cleanup EXIT

# Delete the realm this check builds if a run that was killed, or whose destroy
# failed, left it in a keycloak-test that is still up. Absent (404) is fine.
# Once the realm is gone, the working directories earlier runs left behind
# describe nothing and are deleted with their secrets. No other run is using
# one: this run holds the compose lock.
delete_leftover_realm() {
  local token code
  token="$(curl -fsS "$base_url/realms/master/protocol/openid-connect/token" \
    --data-urlencode grant_type=password --data-urlencode client_id=admin-cli \
    --data-urlencode "username=$KEYCLOAK_USER" --data-urlencode "password=$KEYCLOAK_PASSWORD" |
    jq -er .access_token)"
  code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
    -H "Authorization: Bearer $token" "$base_url/admin/realms/$terraform_realm")"
  case "$code" in
    204) echo "--- deleted realm $terraform_realm left by an earlier run" ;;
    404) ;;
    *) echo "could not delete leftover realm $terraform_realm (HTTP $code)" >&2; return 1 ;;
  esac
  find "$runs" -mindepth 1 -maxdepth 1 -name 'run.*' ! -name "${work##*/}" -exec rm -rf {} +
}

# Own working copy and state: never the root's terraform.tfstate. Only the
# root's .tf files git does not ignore are copied, so a local override.tf or
# *_override.tf (git-ignored) never changes what the check applies.
work="$(mktemp -d "$runs/run.XXXXXX")"
git -C "$repo" ls-files -z --cached --others --exclude-standard -- ':(glob)infra/keycloak/*.tf' |
  (cd "$repo" && while IFS= read -r -d '' file; do cp "$file" "$work/"; done)
cp "$root/.terraform.lock.hcl" "$work/"

# On its own line so a failing `docker compose ps` stops the script (set -e
# does not see a failure inside `[ -z "$(...)" ]`), instead of reading as "not
# running" and letting cleanup take down another session's keycloak-test.
running="$("${compose[@]}" ps --status running --quiet keycloak-test)"
if [ -z "$running" ]; then
  # A stopped keycloak-test keeps the realm it imported when it was created
  # (--import-realm skips a realm that exists), which may predate this
  # checkout's import file. Starting it, recreate it so the comparison reads
  # the current import.
  started=1
  "${compose[@]}" up -d --wait --force-recreate --renew-anon-volumes keycloak-test
else
  # Running for another test run or session: left as it is, so its realm is
  # whatever was imported when it was created (hint below if the check fails).
  "${compose[@]}" up -d --wait keycloak-test
fi
delete_leftover_realm

terraform -chdir="$work" init -input=false >/dev/null
applied=1
terraform -chdir="$work" apply -auto-approve -input=false "${vars[@]}"

echo "--- second plan (must be empty)"
plan_status=0
terraform -chdir="$work" plan -detailed-exitcode -input=false "${vars[@]}" || plan_status=$?
case "$plan_status" in
  0) ;;
  2) echo "second plan is not empty: the root drifts from what it applied" >&2; exit 1 ;;
  *) echo "second plan failed (exit $plan_status)" >&2; exit 1 ;;
esac

terraform -chdir="$work" output -json >"$work/outputs.json"
echo "--- comparing with the imported realm"
PYTHONPATH="$repo/backend" "$python" "$root/scripts/check_realm.py" \
  --base-url "$base_url" \
  --import-realm "$import_realm" \
  --terraform-realm "$terraform_realm" \
  --terraform-outputs "$work/outputs.json" || {
  compare_status=$?
  if [ "$started" = 0 ]; then
    echo "keycloak-test was already running, so realm $import_realm is the import from when that container was created; if keycloak/realm/identity-stack-realm.json changed since, 'make test-down' (once no other run uses it) and run the check again" >&2
  fi
  exit "$compare_status"
}
