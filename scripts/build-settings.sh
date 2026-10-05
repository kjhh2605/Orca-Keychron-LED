#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_PATH="$PROJECT_DIR/build/Orca LED Settings.app"
SETTINGS_ARCH=$(uname -m)
case "$SETTINGS_ARCH" in arm64|x86_64) ;; *) printf '%s\n' "Unsupported Mac architecture: $SETTINGS_ARCH" >&2; exit 1 ;; esac
mkdir -p "$PROJECT_DIR/build/settings-module-cache" "$APP_PATH/Contents/MacOS"
xcrun swiftc -swift-version 6 -O -target "$SETTINGS_ARCH-apple-macosx14.0" \
  -module-cache-path "$PROJECT_DIR/build/settings-module-cache" \
  -framework AppKit -framework SwiftUI \
  "$PROJECT_DIR"/native/settings/*.swift -o "$APP_PATH/Contents/MacOS/OrcaLEDSettings"
cp "$PROJECT_DIR/native/settings/Info.plist" "$APP_PATH/Contents/Info.plist"
mkdir -p "$APP_PATH/Contents/Resources"
cp "$("$PROJECT_DIR/scripts/build-icon.sh")" "$APP_PATH/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$APP_PATH"
"$APP_PATH/Contents/MacOS/OrcaLEDSettings" --self-test
printf '%s\n' "$APP_PATH"
