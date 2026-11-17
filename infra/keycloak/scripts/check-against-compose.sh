#!/usr/bin/env bash
# Apply this root to a fresh realm in the compose keycloak-test container, check
# that a second plan is empty, compare the result with the imported realm
# (check_realm.py), then destroy it.
#
# With --integration-tests it also runs the backend's Keycloak integration tests
# (backend/tests/integration/test_keycloak_*.py) against the Terraform-built
# realm before the destroy, with the generated user ids, passwords and directory
# secret (backend/tests/helpers/keycloak_target.py). Those tests also need the
# rest of the test stack (TEST_DATABASE_URL) and, for one of them, a Descope
# client (DESCOPE_PROJECT_ID, DESCOPE_CLIENT_ID, DESCOPE_CLIENT_SECRET).
#
# The script needs keycloak-test up and healthy (`make test-up-keycloak`) and
# fails when it is not. It never starts, stops or removes the container. Linux
# and CI only. Run from anywhere in the repo, with terraform, curl, jq and the
# backend venv (cd backend && uv sync --locked --extra dev).
set -euo pipefail

integration_tests=0
case "${1:-}" in
  "") ;;
  --integration-tests) integration_tests=1 ;;
  *) echo "usage: $0 [--integration-tests]" >&2; exit 2 ;;
esac

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
root="$repo/infra/keycloak"
python="$repo/backend/.venv/bin/python"

missing=()
for command in terraform curl jq; do
  command -v "$command" >/dev/null || missing+=("$command")
done
[ -x "$python" ] || missing+=("$python (cd backend && uv sync --locked --extra dev)")
if [ "${#missing[@]}" -gt 0 ]; then
  printf 'missing prerequisite: %s\n' "${missing[@]}" >&2
  exit 1
fi

cache="${XDG_CACHE_HOME:-$HOME/.cache}"
# Every run's working directory (state and generated secrets) lives here, so a
# later run can find and delete the ones an earlier run kept or was killed with.
runs="$cache/identity-stack-keycloak-terraform-check"
mkdir -p "$runs"
chmod 700 "$runs"

base_url="http://keycloak-test.localhost:18180"
import_realm="identity-stack"
terraform_realm="identity-stack-terraform"

# The compose bootstrap admin (a local fixture login, see docker-compose.test.yml).
export KEYCLOAK_USER=admin KEYCLOAK_PASSWORD=admin
vars=(-var "keycloak_url=$base_url" -var "realm=$terraform_realm")
work=""
applied=0

if ! curl -fsS --connect-timeout 5 --max-time 20 -o /dev/null "$base_url/realms/$import_realm/.well-known/openid-configuration"; then
  echo "keycloak-test is not answering at $base_url with realm $import_realm: run 'make test-up-keycloak' first" >&2
  exit 1
fi

# $work holds the state and outputs.json, with the generated user passwords and
# the directory client secret. It is kept only when the destroy failed, because
# the realm it describes may still exist. The next run deletes that realm and
# then every kept directory (delete_leftover_realm).
cleanup() {
  status=$?
  if [ "$applied" = 1 ] && ! terraform -chdir="$work" destroy -auto-approve -input=false "${vars[@]}" >/dev/null; then
    echo "destroy failed: realm $terraform_realm may still be in keycloak-test; state kept in $work (it holds generated secrets); the next run deletes the realm and then this directory" >&2
    exit 1
  fi
  [ -n "$work" ] && rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

# Delete the realm this check builds if a run that was killed, or whose destroy
# failed, left it behind. Absent (404) is fine. Once the realm is gone, the
# working directories earlier runs left behind describe nothing and are deleted
# with their secrets. Two runs of this check against one keycloak-test at the
# same time are not supported.
delete_leftover_realm() {
  local token code
  token="$(curl -fsS --connect-timeout 5 --max-time 20 "$base_url/realms/master/protocol/openid-connect/token" \
    --data-urlencode grant_type=password --data-urlencode client_id=admin-cli \
    --data-urlencode "username=$KEYCLOAK_USER" --data-urlencode "password=$KEYCLOAK_PASSWORD" |
    jq -er .access_token)"
  code="$(curl -sS --connect-timeout 5 --max-time 20 -o /dev/null -w '%{http_code}' -X DELETE \
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
  (cd "$repo" && while IFS= read -r -d '' file; do [ -e "$file" ] || continue; cp "$file" "$work/"; done)
cp "$root/.terraform.lock.hcl" "$work/"

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
  echo "realm $import_realm is the import from when keycloak-test was created; if keycloak/realm/identity-stack-realm.json changed since, run 'make test-down && make test-up-keycloak' and run the check again" >&2
  exit "$compare_status"
}

if [ "$integration_tests" = 1 ]; then
  echo "--- the backend's Keycloak integration tests against realm $terraform_realm"
  (
    cd "$repo/backend"
    KEYCLOAK_TEST_REALM="$terraform_realm" \
      KEYCLOAK_TEST_USER_IDS="$(jq -c .user_ids.value "$work/outputs.json")" \
      KEYCLOAK_TEST_USER_PASSWORDS="$(jq -c .user_passwords.value "$work/outputs.json")" \
      KEYCLOAK_TEST_DIRECTORY_CLIENT_SECRET="$(jq -er .keycloak_directory_client_secret.value "$work/outputs.json")" \
      KEYCLOAK_TEST_ADMIN_CLIENT_SECRET="$(jq -er .keycloak_admin_client_secret.value "$work/outputs.json")" \
      "$python" -m pytest tests/integration/test_keycloak_*.py -v
  )
fi
