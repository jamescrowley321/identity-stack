#!/usr/bin/env bash
# Apply this root to a fresh realm in the compose keycloak-test container, check
# that a second plan is empty, compare the result with the imported realm
# (check_realm.py), then destroy it. The container is stopped (with its
# volumes) only when this script started it: a keycloak-test that was already
# running, for another test run or session, is left up.
#
# Run from anywhere in the repo, with Docker, terraform and the backend venv
# (cd backend && uv sync --locked --extra dev). Hold the compose lock if other
# sessions share the Docker host:
#   flock ~/.cache/identity-stack-compose.lock infra/keycloak/scripts/check-against-compose.sh
set -euo pipefail

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
root="$repo/infra/keycloak"
base_url="http://keycloak-test.localhost:18180"
import_realm="identity-stack"
terraform_realm="identity-stack-terraform"
compose=(docker compose -f "$repo/docker-compose.test.yml" --profile keycloak)

# Own working copy and state: never the root's terraform.tfstate.
work="$(mktemp -d "${TMPDIR:-/var/tmp}/keycloak-terraform-check.XXXXXX")"
cp "$root"/*.tf "$root/.terraform.lock.hcl" "$work/"

# The compose bootstrap admin (a local fixture login, see docker-compose.test.yml).
export KEYCLOAK_USER=admin KEYCLOAK_PASSWORD=admin
vars=(-var "keycloak_url=$base_url" -var "realm=$terraform_realm")
applied=0
started=0

cleanup() {
  status=$?
  if [ "$applied" = 1 ]; then
    terraform -chdir="$work" destroy -auto-approve -input=false "${vars[@]}" >/dev/null || status=1
  fi
  if [ "$started" = 1 ]; then
    "${compose[@]}" down -v keycloak-test >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

if [ -z "$("${compose[@]}" ps --status running --quiet keycloak-test)" ]; then
  started=1
fi
"${compose[@]}" up -d --wait keycloak-test

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
PYTHONPATH="$repo/backend" "$repo/backend/.venv/bin/python" "$root/scripts/check_realm.py" \
  --base-url "$base_url" \
  --import-realm "$import_realm" \
  --terraform-realm "$terraform_realm" \
  --terraform-outputs "$work/outputs.json"
