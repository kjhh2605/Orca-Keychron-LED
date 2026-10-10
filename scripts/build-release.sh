#!/bin/sh
# Build a downloadable Orca LED.app that runs from anywhere (e.g. /Applications):
# the Python bridge goes in Contents/Resources and the settings app in
# Contents/Helpers. Writes dist/Orca-LED-<version>-macos-<arch>.zip.
# Does not install anything or touch the apps in the project folder.
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST_DIR="$PROJECT_DIR/dist"
APP_PATH="$DIST_DIR/Orca LED.app"
VERSION=$(/usr/bin/plutil -extract CFBundleShortVersionString raw -o - "$PROJECT_DIR/native/Info.plist")
ARCH=$(uname -m)
ZIP_PATH="$DIST_DIR/Orca-LED-$VERSION-macos-$ARCH.zip"

"$PROJECT_DIR/scripts/build-native.sh" >/dev/null
"$PROJECT_DIR/scripts/build-settings.sh" >/dev/null

mkdir -p "$DIST_DIR"
rm -rf "$APP_PATH" "$ZIP_PATH"
cp -R "$PROJECT_DIR/build/Orca LED.app" "$APP_PATH"

RESOURCES="$APP_PATH/Contents/Resources"
HELPERS="$APP_PATH/Contents/Helpers"
mkdir -p "$RESOURCES/orca_led" "$HELPERS"
cp "$PROJECT_DIR"/orca_led/*.py "$PROJECT_DIR"/orca_led/*.js "$RESOURCES/orca_led/"
cp "$PROJECT_DIR/LICENSE" "$PROJECT_DIR/THIRD_PARTY_NOTICES.md" "$RESOURCES/"
cp -R "$PROJECT_DIR/build/Orca LED Settings.app" "$HELPERS/"

# Sign inside-out: the embedded settings app first, then the outer bundle.
IDENTITY=$("$PROJECT_DIR/scripts/signing-identity.sh")
codesign --force --sign "$IDENTITY" "$HELPERS/Orca LED Settings.app"
codesign --force --sign "$IDENTITY" "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

# Smoke checks: the bundled package imports, and the helper passes its self-test.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RESOURCES" python3 -c "import orca_led.__main__, orca_led.cdp_ui"
"$APP_PATH/Contents/MacOS/OrcaLED" --self-test >/dev/null

/usr/bin/ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
/usr/bin/shasum -a 256 "$ZIP_PATH"
