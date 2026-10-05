import argparse
from contextlib import ExitStack
import json
import signal
import sys
import threading
import time
from pathlib import Path
from . import protocol
from .controller import Controller, atomic_json
from .native import NativeClient, BluetoothWriter
from .lock import DeviceLock


def ui_provider(args):
    if args.cdp_port is not None:
        from .cdp_ui import CDPUIProvider
        return CDPUIProvider(port=args.cdp_port)
    from .orca import LocalUIFileProvider
    return LocalUIFileProvider(args.ui_snapshot)


def main():
    parser = argparse.ArgumentParser(description="Orca × Keychron K10 Pro")
    parser.add_argument("--helper", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="Read-only keyboard and permission diagnostics")
    sub.add_parser("verify-usb", help="Query the custom firmware identity over USB")
    verify_theme = sub.add_parser("verify-theme", help="Query USB theme support and save a local capability receipt")
    verify_theme.add_argument("--receipt-file", type=Path, default=Path("runtime/theme-firmware.json"))
    usb_demo = sub.add_parser("usb-demo", help="Send and read back a test map on verified custom USB firmware")
    usb_demo.add_argument("--brightness", type=int, default=72)
    orca_status = sub.add_parser("orca-status", help="Read actual Orca integration diagnostics")
    orca_status.add_argument("--ui-snapshot", type=Path, default=Path("runtime/orca-ui.json"))
    orca_status.add_argument("--cdp-port", type=int, help="Use an explicitly enabled local Orca debugging connection")
    demo = sub.add_parser("demo", help="Encode or send a 12-key LED test frame")
    demo.add_argument("--write", action="store_true", help="Send to an already flashed keyboard")
    demo.add_argument("--firmware-installed", action="store_true")
    demo.add_argument("--brightness", type=int, default=72)
    demo.add_argument("--hold-seconds", type=int, default=0, help="Maintain the visual test for up to 600 seconds, then restore lighting")
    demo.add_argument("--high-ms", type=int, default=150)
    demo.add_argument("--low-ms", type=int, default=100)
    demo.add_argument("--symbol-method", choices=["individual", "multiple", "transaction"], default="transaction")
    run = sub.add_parser("run", help="Run foreground bridge; Ctrl+C to stop")
    run.add_argument("--firmware-installed", action="store_true")
    run.add_argument("--ui-snapshot", type=Path, default=Path("runtime/orca-ui.json"))
    run.add_argument("--cdp-port", type=int, help="Use an explicitly enabled local Orca debugging connection")
    run.add_argument("--status-file", type=Path, default=Path("runtime/status.json"))
    run.add_argument("--theme-file", type=Path, help="Theme JSON; default: theme.json beside the status file")
    run.add_argument("--brightness", type=int, default=72)
    run.add_argument("--high-ms", type=int, default=120)
    run.add_argument("--low-ms", type=int, default=80)
    args = parser.parse_args()
    if args.command == "verify-theme" and args.helper is None:
        parser.error("verify-theme requires --helper pointing to the freshly built helper with theme query support")
    if args.command == "demo" and not 0 <= args.hold_seconds <= 600:
        parser.error("--hold-seconds must be between 0 and 600")
    if args.command == "demo" and not args.write:
        data = protocol.snapshot([2,3,4,5,1,6,2,3,4,5,1,0], args.brightness)
        print(json.dumps({"mode":"dry_run", "packet":list(data), "symbols":protocol.symbols(data),
                          "estimated_wire_seconds":(len(protocol.symbols(data))*(args.high_ms+args.low_ms)+args.low_ms)/1000}, indent=2))
        return 0
    if args.command in ("run", "demo") and not args.firmware_installed:
        parser.error("Install and verify this project's firmware first; then use --firmware-installed. Stock firmware is unsupported.")
    if args.command == "orca-status":
        from .orca import OrcaClient
        print(json.dumps(OrcaClient(ui_provider=ui_provider(args)).snapshot(), ensure_ascii=False, indent=2))
        return 0
    stack = ExitStack()
    try:
        if args.command in ("run", "demo", "usb-demo", "verify-usb", "verify-theme"):
            stack.enter_context(DeviceLock())
        native = NativeClient(args.helper)
        stack.callback(native.close)
        if args.command == "devices":
            print(json.dumps({"devices":native.call("devices"),"permissions":native.call("permissions"),"session":native.call("session")}, ensure_ascii=False, indent=2))
        elif args.command in ("verify-usb", "usb-demo", "verify-theme"):
            devices = native.call("devices")["devices"]
            targets = [d for d in devices if d.get("vendor_id") == 0x3434 and d.get("product_id") == 0x02A0
                       and d.get("transport") == "USB" and d.get("usage_page") == 0xFF60]
            if len(targets) != 1:
                raise RuntimeError("Connect exactly one K10 Pro ANSI RGB by USB in Cable mode.")
            native.call("open", product_id=0x02A0, transport="USB", registry_id=targets[0]["registry_id"])
            result = native.call("query")
            if args.command == "verify-theme":
                if (result.get("firmware_verified") is not True or type(result.get("version")) is not int
                        or result["version"] != 1 or result.get("transport") != "USB"):
                    raise RuntimeError("The USB reply did not verify this project's OL v1 firmware.")
                if type(result.get("theme_protocol")) is not int or result["theme_protocol"] not in (1, 2):
                    # Clear an earlier success after a verified old/unsupported
                    # firmware reply, rather than leaving misleading capability.
                    atomic_json(args.receipt_file, {"schema": 1, "theme_protocol": 0,
                        "verified_via": "usb_query", "verified_at": time.time(),
                        "vendor_id": 0x3434, "product_id": 0x02A0})
                    raise RuntimeError("This keyboard did not confirm theme protocol 1 or 2. Use the theme firmware and a newly built helper.")
                atomic_json(args.receipt_file, {"schema": 1, "theme_protocol": result["theme_protocol"],
                    "verified_via": "usb_query", "verified_at": time.time(),
                    "vendor_id": 0x3434, "product_id": 0x02A0})
                result["receipt_file"] = str(args.receipt_file)
            if args.command == "usb-demo":
                desired = [2,3,4,5,1,6,2,3,4,5,1,0]
                native.call("raw", bytes=protocol.raw_report(protocol.snapshot(desired, args.brightness)))
                result = native.call("query")
                if result.get("states") != desired or not result.get("mode_active"):
                    raise RuntimeError("USB write was not confirmed by the keyboard's state reply.")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "demo":
            native.call("open", product_id=0x02A0, transport="Bluetooth")
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            writer = BluetoothWriter(native, args.high_ms, args.low_ms, stop, args.symbol_method)
            try:
                started = time.monotonic()
                writer.send(protocol.snapshot([2,3,4,5,1,6,2,3,4,5,1,0], args.brightness))
                print(json.dumps({"host_writes_accepted": True, "device_acknowledged": False,
                                  "full_frame_seconds": round(time.monotonic()-started, 3),
                                  "hold_seconds": args.hold_seconds}), flush=True)
                deadline = time.monotonic() + args.hold_seconds
                while args.hold_seconds and time.monotonic() < deadline:
                    if stop.wait(min(5, max(0, deadline-time.monotonic()))):
                        break
                    if time.monotonic() < deadline:
                        writer.send(protocol.heartbeat())
            except InterruptedError:
                pass
            finally:
                if args.hold_seconds or stop.is_set():
                    BluetoothWriter(native, args.high_ms, args.low_ms, symbol_method=args.symbol_method).send(protocol.disable())
        elif args.command == "run":
            from .orca import OrcaClient
            permissions = native.call("permissions")
            if not permissions.get("input_monitoring") or not permissions.get("accessibility"):
                raise RuntimeError("Grant Input Monitoring and Accessibility to the native helper in macOS settings before running the bridge.")
            orca = OrcaClient(ui_provider=ui_provider(args))
            initial = orca.snapshot()
            if not initial.get("supported"):
                raise RuntimeError(initial.get("reason", "Exact Orca sidebar/selection provider is required"))
            controller = Controller(orca, native, args.status_file, args.brightness, args.high_ms, args.low_ms, args.theme_file)
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: controller.stop.set())
            controller.run()
        return 0
    finally:
        stack.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Orca LED: {exc}", file=sys.stderr)
        sys.exit(1)
