"""CDP target and renderer identity checks using injected, in-memory transports."""
import base64
from copy import deepcopy
import hashlib
import json
import shutil
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch

from orca_led.cdp_ui import CDPUIProvider, MAX_MESSAGE_BYTES, _WebSocket, _evaluate, _fetch_targets, _expression
from orca_led.orca import OrcaError


PORT = 19347
MAIN_URL = "file:///Applications/Orca.app/Contents/Resources/app.asar/out/renderer/index.html"
MAIN_ID = "main-renderer-1"
MAIN_WS = f"ws://127.0.0.1:{PORT}/devtools/page/{MAIN_ID}"


def target(**changes):
    return {"id": MAIN_ID, "type": "page", "url": MAIN_URL,
            "webSocketDebuggerUrl": MAIN_WS, **changes}


def snapshot(**changes):
    return {"schema": 1, "source": "orca-renderer", "app_version": "1.4.216",
            "runtime_id": "runtime1", "window_id": 1, "producer_id": "producer1",
            "sequence": 17, "captured_at": 1800000000.0,
            "active_view": "terminal", "modal_open": False,
            "selected_id": "repo::/z", "selected_host_id": "local",
            "slots": [{"id": "repo::/z", "label": "Z", "execution_host_id": "local"},
                      {"id": "folder:standalone", "label": "Folder", "execution_host_id": "local"},
                      {"id": "repo::/a", "label": "A", "execution_host_id": "local"}],
            **changes}


class FakeRPC:
    def __init__(self, **changes):
        self.status = {"runtimeId": "runtime1", "appVersion": "1.4.216",
                       "authoritativeWindowId": 1, "graphStatus": "ready",
                       "desktopWindowStatus": "available", **changes}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method != "status.get":
            raise AssertionError(f"Unexpected runtime operation: {method}")
        return deepcopy(self.status)


class RendererProgramTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the renderer contract regression")
    def test_preload_runtime_status_without_version_uses_separate_version_api(self):
        # The installed preload's getStatus() omits appVersion, even though the
        # Unix socket's status.get includes it. Execute the actual reader against
        # that observed contract so a Python-only response mock cannot hide it.
        harness = r"""
const {createContext, Script, SyntheticModule} = require('node:vm');
const {readFileSync} = require('node:fs');
const expression = JSON.parse(readFileSync(0, 'utf8'));
const url = 'file:///Applications/Orca.app/Contents/Resources/app.asar/out/renderer/index.html';
const context = createContext({URL, location:{href:url}, document:{baseURI:url}, api:{
  runtime:{getStatus:async()=>({runtimeId:'runtime1', authoritativeWindowId:1,
    graphStatus:'ready', desktopWindowStatus:'available'})},
  updater:{getVersion:async()=>'1.4.216'},
  agentStatus:{getSnapshot:async()=>[{
    state:'done', agentType:'codex', paneKey:'parent:leaf', terminalHandle:'terminal1',
    tabId:'parent', worktreeId:'repo::/a', receivedAt:1799999999975,
    evidenceObservedAt:1799999999975, mainAgent:{state:'done', stateStartedAt:1799999999975},
    observation:{origin:'hook',authorityId:'main-agent-hooks:one',incarnation:3,revision:60,
      observedAt:1799999999975,kind:'transition'},
    prompt:'PRIVATE_PROMPT', lastAssistantMessage:'PRIVATE_REPLY', toolInput:{text:'PRIVATE_TOOL'},
    providerSession:{key:'session_id',id:'session1',transcriptPath:'/PRIVATE_TRANSCRIPT'}
  }, {mainAgent:{state:'done',outcome:{invalid:'PRIVATE_INVALID'}}}]}
}});
new Script('globalThis.top=globalThis').runInContext(context);
const ordered = [{id:'repo::/z'}, {id:'repo::/a'}];
const modules = {
  'worktree-activation-D6bsPnag.js':new SyntheticModule(['d'], function(){
    this.setExport('d', ()=>ordered);
  }, {context}),
  'store-U3p5kNAJ.js':new SyntheticModule(['t'], function(){
    this.setExport('t', {getState:()=>({activeWorktreeId:'repo::/a',
      activeWorkspaceExecutionHostId:'local', activeView:'terminal', activeModal:'none',
      getKnownWorktreeById:(id)=>({displayName:id.endsWith('/a')?'Agent A':'Agent Z'})})});
  }, {context})
};
(async()=>{
  for(const module of Object.values(modules)){await module.link(()=>{});await module.evaluate();}
  const program = new Script(expression, {importModuleDynamically:async(specifier)=>{
    const name=specifier.split('/').pop();
    if(!modules[name])throw new Error('Unexpected module import');
    return modules[name];
  }});
  const snapshot=await program.runInContext(context,{timeout:1000});
  process.stdout.write(JSON.stringify(snapshot));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1});
"""
        result = subprocess.run([shutil.which("node"), "--experimental-vm-modules", "-e", harness],
                                input=json.dumps(_expression("runtime1", 1)), text=True,
                                capture_output=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["app_version"], "1.4.216")
        self.assertEqual(value["runtime_id"], "runtime1")
        self.assertEqual(value["window_id"], 1)
        self.assertEqual([row["id"] for row in value["slots"]], ["repo::/z", "repo::/a"])
        self.assertEqual(value["selected_id"], "repo::/a")
        self.assertFalse(value["modal_open"])
        raw = value["hook_statuses"][0]
        self.assertEqual(raw["mainAgent"], {"state": "done", "stateStartedAt": 1799999999975})
        self.assertEqual(raw["observation"]["origin"], "hook")
        self.assertEqual(raw["providerSession"], {"key": "session_id", "id": "session1"})
        self.assertEqual(raw["evidenceObservedAt"], 1799999999975)
        self.assertNotIn("PRIVATE", json.dumps(value))
        # A malformed explicit outcome must remain present and invalid, never
        # silently become the missing-outcome form accepted for Codex Stop.
        self.assertIsNone(value["hook_statuses"][1]["mainAgent"]["outcome"])


class CDPUIProviderTest(unittest.TestCase):
    def make_provider(self, targets=None, ui=None, rpc=None, response=None):
        fetch = Mock(return_value=[target()] if targets is None else targets)
        evaluator = Mock(return_value=(
            {"result": {"type": "object", "value": snapshot() if ui is None else ui}}
            if response is None else response))
        runtime = rpc if rpc is not None else FakeRPC()
        provider = CDPUIProvider(port=PORT, rpc=runtime, timeout=1.25,
                                 fetch_targets=fetch, evaluate=evaluator)
        return provider, runtime, fetch, evaluator

    def test_exact_main_renderer_is_evaluated_and_snapshot_preserved(self):
        ui = snapshot()
        provider, rpc, fetch, evaluate = self.make_provider(ui=ui)

        self.assertEqual(provider(), ui)
        fetch.assert_called_once_with()
        evaluate.assert_called_once()
        ws_url, expression, timeout = evaluate.call_args.args
        self.assertEqual(ws_url, MAIN_WS)
        self.assertIsInstance(expression, str)
        self.assertTrue(expression.strip())
        self.assertEqual(timeout, 1.25)
        self.assertTrue(rpc.calls)
        self.assertTrue(all(method == "status.get" for method, _ in rpc.calls))

    def test_guest_and_devtools_targets_do_not_displace_main_renderer(self):
        guests = [
            target(id="review-guest", url="file:///tmp/review.html",
                   webSocketDebuggerUrl=f"ws://127.0.0.1:{PORT}/devtools/page/review-guest"),
            target(id="webview", type="webview"),
            target(id="devtools", url="devtools://devtools/bundled/inspector.html"),
            target(id="browser", type="browser", url=""),
        ]
        provider, _, _, evaluate = self.make_provider(targets=guests + [target()])

        self.assertEqual(provider()["runtime_id"], "runtime1")
        self.assertEqual(evaluate.call_args.args[0], MAIN_WS)

    def test_guest_only_or_near_match_url_is_rejected_before_evaluation(self):
        rejected = [
            [],
            [target(url="file:///tmp/review.html")],
            [target(type="webview")],
            [target(type="iframe")],
            [target(url=MAIN_URL + "?guest=1")],
            [target(url=MAIN_URL + "#settings")],
            [target(url=MAIN_URL.replace("Orca.app", "Orca Copy.app"))],
            [target(url="http://127.0.0.1:5173/")],
        ]
        for targets in rejected:
            with self.subTest(targets=targets):
                provider, _, _, evaluate = self.make_provider(targets=targets)
                with self.assertRaises(OrcaError):
                    provider()
                evaluate.assert_not_called()

    def test_duplicate_main_targets_are_rejected_before_evaluation(self):
        for duplicate in [target(), target(id="main-renderer-2",
                                           webSocketDebuggerUrl=f"ws://127.0.0.1:{PORT}/devtools/page/main-renderer-2")]:
            with self.subTest(duplicate=duplicate):
                provider, _, _, evaluate = self.make_provider(targets=[target(), duplicate])
                with self.assertRaises(OrcaError):
                    provider()
                evaluate.assert_not_called()

    def test_websocket_must_match_loopback_port_and_exact_target_path(self):
        urls = [
            f"ws://example.com:{PORT}/devtools/page/{MAIN_ID}",
            f"ws://127.0.0.2:{PORT}/devtools/page/{MAIN_ID}",
            f"ws://localhost:{PORT}/devtools/page/{MAIN_ID}",
            f"ws://127.0.0.1:{PORT + 1}/devtools/page/{MAIN_ID}",
            f"wss://127.0.0.1:{PORT}/devtools/page/{MAIN_ID}",
            f"http://127.0.0.1:{PORT}/devtools/page/{MAIN_ID}",
            f"ws://user@127.0.0.1:{PORT}/devtools/page/{MAIN_ID}",
            f"ws://127.0.0.1:{PORT}/devtools/browser/{MAIN_ID}",
            f"ws://127.0.0.1:{PORT}/devtools/page/another-target",
            MAIN_WS + "/", MAIN_WS + "?extra=1", MAIN_WS + "#extra", "", None,
        ]
        for url in urls:
            with self.subTest(url=url):
                provider, _, _, evaluate = self.make_provider(
                    targets=[target(webSocketDebuggerUrl=url)])
                with self.assertRaises(OrcaError):
                    provider()
                evaluate.assert_not_called()

    def test_invalid_ports_are_rejected_without_transport_use(self):
        for port in [0, -1, 65536, True, None, "19347", 19347.5]:
            with self.subTest(port=port):
                fetch, evaluate = Mock(), Mock()
                with self.assertRaises((ValueError, OrcaError)):
                    provider = CDPUIProvider(port=port, rpc=FakeRPC(),
                                             fetch_targets=fetch, evaluate=evaluate)
                    provider()
                fetch.assert_not_called()
                evaluate.assert_not_called()

    def test_unsupported_or_not_ready_runtime_prevents_evaluation(self):
        for changes in [{"appVersion": "1.4.217"}, {"graphStatus": "initializing"},
                        {"desktopWindowStatus": "unavailable"}]:
            with self.subTest(changes=changes):
                provider, _, _, evaluate = self.make_provider(rpc=FakeRPC(**changes))
                with self.assertRaises(OrcaError):
                    provider()
                evaluate.assert_not_called()

    def test_renderer_identity_must_match_current_runtime_window_and_version(self):
        for changes in [{"runtime_id": "previous-runtime"}, {"window_id": 2},
                        {"app_version": "1.4.215"}]:
            with self.subTest(changes=changes):
                provider, _, _, _ = self.make_provider(ui=snapshot(**changes))
                with self.assertRaises(OrcaError):
                    provider()

    def test_target_fetch_failure_is_reported_as_orca_error(self):
        for error in [TimeoutError("target fetch expired"), OSError("connection unavailable")]:
            with self.subTest(error=type(error).__name__):
                provider, _, fetch, evaluate = self.make_provider()
                fetch.side_effect = error
                with self.assertRaises(OrcaError):
                    provider()
                evaluate.assert_not_called()

    def test_evaluation_transport_failure_is_reported_as_orca_error(self):
        for error in [TimeoutError("evaluation expired"), OSError("socket closed")]:
            with self.subTest(error=type(error).__name__):
                provider, _, _, evaluate = self.make_provider()
                evaluate.side_effect = error
                with self.assertRaises(OrcaError):
                    provider()

    def test_cdp_exception_or_missing_object_value_is_rejected(self):
        responses = [
            {"exceptionDetails": {"text": "Uncaught exception"},
             "result": {"type": "object", "value": snapshot()}},
            {"error": {"code": -32000, "message": "Context destroyed"}},
            {"result": {"type": "undefined"}},
            {"result": {"type": "object", "subtype": "null", "value": None}},
            {"result": {"type": "string", "value": "not a UI snapshot"}},
            {},
        ]
        for response in responses:
            with self.subTest(response=response):
                provider, _, _, _ = self.make_provider(response=response)
                with self.assertRaises(OrcaError):
                    provider()

    def test_renderer_slot_order_and_sequence_are_not_rebuilt(self):
        rows = [{"id": f"repo::/{index:02}", "label": f"Workspace {index}"}
                for index in [12, 3, 10, 1, 8, 4, 14, 6, 2, 9, 7, 5, 13, 11]]
        ui = snapshot(slots=rows, sequence=904, selected_id=rows[4]["id"])
        before = deepcopy(ui)
        provider, _, _, _ = self.make_provider(ui=ui)

        result = provider()

        self.assertEqual(result["slots"], rows)
        self.assertEqual(result["sequence"], 904)
        self.assertEqual(result["selected_id"], rows[4]["id"])
        self.assertEqual(ui, before)


def server_frame(payload, opcode=1, final=True):
    size = len(payload)
    prefix = bytes([(0x80 if final else 0) | opcode])
    if size < 126:
        prefix += bytes([size])
    elif size < 65536:
        prefix += b"\x7e" + struct.pack("!H", size)
    else:
        prefix += b"\x7f" + struct.pack("!Q", size)
    return prefix + payload


def decode_client_frame(frame):
    """Decode the peer-visible frame to check masking and payload independently."""
    first, second = frame[:2]
    length, offset = second & 0x7f, 2
    if length == 126:
        length, offset = struct.unpack("!H", frame[2:4])[0], 4
    elif length == 127:
        length, offset = struct.unpack("!Q", frame[2:10])[0], 10
    mask = frame[offset:offset + 4]
    payload = frame[offset + 4:]
    if not second & 0x80 or len(mask) != 4 or len(payload) != length:
        raise AssertionError("Client frame must be masked and have its declared length")
    return first, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


class FakeWebSocketPeer:
    """A socket-shaped byte stream; never creates a connection or listener."""
    def __init__(self, frames=(), accept_valid=True):
        self.incoming = list(frames)
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.accept_valid = accept_valid
        self.receives = 0

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, payload):
        self.sent.append(payload)
        if not payload.startswith(b"GET "):
            return
        headers = {}
        for line in payload.decode("ascii").split("\r\n")[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.lower()] = value.strip()
        key = headers["sec-websocket-key"]
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii") if self.accept_valid else "invalid-accept"
        handshake = ("HTTP/1.1 101 Switching Protocols\r\n"
                     "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii")
        self.incoming.insert(0, handshake)

    def recv(self, length):
        self.receives += 1
        if not self.incoming:
            return b""
        value = self.incoming.pop(0)
        if len(value) > length:
            self.incoming.insert(0, value[length:])
        return value[:length]

    def close(self):
        self.closed = True


class WebSocketTransportTest(unittest.TestCase):
    def connect(self, peer):
        connection = patch("orca_led.cdp_ui.socket.create_connection", return_value=peer)
        self.addCleanup(connection.stop)
        creator = connection.start()
        websocket = _WebSocket(MAIN_WS, 1.25)
        self.addCleanup(websocket.close)
        creator.assert_called_once_with(("127.0.0.1", PORT), timeout=1.25)
        return websocket

    def test_handshake_key_and_masked_client_text_round_trip(self):
        peer = FakeWebSocketPeer()
        websocket = self.connect(peer)
        self.assertIn(f"GET /devtools/page/{MAIN_ID} HTTP/1.1\r\n".encode(), peer.sent[0])
        for payload in [b"small", b"x" * 126, b"y" * 65536]:
            with self.subTest(length=len(payload)):
                websocket.send(payload)
                first, decoded = decode_client_frame(peer.sent[-1])
                self.assertEqual(first, 0x81)
                self.assertEqual(decoded, payload)

    def test_fragmented_unicode_text_allows_interleaved_ping_and_masks_pong(self):
        payload = "에이전트 작업 상태".encode("utf-8")
        wire = (server_frame(payload[:2], final=False) +
                server_frame(b"still-here", opcode=9) +
                server_frame(payload[2:], opcode=0))
        # Split frame headers and a UTF-8 character across separate socket reads.
        peer = FakeWebSocketPeer([wire[:1], wire[1:4], wire[4:9], wire[9:]])
        websocket = self.connect(peer)

        self.assertEqual(websocket.receive_text(), payload.decode("utf-8"))
        self.assertEqual(decode_client_frame(peer.sent[-1]), (0x8a, b"still-here"))

    def test_masked_server_frame_is_rejected(self):
        peer = FakeWebSocketPeer([b"\x81\x80"])
        websocket = self.connect(peer)
        with self.assertRaises(OrcaError):
            websocket.receive_text()

    def test_oversized_advertised_payload_is_rejected_without_reading_body(self):
        peer = FakeWebSocketPeer([b"\x81\x7f" + struct.pack("!Q", MAX_MESSAGE_BYTES + 1)])
        websocket = self.connect(peer)
        with self.assertRaises(OrcaError):
            websocket.receive_text()
        self.assertEqual(peer.receives, 2)  # Handshake and header; no payload allocation/read.

    def test_fragmented_total_size_is_bounded(self):
        peer = FakeWebSocketPeer([server_frame(b"12345", final=False),
                                  server_frame(b"6789", opcode=0)])
        websocket = self.connect(peer)
        with patch("orca_led.cdp_ui.MAX_MESSAGE_BYTES", 8):
            with self.assertRaises(OrcaError):
                websocket.receive_text()

    def test_invalid_handshake_accept_closes_socket(self):
        peer = FakeWebSocketPeer(accept_valid=False)
        with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer):
            with self.assertRaises(OrcaError):
                _WebSocket(MAIN_WS, 1.25)
        self.assertTrue(peer.closed)

    def test_invalid_control_and_fragment_sequences_are_rejected(self):
        wires = [server_frame(b"bad-ping", opcode=9, final=False),
                 server_frame(b"x" * 126, opcode=9),
                 server_frame(b"orphan", opcode=0),
                 server_frame(b"first", final=False) + server_frame(b"second"),
                 server_frame(b"binary", opcode=2)]
        for wire in wires:
            with self.subTest(wire=wire[:16]):
                peer = FakeWebSocketPeer([wire])
                with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer):
                    websocket = _WebSocket(MAIN_WS, 1.25)
                    try:
                        with self.assertRaises(OrcaError):
                            websocket.receive_text()
                    finally:
                        websocket.close()

    def test_cdp_evaluation_ignores_events_returns_response_and_closes_socket(self):
        result = {"result": {"type": "object", "value": snapshot()}}
        peer = FakeWebSocketPeer([
            server_frame(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}).encode()),
            server_frame(json.dumps({"id": 1, "result": result}).encode()),
        ])
        with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer):
            self.assertEqual(_evaluate(MAIN_WS, "fixed-reader-expression", 1.25), result)
        first, request_bytes = decode_client_frame(peer.sent[1])
        request = json.loads(request_bytes)
        self.assertEqual(first, 0x81)
        self.assertEqual(request["method"], "Runtime.evaluate")
        self.assertEqual(request["params"]["expression"], "fixed-reader-expression")
        self.assertTrue(request["params"]["awaitPromise"])
        self.assertTrue(request["params"]["returnByValue"])
        self.assertFalse(request["params"]["userGesture"])
        self.assertTrue(peer.closed)


class TargetListHTTPTransportTest(unittest.TestCase):
    def test_split_headers_and_body_return_content_length_json_and_close(self):
        targets = [target(), target(id="guest", url="file:///tmp/review.html")]
        body = json.dumps(targets).encode("utf-8")
        header = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                  "Content-Type: application/json\r\n\r\n").encode("ascii")
        peer = Mock()
        peer.recv.side_effect = [header[:13], header[13:-2], header[-2:] + body[:7],
                                 body[7:29], body[29:]]
        with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer) as connect:
            self.assertEqual(_fetch_targets(PORT, 1.25), targets)
        connect.assert_called_once_with(("127.0.0.1", PORT), timeout=1.25)
        peer.sendall.assert_called_once_with(
            (f"GET /json/list HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
             "Connection: close\r\n\r\n").encode("ascii"))
        peer.close.assert_called_once_with()

    def test_redirect_oversize_and_missing_length_are_rejected_without_body_reads(self):
        headers = [
            b"HTTP/1.1 302 Found\r\nLocation: http://example.com/\r\nContent-Length: 0\r\n\r\n",
            f"HTTP/1.1 200 OK\r\nContent-Length: {MAX_MESSAGE_BYTES + 1}\r\n\r\n".encode("ascii"),
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n",
        ]
        for header in headers:
            with self.subTest(header=header):
                peer = Mock()
                peer.recv.return_value = header
                with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer) as connect:
                    with self.assertRaises(OrcaError):
                        _fetch_targets(PORT, 1.25)
                connect.assert_called_once()
                peer.recv.assert_called_once()
                peer.close.assert_called_once_with()

    def test_socket_timeout_is_mapped_and_connection_closed(self):
        for operation in ["sendall", "recv"]:
            with self.subTest(operation=operation):
                peer = Mock()
                getattr(peer, operation).side_effect = TimeoutError("socket operation expired")
                with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer):
                    with self.assertRaises(OrcaError) as error:
                        _fetch_targets(PORT, 1.25)
                self.assertEqual(error.exception.code, "cdp_timeout")
                peer.close.assert_called_once_with()

    def test_slow_partial_header_cannot_extend_absolute_deadline(self):
        peer = Mock()
        peer.recv.return_value = b"HTTP/1.1 "
        with patch("orca_led.cdp_ui.socket.create_connection", return_value=peer), \
                patch("orca_led.cdp_ui.time.monotonic", side_effect=[10.0, 10.05, 10.1, 11.1]):
            with self.assertRaises(OrcaError) as error:
                _fetch_targets(PORT, 1.0)
        self.assertEqual(error.exception.code, "cdp_timeout")
        peer.recv.assert_called_once()
        peer.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
