#!/usr/bin/env python3
"""Install only the Orca keymap and guarded hooks; refuse unexpected upstream text."""
from pathlib import Path
import argparse, hashlib, json, shutil
PIN = "618127a725a1773e85f13455602cf6f72ab4de17"
ROOT = Path(__file__).resolve().parent

def inject(path, original, replacement):
    text = path.read_text()
    if replacement in text:
        return
    if text.count(original) != 1:
        raise SystemExit(f"Refusing unexpected upstream source: {path}")
    path.write_text(text.replace(original, replacement))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("qmk", type=Path)
    args = parser.parse_args()
    qmk = args.qmk.resolve()
    lock = json.loads((ROOT / "upstream.lock.json").read_text())
    for name, expected in lock["upstream_files"].items():
        actual = hashlib.sha256((qmk / name).read_bytes()).hexdigest()
        if actual not in {expected, lock["patched_files"][name]}:
            raise SystemExit(f"Refusing source other than the pinned upstream or our exact guarded patch: {name}")
    board = qmk / "keyboards/keychron/k10_pro"
    info = json.loads((board / "ansi/rgb/info.json").read_text())
    if info["usb"]["pid"].lower() != "0x02a0":
        raise SystemExit("Only K10 Pro ANSI RGB PID 0x02A0 is supported")
    dest = board / "ansi/rgb/keymaps/orca_led"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(board / "ansi/rgb/keymaps/via/keymap.c", dest / "keymap.c")
    for source in (ROOT / "src").glob("*.[ch]"):
        shutil.copy2(source, dest / source.name)
    (dest / "rules.mk").write_text("VIA_ENABLE = yes\nOPT_DEFS += -DORCA_LED_ENABLE\nEXTRAINCDIRS += $(KEYMAP_PATH)\nSRC += $(KEYMAP_PATH)/orca_led.c $(KEYMAP_PATH)/orca_input.c $(KEYMAP_PATH)/orca_qmk.c\n")
    inject(board / "k10_pro.c", "bool via_command_kb(uint8_t *data, uint8_t length) {", """#ifdef ORCA_LED_ENABLE
bool orca_led_raw_hid(uint8_t *data, uint8_t length);
#endif
bool via_command_kb(uint8_t *data, uint8_t length) {
#ifdef ORCA_LED_ENABLE
    if (length && data[0] == 0xC7) return orca_led_raw_hid(data, length);
#endif""")
    factory = qmk / "keyboards/keychron/bluetooth/factory_test.c"
    inject(factory, "bool rgb_matrix_indicators_user(void) {", """#ifdef ORCA_LED_ENABLE
void orca_led_overlay(void);
#endif
bool rgb_matrix_indicators_user(void) {
#ifdef ORCA_LED_ENABLE
    orca_led_overlay();
#endif""")
    usb = qmk / "tmk_core/protocol/usb_descriptor.c"
    inject(usb, "        HID_RI_USAGE_MAXIMUM(16, 0x02A0), // AC Desktop Show All Applications", """#ifdef ORCA_LED_ENABLE
        HID_RI_USAGE_MAXIMUM(16, 0x03FF), // Includes experimental Orca Consumer usages
#else
        HID_RI_USAGE_MAXIMUM(16, 0x02A0), // AC Desktop Show All Applications
#endif""")
    inject(usb, "        HID_RI_LOGICAL_MAXIMUM(16, 0x02A0),", """#ifdef ORCA_LED_ENABLE
        HID_RI_LOGICAL_MAXIMUM(16, 0x03FF),
#else
        HID_RI_LOGICAL_MAXIMUM(16, 0x02A0),
#endif""")
    renderer = qmk / "quantum/rgb_matrix/rgb_matrix.c"
    inject(renderer, "void rgb_matrix_task(void) {", """#ifdef ORCA_LED_ENABLE
bool orca_led_render_active(void);
#endif
void rgb_matrix_task(void) {""")
    inject(renderer, "    uint8_t effect = suspend_backlight || !rgb_matrix_config.enable ? 0 : rgb_matrix_config.mode;", """    uint8_t effect = suspend_backlight || !rgb_matrix_config.enable ? 0 : rgb_matrix_config.mode;
#ifdef ORCA_LED_ENABLE
    /* Render the overlay without mutating the user's live or persisted RGB config. */
    if (!suspend_state && orca_led_render_active()) effect = rgb_matrix_config.mode ? rgb_matrix_config.mode : RGB_MATRIX_SOLID_COLOR;
#endif""")
    indicator = qmk / "keyboards/keychron/bluetooth/indicator.c"
    inject(indicator, "bool led_update_kb(led_t led_state) {", """#ifdef ORCA_LED_ENABLE
bool orca_led_render_active(void);
#endif
bool led_update_kb(led_t led_state) {""")
    inject(indicator, "        if (!LED_DRIVER_IS_ENABLED()) {", """        if (!LED_DRIVER_IS_ENABLED()
#ifdef ORCA_LED_ENABLE
            && !orca_led_render_active()
#endif
        ) {""")
    inject(renderer, "void rgb_matrix_set_color(int index, uint8_t red, uint8_t green, uint8_t blue) {", """#ifdef ORCA_LED_ENABLE
void orca_led_track_background(int index, uint8_t red, uint8_t green, uint8_t blue);
#endif
void rgb_matrix_set_color(int index, uint8_t red, uint8_t green, uint8_t blue) {
#ifdef ORCA_LED_ENABLE
    orca_led_track_background(index, red, green, blue);
#endif""")
    inject(renderer, "void rgb_matrix_set_color_all(uint8_t red, uint8_t green, uint8_t blue) {", """void rgb_matrix_set_color_all(uint8_t red, uint8_t green, uint8_t blue) {
#ifdef ORCA_LED_ENABLE
    for (int index = 1; index <= 12; ++index) orca_led_track_background(index, red, green, blue);
#endif""")
    quantum = qmk / "quantum/quantum.c"
    inject(quantum, "bool process_record_quantum(keyrecord_t *record) {", """#ifdef ORCA_LED_ENABLE
bool orca_process_record(uint16_t keycode, keyrecord_t *record);
#endif
bool process_record_quantum(keyrecord_t *record) {""")
    inject(quantum, "    if (!(\n#if defined(KEY_LOCK_ENABLE)", """    if (!(
#ifdef ORCA_LED_ENABLE
            orca_process_record(keycode, record) &&
#endif
#if defined(KEY_LOCK_ENABLE)""")
    print(f"Installed {dest}; original keymap preserved; no device writes")
if __name__ == "__main__": main()
