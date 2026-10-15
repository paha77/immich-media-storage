#!/bin/bash
# Host entry point: macOS supplies osascript; all catalog access stays in Docker.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(uname -s)" != Darwin ]; then
  echo "This runner requires macOS and normal Google Chrome." >&2
  exit 1
fi
export PUID="$(id -u)" PGID="$(id -g)"
if ! docker_path="$(command -v docker)"; then
  echo "Docker was not found in PATH. Start Docker Desktop and make its CLI available." >&2
  exit 1
fi
mkdir -p .import-state/google-photos-cleanup
# Prevent two automated runners from driving Chrome concurrently.
lock=.import-state/google-photos-cleanup/chrome-runner.lock
if ! mkdir "$lock" 2>/dev/null; then
  echo "Chrome runner lock exists: $lock. If no runner is active, remove this empty directory." >&2
  exit 1
fi
trap 'rmdir "$lock"' EXIT
if [ "${1:-}" != "--check" ]; then
  echo "Building Google Photos checkpoint helper..." >&2
  docker compose -f docker-compose.google-photos-cleanup.yml build google-photos-cleanup
fi
echo "Checking Chrome and the expected Google Photos account..." >&2
/usr/bin/osascript -l JavaScript google-photos-cleanup-chrome.js "$PWD" "$docker_path" "$@"
