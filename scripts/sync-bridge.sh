#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_REPOSITORY="https://github.com/okqixiaobao727-design/review-switch"
UPSTREAM_RAW_BASE="https://raw.githubusercontent.com/okqixiaobao727-design/review-switch"
UPSTREAM_COMMIT="a5fdabada7eb628a476683b7e885f1478209159a"
UPSTREAM_PATH="skills/review-switch-codex/scripts/tui_review_bridge.py"
VENDORED_PATH="skills/crew/assets/review/scripts/tui_review_bridge.py"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SOURCE_URL="${UPSTREAM_RAW_BASE}/${UPSTREAM_COMMIT}/${UPSTREAM_PATH}"

source_file="$(mktemp)"
rendered_file="$(mktemp)"
trap 'rm -f "$source_file" "$rendered_file"' EXIT

curl --fail --silent --show-error --location --output "$source_file" "$SOURCE_URL"
first_line="$(head -n 1 "$source_file")"
if [[ "$first_line" != '#!'* ]]; then
    printf 'upstream bridge has no shebang: %s\n' "$SOURCE_URL" >&2
    exit 1
fi
{
    head -n 1 "$source_file"
    printf '# Vendored from %s\n' "$UPSTREAM_REPOSITORY"
    printf '# Pinned upstream commit: %s\n' "$UPSTREAM_COMMIT"
    printf '%s\n' '# Changes belong upstream; update scripts/sync-bridge.sh when upgrading.'
    tail -n +2 "$source_file"
} > "$rendered_file"

cp "$rendered_file" "$REPOSITORY_ROOT/$VENDORED_PATH"
