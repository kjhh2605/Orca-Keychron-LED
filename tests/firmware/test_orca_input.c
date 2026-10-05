#include "orca_input.h"
#include <assert.h>
#include <stdio.h>
int main(void) {
    ol_input q; ol_input_init(&q); uint16_t out;
    assert(ol_input_push(&q, 0, 0) && ol_input_push(&q, 11, 0));
    assert(ol_input_tick(&q, true, 0, 0, &out) && out == 0x3e0);
    assert(!ol_input_tick(&q, true, 0x3e0, 29, &out));
    assert(ol_input_tick(&q, true, 0x3e0, 30, &out) && out == 0);
    assert(ol_input_tick(&q, true, 0, 31, &out) && out == 0x3eb);
    /* A volume key arriving during our pulse must not be released by us. */
    assert(!ol_input_tick(&q, true, 0xe9, 61, &out));
    assert(ol_input_push(&q, 2, 62));
    assert(!ol_input_tick(&q, true, 0xe9, 62, &out));
    assert(ol_input_tick(&q, true, 0, 63, &out) && out == 0x3e2);
    assert(ol_input_push(&q, 3, 63));
    assert(ol_input_tick(&q, false, 0x3e2, 64, &out) && out == 0 && q.count == 0);
    assert(!ol_input_tick(&q, true, 0, 65, &out));
    assert(ol_input_push(&q, 1, 100));
    assert(!ol_input_tick(&q, true, 0, 1100, &out));
    assert(!ol_input_push(&q, 12, 0));
    for (unsigned i=0;i<OL_INPUT_QUEUE;i++) assert(ol_input_push(&q, i%12, 1200));
    assert(!ol_input_push(&q, 1, 1200));
    puts("firmware consumer pulse, media coexistence, queue and expiration tests passed");
}
