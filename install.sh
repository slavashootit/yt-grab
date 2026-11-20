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
# Quitting is asynchronous: opening the new copy while the old one is still closing just brings the
# closing one forward, and nothing is left running. Wait (up to 15 s) until it is gone.
for _ in $(seq 1 50); do
  pgrep -f "YT Grab.app/Contents/MacOS/YTGrab" >/dev/null 2>&1 || break
  sleep 0.3
done
# Releases built on GitHub are signed ad-hoc. If the copy being replaced was signed with a certificate
# this Mac has (a self-signed "Kadr Dev" and the like), sign the new copy the same way, so macOS keeps
# the permissions it gave YT Grab (Downloads folder and so on) instead of asking again.
SIGNER="$(codesign -dvv "$DEST/YT Grab.app" 2>&1 | sed -n 's/^Authority=//p' | head -1 || true)"
rm -rf "$DEST/YT Grab.app"
ditto -x -k "$TMP/YT-Grab.zip" "$DEST"
if [ -n "$SIGNER" ] && security find-identity -p codesigning 2>/dev/null | grep -q "\"$SIGNER\""; then
  codesign --force --sign "$SIGNER" "$DEST/YT Grab.app/Contents/MacOS/uv" 2>/dev/null || true
  codesign --force --sign "$SIGNER" "$DEST/YT Grab.app" 2>/dev/null || true
fi

echo "Готово: $DEST/YT Grab.app"
open "$DEST/YT Grab.app"
