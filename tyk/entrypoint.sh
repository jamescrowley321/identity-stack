#!/bin/sh
set -e

# Substitute environment variables in Tyk API definition templates.
# Template files are mounted read-only at /opt/tyk-gateway/apps-templates/.
# Processed files are written to /opt/tyk-gateway/apps/ (container-local).

# Validate required environment variables
if [ -z "$DESCOPE_PROJECT_ID" ]; then
    echo "FATAL: DESCOPE_PROJECT_ID is required but empty or unset" >&2
    echo "Set DESCOPE_PROJECT_ID in your .env file" >&2
    exit 1
fi

# Validate DESCOPE_PROJECT_ID contains only safe characters (alphanumeric, hyphens, underscores)
case "$DESCOPE_PROJECT_ID" in
    *[!A-Za-z0-9_-]*)
        echo "FATAL: DESCOPE_PROJECT_ID contains invalid characters (expected alphanumeric, hyphens, underscores)" >&2
        exit 1
        ;;
esac

TEMPLATES_DIR="/opt/tyk-gateway/apps-templates"
APPS_DIR="/opt/tyk-gateway/apps"

mkdir -p "$APPS_DIR"

for f in "$TEMPLATES_DIR"/*.json; do
    [ -f "$f" ] || continue
    outfile="$APPS_DIR/$(basename "$f")"
    sed "s|\${DESCOPE_PROJECT_ID}|${DESCOPE_PROJECT_ID}|g" "$f" > "$outfile"
done

exec /opt/tyk-gateway/tyk
