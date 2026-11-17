#!/usr/bin/env bash
# Apply this root to the compose Zitadel, check that a second plan is empty,
# then read the instance back (check_instance.py) and check the outputs hold no
# secret outside the sensitive ones.
#
# The instance must be fresh: run `make zitadel-reset` first. The root creates
# organizations by name, so a second run against the same instance fails at
# apply. The script needs the instance up and healthy and fails when it is not.
# It never starts, stops or resets a container. Linux and CI only. Run from
# anywhere in the repo, with terraform, curl, the backend venv and the bootstrap
# admin's token in TF_VAR_zitadel_access_token (`make zitadel-terraform-check`
# sets it from `make -s zitadel-admin-pat`).
#
# With --integration-tests it then runs the backend's live Zitadel suite
# (backend/tests/integration_zitadel/) against the applied root, with its issuer,
# project id, sync_admin_token, SPA client id and the test machine user's
# client id and secret. That suite also needs the test Postgres
# (`make test-up`, TEST_DATABASE_URL).
set -euo pipefail

integration_tests=0
case "${1:-}" in
  "") ;;
  --integration-tests) integration_tests=1 ;;
  *) echo "usage: $0 [--integration-tests]" >&2; exit 2 ;;
esac

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
root="$repo/infra/zitadel"
python="$repo/backend/.venv/bin/python"

missing=()
for command in terraform curl; do
  command -v "$command" >/dev/null || missing+=("$command")
done
[ -x "$python" ] || missing+=("$python (cd backend && uv sync --locked)")
if [ "${#missing[@]}" -gt 0 ]; then
  printf 'missing prerequisite: %s\n' "${missing[@]}" >&2
  exit 1
fi

if [ -z "${TF_VAR_zitadel_access_token:-}" ]; then
  echo "TF_VAR_zitadel_access_token is empty: export it from 'make -s zitadel-admin-pat', or run 'make zitadel-terraform-check'" >&2
  exit 1
fi

# The instance the root targets, from the one place it is set.
issuer="$(sed -n 's/^zitadel_url *= *"\(.*\)"$/\1/p' "$root/environments/dev.tfvars")"
if [ -z "$issuer" ]; then
  echo "environments/dev.tfvars sets no zitadel_url" >&2
  exit 1
fi
lifetime="$(sed -n 's/^access_token_lifetime *= *"\(.*\)"$/\1/p' "$root/environments/dev.tfvars")"
if [ -z "$lifetime" ]; then
  echo "environments/dev.tfvars sets no access_token_lifetime" >&2
  exit 1
fi
if ! curl -fsS --connect-timeout 5 --max-time 20 -o /dev/null "$issuer/.well-known/openid-configuration"; then
  echo "the local Zitadel is not answering at $issuer: run 'make zitadel-reset' first" >&2
  exit 1
fi

# $work holds the state and outputs.json, with the generated passwords and the
# machine user's secret. The instance is disposable, so nothing is destroyed:
# `make zitadel-reset` clears it.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Only the files git tracks, so an untracked or ignored override.tf or state never
# changes what is applied. A new .tf file is applied once it is staged (`git add`).
git -C "$root" ls-files -z --cached -- ':(glob)*.tf' .terraform.lock.hcl environments |
  (cd "$root" && while IFS= read -r -d '' file; do [ -e "$file" ] || continue; cp --parents "$file" "$work/"; done)

tf() { terraform -chdir="$work" "$@"; }
tf init -input=false >/dev/null
if ! tf apply -auto-approve -input=false -var-file=environments/dev.tfvars >"$work/apply.log" 2>&1; then
  cat "$work/apply.log" >&2
  echo "terraform apply failed. Against an instance this root was applied to before, run 'make zitadel-reset' first." >&2
  exit 1
fi
echo "terraform apply: ok"

set +e
tf plan -detailed-exitcode -input=false -var-file=environments/dev.tfvars >"$work/plan.log" 2>&1
plan=$?
set -e
if [ "$plan" -ne 0 ]; then
  cat "$work/plan.log" >&2
  echo "terraform plan after apply is not empty (exit $plan)" >&2
  exit 1
fi
echo "terraform plan after apply: no changes"

tf output -json >"$work/outputs.json"
"$python" "$root/scripts/check_instance.py" "$work/outputs.json" "$lifetime"

if [ "$integration_tests" = 1 ]; then
  echo "--- the backend's live Zitadel suite against the applied root"
  (
    cd "$repo/backend"
    output() {
      "$python" -c 'import json, sys; value = json.load(open(sys.argv[1]))[sys.argv[2]]["value"]; print(value[sys.argv[3]] if len(sys.argv) > 3 else value)' "$work/outputs.json" "$@"
    }
    ZITADEL_TEST_ISSUER="$(output issuer_url)" \
      ZITADEL_TEST_PROJECT_ID="$(output project_id)" \
      ZITADEL_TEST_SYNC_TOKEN="$(output sync_admin_token)" \
      ZITADEL_TEST_SPA_CLIENT_ID="$(output spa_client_id)" \
      ZITADEL_TEST_MACHINE_CLIENT_ID="$(output machine_user client_id)" \
      ZITADEL_TEST_MACHINE_CLIENT_SECRET="$(output machine_user_client_secret)" \
      "$python" -m pytest tests/integration_zitadel/ -v
  )
fi
