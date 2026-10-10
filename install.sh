#!/bin/bash
# YT Grab one-line installer:
#   curl -fsSL https://raw.githubusercontent.com/slavashootit/yt-grab/main/install.sh | bash
# Downloads the latest release and puts "YT Grab.app" into Applications.
set -euo pipefail

URL="https://github.com/slavashootit/yt-grab/releases/latest/download/YT-Grab.zip"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "YT Grab працює на macOS." >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Завантажую YT Grab…"
curl -fL --progress-bar "$URL" -o "$TMP/YT-Grab.zip"

DEST="/Applications"
[ -w "$DEST" ] || DEST="$HOME/Applications"
mkdir -p "$DEST"

osascript -e 'tell application id "app.ytgrab.mac" to quit' >/dev/null 2>&1 || true
rm -rf "$DEST/YT Grab.app"
ditto -x -k "$TMP/YT-Grab.zip" "$DEST"

echo "Готово: $DEST/YT Grab.app"
open "$DEST/YT Grab.app"
