#!/bin/sh
set -e

# Substitute environment variables in Tyk API definition templates.
# Template files are mounted read-only at /opt/tyk-gateway/apps-templates/.
# Processed files are written to /opt/tyk-gateway/apps/ (container-local).
TEMPLATES_DIR="/opt/tyk-gateway/apps-templates"
APPS_DIR="/opt/tyk-gateway/apps"

mkdir -p "$APPS_DIR"

for f in "$TEMPLATES_DIR"/*.json; do
    [ -f "$f" ] || continue
    outfile="$APPS_DIR/$(basename "$f")"
    sed "s|\${DESCOPE_PROJECT_ID}|${DESCOPE_PROJECT_ID:-}|g" "$f" > "$outfile"
done

exec /opt/tyk-gateway/tyk
