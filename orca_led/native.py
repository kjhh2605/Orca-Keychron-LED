"""Bounded JSONL RPC to the native helper; asynchronous slot events are separate."""
import json
import queue
import subprocess
import threading
import time
from pathlib import Path


class NativeError(RuntimeError):
    pass


class NativeClient:
    def __init__(self, executable=None):
        base = Path(__file__).resolve().parent.parent
        app_binary = base / "Orca LED.app" / "Contents" / "MacOS" / "OrcaLED"
        executable = executable or (app_binary if app_binary.exists() else base / "build" / "orca-keychron-helper")
        self.process = subprocess.Popen([str(executable), "--serve"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=None, text=True, bufsize=1,
                                        start_new_session=True)
        self.events = queue.Queue(maxsize=64)
        self._pending = {}
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    response = json.loads(line)
                    if not isinstance(response, dict):
                        continue
                except (ValueError, TypeError):
                    continue
                if "event" in response:
                    response["received_at"] = time.monotonic()
                    try:
                        self.events.put_nowait(response)
                    except queue.Full:
                        # Keep recent input, not the first minute of a stalled queue.
                        try:
                            self.events.get_nowait()
                            self.events.put_nowait(response)
                        except (queue.Empty, queue.Full):
                            pass
                else:
                    with self._lock:
                        waiter = self._pending.get(response.get("id"))
                    if waiter:
                        waiter.put(response)
        finally:
            with self._lock:
                self._closed = True
                for waiter in self._pending.values():
                    waiter.put({"ok": False, "error": {"message": "Native helper stopped"}})

    def call(self, op, timeout=5, **args):
        waiter = queue.Queue()
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise NativeError("Native helper is not running")
            self._sequence += 1
            request_id = str(self._sequence)
            self._pending[request_id] = waiter
            try:
                self.process.stdin.write(json.dumps({"id": request_id, "op": op, **args}) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._pending.pop(request_id, None)
                raise NativeError(str(exc)) from exc
        try:
            response = waiter.get(timeout=timeout)
            if not response.get("ok"):
                raise NativeError(str(response.get("error", "Unknown native error")))
            return response.get("result", {})
        except queue.Empty as exc:
            raise NativeError(f"Native operation timed out: {op}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


class FramePreempted(Exception):
    """A redundant frame was aborted so newer state can go out first."""


class BluetoothWriter:
    """Serial symbol transmission. Success means host accepted writes, not an ACK."""
    def __init__(self, native, high_ms=150, low_ms=100, stop=None, symbol_method="transaction"):
        if high_ms < 40 or low_ms < 30:
            raise ValueError("Unvalidated timing: high>=40ms, low>=30ms required")
        self.native = native
        self.high = high_ms / 1000
        self.low = low_ms / 1000
        self.stop = stop or threading.Event()
        self.symbol_options = {"method": symbol_method} if symbol_method is not None else {}

    def _symbol(self, value, **kwargs):
        return self.native.call("symbol", value=value, **self.symbol_options, **kwargs)

    def _wait(self, seconds):
        if self.stop.wait(seconds):
            raise InterruptedError("Transmission stopped")

    def send(self, frame, abort=None):
        """`abort` is polled between symbols; a true result sends ABORT and
        raises FramePreempted. Use it only for frames that repeat known state."""
        from .protocol import symbols
        try:
            self._symbol(0)
            self._wait(self.low)
            for index, value in enumerate(symbols(frame)):
                if abort is not None and abort():
                    if index:
                        self._symbol(5)
                        self._wait(self.high)
                        self._symbol(0)
                        self._wait(self.low)
                    raise FramePreempted()
                self._symbol(value)
                self._wait(self.high)
                self._symbol(0)
                self._wait(self.low)
        finally:
            # A failing connection must never leave the host command field asserted.
            try:
                self._symbol(0, timeout=1)
            except NativeError:
                pass
