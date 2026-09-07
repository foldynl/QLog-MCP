#!/usr/bin/env bash

set -euo pipefail

readonly project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

case "$(uname -m)" in
    x86_64|amd64)
        readonly architecture=x86_64
        ;;
    aarch64|arm64)
        readonly architecture=aarch64
        ;;
    *)
        echo "Unsupported architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

readonly requirements=$(mktemp "${TMPDIR:-/tmp}/qlog-mcp-appimage-requirements.XXXXXX")
trap 'rm -f "${requirements}"' EXIT

# appimage.ctl installs with pip internally. Exporting uv.lock as an additional
# requirements target keeps the AppImage dependencies identical to uv sync.
uv export \
    --quiet \
    --directory "${project_root}" \
    --locked \
    --no-dev \
    --no-emit-project \
    --no-hashes \
    --no-header \
    --no-annotate \
    --output-file "${requirements}"

uv run \
    --no-project \
    --with appimage==4.0.1 \
    python -m appimage.ctl build \
    --project-dir "${project_root}" \
    --package="-r${requirements}"

readonly artifact="${project_root}/dist/qlog-mcp-${architecture}.AppImage"
APPIMAGE_EXTRACT_AND_RUN=1 "${artifact}" --help >/dev/null

(
    cd "${project_root}/dist"
    sha256sum "$(basename "${artifact}")" >"$(basename "${artifact}").sha256"
)

echo "AppImage written to ${artifact}"
