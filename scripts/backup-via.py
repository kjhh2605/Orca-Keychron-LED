#!/usr/bin/env python3
"""Read-only VIA backup for K10 Pro ANSI RGB. Never resets, writes settings or enters DFU."""
from pathlib import Path
from datetime import datetime, timezone
import argparse, hashlib, json, os, select, subprocess, sys, time
ROOT = Path(__file__).resolve().parent.parent
class HelperError(RuntimeError):
    def __init__(self, error):
        self.code = error.get("code", "unknown")
        super().__init__(f"{self.code}: {error.get('message','')}")
class Helper:
    def __init__(self, path):
        self.process = subprocess.Popen([str(path), "--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1)
        self.sequence = 0
        self.transcript = []
    def request(self, op, **fields):
        self.sequence += 1
        request = {"id": self.sequence, "op": op, **fields}
        self.process.stdin.write(json.dumps(request)+"\n"); self.process.stdin.flush()
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if not select.select([self.process.stdout], [], [], max(0, deadline-time.monotonic()))[0]: break
            line = self.process.stdout.readline()
            if not line: raise RuntimeError("Native helper exited")
            response = json.loads(line)
            if response.get("id") != self.sequence: continue
            self.transcript.append({"request": request, "response": response})
            if not response.get("ok"): raise HelperError(response.get("error", {}))
            return response["result"]
        raise RuntimeError(f"Helper timeout: {op}")
    def read(self, *prefix):
        if len(prefix) > 32: raise ValueError("Report too long")
        packet = list(prefix)+[0]*(32-len(prefix))
        result = self.request("via-read", bytes=packet)
        reply = result.get("bytes")
        if not isinstance(reply, list) or len(reply) != 32 or any(type(v) is not int or not 0 <= v <= 255 for v in reply): raise RuntimeError("Malformed helper VIA reply")
        if reply[0] != prefix[0]: raise RuntimeError("VIA command response mismatch")
        if prefix[0] in (0x0e, 0x12) and reply[:4] != packet[:4]: raise RuntimeError("VIA buffer response mismatch")
        if prefix[0] == 0x02 and reply[1] != prefix[1]: raise RuntimeError("VIA keyboard value mismatch")
        if prefix[0] == 0x08 and reply[:3] != packet[:3]: raise RuntimeError("VIA lighting value mismatch")
        return bytes(reply)
    def buffer(self, command, size):
        result = bytearray()
        for offset in range(0, size, 28):
            n = min(28,size-offset)
            reply = self.read(command, offset >> 8, offset & 255, n)
            result.extend(reply[4:4+n])
        return bytes(result)
    def close(self):
        try: self.request("close")
        except Exception: pass
        if self.process.stdin: self.process.stdin.close()
        try: self.process.wait(timeout=2)
        except subprocess.TimeoutExpired: self.process.terminate(); self.process.wait(timeout=2)

def digest(data): return hashlib.sha256(data).hexdigest()
def capture(helper):
    protocol = int.from_bytes(helper.read(0x01)[1:3], "big")
    # This installation is VIA 12. Older generations use different lighting/keycode layouts.
    if protocol != 12: raise RuntimeError(f"Unreviewed VIA protocol {protocol}; backup stopped")
    layers = helper.read(0x11)[1]
    if not 1 <= layers <= 16: raise RuntimeError("Unexpected layer count")
    macro_count = helper.read(0x0c)[1]
    macro_size = int.from_bytes(helper.read(0x0d)[1:3], "big")
    if macro_size > 4096 or macro_count > 64: raise RuntimeError("Unexpected macro allocation")
    layout = helper.read(0x02, 0x02)[2:6].hex()
    firmware_version = helper.read(0x02, 0x04)[2:6].hex()
    keymap = helper.buffer(0x12, layers*6*21*2)
    macros = helper.buffer(0x0e, macro_size)
    lighting = None
    lighting_errors = []
    for channel in (3,2):
        try:
            fields = {str(field): helper.read(0x08, channel, field)[3:5 if field == 4 else 4].hex() for field in range(1,5)}
            lighting = {"channel": channel, "fields": fields}; break
        except HelperError as e:
            if e.code != "via_unhandled": raise
            lighting_errors.append({"channel":channel,"error":e.code})
    return {"via_protocol":protocol,"layers":layers,"rows":6,"columns":21,
            "macro_count":macro_count,"macro_size":macro_size,
            "layout_options_be_hex":layout,"firmware_version_be_hex":firmware_version,
            "keymap_be_hex":keymap.hex(),"macro_buffer_hex":macros.hex(),
            "keymap_sha256":digest(keymap),"macro_sha256":digest(macros),
            "lighting":lighting,"lighting_unavailable":lighting_errors if lighting is None else []}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper",type=Path,default=ROOT/"build/orca-keychron-helper")
    parser.add_argument("--registry-id")
    parser.add_argument("--output",type=Path,default=ROOT/"firmware/backups"/datetime.now(timezone.utc).strftime("via-%Y%m%dT%H%M%SZ"))
    args=parser.parse_args()
    os.umask(0o077)
    if args.output.exists(): raise SystemExit("Refusing to overwrite an existing backup directory")
    args.output.mkdir(parents=True,mode=0o700)
    helper=Helper(args.helper.resolve())
    try:
        devices=helper.request("devices")["devices"]
        candidates=[d for d in devices if d.get("vendor_id")==0x3434 and d.get("product_id")==0x02a0 and d.get("transport","").lower()=="usb" and d.get("usage_page")==0xff60 and d.get("usage")==0x61 and (args.registry_id is None or d.get("registry_id")==args.registry_id)]
        if len(candidates)!=1: raise RuntimeError("Expected exactly one K10 Pro ANSI RGB USB Raw HID interface; specify --registry-id")
        device=candidates[0]
        helper.request("open", product_id=0x02a0, transport="USB", registry_id=device["registry_id"])
        first=capture(helper)
        second=capture(helper)
        if first!=second: raise RuntimeError("Two independent reads differ; no verified backup was produced")
        backup={"schema":"orca-k10-pro-via-backup-v1","created_at_utc":datetime.now(timezone.utc).isoformat(),
                "device":device,"verified_twice":True,"read_only":True,
                "firmware_image_backup":False,"firmware_version_note":"VIA field only; not proof of the installed release or firmware bytes.",**first}
        (args.output/"backup.json").write_text(json.dumps(backup,indent=2)+"\n")
        (args.output/"keymap.bin").write_bytes(bytes.fromhex(first["keymap_be_hex"]))
        (args.output/"macros.bin").write_bytes(bytes.fromhex(first["macro_buffer_hex"]))
        (args.output/"reads.json").write_text(json.dumps(helper.transcript,indent=2)+"\n")
        print(json.dumps({"backup":str(args.output.resolve()),"verified_twice":True,"via_protocol":first["via_protocol"],"layers":first["layers"],"keymap_bytes":len(first["keymap_be_hex"])//2,"macro_bytes":first["macro_size"],"lighting_read":first["lighting"] is not None,"keymap_sha256":first["keymap_sha256"],"macro_sha256":first["macro_sha256"],"firmware_image_backup":False},indent=2))
    except Exception as e:
        (args.output/"failed-read-log.json").write_text(json.dumps({"error":str(e),"reads":helper.transcript},indent=2)+"\n")
        raise
    finally: helper.close()
if __name__=="__main__":
    try: main()
    except (RuntimeError, OSError, ValueError) as e: raise SystemExit(str(e))
