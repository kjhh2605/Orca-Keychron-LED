# Orca LED protocol v1

Status values: 0 empty, 1 idle, 2 running, 3 waiting, 4 complete, 5 error, 6 unknown, 7 complete-seen (theme capability 2 only; reserved before). Physical slots 0..11 (F1..F12).

Every packet ends with CRC-8(poly 0x07, init 0, no reflection/xorout), covering preceding bytes. Absolute/idempotent states; never write EEPROM for status.

- SNAPSHOT 0x11: [0x11, brightness, states0..4, CRC] = 8 bytes. Pack each of 12 states into consecutive 3-bit fields little-endian (slot0 bits0..2); top 4 bits of byte states4 must be zero.
- DELTA 0x12: [0x12, (slot << 3) | state, CRC] = 3 bytes. Slot is 0..11.
- HEARTBEAT 0x13: [0x13, CRC] = 2 bytes; refresh lease only after a valid snapshot.
- DISABLE 0x14: [0x14, CRC] = 2 bytes. Clear override, release held synthetic keys if needed, restore original behavior.
- USB QUERY 0x15: [0x15, CRC]. Reply must identify magic ASCII OL, version 1, applied packet counter, and state. No need to accept QUERY over BT.

Bluetooth LED element transport: only ScrollLock/Compose/Kana (HID LED usages 3/4/5), mask0x1C shift2. NumLock/CapsLock preserved. Symbol0 idle separator, 7 START (resets parser), data symbols1..4 encode 2bits 0..3, 6 END validates frame and applies atomically;5 ABORT. Byte symbols MSB-first: ((b>>6)&3)+1, ((b>>4)&3)+1, ((b>>2)&3)+1, (b&3)+1. Insert idle0 between every nonzero symbol. Receiver accepts only nonzero symbols after field stable for20ms, rearms only after stable0. Host uses one IOHID output transaction per symbol; high/zero timing is configurable. Initial 80/40ms tests did not produce visible output. The 200/200ms transaction test produced visible Bluetooth LEDs, and 150/100ms subsequently passed real F-key navigation/minimization. Full 12-slot frame measured about8.9s at150/100ms; a three-byte delta has14symbols (about3.6s plus scheduling). Timing is configurable (`--high-ms`/`--low-ms`); the bridge default is 120/80ms, which passed live F11 completion checks. Long-run reliability remains unmeasured. Timeout partial frame after2000ms of inactivity since the last accepted symbol; START resets framing, malformed/CRC/length errors must have no lighting effect.

Lease: valid snapshot enables mode. Valid snapshot/delta/heartbeat renews alive lease. After15000ms without valid packets, show unknown on previously assigned slots and restore original F-key behavior; after30000ms restore original lighting entirely. No heartbeat resurrects expired override without a new snapshot. Connect/reconnect must send SNAPSHOT.

Firmware overlay only F-row LED indices1..12 for ANSI RGB. Other variants require confirmed index mapping before build. State0 overlay black but original key function. Active assigned physical F-row presses send Consumer page0x0C usages0x03E0..0x03EB (slot0..11), followed by release0, through the normal host consumer/BT queue. No direct CKBT51 calls. Other physical F13..F16 keys remain unmodified. Only emit on physical down once, preserve original key press/release outside active lease/assignment and with Fn/modifiers. macOS helper listens to the target device consumer reportID0x0A or corresponding IOHID values (not global Fkey events). No keyboard-event tap needed for reserved consumer usages. Current USB-IF HUT1.7 p135 lists2D5..4FF Reserved; this is an experimental use of a reserved range, not a vendor-defined standard. Real device descriptor accepts consumer usages up to0x03FF. Verify no OS side effects on target OS.

Navigation pulse queuing: normal host_consumer_send, brief30ms pulse then0; do not clobber real held multimedia usages. Multiple slot presses serialized, never repeat from hold; discard stale queued navigation after1s and clear queue on lease expiry.

USB custom raw HID prefix0xC7: [0xC7, packet_length, packet bytes, zero pad to32]; route through custom handler without breaking VIA. Query replies [0xC7, O, L, 1, applied_counter_lo, applied_counter_hi, mode_active, brightness,12 state bytes,...zero pad32]. USB is diagnostics/development; final transport Bluetooth required.

## Configurable state styles (theme capability 1)

This extension keeps protocol version 1. Query response byte20 is `1` when configurable styles are supported; it is `0` in the initial installed firmware. Bytes21..31 remain zero. An old host can ignore the new field. A new host must not treat a successful transport write to old firmware as a successful style application. USB query can verify capability and the applied packet counter; Bluetooth transport has no acknowledgement.

STYLE `0x16` is exactly eight bytes: `[0x16, state, red, green, blue, effect, period_100ms, CRC]`. State is 1..6; state0 always remains black and cannot be configured. RGB channels accept 0..255. The host scales RGB by the per-state brightness before encoding; the existing snapshot brightness is an additional global multiplier. Effect accepts 0..5. Period accepts 8..100 inclusive, representing 800..10000 milliseconds, and is validated for every effect, including solid and off.

| Effect | Value | Amplitude over period P |
| --- | --- | --- |
| Solid | 0 | 255 throughout |
| Breathe | 1 | Triangle from 55 to255 at P/2 and back to55 |
| Blink | 2 | 255 for first P/2, then0 |
| Double pulse | 3 | 255 at [0,P/10) and [2P/10,3P/10), otherwise0 |
| Triple pulse | 4 | Double pulse plus [4P/10,5P/10) |
| Off | 5 | 0 throughout |

Phase is the keyboard's unsigned millisecond timer modulo P. Breathe amplitude is `55 + floor(200 * triangle / (P/2))`, where `triangle = min(phase, P - phase)`. Each output channel is `floor(floor(channel * amplitude / 255) * global_brightness / 255)`. All configured states share the timer phase; updates do not restart an animation. At the 32-bit timer rollover the phase restarts from zero, while lease arithmetic remains wrap-safe.

A valid style atomically replaces only the selected state's RAM style and increments the applied packet counter. It never enables navigation or lighting, renews the telemetry lease, changes assignments, or resets renderer/power-saving activity timers. `ol_tick` runs before packet validation, so normal lease expiry still occurs while styles arrive. All format, CRC, state, effect and period checks complete before configuration changes. Rejected styles do not increment the packet counter. No style packet writes EEPROM.

Until a state receives its first valid STYLE, its exact original palette and timing remain in use. Styles survive DISABLE, telemetry expiry and transport invalidation/reconnection within the same boot; power-on initialization clears them. The host should resend saved styles after a keyboard reboot. The unknown-state style also applies to previously assigned slots after the telemetry lease expires. The overlay still disappears after the normal30-second timeout.

One style frame is 34 symbols: about 8.6 s at 150/100 ms and about 6.9 s at the 120/80 ms bridge default. At 120/80 ms, seven states took about two minutes including interleaved heartbeats and refresh snapshots. Configure before the initial snapshot, or send fresh telemetry/heartbeats between style frames while active. Style traffic alone intentionally cannot keep F-key navigation alive.

## Acknowledged completion (theme capability 2)

Query byte20 is `2` on firmware that also accepts state 7, `complete-seen`: a completion the user has already looked at. SNAPSHOT, DELTA and STYLE accept state 7 like any other state; everything else is unchanged. Firmware with capability 1 rejects any frame containing state 7, so the host sends 7 only after `verify-theme` recorded capability 2 over USB, and otherwise sends 4 instead.

Unstyled defaults: complete (4) blinks green with a 2400 ms period, 50% duty; complete-seen (7) is solid green.

The host decides acknowledgement. A completion becomes seen when its workspace is Orca's selected terminal view while Orca is the frontmost, unminimized app. It is keyed by workspace and each agent's completion start time, so the next finished turn shows as unseen again. Keys are kept in `runtime/completion-seen.json`.

## Preempting redundant frames

Periodic full snapshots (every 12 s) and heartbeats repeat state the keyboard already has. While sending one, the host polls Orca between symbols; if the desired states changed, it sends ABORT (symbol 5), then the changed slots as DELTA frames, and only then resumes the periodic refresh. Rearming snapshots, brightness changes and STYLE frames are never preempted.

