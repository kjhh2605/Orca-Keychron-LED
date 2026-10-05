/* SPDX-License-Identifier: GPL-2.0-or-later */
#include QMK_KEYBOARD_H
#include "orca_led.h"
#include "orca_input.h"
#include "raw_hid.h"
#include "host.h"
#include "bluetooth.h"
#include "transport.h"
#include "lpm.h"

static ol_state state;
static ol_input input;
static uint8_t last_transport;
static bool was_connected;
static uint16_t previous_telemetry_counter;
static uint8_t background[OL_SLOT_COUNT][3];
static bool painting_overlay, had_overlay;

/* Observe original effect/indicator writes, excluding our own paint operation. */
void orca_led_track_background(int index, uint8_t red, uint8_t green, uint8_t blue) {
    if (painting_overlay || index < 1 || index > OL_SLOT_COUNT) return;
    background[index - 1][0] = red;
    background[index - 1][1] = green;
    background[index - 1][2] = blue;
}
static void restore_background_task(void) {
    if (had_overlay && !state.overlay) {
        for (uint8_t i = 0; i < OL_SLOT_COUNT; ++i) {
            uint8_t *rgb = background[i];
            if (rgb_matrix_is_enabled()) rgb_matrix_set_color(i + 1, rgb[0], rgb[1], rgb[2]);
            else rgb_matrix_set_color(i + 1, 0, 0, 0);
        }
    }
    had_overlay = state.overlay;
}

static void input_task(uint32_t now) {
    uint16_t usage;
    (void)ol_take_releases(&state); /* Input is finite pulses; physical pairing remains in core. */
    if (ol_input_tick(&input, state.active, host_last_consumer_usage(), now, &usage)) host_consumer_send(usage);
}
static void accepted_task(void) {
    if (state.telemetry_counter == previous_telemetry_counter) return;
    previous_telemetry_counter = state.telemetry_counter;
    if (state.active) {
        /* Live telemetry keeps the existing renderer awake without EEPROM writes. */
        rgb_matrix_disable_time_reset();
        lpm_timer_reset();
    }
}
void keyboard_post_init_user(void) {
    ol_init(&state);
    ol_input_init(&input);
    last_transport = get_transport();
    was_connected = bluetooth_get_state() == BLUETOOTH_CONNECTED;
}
void matrix_scan_user(void) {
    uint32_t now = timer_read32();
    bool connected = bluetooth_get_state() == BLUETOOTH_CONNECTED;
    if (last_transport != get_transport() || (get_transport() == TRANSPORT_BLUETOOTH && was_connected && !connected)) ol_invalidate(&state);
    last_transport = get_transport();
    was_connected = connected;
    if (get_transport() == TRANSPORT_BLUETOOTH && connected) ol_led_sample(&state, host_keyboard_led_state().raw, now);
    else ol_tick(&state, now);
    accepted_task();
    input_task(now);
    restore_background_task();
}
bool orca_process_record(uint16_t keycode, keyrecord_t *record) {
    (void)keycode;
    if (record->event.key.row != 0 || record->event.key.col < 1 || record->event.key.col > 12) return true;
    uint8_t slot = record->event.key.col - 1;
    /* Original base layers are 0 (Mac) and 2 (Windows); Fn layers are 1 and 3. */
    bool eligible = !(layer_state & ~((layer_state_t)1 | ((layer_state_t)1 << 2))) &&
                    !get_mods() && !get_weak_mods() && !get_oneshot_mods();
    ol_key_action action = ol_key(&state, slot, record->event.pressed, eligible, timer_read32());
    if (action == OL_PASS) return true;
    if (action == OL_PRESS) {
        lpm_timer_reset();
        (void)ol_input_push(&input, slot, timer_read32());
    }
    input_task(timer_read32());
    return false;
}
void suspend_power_down_user(void) {
    ol_invalidate(&state);
    input_task(timer_read32());
}
/* Called before factory/battery/Bluetooth indicators, preserving their priority. */
bool orca_led_render_active(void) { return state.overlay; }
void orca_led_overlay(void) {
    if (!state.overlay) return;
    had_overlay = true;
    painting_overlay = true;
    if (!rgb_matrix_is_enabled()) rgb_matrix_set_color_all(0, 0, 0);
    uint32_t now = timer_read32();
    for (uint8_t i = 0; i < OL_SLOT_COUNT; ++i) {
        uint8_t rgb[3];
        ol_color(&state, i, now, rgb);
        rgb_matrix_set_color(i + 1, rgb[0], rgb[1], rgb[2]);
    }
    painting_overlay = false;
}
/* This is reached from VIA's early keyboard hook, only for prefix 0xC7. */
bool orca_led_raw_hid(uint8_t *data, uint8_t length) {
    if (length != 32 || data[0] != 0xc7) return false;
    uint8_t packet_len = data[1];
    if (packet_len < 2 || packet_len > OL_PACKET_MAX) return true;
    for (uint8_t i = packet_len + 2; i < length; ++i) if (data[i]) return true;
    uint8_t *packet = data + 2;
    if (packet_len == 2 && packet[0] == OL_QUERY && ol_crc8(packet, 1) == packet[1]) {
        ol_tick(&state, timer_read32());
        input_task(timer_read32());
        uint8_t response[32] = {0xc7, 'O', 'L', 1};
        response[4] = state.applied_counter & 255;
        response[5] = state.applied_counter >> 8;
        response[6] = state.active;
        response[7] = state.brightness;
        for (uint8_t i = 0; i < OL_SLOT_COUNT; ++i) response[i + 8] = state.states[i];
        response[20] = OL_THEME_CAPABILITY;
        raw_hid_send(response, sizeof(response));
    } else {
        (void)ol_packet(&state, packet, packet_len, timer_read32());
        accepted_task();
        input_task(timer_read32());
    }
    return true;
}
