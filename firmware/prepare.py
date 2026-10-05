#!/usr/bin/env python3
"""Project-local reproducible build preparation. Never accesses a USB/HID device."""
from pathlib import Path
import hashlib, json, os, platform, subprocess, sys
ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
def run(*args, **kw): subprocess.run(args, check=True, **kw)
def main():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Bundled compiler setup targets macOS Apple Silicon; provide your own ARM compiler on other hosts.")
    VENDOR.mkdir(exist_ok=True)
    lock = json.loads((ROOT / "upstream.lock.json").read_text())
    for resource in lock["resources"]:
        archive = VENDOR / resource["archive"]
        if not archive.exists():
            run("curl", "--fail", "--location", "--show-error", resource["url"], "--output", str(archive))
        if hashlib.sha256(archive.read_bytes()).hexdigest() != resource["sha256"]:
            raise SystemExit(f"Checksum mismatch: {archive}; refusing extraction")
        destination = VENDOR / resource["destination"]
        if not destination.exists() or not any(destination.iterdir()):
            destination.mkdir(parents=True, exist_ok=True)
            run("tar", "-xf", str(archive), "--strip-components=1", "-C", str(destination))
    venv = VENDOR / "venv"
    if not (venv / "bin/python").exists(): run(sys.executable, "-m", "venv", str(venv))
    run(str(venv / "bin/pip"), "install", "--no-cache-dir", "--disable-pip-version-check", "--no-input", "-r", str(ROOT / "requirements-build.txt"))
    if not (VENDOR / "dfu-tools/bin/dfu-suffix").exists():
        env = os.environ.copy()
        # Uses an existing libusb development install; never installs globally.
        libusb = Path("/opt/homebrew/opt/libusb/lib/pkgconfig")
        if libusb.exists(): env["PKG_CONFIG_PATH"] = str(libusb) + ":" + env.get("PKG_CONFIG_PATH", "")
        source = VENDOR / "dfu-util-0.11"
        run("./configure", "--prefix=" + str(VENDOR / "dfu-tools"), cwd=source, env=env)
        run("make", "-j4", cwd=source)
        run("make", "install", cwd=source)
    print("Build dependencies prepared locally; no keyboard commands executed.")
if __name__ == "__main__": main()
