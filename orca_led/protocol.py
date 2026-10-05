"""Wire encoding shared with the firmware. See docs/protocol.md."""
from enum import IntEnum


class State(IntEnum):
    EMPTY = 0
    IDLE = 1
    RUNNING = 2
    WAITING = 3
    COMPLETE = 4
    ERROR = 5
    UNKNOWN = 6
    COMPLETE_SEEN = 7  # theme protocol 2 only


def crc8(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ (7 if crc & 128 else 0)) & 255
    return crc


def packet(payload: bytes) -> bytes:
    return payload + bytes([crc8(payload)])


def snapshot(states, brightness=72) -> bytes:
    states = tuple(states)
    if len(states) != 12 or any(type(s) not in (int, State) or not 0 <= s <= 7 for s in states):
        raise ValueError("Exactly 12 valid states required")
    if type(brightness) is not int or not 0 <= brightness <= 255:
        raise ValueError("Brightness must be 0..255")
    bits = sum(int(s) << (3 * i) for i, s in enumerate(states))
    return packet(bytes([0x11, brightness]) + bits.to_bytes(5, "little"))


def delta(slot, state) -> bytes:
    if type(slot) is not int or not 0 <= slot < 12 or type(state) not in (int, State) or not 0 <= state <= 7:
        raise ValueError("Invalid slot or state")
    return packet(bytes([0x12, (slot << 3) | int(state)]))


def heartbeat() -> bytes:
    return packet(bytes([0x13]))


def disable() -> bytes:
    return packet(bytes([0x14]))


def query() -> bytes:
    return packet(bytes([0x15]))


def style(state, red, green, blue, effect, period_ms) -> bytes:
    """Volatile per-state style; this frame does not renew the navigation lease."""
    if type(state) not in (int, State) or not 1 <= state <= 7:
        raise ValueError("Style state must be 1..7")
    if any(type(value) is not int or not 0 <= value <= 255 for value in (red, green, blue)):
        raise ValueError("Style RGB channels must be 0..255")
    if type(effect) is not int or not 0 <= effect <= 5:
        raise ValueError("Style effect must be 0..5")
    if type(period_ms) is not int or not 800 <= period_ms <= 10000 or period_ms % 100:
        raise ValueError("Style period must be 800..10000 ms in 100 ms steps")
    return packet(bytes([0x16, int(state), red, green, blue, effect, period_ms // 100]))


def symbols(data: bytes) -> list[int]:
    """Each nonzero symbol must have a stable zero separator on the wire."""
    result = [7]
    for value in data:
        result.extend(((value >> shift) & 3) + 1 for shift in (6, 4, 2, 0))
    return result + [6]


def raw_report(data: bytes) -> list[int]:
    if not 2 <= len(data) <= 30:
        raise ValueError("Raw HID packet does not fit")
    return list(bytes([0xC7, len(data)]) + data + bytes(30 - len(data)))


def choose_update(previous, desired, brightness=72, force=False):
    """Return idempotent update frames; caller commits its cache only on success."""
    desired = tuple(desired)
    full = snapshot(desired, brightness)
    if previous is None or force:
        return [full]
    changes = [i for i in range(12) if previous[i] != desired[i]]
    if len(changes) >= 3:
        return [full]
    return [delta(i, desired[i]) for i in changes]
