/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "orca_led.h"
#include <string.h>

void ol_init(ol_state *s) { memset(s, 0, sizeof(*s)); }
uint8_t ol_crc8(const uint8_t *data, size_t length) {
    uint8_t crc = 0;
    while (length--) {
        crc ^= *data++;
        for (uint8_t i = 0; i < 8; ++i) crc = (uint8_t)((crc << 1) ^ ((crc & 0x80) ? 0x07 : 0));
    }
    return crc;
}
static void release_all(ol_state *s) {
    s->pending_release |= s->down_mask;
    s->down_mask = 0;
    /* held_mask survives: consume the physical up after forced synthetic up. */
}
static void reset_frame(ol_state *s) {
    s->frame_open = false;
    s->frame_len = s->dibits = s->partial_byte = 0;
}
void ol_tick(ol_state *s, uint32_t now) {
    if (s->active && (uint32_t)(now - s->last_packet_at) >= OL_LEASE_MS) {
        s->active = false;
        release_all(s);
    }
    if (s->overlay && (uint32_t)(now - s->last_packet_at) >= OL_RESTORE_MS) s->overlay = false;
    if (s->frame_open && (uint32_t)(now - s->last_symbol_at) >= OL_FRAME_IDLE_MS) reset_frame(s);
}
bool ol_packet(ol_state *s, const uint8_t *data, size_t length, uint32_t now) {
    ol_tick(s, now);
    if (!data || length < 2 || length > OL_PACKET_MAX || ol_crc8(data, length - 1) != data[length - 1]) return false;
    switch (data[0]) {
        case OL_SNAPSHOT: {
            if (length != 8 || (data[6] & 0xf0)) return false;
            uint8_t states[OL_SLOT_COUNT];
            for (uint8_t slot = 0; slot < OL_SLOT_COUNT; ++slot) {
                uint8_t bit = slot * 3;
                uint16_t packed = data[2 + bit / 8];
                if (bit % 8 > 5) packed |= (uint16_t)data[3 + bit / 8] << 8;
                states[slot] = (packed >> (bit % 8)) & 7;
            }
            memcpy(s->states, states, sizeof(states));
            s->brightness = data[1];
            s->active = s->overlay = true;
            break;
        }
        case OL_DELTA: {
            if (length != 3 || !s->active) return false;
            uint8_t slot = data[1] >> 3, state = data[1] & 7;
            if (slot >= OL_SLOT_COUNT) return false;
            s->states[slot] = state;
            break;
        }
        case OL_HEARTBEAT:
            if (length != 2 || !s->active) return false;
            break;
        case OL_DISABLE:
            if (length != 2) return false;
            s->active = s->overlay = false;
            release_all(s);
            break;
        case OL_STYLE: {
            if (length != 8 || data[1] < OL_IDLE || data[1] > OL_COMPLETE_SEEN ||
                data[5] > OL_OFF || data[6] < OL_PERIOD_MIN || data[6] > OL_PERIOD_MAX) return false;
            ol_style *style = &s->styles[data[1]];
            memcpy(style->rgb, data + 2, sizeof(style->rgb));
            style->effect = data[5];
            style->period_100ms = data[6];
            s->styled_mask |= (uint8_t)(1u << data[1]);
            /* Configuration is not evidence of live Orca telemetry. */
            ++s->applied_counter;
            return true;
        }
        default: return false;
    }
    s->last_packet_at = now;
    ++s->applied_counter;
    if (s->active) ++s->telemetry_counter;
    return true;
}
static void symbol(ol_state *s, uint8_t value, uint32_t now) {
    if (value == 7) {
        reset_frame(s);
        s->frame_open = true;
        s->last_symbol_at = now;
        return;
    }
    if (!s->frame_open) return;
    s->last_symbol_at = now;
    if (value == 5) { reset_frame(s); return; }
    if (value == 6) {
        if (s->dibits == 0) (void)ol_packet(s, s->frame, s->frame_len, now);
        reset_frame(s);
        return;
    }
    if (value < 1 || value > 4 || s->frame_len >= OL_PACKET_MAX) { reset_frame(s); return; }
    s->partial_byte = (uint8_t)((s->partial_byte << 2) | (value - 1));
    if (++s->dibits == 4) {
        s->frame[s->frame_len++] = s->partial_byte;
        s->dibits = s->partial_byte = 0;
    }
}
void ol_led_sample(ol_state *s, uint8_t host_leds, uint32_t now) {
    ol_tick(s, now);
    uint8_t value = (host_leds >> 2) & 7;
    if (!s->have_candidate || value != s->candidate) {
        s->candidate = value;
        s->candidate_at = now;
        s->have_candidate = true;
        return;
    }
    if ((uint32_t)(now - s->candidate_at) < OL_SYMBOL_STABLE_MS) return;
    if (!value) { s->armed = true; return; }
    if (!s->armed) return;
    s->armed = false;
    symbol(s, value, now);
}
ol_key_action ol_key(ol_state *s, uint8_t slot, bool pressed, bool eligible, uint32_t now) {
    ol_tick(s, now);
    if (slot >= OL_SLOT_COUNT) return OL_PASS;
    uint16_t bit = (uint16_t)1 << slot;
    if (!pressed) {
        if (!(s->held_mask & bit)) return OL_PASS;
        s->held_mask &= (uint16_t)~bit;
        if (!(s->down_mask & bit)) return OL_SWALLOW;
        s->down_mask &= (uint16_t)~bit;
        return OL_RELEASE;
    }
    if (s->held_mask & bit) return OL_SWALLOW;
    if (!s->active || !s->states[slot] || !eligible) return OL_PASS;
    s->held_mask |= bit;
    s->down_mask |= bit;
    return OL_PRESS;
}
uint16_t ol_take_releases(ol_state *s) {
    uint16_t value = s->pending_release;
    s->pending_release = 0;
    return value;
}
uint8_t ol_display_state(const ol_state *s, uint8_t slot) {
    if (slot >= OL_SLOT_COUNT || !s->states[slot]) return OL_EMPTY;
    return s->active ? s->states[slot] : OL_UNKNOWN;
}
static uint8_t style_amplitude(const ol_style *style, uint32_t now) {
    uint32_t period = (uint32_t)style->period_100ms * 100u;
    uint32_t phase = now % period;
    switch (style->effect) {
        case OL_SOLID: return 255;
        case OL_BREATHE: {
            uint32_t half = period / 2u;
            uint32_t triangle = phase < half ? phase : period - phase;
            return (uint8_t)(55u + triangle * 200u / half);
        }
        case OL_BLINK: return phase < period / 2u ? 255 : 0;
        case OL_DOUBLE:
        case OL_TRIPLE: {
            uint32_t pulse = period / 10u;
            bool on = phase < pulse || (phase >= pulse * 2u && phase < pulse * 3u);
            if (style->effect == OL_TRIPLE) on |= phase >= pulse * 4u && phase < pulse * 5u;
            return on ? 255 : 0;
        }
        default: return 0;
    }
}
void ol_color(const ol_state *s, uint8_t slot, uint32_t now, uint8_t rgb[3]) {
    uint8_t display = ol_display_state(s, slot);
    if (display != OL_EMPTY && (s->styled_mask & (1u << display))) {
        const ol_style *style = &s->styles[display];
        uint8_t amplitude = style_amplitude(style, now);
        for (uint8_t i = 0; i < 3; ++i) {
            uint8_t channel = (uint16_t)style->rgb[i] * amplitude / 255u;
            rgb[i] = (uint16_t)channel * s->brightness / 255u;
        }
        return;
    }
    uint8_t r = 0, g = 0, b = 0;
    uint32_t phase;
    switch (display) {
        case OL_IDLE: r = g = b = 42; break;
        case OL_RUNNING:
            phase = now % 2400;
            b = (uint8_t)(55 + (phase < 1200 ? phase : 2400 - phase) / 6);
            g = b / 5;
            break;
        case OL_WAITING:
            phase = now % 2400;
            if (phase < 220 || (phase >= 440 && phase < 660)) { r = 255; g = 90; }
            break;
        case OL_COMPLETE:
            if (now % 2400 < 1200) { g = 255; r = 12; }
            break;
        case OL_COMPLETE_SEEN: g = 255; r = 12; break;
        case OL_ERROR:
            phase = now % 2800;
            if (phase < 220 || (phase >= 440 && phase < 660) || (phase >= 880 && phase < 1100)) r = 255;
            break;
        case OL_UNKNOWN:
            if (now % 2400 < 1000) { r = 150; b = 220; }
            break;
        default: break;
    }
    rgb[0] = (uint16_t)r * s->brightness / 255;
    rgb[1] = (uint16_t)g * s->brightness / 255;
    rgb[2] = (uint16_t)b * s->brightness / 255;
}
void ol_invalidate(ol_state *s) { s->active = false; release_all(s); reset_frame(s); s->armed = false; }
