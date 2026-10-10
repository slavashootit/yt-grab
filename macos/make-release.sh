#!/bin/bash
# Build the universal app and package it as dist/YT-Grab.dmg and dist/YT-Grab.zip.
# Usage: macos/make-release.sh 1.0.0
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-0.0.0}"
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] || VERSION="0.0.0"
DIST="$HERE/../dist"
APP="$HERE/build/YT Grab.app"

YTGRAB_VERSION="$VERSION" bash "$HERE/build-app.sh" --universal --no-install

rm -rf "$DIST"
mkdir -p "$DIST"

echo "→ Пакую DMG…"
STAGE="$HERE/build/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "YT Grab" -srcfolder "$STAGE" -ov -format UDZO "$DIST/YT-Grab.dmg" >/dev/null

echo "→ Пакую ZIP…"
ditto -c -k --keepParent "$APP" "$DIST/YT-Grab.zip"

(cd "$DIST" && shasum -a 256 YT-Grab.dmg YT-Grab.zip > SHA256SUMS.txt)
lipo -info "$APP/Contents/MacOS/YTGrab" "$APP/Contents/MacOS/uv"
ls -la "$DIST"
