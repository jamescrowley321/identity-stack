#!/usr/bin/env bash
# Apply this root to the compose openfga-test server in a scratch copy, check
# that a second plan is empty and that the server holds model.json's types and
# relations under the applied model id, then destroy the store.
#
# The script needs openfga-test up and healthy (`make test-up-openfga`) and
# fails when it is not. It never starts, stops or removes the container. Run
# from anywhere in the repo, with terraform, curl and jq.
set -euo pipefail

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
root="$repo/infra/openfga"
api_url="${OPENFGA_CHECK_API_URL:-http://localhost:18380}"

missing=()
for command in terraform curl jq; do
  command -v "$command" >/dev/null || missing+=("$command")
done
if [ "${#missing[@]}" -gt 0 ]; then
  printf 'missing prerequisite: %s\n' "${missing[@]}" >&2
  exit 1
fi

if ! curl -fsS --connect-timeout 5 --max-time 20 -o /dev/null "$api_url/healthz"; then
  echo "openfga-test is not answering at $api_url: run 'make test-up-openfga' first" >&2
  exit 1
fi

# Own working copy and state: never the root's terraform.tfstate. Only the
# root's files git does not ignore are copied, so a local override.tf never
# changes what the check applies. A unique store name keeps runs apart.
work="$(mktemp -d)"
tfdir="$work/openfga"
mkdir -p "$tfdir"
git -C "$repo" ls-files -z --cached --others --exclude-standard -- ':(glob)infra/openfga/*.tf' 'infra/openfga/model.json' |
  (cd "$repo" && while IFS= read -r -d '' file; do [ -e "$file" ] || continue; cp "$file" "$tfdir/"; done)
cp "$root/.terraform.lock.hcl" "$tfdir/"
vars=(-var "api_url=$api_url" -var "store_name=identity-stack-check-${work##*.}")
applied=0

cleanup() {
  status=$?
  if [ "$applied" = 1 ] && ! terraform -chdir="$tfdir" destroy -auto-approve -input=false "${vars[@]}" >/dev/null; then
    echo "destroy failed: the check's store may still be in openfga-test (state kept in $work); 'make test-down' removes it" >&2
    exit 1
  fi
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

terraform -chdir="$tfdir" init -input=false -lockfile=readonly >/dev/null
applied=1
terraform -chdir="$tfdir" apply -auto-approve -input=false "${vars[@]}"

echo "--- second plan (must be empty)"
plan_status=0
terraform -chdir="$tfdir" plan -detailed-exitcode -input=false "${vars[@]}" || plan_status=$?
case "$plan_status" in
  0) ;;
  2) echo "second plan is not empty: the root drifts from what it applied" >&2; exit 1 ;;
  *) echo "second plan failed (exit $plan_status)" >&2; exit 1 ;;
esac

echo "--- comparing the applied model with model.json"
store_id="$(terraform -chdir="$tfdir" output -raw store_id)"
model_id="$(terraform -chdir="$tfdir" output -raw authorization_model_id)"
# Each type with its relations, and the subject types a relation takes directly.
shape='[.type_definitions[]
  | {type, relations: ((.relations // {}) | keys),
     direct: ((.metadata.relations // {}) | with_entries(.value |= [(.directly_related_user_types // [])[].type]))}]
  | sort_by(.type)'
applied_shape="$(curl -fsS --max-time 20 "$api_url/stores/$store_id/authorization-models/$model_id" |
  jq -Sce ".authorization_model | $shape")"
wanted_shape="$(jq -Sce "$shape" "$root/model.json")"
if [ "$applied_shape" != "$wanted_shape" ]; then
  echo "the applied model differs from model.json:" >&2
  echo "  applied: $applied_shape" >&2
  echo "  wanted:  $wanted_shape" >&2
  exit 1
fi
echo "openfga-terraform-check: store $store_id, model $model_id applied; second plan empty; model matches model.json"
