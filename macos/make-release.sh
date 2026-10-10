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
# hdiutil sometimes misjudges the image size on CI runners ("No space left on device"),
# so give it an explicit size with headroom and retry once.
SIZE_MB=$(( $(du -sm "$STAGE" | cut -f1) + 64 ))
for attempt in 1 2; do
  if hdiutil create -volname "YT Grab" -srcfolder "$STAGE" -size "${SIZE_MB}m" -ov -format UDZO "$DIST/YT-Grab.dmg" >/dev/null; then
    break
  fi
  [ "$attempt" = 2 ] && exit 1
  echo "hdiutil не вдалося, пробую ще раз…"
  sleep 5
done

echo "→ Пакую ZIP…"
ditto -c -k --keepParent "$APP" "$DIST/YT-Grab.zip"

(cd "$DIST" && shasum -a 256 YT-Grab.dmg YT-Grab.zip > SHA256SUMS.txt)
lipo -info "$APP/Contents/MacOS/YTGrab" "$APP/Contents/MacOS/uv"
ls -la "$DIST"
