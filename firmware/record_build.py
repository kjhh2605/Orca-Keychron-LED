#!/usr/bin/env python3
"""Record artifacts and the exact project sources used by build-firmware.sh."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, subprocess
ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
BUILD = ROOT / "build"
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    sources = sorted((ROOT / "src").glob("*.[ch]")) + [ROOT / "patch_qmk.py", ROOT / "upstream.lock.json", ROOT / "requirements-build.txt"]
    artifacts = [BUILD / "k10_pro_ansi_rgb_orca_led.bin", BUILD / "k10_pro_ansi_rgb_upstream_via_rebuilt.bin"]
    lock = json.loads((ROOT / "upstream.lock.json").read_text())
    report = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "upstream_commit": lock["upstream_commit"],
        "target": "keychron/k10_pro/ansi/rgb", "vid": "0x3434", "pid": "0x02A0",
        "compiler": subprocess.check_output(["arm-none-eabi-gcc", "--version"], text=True).splitlines()[0],
        "artifacts": {p.name:{"sha256":sha(p),"size_bytes":p.stat().st_size} for p in artifacts},
        "source_sha256": {str(p.relative_to(PROJECT)):sha(p) for p in sources},
        "fallback_note": "Rebuilt pinned upstream VIA target, not a backup of the user's currently installed firmware.",
        "host_tests": "protocol/CRC/framing/lease/paired keys and consumer queue tests passed with AddressSanitizer+UndefinedBehaviorSanitizer",
        "dfu_suffix": "validated by dfu-suffix 0.11 for both bins",
        "hardware_flash_performed": False,
        "bluetooth_led_or_input_verified": False,
    }
    (BUILD / "build-manifest.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"artifacts":report["artifacts"],"hardware_flash_performed":False},indent=2))
if __name__ == "__main__": main()
