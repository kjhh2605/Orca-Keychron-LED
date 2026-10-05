"""Concurrent state polling, Bluetooth delivery, and deliberate navigation."""
import json
import math
import os
import queue
import threading
import time
import tempfile
from pathlib import Path
from . import protocol
from .native import BluetoothWriter, FramePreempted, NativeError
from .acknowledge import CompletionTracker, COMPLETE_SEEN
from .theme import ThemeDelivery, firmware_theme_protocol


def states_from_snapshot(value):
    if not value.get("supported"):
        raise ValueError(value.get("reason") or "Authoritative Orca UI state unavailable")
    states = [0] * 12
    seen = set()
    for item in value.get("slots", []):
        index = item["index"] - 1
        if not 0 <= index < 12 or index in seen:
            raise ValueError("Invalid or duplicate slot")
        seen.add(index)
        state = int(item.get("state", 6))
        if state not in range(8):
            raise ValueError("Invalid state")
        states[index] = state if item.get("id") else 0
    return tuple(states)


def navigation_action(snapshot, slot, frontmost_bundle, bundle="com.stablyai.orca"):
    """Use actual UI selection, never the last key we happened to observe."""
    if not snapshot.get("supported") or not snapshot.get("app_running"):
        return {"action": "none", "reason": "Orca UI unavailable"}
    item = next((s for s in snapshot.get("slots", []) if s["index"] == slot + 1), None)
    if not item or not item.get("id"):
        return {"action": "none", "reason": "Unassigned slot"}
    current = snapshot.get("selected_id")
    if (frontmost_bundle == bundle and current == item["id"]
            and snapshot.get("active_view", "workspace") in ("workspace", "worktree", "folder", "terminal")
            and not snapshot.get("is_minimized", False)):
        return {"action": "minimize", "target_id": item["id"], "window_id": snapshot.get("window_id")}
    return {"action": "select", "target_id": item["id"], "window_id": snapshot.get("window_id")}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


class Controller:
    def __init__(self, orca, native, status_file, brightness=72, high_ms=150, low_ms=100, theme_file=None):
        self.orca, self.native = orca, native
        self.status_file, self.brightness = Path(status_file), brightness
        # Theme protocol 2 firmware renders acknowledged completion (state 7);
        # older firmware rejects it, so it is shown as plain completion there.
        self.seen_supported = firmware_theme_protocol(self.status_file.with_name("theme-firmware.json")) >= 2
        self.themes = ThemeDelivery(theme_file or self.status_file.with_name("theme.json"), brightness,
                                    max_state=COMPLETE_SEEN if self.seen_supported else COMPLETE_SEEN - 1)
        self.completions = CompletionTracker(self.status_file.with_name("completion-seen.json"))
        self._theme_status = self.themes.status()
        self.stop = threading.Event()
        self.writer = BluetoothWriter(native, high_ms, low_ms, self.stop)
        self._lock = threading.Lock()
        self._orca_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._snapshot = None
        self._snapshot_at = 0
        self._error = None
        self._poll_error = self._transport_error = self._navigation_error = None
        self._last_navigation = None
        self._transport = "not_started"
        self._last_write = None
        self._navigating = False
        self._delivered_ids = None
        self._mapping_activated_at = float("inf")

    def _refresh_error(self):
        # Caller holds _lock. One successful subsystem cannot clear another's error.
        self._error = self._transport_error or self._navigation_error or self._poll_error

    def _save(self):
        with self._save_lock:
            with self._lock:
                value = {"updated_at": time.time(), "transport": self._transport,
                         "last_host_write_at": self._last_write, "device_acknowledged": False,
                         "error": self._error, "snapshot": self._snapshot,
                         "navigation": self._last_navigation, "theme": dict(self._theme_status)}
            try:
                atomic_json(self.status_file, value)
            except OSError:
                # A dead diagnostics writer must not leave a silently running bridge.
                self.stop.set()
                raise

    def _event_current(self, received_at):
        now = time.monotonic()
        with self._lock:
            return (not self.stop.is_set() and math.isfinite(received_at)
                    and 0 <= now - received_at <= 1
                    and received_at >= self._mapping_activated_at)

    def _navigation_snapshot(self, value, expected=None):
        states_from_snapshot(value)
        with self._lock:
            if tuple(row.get("id") for row in value.get("slots", [])) != self._delivered_ids:
                raise RuntimeError("Workspace order changed; waiting for LED synchronization")
        if expected is not None and any(value.get(key) != expected.get(key)
                                        for key in ("runtime_id", "window_id")):
            raise RuntimeError("Orca runtime or target window changed during navigation")

    def _poll(self):
        while not self.stop.is_set():
            try:
                with self._orca_lock:
                    value = self.orca.snapshot()
                states_from_snapshot(value)
                value = self.completions.apply(value, self._orca_focused())
                with self._lock:
                    self._snapshot = value
                    self._snapshot_at = time.monotonic()
                    self._poll_error = None
                    self._refresh_error()
            except Exception as exc:
                with self._lock:
                    self._poll_error = str(exc)
                    self._snapshot_at = 0
                    self._mapping_activated_at = float("inf")
                    self._refresh_error()
            self._save()
            self.stop.wait(0.75)

    def _states_changed(self, sent):
        with self._lock:
            value = self._snapshot
        try:
            return self._wire_states(states_from_snapshot(value)) != sent
        except (ValueError, TypeError, AttributeError):
            return False

    def _wire_states(self, states):
        if self.seen_supported:
            return states
        return tuple(int(protocol.State.COMPLETE) if state == COMPLETE_SEEN else state for state in states)

    def _orca_focused(self):
        try:
            front = self.native.call("frontmost", timeout=1)
        except Exception:
            return False
        return (front.get("bundle_id", front.get("bundleId")) == "com.stablyai.orca"
                and front.get("is_minimized") is False)

    def _publish_theme(self):
        status = self.themes.status()
        with self._lock:
            changed = status != self._theme_status
            self._theme_status = status
        if changed:
            self._save()

    def _current_mapping(self, identities):
        with self._lock:
            latest = self._snapshot
            return (not self.stop.is_set() and latest is not None
                    and time.monotonic() - self._snapshot_at < 4
                    and identities == tuple(row.get("id") for row in latest.get("slots", [])))

    def _suspend_input(self):
        self.themes.pause()
        self._publish_theme()
        with self._lock:
            self._mapping_activated_at = float("inf")
        self.native.call("listen", enabled=False)

    def _send(self):
        previous = None
        last_sent = 0.0
        last_full = 0.0
        opened = False
        last_mask = None
        last_fresh = None
        last_brightness = None
        while not self.stop.is_set():
            try:
                self.themes.refresh()
                self._publish_theme()
                brightness = self.themes.brightness(self.brightness)
                with self._lock:
                    value = self._snapshot
                    fresh = time.monotonic() - self._snapshot_at < 4
                if value is None:
                    self.stop.wait(0.2)
                    continue
                wanted = self._wire_states(states_from_snapshot(value))
                identities = tuple(row.get("id") for row in value.get("slots", []))
                if not fresh:
                    self.themes.pause()
                    self._publish_theme()
                    # Do not renew an active firmware navigation lease when the
                    # host cannot act on navigation. Firmware expires it, shows
                    # unknown, then restores the user's normal backlight.
                    with self._lock:
                        was_armed = math.isfinite(self._mapping_activated_at)
                        self._mapping_activated_at = float("inf")
                    if previous is not None or was_armed:
                        self.native.call("listen", enabled=False)
                        previous, last_mask, last_fresh = None, None, None
                        with self._lock:
                            self._transport = "ui_stale_lease_expiring"
                        self._save()
                    self.stop.wait(0.2)
                    continue
                if not opened:
                    self.native.call("open", product_id=0x02A0, transport="Bluetooth")
                    opened = True
                    previous = None
                    self.themes.restart()
                    self._publish_theme()
                now = time.monotonic()
                with self._lock:
                    remapping = identities != self._delivered_ids
                    rearming = remapping or not math.isfinite(self._mapping_activated_at)
                style_pending = self.themes.next_style(now) is not None
                # Heartbeats around styles preserve the normal lease budget.
                # Full snapshots still have a bounded refresh interval: a lost
                # heartbeat can expire firmware mode, and later heartbeats or
                # deltas alone cannot reactivate it. Repeated saves must not
                # starve these full snapshots on an unacknowledged transport.
                # A real change goes out as deltas before any periodic refresh.
                refresh = (previous == wanted and not rearming and brightness == last_brightness
                           and now-last_full >= (24 if style_pending else 12))
                frames = protocol.choose_update(previous, wanted, brightness,
                    force=rearming or brightness != last_brightness or refresh)
                if not frames and now-last_sent >= 5:
                    frames = [protocol.heartbeat()]
                    refresh = True
                # Refreshes and heartbeats repeat known state, so a newer
                # Orca reading may abort them mid-frame.
                abort = (lambda: self._states_changed(wanted)) if refresh else None
                if frames:
                    # Keep navigation responsive during ordinary state/heartbeat writes.
                    # Disable only when changing target identities or losing UI truth.
                    if rearming:
                        with self._lock:
                            self._mapping_activated_at = float("inf")
                        self.native.call("listen", enabled=False)
                    try:
                        for frame in frames:
                            self.writer.send(frame, abort=abort)
                    except FramePreempted:
                        continue
                    for frame in frames:
                        if frame[0] == 0x11:
                            last_full = time.monotonic()
                            last_brightness = brightness
                            self.themes.brightness_accepted(brightness, last_full)
                            self._publish_theme()
                    # A full Bluetooth frame takes seconds. Do not arm input
                    # for an order or UI observation that expired during delivery.
                    if not self._current_mapping(identities):
                        with self._lock:
                            self._mapping_activated_at = float("inf")
                        self.native.call("listen", enabled=False)
                        previous, last_mask, last_fresh = None, None, None
                        self.stop.wait(0.1)
                        continue
                    previous = wanted
                    last_sent = time.monotonic()
                    mask = sum((1 << i) for i, state in enumerate(wanted) if state)
                    if rearming or mask != last_mask or fresh != last_fresh or self._transport != "host_writes_accepted_unverified":
                        self.native.call("listen", enabled=fresh, assigned_mask=mask)
                    last_mask = mask
                    last_fresh = fresh
                    with self._lock:
                        self._transport = "host_writes_accepted_unverified"
                        self._last_write = time.time()
                        self._delivered_ids = identities
                        if rearming:
                            self._mapping_activated_at = time.monotonic()
                        self._transport_error = None
                        self._refresh_error()
                    self._save()
                # Re-read after a long normal frame so a superseded theme does
                # not spend another frame on the wire. Only one style per pass
                # lets fresh status and target-order changes take priority.
                self.themes.refresh()
                style = self.themes.next_style(time.monotonic())
                self._publish_theme()
                if (style is not None and brightness == self.themes.brightness(self.brightness)
                        and time.monotonic() - last_full < 24
                        and self._current_mapping(identities)):
                    frame = style.frame()
                    # A style does not extend firmware's 15-second lease. The
                    # immediately following heartbeat must fit that budget.
                    wire_budget = ((len(protocol.symbols(frame)) + len(protocol.symbols(protocol.heartbeat())))
                                   * (self.writer.high + self.writer.low) + 2 * self.writer.low)
                    if wire_budget > 13:
                        self.themes.failed("Theme timing cannot preserve the keyboard lease; use 150/100 ms")
                        self._publish_theme()
                    else:
                        self.themes.sending()
                        self._publish_theme()
                        self.writer.send(protocol.heartbeat())
                        last_sent = time.monotonic()
                        if not self._current_mapping(identities):
                            self._suspend_input()
                            previous, last_mask, last_fresh = None, None, None
                            continue
                        self.writer.send(frame)
                        self.themes.accepted(style, time.monotonic())
                        self.themes.refresh()
                        self._publish_theme()
                        if not self._current_mapping(identities):
                            self._suspend_input()
                            previous, last_mask, last_fresh = None, None, None
                            continue
                        # Do this before the next (potentially full) state frame:
                        # otherwise style + full snapshot could expire the lease.
                        self.writer.send(protocol.heartbeat())
                        last_sent = time.monotonic()
                        if not self._current_mapping(identities):
                            self._suspend_input()
                            previous, last_mask, last_fresh = None, None, None
                            continue
                        with self._lock:
                            self._last_write = time.time()
                        self._save()
                self.stop.wait(0.1)
            except InterruptedError:
                self.themes.pause()
                self._publish_theme()
                break
            except Exception as exc:
                self.themes.failed(str(exc))
                self._publish_theme()
                previous, opened = None, False
                last_mask = None
                last_fresh = None
                with self._lock:
                    self._mapping_activated_at = float("inf")
                try:
                    self.native.call("listen", enabled=False, timeout=1)
                except Exception:
                    pass
                with self._lock:
                    self._transport = "disconnected_or_error"
                    self._transport_error = str(exc)
                    self._refresh_error()
                self._save()
                self.stop.wait(2)

    def _verify_selection(self, target_id, expected, issued_at):
        # Verify fresh renderer evidence; never retry the selection mutation.
        # Each snapshot also has the RPC's absolute timeout. At most six reads.
        deadline = time.monotonic() + 2
        for _ in range(6):
            if self.stop.is_set():
                break
            value = self.orca.snapshot()
            self._navigation_snapshot(value, expected)
            captured = value.get("captured_at")
            if (isinstance(captured, (int, float)) and captured >= issued_at
                    and value.get("selected_id") == target_id):
                front = self.native.call("frontmost")
                if (front.get("bundle_id", front.get("bundleId")) == "com.stablyai.orca"
                        and front.get("is_minimized") is False):
                    return
                raise RuntimeError("Orca selection changed but the application is no longer frontmost")
            if time.monotonic() >= deadline or self.stop.wait(0.25):
                break
        raise RuntimeError("Orca accepted selection, but fresh renderer selection was not confirmed")

    def _snapshot_after_input(self, received_at, captured_after):
        # A valid but older file can still precede a mouse/Cmd+number change.
        # Await evidence sampled after this key, within the same one-second budget.
        for _ in range(21):
            if not self._event_current(received_at):
                return None
            value = self.orca.snapshot()
            if not self._event_current(received_at):
                return None
            self._navigation_snapshot(value)
            captured = value.get("captured_at")
            if not isinstance(captured, (int, float)) or not math.isfinite(captured):
                raise RuntimeError("Orca UI snapshot has no valid capture time")
            if captured >= captured_after:
                return value
            if self.stop.wait(0.05):
                return None
        return None

    def _navigate(self, slot, received_at=None):
        if received_at is None:
            received_at = time.monotonic()
        captured_after = time.time() - (time.monotonic() - received_at)
        # Check after every potentially slow read, not just before entering it.
        with self._orca_lock:
            value = self._snapshot_after_input(received_at, captured_after)
            if value is None:
                return False
            session = self.native.call("session")
            if session.get("active") is not True:
                raise RuntimeError("Navigation requires an unlocked active local session")
            if not self._event_current(received_at):
                return False
            front = self.native.call("frontmost")
            if not self._event_current(received_at):
                return False
            bundle = front.get("bundle_id", front.get("bundleId"))
            if bundle == "com.stablyai.orca":
                if type(front.get("is_minimized")) is not bool:
                    raise RuntimeError("Orca target window state could not be confirmed")
                value = {**value, "is_minimized": front["is_minimized"]}
            action = navigation_action(value, slot, bundle)
            if action["action"] == "none":
                return False
            # Native operations validate the actual session and the sole AX window.
            args = {"bundle_id": "com.stablyai.orca", "single_window": True}
            if action["action"] == "minimize":
                result = self.native.call("minimize", **args)
                if result.get("minimized") is not True:
                    raise RuntimeError("Orca window minimization was not confirmed")
            else:
                # Fail the session/window check before dispatching Orca's mutation.
                self.native.call("activate", **args)
                if not self._event_current(received_at):
                    return False
                for _ in range(20):
                    foreground = self.native.call("frontmost")
                    if not self._event_current(received_at):
                        return False
                    if (foreground.get("bundle_id", foreground.get("bundleId")) == "com.stablyai.orca"
                            and foreground.get("is_minimized") is False):
                        break
                    if self.stop.wait(0.05):
                        return False
                else:
                    raise RuntimeError("Orca activation did not become frontmost")
                issued = {}
                def before_dispatch(current):
                    self._navigation_snapshot(current, value)
                    if not self._event_current(received_at):
                        raise RuntimeError("Navigation input expired before selection")
                    if self.native.call("session").get("active") is not True:
                        raise RuntimeError("The local session became inactive before selection")
                    foreground = self.native.call("frontmost")
                    if (foreground.get("bundle_id", foreground.get("bundleId")) != "com.stablyai.orca"
                            or foreground.get("is_minimized") is not False):
                        raise RuntimeError("Orca is not confirmed frontmost and restored; selection was not dispatched")
                    if not self._event_current(received_at):
                        raise RuntimeError("Navigation input expired before selection")
                    issued["at"] = time.time()
                self.orca.select(action["target_id"], before_dispatch=before_dispatch)
                with self._lock:
                    self._last_navigation = {**action, "verified": False, "requested_at": issued["at"]}
                self._verify_selection(action["target_id"], value, issued["at"])
            with self._lock:
                self._last_navigation = {**action, "verified": True, "verified_at": time.time()}
                self._navigation_error = None
                self._refresh_error()
            self._save()
            return True

    def run(self):
        workers = [threading.Thread(target=self._poll, daemon=True),
                   threading.Thread(target=self._send, daemon=True)]
        for worker in workers:
            worker.start()
        try:
            while not self.stop.is_set():
                try:
                    event = self.native.events.get(timeout=0.5)
                except queue.Empty:
                    continue
                if event.get("event") != "slot":
                    continue
                slot = event.get("slot")
                if type(slot) is not int or not 0 <= slot < 12:
                    continue
                try:
                    self._navigate(slot, event.get("received_at"))
                except Exception as exc:
                    with self._lock:
                        self._navigation_error = f"Navigation failed: {exc}"
                        self._refresh_error()
                    self._save()
        finally:
            self.stop.set()
            with self._lock:
                self._mapping_activated_at = float("inf")
            for worker in workers:
                worker.join(timeout=6)
            try:
                self.native.call("listen", enabled=False)
                BluetoothWriter(self.native).send(protocol.disable())
            except Exception:
                pass  # Firmware lease also restores ordinary key behavior.
            with self._lock:
                self._transport = "stopped"
            self._save()
