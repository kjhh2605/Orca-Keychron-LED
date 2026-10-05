/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#define OL_USAGE_BASE 0x03e0u
#define OL_INPUT_QUEUE 16
#define OL_PULSE_MS 30u
#define OL_INPUT_TTL_MS 1000u
typedef struct {
    uint8_t slots[OL_INPUT_QUEUE], head, count;
    uint32_t queued_at[OL_INPUT_QUEUE], pulse_at;
    uint16_t pulse_usage;
} ol_input;
void ol_input_init(ol_input *q);
bool ol_input_push(ol_input *q, uint8_t slot, uint32_t now);
/* Returns at most one standard consumer report. Never clears a native media usage. */
bool ol_input_tick(ol_input *q, bool active, uint16_t current_usage, uint32_t now, uint16_t *send_usage);
