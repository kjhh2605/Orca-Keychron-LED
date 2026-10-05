/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define OL_SLOT_COUNT 12
#define OL_PACKET_MAX 8
#define OL_LEASE_MS 15000u
#define OL_RESTORE_MS 30000u
#define OL_SYMBOL_STABLE_MS 20u
#define OL_FRAME_IDLE_MS 2000u

enum { OL_EMPTY, OL_IDLE, OL_RUNNING, OL_WAITING, OL_COMPLETE, OL_ERROR, OL_UNKNOWN, OL_COMPLETE_SEEN };
enum { OL_SNAPSHOT = 0x11, OL_DELTA, OL_HEARTBEAT, OL_DISABLE, OL_QUERY, OL_STYLE };
enum { OL_SOLID, OL_BREATHE, OL_BLINK, OL_DOUBLE, OL_TRIPLE, OL_OFF };
/* 1: STYLE for states 1..6. 2: adds OL_COMPLETE_SEEN (state 7). */
#define OL_THEME_CAPABILITY 2u
#define OL_PERIOD_MIN 8u
#define OL_PERIOD_MAX 100u
typedef enum { OL_PASS, OL_PRESS, OL_RELEASE, OL_SWALLOW } ol_key_action;
typedef struct {
    uint8_t rgb[3], effect, period_100ms;
} ol_style;
typedef struct {
    uint8_t states[OL_SLOT_COUNT], brightness;
    uint16_t applied_counter, telemetry_counter, held_mask, down_mask, pending_release;
    uint32_t last_packet_at;
    bool active, overlay;
    /* Volatile custom styles do not change the user's persisted RGB settings. */
    ol_style styles[OL_COMPLETE_SEEN + 1];
    uint8_t styled_mask;
    uint8_t frame[OL_PACKET_MAX], frame_len, partial_byte, dibits;
    uint8_t candidate;
    bool frame_open, armed, have_candidate;
    uint32_t candidate_at, last_symbol_at;
} ol_state;

void ol_init(ol_state *s);
uint8_t ol_crc8(const uint8_t *data, size_t length);
bool ol_packet(ol_state *s, const uint8_t *data, size_t length, uint32_t now);
void ol_tick(ol_state *s, uint32_t now);
void ol_led_sample(ol_state *s, uint8_t host_leds, uint32_t now);
ol_key_action ol_key(ol_state *s, uint8_t slot, bool pressed, bool eligible, uint32_t now);
uint16_t ol_take_releases(ol_state *s);
uint8_t ol_display_state(const ol_state *s, uint8_t slot);
void ol_color(const ol_state *s, uint8_t slot, uint32_t now, uint8_t rgb[3]);
/* Connection loss stops navigation until a fresh snapshot; stale colors expire normally. */
void ol_invalidate(ol_state *s);
