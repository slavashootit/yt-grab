#!/bin/bash
# Build "YT Grab.app".
# Usage: macos/build-app.sh [--universal] [--no-install]
#   --universal   build for Apple Silicon and Intel (used for releases)
#   --no-install  only build into macos/build, do not copy to /Applications
# Needs Xcode Command Line Tools (swiftc, lipo, sips, iconutil, codesign): xcode-select --install
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../app" && pwd)"
BUILD="$HERE/build"
APP="$BUILD/YT Grab.app"
UNIVERSAL=0
INSTALL=1
for arg in "$@"; do
  case "$arg" in
    --universal) UNIVERSAL=1 ;;
    --no-install) INSTALL=0 ;;
    *) echo "Невідомий параметр: $arg" >&2; exit 1 ;;
  esac
done

if ! command -v swiftc >/dev/null 2>&1; then
  echo "Немає swiftc. Встанови інструменти розробника: xcode-select --install" >&2
  exit 1
fi

if [ "$UNIVERSAL" = 1 ]; then ARCHS="arm64 x86_64"; else ARCHS="$(uname -m)"; fi

rm -rf "$APP" "$BUILD/AppIcon.iconset" "$BUILD"/YTGrab-* "$BUILD"/uv-*
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/ytgrab"

for arch in $ARCHS; do
  echo "→ Компілюю для $arch…"
  swiftc -O -target "$arch-apple-macos12.0" -o "$BUILD/YTGrab-$arch" "$HERE/main.swift" -framework Cocoa -framework WebKit

  echo "→ Завантажую uv для $arch…"
  uv_arch="$arch"; [ "$arch" = arm64 ] && uv_arch=aarch64
  mkdir -p "$BUILD/uv-$arch"
  curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$uv_arch-apple-darwin.tar.gz" \
    | tar -xz -C "$BUILD/uv-$arch" --strip-components 1
done
lipo -create -output "$APP/Contents/MacOS/YTGrab" "$BUILD"/YTGrab-*
lipo -create -output "$APP/Contents/MacOS/uv" "$BUILD"/uv-*/uv

cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
if [ -n "${YTGRAB_VERSION:-}" ]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $YTGRAB_VERSION" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $YTGRAB_VERSION" "$APP/Contents/Info.plist"
fi

echo "→ Копіюю рушій…"
cp "$SRC"/*.py "$SRC/index.html" "$APP/Contents/Resources/ytgrab/"

echo "→ Роблю іконку…"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$HERE/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$HERE/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "→ Підписую (ad-hoc)…"
codesign --force --sign - "$APP/Contents/MacOS/uv"
codesign --force --sign - "$APP"

if [ "$INSTALL" = 0 ]; then
  echo "Готово: $APP"
  exit 0
fi

DEST="/Applications"
[ -w "$DEST" ] || DEST="$HOME/Applications"
mkdir -p "$DEST"
osascript -e 'tell application id "app.ytgrab.mac" to quit' >/dev/null 2>&1 || true
rm -rf "$DEST/YT Grab.app"
cp -R "$APP" "$DEST/"
echo "Готово: $DEST/YT Grab.app"
open "$DEST/YT Grab.app"
