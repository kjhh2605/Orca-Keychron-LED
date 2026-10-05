/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "orca_input.h"
#include <string.h>
void ol_input_init(ol_input *q) { memset(q, 0, sizeof(*q)); }
bool ol_input_push(ol_input *q, uint8_t slot, uint32_t now) {
    if (slot >= 12 || q->count == OL_INPUT_QUEUE) return false;
    uint8_t index = (q->head + q->count) % OL_INPUT_QUEUE;
    q->slots[index] = slot; q->queued_at[index] = now; ++q->count;
    return true;
}
static void pop(ol_input *q) { q->head = (q->head + 1) % OL_INPUT_QUEUE; --q->count; }
bool ol_input_tick(ol_input *q, bool active, uint16_t current, uint32_t now, uint16_t *out) {
    if (!active) q->count = 0;
    if (q->pulse_usage) {
        if (active && (uint32_t)(now - q->pulse_at) < OL_PULSE_MS) return false;
        bool owns = current == q->pulse_usage;
        q->pulse_usage = 0;
        if (owns) { *out = 0; return true; }
    }
    if (!active) return false;
    while (q->count && (uint32_t)(now - q->queued_at[q->head]) >= OL_INPUT_TTL_MS) pop(q);
    if (!q->count || current != 0) return false;
    *out = q->pulse_usage = OL_USAGE_BASE + q->slots[q->head];
    q->pulse_at = now; pop(q);
    return true;
}
