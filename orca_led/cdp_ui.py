"""On-demand, read-only UI snapshots from Orca's exact main renderer.

Requires Orca to have been explicitly started with a loopback debugging port.
This module never launches/restarts the app or installs a renderer timer. The
only evaluated program is the fixed, version-pinned workspace selector below.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import struct
import time
from typing import Callable
from urllib.parse import urlsplit

from .orca import OrcaError, OrcaRPC, TESTED_ORCA_VERSION

DEFAULT_CDP_PORT = 19347
MAIN_RENDERER_URL = "file:///Applications/Orca.app/Contents/Resources/app.asar/out/renderer/index.html"
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_WIRE_BYTES = 8 * MAX_MESSAGE_BYTES


def _expression(runtime_id: str, window_id: int) -> str:
    expected = json.dumps({"url": MAIN_RENDERER_URL, "runtime": runtime_id,
                           "window": window_id, "version": TESTED_ORCA_VERSION})
    return "(async () => { const expected = " + expected + ";" + r"""
      if (location.href !== expected.url || document.baseURI !== expected.url ||
          globalThis !== top || typeof globalThis.api?.runtime?.getStatus !== 'function' ||
          typeof globalThis.api?.updater?.getVersion !== 'function' ||
          typeof globalThis.api?.agentStatus?.getSnapshot !== 'function') {
        throw new Error('Not the verified Orca main renderer');
      }
      const [status, appVersion] = await Promise.all([
        api.runtime.getStatus(), api.updater.getVersion()
      ]);
      if (appVersion !== expected.version || status.runtimeId !== expected.runtime ||
          status.authoritativeWindowId !== expected.window ||
          status.graphStatus !== 'ready' || status.desktopWindowStatus !== 'available') {
        throw new Error('Orca runtime or authoritative window changed');
      }
      const [activation, storeModule] = await Promise.all([
        import(new URL('./assets/worktree-activation-D6bsPnag.js', expected.url).href),
        import(new URL('./assets/store-U3p5kNAJ.js', expected.url).href)
      ]);
      if (typeof activation.d !== 'function' || typeof storeModule.t?.getState !== 'function') {
        throw new Error('Orca renderer exports changed');
      }
      const rows = activation.d();
      const state = storeModule.t.getState();
      if (!Array.isArray(rows) || rows.length > 10000 ||
          typeof state.getKnownWorktreeById !== 'function') {
        throw new Error('Orca numbered workspace order is unavailable');
      }
      const slots = rows.map(row => {
        if (typeof row?.id !== 'string' || !row.id) throw new Error('Invalid workspace ID');
        const workspace = state.getKnownWorktreeById(row.id, row.executionHostId);
        return {id: row.id,
          label: String(workspace?.displayName || workspace?.branch || workspace?.name || row.id).slice(0, 512),
          execution_host_id: row.executionHostId || 'local'};
      });
      // Only status and routing evidence leave the renderer. In particular,
      // prompts, tool input, transcript paths and assistant messages do not.
      const hookRows = await api.agentStatus.getSnapshot();
      if (!Array.isArray(hookRows) || hookRows.length > 10000) {
        throw new Error('Invalid Orca hook snapshot');
      }
      const pick = (source, keys) => Object.fromEntries(keys.filter(key =>
        Object.hasOwn(source, key)).map(key => {
          const value = source[key];
          return [key, typeof value === 'string' ? (value.length <= 4096 ? value : null) :
            typeof value === 'boolean' || typeof value === 'number' || value === null ? value : null];
        }));
      const hook_statuses = hookRows.filter(row => row && typeof row === 'object' &&
        !Array.isArray(row)).map(row => {
          const result = pick(row, ['state', 'agentType', 'paneKey', 'terminalHandle',
            'tabId', 'worktreeId', 'runId', 'executionId', 'receivedAt',
            'evidenceObservedAt', 'stateStartedAt', 'interrupted', 'sessionBoundary',
            'restoredUnconfirmed', 'providerSessionOnly', 'structuredHost']);
          if (row.mainAgent && typeof row.mainAgent === 'object') {
            result.mainAgent = pick(row.mainAgent, ['state', 'stateStartedAt', 'outcome']);
          }
          if (row.observation && typeof row.observation === 'object') {
            result.observation = pick(row.observation, ['origin', 'authorityId', 'incarnation',
              'revision', 'observedAt', 'kind', 'boundary']);
          }
          if (row.providerSession && typeof row.providerSession === 'object') {
            result.providerSession = pick(row.providerSession, ['key', 'id']);
          }
          return result;
        });
      return {schema: 1, source: 'orca-renderer', captured_at: Date.now() / 1000,
        runtime_id: status.runtimeId, app_version: appVersion,
        window_id: status.authoritativeWindowId,
        selected_id: state.activeWorktreeId || null,
        selected_host_id: state.activeWorkspaceExecutionHostId || 'local',
        active_view: state.activeView, modal_open: state.activeModal !== 'none', slots,
        hook_statuses};
    })()"""


def _fetch_targets(port: int, timeout: float) -> list:
    connection = None
    deadline = time.monotonic() + timeout
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection.settimeout(remaining)
        connection.sendall((f"GET /json/list HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                            "Connection: close\r\n\r\n").encode("ascii"))
        response = bytearray()
        def receive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection.settimeout(remaining)
            value = connection.recv(65536)
            if not value:
                raise OrcaError("cdp_closed", "Orca closed its debugging target list response.")
            response.extend(value)
            if len(response) > MAX_MESSAGE_BYTES + 16384:
                raise OrcaError("cdp_invalid_response", "The debugging target list exceeded the size limit.")
        while b"\r\n\r\n" not in response:
            receive()
            if b"\r\n\r\n" not in response and len(response) > 16384:
                raise OrcaError("cdp_invalid_response", "The debugging response headers are too large.")
        header, _, body = response.partition(b"\r\n\r\n")
        if len(header) > 16384:
            raise OrcaError("cdp_invalid_response", "The debugging response headers are too large.")
        lines = header.decode("ascii").split("\r\n")
        if lines[0].split()[:2] != ["HTTP/1.1", "200"]:
            raise OrcaError("cdp_unavailable", "Orca's local debugging endpoint did not return a target list.")
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or name.lower() in headers:
                raise ValueError("Invalid HTTP headers")
            headers[name.lower()] = value.strip()
        # Chromium's embedded DevTools HTTP server supplies a Content-Length.
        # Other HTTP transports or proxies are intentionally not supported.
        length = int(headers.get("content-length", "-1"))
        if "transfer-encoding" in headers or not 0 <= length <= MAX_MESSAGE_BYTES:
            raise OrcaError("cdp_invalid_response", "The debugging target list exceeded the size limit.")
        header_length = len(header) + 4
        while len(response) - header_length < length:
            receive()
        targets = json.loads(response[header_length:header_length + length])
        if not isinstance(targets, list):
            raise OrcaError("cdp_invalid_response", "The debugging endpoint returned an invalid target list.")
        return targets
    except (TimeoutError, socket.timeout):
        raise OrcaError("cdp_timeout", "Timed out listing Orca's debugging targets.") from None
    except (OSError, ValueError, UnicodeError):
        raise OrcaError("cdp_unavailable", "Start Orca explicitly with the project's local debugging launcher.") from None
    finally:
        if connection is not None:
            connection.close()


class _WebSocket:
    """Small RFC 6455 client for one bounded, loopback CDP request.

    No extensions, redirects, credentials, proxies, reconnects or listeners.
    Client frames are masked; server text fragmentation and ping are supported.
    """
    def __init__(self, url: str, timeout: float):
        parsed = urlsplit(url)
        self.deadline = time.monotonic() + timeout
        self.buffer = bytearray()
        self.received = 0
        self.connection = socket.create_connection(("127.0.0.1", parsed.port), timeout=timeout)
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (f"GET {parsed.path} HTTP/1.1\r\nHost: 127.0.0.1:{parsed.port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self._send(request.encode("ascii"))
            while b"\r\n\r\n" not in self.buffer:
                self._receive()
                if b"\r\n\r\n" not in self.buffer and len(self.buffer) > 16384:
                    raise OrcaError("cdp_invalid_response", "The debugging WebSocket handshake is too large.")
            header, _, remainder = self.buffer.partition(b"\r\n\r\n")
            if len(header) > 16384:
                raise OrcaError("cdp_invalid_response", "The debugging WebSocket handshake is too large.")
            self.buffer = bytearray(remainder)
            lines = header.decode("ascii").split("\r\n")
            headers = {}
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if not separator or name.lower() in headers:
                    raise ValueError("Invalid WebSocket headers")
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if (len(lines[0].split()) < 2 or lines[0].split()[:2] != ["HTTP/1.1", "101"] or
                    headers.get("upgrade", "").lower() != "websocket" or
                    "upgrade" not in {value.strip().lower() for value in headers.get("connection", "").split(",")} or
                    headers.get("sec-websocket-accept") != accept or
                    "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
                raise OrcaError("cdp_invalid_response", "The debugging WebSocket handshake was rejected.")
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return remaining

    def _send(self, value: bytes):
        self.connection.settimeout(self._remaining())
        self.connection.sendall(value)

    def _receive(self):
        self.connection.settimeout(self._remaining())
        value = self.connection.recv(65536)
        if not value:
            raise OrcaError("cdp_closed", "Orca closed its debugging connection.")
        self.received += len(value)
        if self.received > MAX_WIRE_BYTES:
            raise OrcaError("cdp_invalid_response", "The debugging response exceeded the total size limit.")
        self.buffer.extend(value)

    def _read(self, length: int) -> bytes:
        self._remaining()
        while len(self.buffer) < length:
            self._receive()
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def send(self, payload: bytes, opcode: int = 1):
        if len(payload) > MAX_MESSAGE_BYTES:
            raise OrcaError("cdp_invalid_response", "The debugging message exceeded the size limit.")
        size = len(payload)
        prefix = bytes([0x80 | opcode])
        prefix += (bytes([0x80 | size]) if size < 126 else
                   b"\xfe" + struct.pack("!H", size) if size < 65536 else
                   b"\xff" + struct.pack("!Q", size))
        mask = secrets.token_bytes(4)
        self._send(prefix + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))

    def receive_text(self) -> str:
        message = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            final, opcode = bool(first & 0x80), first & 0x0f
            if first & 0x70 or second & 0x80:
                raise OrcaError("cdp_invalid_response", "Unexpected WebSocket extension or masked server frame.")
            length = second & 0x7f
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length > MAX_MESSAGE_BYTES or len(message) + length > MAX_MESSAGE_BYTES:
                raise OrcaError("cdp_invalid_response", "The debugging message exceeded the size limit.")
            if opcode >= 8 and (not final or length > 125):
                raise OrcaError("cdp_invalid_response", "Invalid WebSocket control frame.")
            payload = self._read(length)
            if opcode == 8:
                raise OrcaError("cdp_closed", "Orca closed its debugging connection.")
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0 and not started) or (opcode == 1 and started):
                raise OrcaError("cdp_invalid_response", "Unexpected debugging WebSocket frame sequence.")
            started = True
            message.extend(payload)
            if final:
                return message.decode("utf-8")


def _evaluate(websocket_url: str, expression: str, timeout: float) -> dict:
    connection = None
    try:
        connection = _WebSocket(websocket_url, timeout)
        connection.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
            "timeout": int(timeout * 1000), "silent": True, "userGesture": False,
            "disableBreaks": True}}).encode("utf-8"))
        while True:
            frame = json.loads(connection.receive_text())
            if not isinstance(frame, dict):
                raise OrcaError("cdp_invalid_response", "The debugging endpoint returned an invalid response.")
            if "id" not in frame:
                continue
            if frame["id"] != 1 or "error" in frame or not isinstance(frame.get("result"), dict):
                raise OrcaError("cdp_evaluation_failed", "Orca could not evaluate the fixed UI snapshot reader.")
            return frame["result"]
    except (TimeoutError, socket.timeout):
        raise OrcaError("cdp_timeout", "Timed out reading Orca's main renderer.") from None
    except (OSError, ValueError, UnicodeError):
        raise OrcaError("cdp_unavailable", "Could not read Orca's local debugging connection.") from None
    finally:
        if connection is not None:
            connection.close()


class CDPUIProvider:
    """Callable compatible with OrcaClient(ui_provider=...).

    Target enumeration and evaluation callbacks are injectable for offline
    tests. Production connections are always literal IPv4 loopback addresses.
    """
    def __init__(self, port: int = DEFAULT_CDP_PORT, rpc: OrcaRPC | None = None,
                 timeout: float = 2.0, fetch_targets: Callable[[], list] | None = None,
                 evaluate: Callable[[str, str, float], dict] | None = None):
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("CDP port must be an integer in [1024, 65535]")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 5:
            raise ValueError("CDP timeout must be in (0, 5]")
        self.port, self.timeout = port, float(timeout)
        self.rpc = rpc or OrcaRPC()
        self.fetch_targets = fetch_targets or (lambda: _fetch_targets(self.port, self.timeout))
        self.evaluate = evaluate or _evaluate

    def __call__(self) -> dict:
        status = self.rpc.call("status.get")
        if status.get("appVersion") != TESTED_ORCA_VERSION:
            raise OrcaError("unsupported_version", "The UI reader needs validation for this Orca version.")
        if (status.get("graphStatus") != "ready" or status.get("desktopWindowStatus") != "available" or
                not isinstance(status.get("runtimeId"), str) or not status["runtimeId"] or
                type(status.get("authoritativeWindowId")) is not int or status["authoritativeWindowId"] <= 0):
            raise OrcaError("renderer_unavailable", "Orca's authoritative main renderer is not ready.")
        try:
            targets = self.fetch_targets()
        except (TimeoutError, socket.timeout):
            raise OrcaError("cdp_timeout", "Timed out listing Orca's debugging targets.") from None
        except OSError:
            raise OrcaError("cdp_unavailable", "Could not list Orca's local debugging targets.") from None
        if not isinstance(targets, list):
            raise OrcaError("cdp_invalid_response", "The debugging endpoint returned an invalid target list.")
        candidates = [target for target in targets if isinstance(target, dict) and
                      target.get("type") == "page" and target.get("url") == MAIN_RENDERER_URL]
        if len(candidates) != 1:
            raise OrcaError("cdp_main_target_unavailable", "Exactly one verified Orca main renderer is required.")
        target = candidates[0]
        identity, websocket_url = target.get("id"), target.get("webSocketDebuggerUrl")
        try:
            parsed = urlsplit(websocket_url) if isinstance(websocket_url, str) else None
            valid = (isinstance(identity, str) and identity and all(c.isascii() and (c.isalnum() or c in "-_") for c in identity) and
                     parsed is not None and parsed.scheme == "ws" and parsed.hostname == "127.0.0.1" and
                     parsed.port == self.port and not parsed.username and not parsed.password and
                     not parsed.query and not parsed.fragment and parsed.path == "/devtools/page/" + identity)
        except ValueError:
            valid = False
        if not valid:
            raise OrcaError("cdp_target_rejected", "The main renderer debugging target is not the expected loopback endpoint.")
        try:
            result = self.evaluate(websocket_url, _expression(status["runtimeId"], status["authoritativeWindowId"]), self.timeout)
        except (TimeoutError, socket.timeout):
            raise OrcaError("cdp_timeout", "Timed out reading Orca's main renderer.") from None
        except OSError:
            raise OrcaError("cdp_unavailable", "Could not read Orca's local debugging connection.") from None
        if not isinstance(result, dict) or result.get("exceptionDetails") is not None:
            raise OrcaError("cdp_evaluation_failed", "Orca's fixed UI snapshot reader failed its renderer checks.")
        remote = result.get("result")
        snapshot = remote.get("value") if isinstance(remote, dict) else None
        if not isinstance(snapshot, dict) or snapshot.get("source") != "orca-renderer" or not isinstance(snapshot.get("slots"), list):
            raise OrcaError("invalid_ui_snapshot", "Orca did not return a valid renderer UI snapshot.")
        if snapshot.get("app_version") != TESTED_ORCA_VERSION:
            raise OrcaError("unsupported_version", "The UI snapshot came from an unsupported app version.")
        if snapshot.get("runtime_id") != status["runtimeId"]:
            raise OrcaError("runtime_changed", "Orca restarted while reading its UI snapshot.")
        if snapshot.get("window_id") != status["authoritativeWindowId"]:
            raise OrcaError("window_mismatch", "The UI snapshot does not match Orca's authoritative window.")
        return snapshot
