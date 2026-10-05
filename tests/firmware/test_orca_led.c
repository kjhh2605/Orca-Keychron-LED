#include "orca_led.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static uint32_t clock_ms;
static void finish(uint8_t *p, size_t n) { p[n - 1] = ol_crc8(p, n - 1); }
static void snapshot(uint8_t p[8], const uint8_t states[12]) {
    memset(p, 0, 8); p[0] = OL_SNAPSHOT; p[1] = 96;
    for (uint8_t i = 0; i < 12; ++i) {
        unsigned bit = i * 3;
        p[2 + bit / 8] |= states[i] << (bit % 8);
        if (bit % 8 > 5) p[3 + bit / 8] |= states[i] >> (8 - bit % 8);
    }
    finish(p, 8);
}
static void stable(ol_state *s, uint8_t symbol_value, uint32_t duration) {
    ol_led_sample(s, (symbol_value << 2) | 3, clock_ms);
    clock_ms += duration;
    ol_led_sample(s, (symbol_value << 2) | 3, clock_ms);
}
static void symbol_out(ol_state *s, uint8_t v) { stable(s, 0, 40); stable(s, v, 80); }
static void frame(ol_state *s, const uint8_t *p, size_t n) {
    symbol_out(s, 7);
    for (size_t i = 0; i < n; ++i) for (int shift = 6; shift >= 0; shift -= 2) symbol_out(s, ((p[i] >> shift) & 3) + 1);
    symbol_out(s, 6);
}
static void style_packet(uint8_t p[8], uint8_t state, uint8_t r, uint8_t g, uint8_t b, uint8_t effect, uint8_t period) {
    const uint8_t value[8] = {OL_STYLE, state, r, g, b, effect, period, 0};
    memcpy(p, value, sizeof(value)); finish(p, 8);
}
static void expect_color(const ol_state *s, uint8_t slot, uint32_t now, uint8_t r, uint8_t g, uint8_t b) {
    uint8_t rgb[3]; ol_color(s, slot, now, rgb);
    assert(rgb[0] == r && rgb[1] == g && rgb[2] == b);
}
/* Captured legacy palette/effect values are independent of theme implementation. */
static void test_legacy_colors(void) {
    ol_state s; ol_init(&s);
    uint8_t values[12] = {0, 1, 2, 3, 4, 5, 6, 7}, p[8];
    snapshot(p, values); p[1] = 255; finish(p, 8);
    assert(ol_packet(&s, p, 8, 0));
    expect_color(&s, 0, 0, 0, 0, 0);
    expect_color(&s, 1, 0, 42, 42, 42);
    expect_color(&s, 2, 0, 0, 11, 55);
    expect_color(&s, 2, 600, 0, 31, 155);
    expect_color(&s, 2, 1200, 0, 51, 255);
    expect_color(&s, 2, 2399, 0, 11, 55);
    expect_color(&s, 2, 2400, 0, 11, 55);
    expect_color(&s, 3, 219, 255, 90, 0);
    expect_color(&s, 3, 220, 0, 0, 0);
    expect_color(&s, 3, 440, 255, 90, 0);
    expect_color(&s, 3, 659, 255, 90, 0);
    expect_color(&s, 3, 660, 0, 0, 0);
    /* Unacknowledged completion blinks; acknowledged completion stays lit. */
    expect_color(&s, 4, 1199, 12, 255, 0);
    expect_color(&s, 4, 1200, 0, 0, 0);
    expect_color(&s, 4, 2400, 12, 255, 0);
    expect_color(&s, 7, 1200, 12, 255, 0);
    expect_color(&s, 7, 2399, 12, 255, 0);
    expect_color(&s, 5, 219, 255, 0, 0);
    expect_color(&s, 5, 220, 0, 0, 0);
    expect_color(&s, 5, 440, 255, 0, 0);
    expect_color(&s, 5, 660, 0, 0, 0);
    expect_color(&s, 5, 880, 255, 0, 0);
    expect_color(&s, 5, 1100, 0, 0, 0);
    expect_color(&s, 5, 2800, 255, 0, 0);
    expect_color(&s, 6, 999, 150, 0, 220);
    expect_color(&s, 6, 1000, 0, 0, 0);
    expect_color(&s, 6, 2400, 150, 0, 220);
    s.brightness = 96;
    expect_color(&s, 4, 0, 4, 96, 0);
    expect_color(&s, 6, 0, 56, 0, 82);
    s.brightness = 0;
    for (uint8_t slot = 0; slot < OL_SLOT_COUNT; ++slot) expect_color(&s, slot, 0, 0, 0, 0);
}
static void assert_style_rejected(ol_state *s, uint8_t p[8], size_t length, uint32_t now) {
    ol_state before = *s;
    assert(!ol_packet(s, p, length, now));
    assert(!memcmp(&before, s, sizeof(before)));
}
static void test_styles(void) {
    ol_state s; ol_init(&s); uint8_t p[8], snap[8];
    uint8_t values[12] = {0, 1, 2, 3, 4, 5, 6};
    style_packet(p, OL_RUNNING, 255, 128, 64, OL_SOLID, 24);
    assert(ol_packet(&s, p, 8, 9000));
    assert(s.applied_counter == 1 && s.telemetry_counter == 0 && s.last_packet_at == 0);
    assert(!s.active && !s.overlay && s.styled_mask == (1 << OL_RUNNING));
    for (uint8_t slot = 0; slot < OL_SLOT_COUNT; ++slot) assert(s.states[slot] == OL_EMPTY);
    snapshot(snap, values); snap[1] = 255; finish(snap, 8);
    assert(ol_packet(&s, snap, 8, 10000));
    expect_color(&s, 2, 0, 255, 128, 64);
    /* Configuring one state cannot modify any other legacy animation. */
    expect_color(&s, 1, 0, 42, 42, 42);
    expect_color(&s, 3, 0, 255, 90, 0);
    expect_color(&s, 6, 0, 150, 0, 220);
    expect_color(&s, 0, 0, 0, 0, 0);
    expect_color(&s, OL_SLOT_COUNT, 0, 0, 0, 0);
    s.brightness = 128;
    expect_color(&s, 2, 0, 128, 64, 32);
    s.brightness = 0; expect_color(&s, 2, 0, 0, 0, 0); s.brightness = 255;
    const uint8_t invalid_states[] = {0, 8, 255};
    for (size_t i = 0; i < sizeof(invalid_states); ++i) {
        style_packet(p, invalid_states[i], 1, 2, 3, OL_SOLID, 24);
        assert_style_rejected(&s, p, 8, 10001);
    }
    const uint8_t invalid_effects[] = {6, 255};
    for (size_t i = 0; i < sizeof(invalid_effects); ++i) {
        style_packet(p, OL_RUNNING, 1, 2, 3, invalid_effects[i], 24);
        assert_style_rejected(&s, p, 8, 10001);
    }
    const uint8_t invalid_periods[] = {0, 7, 101, 255};
    for (size_t i = 0; i < sizeof(invalid_periods); ++i) {
        style_packet(p, OL_RUNNING, 1, 2, 3, OL_SOLID, invalid_periods[i]);
        assert_style_rejected(&s, p, 8, 10001);
    }
    style_packet(p, OL_RUNNING, 1, 2, 3, OL_SOLID, 24); p[7] ^= 1;
    assert_style_rejected(&s, p, 8, 10001);
    for (size_t length = 2; length < 8; ++length) {
        style_packet(p, OL_RUNNING, 1, 2, 3, OL_SOLID, 24); finish(p, length);
        assert_style_rejected(&s, p, length, 10001);
    }
    assert_style_rejected(&s, NULL, 8, 10001);
    assert_style_rejected(&s, p, 0, 10001);
    assert_style_rejected(&s, p, OL_PACKET_MAX + 1, 10001);
    for (uint8_t effect = OL_SOLID; effect <= OL_OFF; ++effect) {
        style_packet(p, OL_RUNNING, 255, 255, 255, effect, 10);
        assert(ol_packet(&s, p, 8, 10002));
        switch (effect) {
            case OL_SOLID:
                expect_color(&s, 2, 0, 255, 255, 255);
                expect_color(&s, 2, 999, 255, 255, 255); break;
            case OL_BREATHE:
                expect_color(&s, 2, 0, 55, 55, 55);
                expect_color(&s, 2, 250, 155, 155, 155);
                expect_color(&s, 2, 500, 255, 255, 255);
                expect_color(&s, 2, 750, 155, 155, 155);
                expect_color(&s, 2, 999, 55, 55, 55);
                expect_color(&s, 2, 1000, 55, 55, 55); break;
            case OL_BLINK:
                expect_color(&s, 2, 0, 255, 255, 255);
                expect_color(&s, 2, 499, 255, 255, 255);
                expect_color(&s, 2, 500, 0, 0, 0);
                expect_color(&s, 2, 999, 0, 0, 0);
                expect_color(&s, 2, 1000, 255, 255, 255); break;
            case OL_DOUBLE:
            case OL_TRIPLE:
                expect_color(&s, 2, 0, 255, 255, 255);
                expect_color(&s, 2, 99, 255, 255, 255);
                expect_color(&s, 2, 100, 0, 0, 0);
                expect_color(&s, 2, 199, 0, 0, 0);
                expect_color(&s, 2, 200, 255, 255, 255);
                expect_color(&s, 2, 299, 255, 255, 255);
                expect_color(&s, 2, 300, 0, 0, 0);
                expect_color(&s, 2, 399, 0, 0, 0);
                if (effect == OL_TRIPLE) {
                    expect_color(&s, 2, 400, 255, 255, 255);
                    expect_color(&s, 2, 499, 255, 255, 255);
                } else expect_color(&s, 2, 400, 0, 0, 0);
                expect_color(&s, 2, 500, 0, 0, 0);
                expect_color(&s, 2, 999, 0, 0, 0);
                expect_color(&s, 2, 1000, 255, 255, 255); break;
            default: expect_color(&s, 2, 0, 0, 0, 0); expect_color(&s, 2, 500, 0, 0, 0); break;
        }
        uint8_t rgb[3], wrapped[3]; ol_color(&s, 2, UINT32_MAX, rgb); ol_color(&s, 2, UINT32_MAX % 1000, wrapped);
        assert(!memcmp(rgb, wrapped, sizeof(rgb)));
    }
    /* Both period limits are accepted, and channel scaling truncates per stage. */
    style_packet(p, OL_RUNNING, 255, 128, 64, OL_BREATHE, OL_PERIOD_MIN);
    assert(ol_packet(&s, p, 8, 10003));
    expect_color(&s, 2, 400, 255, 128, 64); s.brightness = 128;
    expect_color(&s, 2, 0, 27, 13, 6); s.brightness = 255;
    style_packet(p, OL_RUNNING, 255, 128, 64, OL_BREATHE, OL_PERIOD_MAX);
    assert(ol_packet(&s, p, 8, 10003)); expect_color(&s, 2, 5000, 255, 128, 64);
    /* A style at lease expiry releases held keys; no heartbeat can resurrect it. */
    assert(ol_key(&s, 2, true, true, 10004) == OL_PRESS);
    style_packet(p, OL_UNKNOWN, 9, 8, 7, OL_SOLID, 24);
    assert(ol_packet(&s, p, 8, 24999)); assert(s.active && s.last_packet_at == 10000);
    assert(ol_packet(&s, p, 8, 25000));
    assert(!s.active && s.overlay && s.last_packet_at == 10000 && s.telemetry_counter == 1);
    assert(ol_take_releases(&s) == (1 << 2));
    expect_color(&s, 2, 25000, 9, 8, 7);
    uint8_t heartbeat[] = {OL_HEARTBEAT, 0}; finish(heartbeat, 2);
    assert(!ol_packet(&s, heartbeat, 2, 25001));
    assert(ol_packet(&s, p, 8, 40000)); assert(!s.active && !s.overlay);
    /* Config survives disable and reconnect/invalidate, but power-on init clears it. */
    uint8_t mask = s.styled_mask; ol_style styles[OL_COMPLETE_SEEN + 1]; memcpy(styles, s.styles, sizeof(styles));
    uint8_t disable[] = {OL_DISABLE, 0}; finish(disable, 2);
    assert(ol_packet(&s, disable, 2, 40001)); ol_invalidate(&s);
    assert(s.styled_mask == mask && !memcmp(s.styles, styles, sizeof(styles)));
    assert(ol_packet(&s, snap, 8, 40002)); expect_color(&s, 2, 5000, 255, 128, 64);
    ol_init(&s); assert(s.styled_mask == 0);
    /* The frame parser applies styles without opening navigation or moving its lease. */
    clock_ms = 0; frame(&s, p, 8);
    assert(s.applied_counter == 1 && s.telemetry_counter == 0 && s.last_packet_at == 0 && !s.active && !s.overlay);
    assert(s.styled_mask == (1 << OL_UNKNOWN));
    /* Theme traffic across clock and packet-counter wrap cannot refresh a stale lease. */
    assert(ol_packet(&s, snap, 8, UINT32_MAX - 100)); s.applied_counter = UINT16_MAX;
    assert(ol_packet(&s, p, 8, 100));
    assert(s.active && s.applied_counter == 0 && s.telemetry_counter == 1 && s.last_packet_at == UINT32_MAX - 100);
    assert(ol_packet(&s, p, 8, 14900)); assert(!s.active && s.overlay);
    assert(ol_packet(&s, p, 8, 29900)); assert(!s.overlay);
    /* Only live telemetry advances the renderer wake generation, including wrap. */
    assert(ol_packet(&s, snap, 8, 30000)); s.telemetry_counter = UINT16_MAX;
    assert(ol_packet(&s, heartbeat, 2, 30001)); assert(s.telemetry_counter == 0);
    uint8_t delta[] = {OL_DELTA, (2 << 3) | OL_COMPLETE, 0}; finish(delta, 3);
    assert(ol_packet(&s, delta, 3, 30002)); assert(s.telemetry_counter == 1);
    assert(ol_packet(&s, p, 8, 30003)); assert(s.telemetry_counter == 1 && s.last_packet_at == 30002);
    assert(ol_packet(&s, disable, 2, 30004)); assert(s.telemetry_counter == 1);
    /* Acknowledged completion has its own configurable style. */
    style_packet(p, OL_COMPLETE_SEEN, 1, 2, 3, OL_SOLID, 24);
    assert(ol_packet(&s, p, 8, 30005) && (s.styled_mask & (1u << OL_COMPLETE_SEEN)));
    assert(s.styles[OL_COMPLETE_SEEN].rgb[2] == 3);
    uint8_t seen_delta[] = {OL_DELTA, (1 << 3) | OL_COMPLETE_SEEN, 0}; finish(seen_delta, 3);
    assert(ol_packet(&s, snap, 8, 30006) && ol_packet(&s, seen_delta, 3, 30007));
    s.brightness = 255; expect_color(&s, 1, 0, 1, 2, 3);
}
int main(void) {
    assert(ol_crc8((const uint8_t *)"123456789", 9) == 0xf4);
    ol_state s; ol_init(&s);
    uint8_t values[12] = {0, 1, 2, 3, 4, 5, 6, 1, 2, 3, 4, 5}, p[8];
    snapshot(p, values);
    uint8_t heartbeat[] = {OL_HEARTBEAT, 0}; finish(heartbeat, 2);
    assert(!ol_packet(&s, heartbeat, 2, 0));
    assert(ol_packet(&s, p, 8, 0));
    assert(s.active && s.overlay && s.brightness == 96 && !memcmp(s.states, values, 12));
    uint8_t invalid[8]; memcpy(invalid, p, 8); invalid[6] |= 0x80; finish(invalid, 8);
    assert(!ol_packet(&s, invalid, 8, 1));
    /* All eight 3-bit values are states; 7 is acknowledged completion. */
    uint8_t seen[12]; memcpy(seen, values, 12); seen[0] = OL_COMPLETE_SEEN;
    snapshot(invalid, seen); invalid[1] = p[1]; finish(invalid, 8);
    ol_state copy = s; assert(ol_packet(&copy, invalid, 8, 1) && copy.states[0] == OL_COMPLETE_SEEN);
    memcpy(invalid, p, 8); invalid[1] ^= 1;
    assert(!ol_packet(&s, invalid, 8, 1));
    assert(!ol_packet(&s, p, 7, 1));
    assert(!memcmp(s.states, values, 12) && s.applied_counter == 1);
    uint8_t delta[] = {OL_DELTA, (11 << 3) | OL_WAITING, 0}; finish(delta, 3);
    assert(ol_packet(&s, delta, 3, 2) && s.states[11] == OL_WAITING);
    delta[1] = 12 << 3; finish(delta, 3);
    assert(!ol_packet(&s, delta, 3, 2));
    assert(ol_key(&s, 0, true, true, 3) == OL_PASS);
    assert(ol_key(&s, 1, true, false, 3) == OL_PASS);
    assert(ol_key(&s, 1, true, true, 3) == OL_PRESS);
    assert(ol_key(&s, 1, true, true, 4) == OL_SWALLOW);
    assert(ol_key(&s, 2, true, true, 5) == OL_PRESS);
    assert(ol_key(&s, 2, false, false, 6) == OL_RELEASE);
    /* Graceful modifier/state changes cannot leak an unmatched physical release. */
    ol_tick(&s, 15002);
    assert(!s.active && s.overlay && ol_display_state(&s, 1) == OL_UNKNOWN);
    assert(ol_take_releases(&s) == (1 << 1));
    assert(ol_take_releases(&s) == 0);
    assert(ol_key(&s, 1, false, false, 15003) == OL_SWALLOW);
    assert(!ol_packet(&s, heartbeat, 2, 15004));
    ol_tick(&s, 30002); assert(!s.overlay);
    assert(ol_packet(&s, p, 8, 30003));
    assert(ol_key(&s, 3, true, true, 30004) == OL_PRESS);
    uint8_t disable[] = {OL_DISABLE, 0}; finish(disable, 2);
    assert(ol_packet(&s, disable, 2, 30005));
    assert(!s.overlay && !s.active && ol_take_releases(&s) == (1 << 3));
    assert(ol_packet(&s, p, 8, 30006));
    assert(ol_key(&s, 3, true, true, 30007) == OL_SWALLOW);
    assert(ol_key(&s, 3, false, true, 30008) == OL_SWALLOW);
    assert(ol_key(&s, 3, true, true, 30009) == OL_PRESS);
    assert(ol_key(&s, 3, false, true, 30010) == OL_RELEASE);
    /* unsigned timers work across the 49-day wrap. */
    ol_init(&s); assert(ol_packet(&s, p, 8, UINT32_MAX - 100));
    ol_tick(&s, 100); assert(s.active);
    ol_tick(&s, 14900); assert(!s.active);
    /* Wire framing, including >2-second total snapshot, lock bits, stable-zero rearm. */
    ol_init(&s); clock_ms = 0; frame(&s, p, 8);
    assert(clock_ms > 2000 && s.applied_counter == 1 && !memcmp(s.states, values, 12));
    uint16_t before = s.applied_counter;
    p[7] ^= 1; frame(&s, p, 8); assert(s.applied_counter == before); p[7] ^= 1;
    symbol_out(&s, 7); symbol_out(&s, 1); symbol_out(&s, 6); assert(s.applied_counter == before);
    symbol_out(&s, 7); symbol_out(&s, 5); assert(!s.frame_open);
    symbol_out(&s, 7); clock_ms += OL_FRAME_IDLE_MS; ol_tick(&s, clock_ms); assert(!s.frame_open);
    /* Repeated highs without stable zero cannot create duplicate data. */
    symbol_out(&s, 7); stable(&s, 1, 80); stable(&s, 2, 80); assert(s.dibits == 0);
    stable(&s, 0, 10); stable(&s, 2, 80); assert(s.dibits == 0);
    frame(&s, p, 8); assert(s.applied_counter == before + 1);
    /* START recovers after interrupted frame; oversized frame never mutates states. */
    symbol_out(&s, 7); for (unsigned i = 0; i < 33; ++i) symbol_out(&s, 1); symbol_out(&s, 6);
    assert(s.applied_counter == before + 1);
    frame(&s, p, 8); assert(s.applied_counter == before + 2);
    uint8_t rgb[3]; s.brightness = 0; ol_color(&s, 4, 0, rgb); assert(!rgb[0] && !rgb[1] && !rgb[2]);
    test_legacy_colors(); test_styles();
    puts("firmware protocol, parser, lease, wraparound, paired key, legacy palette and configurable theme tests passed");
}
