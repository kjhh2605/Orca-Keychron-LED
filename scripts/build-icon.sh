#!/bin/sh
# Generate build/AppIcon.icns from native/AppIcon.png (1024x1024) when it changed.
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE="$PROJECT_DIR/native/AppIcon.png"
ICNS="$PROJECT_DIR/build/AppIcon.icns"
ICONSET="$PROJECT_DIR/build/AppIcon.iconset"
if [ -f "$ICNS" ] && [ ! "$SOURCE" -nt "$ICNS" ]; then printf '%s\n' "$ICNS"; exit 0; fi
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$SOURCE" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  /usr/bin/sips -z "$double" "$double" "$SOURCE" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$ICNS"
rm -rf "$ICONSET"
printf '%s\n' "$ICNS"
