#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_PATH="$PROJECT_DIR/build/Orca LED.app"
mkdir -p "$PROJECT_DIR/build/module-cache" "$APP_PATH/Contents/MacOS"
xcrun swiftc -swift-version 5 -O \
  -module-cache-path "$PROJECT_DIR/build/module-cache" \
  -framework AppKit -framework IOKit -framework ApplicationServices \
  "$PROJECT_DIR/native/main.swift" "$PROJECT_DIR/native/StatusApp.swift" -o "$APP_PATH/Contents/MacOS/OrcaLED"
cp "$PROJECT_DIR/native/Info.plist" "$APP_PATH/Contents/Info.plist"
mkdir -p "$APP_PATH/Contents/Resources"
cp "$("$PROJECT_DIR/scripts/build-icon.sh")" "$APP_PATH/Contents/Resources/AppIcon.icns"
codesign --force --sign "$("$PROJECT_DIR/scripts/signing-identity.sh")" "$APP_PATH"
cat > "$PROJECT_DIR/build/orca-keychron-helper" <<'SH'
#!/bin/sh
set -eu
HELPER_BUILD_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$HELPER_BUILD_DIR/Orca LED.app/Contents/MacOS/OrcaLED" "$@"
SH
chmod +x "$PROJECT_DIR/build/orca-keychron-helper"
printf '%s\n' "$PROJECT_DIR/build/orca-keychron-helper"
