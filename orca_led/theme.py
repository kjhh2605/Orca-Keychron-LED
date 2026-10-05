"""Validated user themes and a coalescing, unacknowledged delivery queue."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import uuid

from . import protocol

EFFECTS = {"solid": 0, "breathe": 1, "blink": 2, "double": 3, "triple": 4, "off": 5}
DEFAULT_STYLES = (
    (1, "#2A2A2A", "solid", 2400),
    (2, "#0033FF", "breathe", 2400),
    (3, "#FF5A00", "double", 2400),
    (4, "#0CFF00", "blink", 2400),
    (5, "#FF0000", "triple", 2800),
    (6, "#9600DC", "blink", 2400),
    (7, "#0CFF00", "solid", 2400),
)
SEEN = 7
MAX_THEME_BYTES = 65536


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


@dataclass(frozen=True)
class Style:
    state: int
    color: str
    brightness: int
    effect: str
    period_ms: int

    def frame(self):
        rgb = tuple((int(self.color[i:i + 2], 16) * self.brightness + 50) // 100
                    for i in (1, 3, 5))
        return protocol.style(self.state, *rgb, EFFECTS[self.effect], self.period_ms)


@dataclass(frozen=True)
class Theme:
    revision: str
    global_brightness: int
    states: tuple[Style, ...]

    def as_dict(self):
        return {"schema": 1, "revision": self.revision,
                "global_brightness": self.global_brightness,
                "states": [vars(style) for style in self.states]}


def default_theme(revision=None):
    return Theme(revision or str(uuid.uuid4()), 72,
                 tuple(Style(state, color, 100, effect, period)
                       for state, color, effect, period in DEFAULT_STYLES))


def validate_theme(value):
    if not isinstance(value, dict) or set(value) != {"schema", "revision", "global_brightness", "states"}:
        raise ValueError("Theme must contain schema, revision, global_brightness, and states")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("Unsupported theme schema")
    revision = value["revision"]
    if not isinstance(revision, str) or len(revision) != 36:
        raise ValueError("Theme revision must be a UUID string")
    try:
        uuid.UUID(revision)
    except ValueError as exc:
        raise ValueError("Theme revision must be a UUID string") from exc
    brightness = _integer(value["global_brightness"], 0, 255, "global_brightness")
    rows = value["states"]
    if not isinstance(rows, list) or len(rows) not in (6, 7):
        raise ValueError("Theme must define states 1..7 exactly once")
    styles = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"state", "color", "brightness", "effect", "period_ms"}:
            raise ValueError("Each state must contain state, color, brightness, effect, and period_ms")
        state = _integer(row["state"], 1, SEEN, "state")
        color = row["color"]
        if not isinstance(color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None:
            raise ValueError("State color must use #RRGGBB")
        level = _integer(row["brightness"], 0, 100, "brightness")
        effect = row["effect"]
        if not isinstance(effect, str) or effect not in EFFECTS:
            raise ValueError("Unknown state effect")
        period = _integer(row["period_ms"], 800, 10000, "period_ms")
        if period % 100:
            raise ValueError("period_ms must use 100 ms steps")
        styles.append(Style(state, color.upper(), level, effect, period))
    states = {style.state for style in styles}
    if len(styles) == 6 and states == set(range(1, 7)):
        # Themes saved before acknowledged completion existed: keep the old
        # completion look for acknowledged rows.
        complete = next(style for style in styles if style.state == 4)
        styles.append(Style(SEEN, complete.color, complete.brightness, "solid", complete.period_ms))
    elif states != set(range(1, SEEN + 1)):
        raise ValueError("Theme must define states 1..7 exactly once")
    return Theme(revision, brightness, tuple(sorted(styles, key=lambda style: style.state)))


def firmware_theme_protocol(path):
    """Theme protocol confirmed over USB by `verify-theme`; 0 when unknown."""
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return 0
    level = value.get("theme_protocol") if isinstance(value, dict) else None
    return level if type(level) is int and level >= 0 else 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_theme(raw):
    if len(raw) > MAX_THEME_BYTES:
        raise ValueError("Theme file exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Theme file is not valid UTF-8 JSON") from exc
    return validate_theme(value)


class ThemeDelivery:
    """A single sender coalesces against the styles it actually submitted.

    `sent` only means the host accepted the frames. Bluetooth has no reply.
    New revisions replace queued frames; an in-flight frame cannot be recalled.
    Initial connection and explicit identical saves resend every style the
    firmware accepts; `max_state` is 6 for theme protocol 1 firmware.
    """
    def __init__(self, path, legacy_brightness=72, max_state=SEEN):
        self.path = Path(path)
        self.legacy_brightness = legacy_brightness
        self.max_state = max_state
        self.theme = None
        self._observed = object()
        self._legacy_reset = False
        self._validation_error = None
        self._read_error = None
        self._transport_error = None
        self._phase = "legacy"
        self._sent_styles = {}
        self._sent_brightness = None
        self._pending = []
        self.sent_count = self.total = 0
        self.completed_at = None

    def refresh(self):
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_THEME_BYTES + 1)
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            self._read_error = f"Cannot read theme: {exc}"
            return
        self._read_error = None
        if raw == self._observed:
            return
        if raw is None:
            self._observed = None
            self._validation_error = None
            if self.theme is not None:
                # Removing a live configuration must not leave its colors in RAM.
                defaults = default_theme()
                self.theme = Theme(defaults.revision, self.legacy_brightness, defaults.states)
                self._legacy_reset = True
                self._queue()
            return
        try:
            candidate = parse_theme(raw)
            if (self.theme is not None and not self._legacy_reset
                    and candidate.revision == self.theme.revision and candidate != self.theme):
                raise ValueError("Changed theme contents require a new revision UUID")
        except ValueError as exc:
            self._observed = raw
            self._validation_error = str(exc)
            return
        retry_identical = (self.theme is not None and not self._legacy_reset
                           and candidate.states == self.theme.states
                           and candidate.global_brightness == self.theme.global_brightness)
        self._observed = raw
        self._validation_error = None
        self._legacy_reset = False
        self.theme = candidate
        self._queue(force=retry_identical)

    def _queue(self, force=False):
        self._transport_error = None
        self.sent_count = 0
        self.completed_at = None
        self._pending = [style for style in self.theme.states if style.state <= self.max_state
                         and (force or self._sent_styles.get(style.state) != style)]
        self.total = len(self._pending)
        self._phase = "pending"
        self._complete_if_ready()

    def _complete_if_ready(self, now=None):
        if (self.theme is not None and not self._pending
                and self._sent_brightness == self.theme.global_brightness):
            self.completed_at = now
            self._phase = "sent"
            if self._legacy_reset:
                self.theme = None
                self._legacy_reset = False
                self.sent_count = self.total = 0
                self._phase = "legacy"

    def restart(self):
        # The keyboard may have lost volatile styles across sleep/reconnection.
        self._sent_styles.clear()
        self._sent_brightness = None
        if self.theme is not None:
            self._queue(force=True)
        else:
            self._phase = "legacy"

    def next_style(self, now=None):
        if self._validation_error or self._read_error or not self._pending:
            return None
        return self._pending[0]

    def sending(self):
        self._transport_error = None
        self._phase = "sending"

    def accepted(self, style, now):
        if not self._pending or self._pending[0] != style:
            raise RuntimeError("Submitted style no longer matches the pending theme")
        self._sent_styles[style.state] = style
        self._pending.pop(0)
        self.sent_count += 1
        self._phase = "pending"
        self._complete_if_ready(now)

    def brightness_accepted(self, brightness, now):
        self._sent_brightness = brightness
        self._complete_if_ready(now)

    def pause(self):
        if self.theme is not None and self._phase != "sent":
            self._phase = "pending"

    def failed(self, message):
        if self.theme is not None:
            self._transport_error = str(message)

    def brightness(self, fallback):
        return self.theme.global_brightness if self.theme else fallback

    def status(self):
        error = self._read_error or self._validation_error or self._transport_error
        return {"revision": self.theme.revision if self.theme and not self._legacy_reset else None,
                "phase": "error" if error else self._phase,
                "sent_count": self.sent_count, "total": self.total,
                "error": error, "device_acknowledged": False}
