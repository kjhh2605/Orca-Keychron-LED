#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
if [ "${1:-}" = "--prepare" ]; then python3 firmware/prepare.py; shift; fi
if [ "$#" -gt 0 ]; then printf 'Usage: %s [--prepare]\n' "$0"; exit 2; fi
vendor="$root/firmware/vendor"
qmk_tree="$vendor/qmk_firmware-618127a725a1773e85f13455602cf6f72ab4de17"
PATH="$vendor/venv/bin:$vendor/arm-gnu-toolchain-13.2.Rel1-darwin-arm64-arm-none-eabi/bin:$vendor/dfu-tools/bin:$PATH"
export PATH
for required in qmk arm-none-eabi-gcc dfu-suffix; do
  command -v "$required" >/dev/null 2>&1 || { printf 'Missing %s. Run this script with --prepare first.\n' "$required"; exit 1; }
done
mkdir -p firmware/build
python3 firmware/patch_qmk.py "$qmk_tree"
./tests/firmware/run.sh
for keymap in orca_led via; do
  make -C "$qmk_tree" "keychron/k10_pro/ansi/rgb:$keymap" -j4 > "firmware/build/qmk-$keymap-build.log" 2>&1 || { tail -60 "firmware/build/qmk-$keymap-build.log"; exit 1; }
  dfu-suffix -c "$qmk_tree/keychron_k10_pro_ansi_rgb_$keymap.bin" > "firmware/build/dfu-$keymap-check.log"
done
cp "$qmk_tree/keychron_k10_pro_ansi_rgb_orca_led.bin" firmware/build/k10_pro_ansi_rgb_orca_led.bin
cp "$qmk_tree/keychron_k10_pro_ansi_rgb_via.bin" firmware/build/k10_pro_ansi_rgb_upstream_via_rebuilt.bin
cp "$qmk_tree/.build/keychron_k10_pro_ansi_rgb_orca_led.elf" firmware/build/k10_pro_ansi_rgb_orca_led.elf
cp "$qmk_tree/.build/keychron_k10_pro_ansi_rgb_orca_led.map" firmware/build/k10_pro_ansi_rgb_orca_led.map
python3 firmware/record_build.py
printf '\nBuilt custom firmware and a rebuilt upstream VIA fallback. No keyboard was flashed.\n'
