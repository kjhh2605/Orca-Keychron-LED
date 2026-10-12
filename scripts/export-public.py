#!/usr/bin/env python3
"""Export reviewed source paths into a separate, source-only publication folder.

Never edits the source project, publishes remotely, or removes personal backups.
Generated destinations are replaceable only while their recorded files are intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import struct
import zlib
import xml.etree.ElementTree as ET

SCHEMA = "orca-led-public-source-v1"
MANIFEST = "PUBLIC_EXPORT.json"
PREVIEW_IMAGE = "docs/public/settings-preview.png"
APP_ICON = "native/AppIcon.png"
KEYBOARD_PHOTO = "docs/public/keyboard-photo.png"
# Reviewed images with no capture metadata; the only permitted binary assets.
REVIEWED_PNGS = frozenset({PREVIEW_IMAGE, APP_ICON, KEYBOARD_PHOTO})
GUIDE_IMAGES = frozenset({"docs/public/connection-modes.svg", "docs/public/firmware-flow.svg"})
EXACT_FILES = frozenset({
    "README.md", "README.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore",
    "docs/protocol.md", "firmware/LICENSE", "firmware/upstream.lock.json",
    "firmware/requirements-build.txt", "firmware/prepare.py", "firmware/patch_qmk.py",
    "firmware/record_build.py", "firmware/flash.py",
    "firmware/recovery/k10_pro_us_rgb_v1.0.json",
    "firmware/recovery/k10_pro_ansi_rgb_orca_led_618127a_via.json",
    "firmware/recovery/custom-via-definition-manifest.json",
    "firmware/recovery/official-manifest.json",
    "scripts/build-native.sh", "scripts/build-settings.sh", "scripts/build-firmware.sh",
    "scripts/build-release.sh", "scripts/build-icon.sh",
    "scripts/start-orca-debug.command", "scripts/backup-via.py", "scripts/restore-via.py",
    "scripts/export-public.py",
    PREVIEW_IMAGE, APP_ICON, KEYBOARD_PHOTO,
    *GUIDE_IMAGES,
})
SOURCE_TREES = {
    "orca_led": {".py", ".js"},
    "native": {".swift", ".plist"},
    "firmware/src": {".c", ".h"},
    "tests": {".py", ".c", ".h", ".sh", ".json"},
    "docs/public": {".md"},
    "examples": {".json", ".md"},
}
REQUIRED_FILES = frozenset({
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore",
    "orca_led/__main__.py", "firmware/upstream.lock.json", "scripts/export-public.py",
})
SKIP_PARTS = frozenset({"__pycache__", ".git", ".venv", "node_modules", ".DS_Store"})
PRIVATE_PATTERNS = (
    ("absolute user-home path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    ("personal dated device backup", re.compile(r"firmware/backups/(?:via|dfu)-\d{8}")),
    ("private key block", re.compile(r"-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


class ExportError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def allowed(relative: str) -> bool:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or any(p in SKIP_PARTS for p in path.parts):
        return False
    if relative in EXACT_FILES:
        return True
    return any(relative.startswith(prefix + "/") and path.suffix in extensions
               for prefix, extensions in SOURCE_TREES.items())


def validate_text(relative: str, data: bytes) -> None:
    if relative in REVIEWED_PNGS:
        # Exclude text/EXIF chunks so private capture metadata cannot ride along.
        if len(data) > 5 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ExportError("Invalid settings preview PNG")
        offset, chunks = 8, []
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(data) or kind not in {b"IHDR", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"cHRM", b"iCCP", b"pHYs", b"PLTE", b"tRNS"}:
                raise ExportError("Unreviewed settings preview PNG metadata or chunk")
            payload = data[offset + 8:end - 4]
            if zlib.crc32(kind + payload) & 0xffffffff != int.from_bytes(data[end - 4:end], "big"):
                raise ExportError("Invalid settings preview PNG checksum")
            if not chunks:
                if kind != b"IHDR" or length != 13:
                    raise ExportError("Invalid settings preview PNG header")
                width, height = struct.unpack(">II", payload[:8])
                if not (1 <= width <= 4096 and 1 <= height <= 4096):
                    raise ExportError("Settings preview PNG dimensions exceed the limit")
            elif kind == b"IHDR":
                raise ExportError("Duplicate settings preview PNG header")
            chunks.append(kind)
            offset = end
            if kind == b"IEND":
                if length != 0 or end != len(data):
                    raise ExportError("Invalid settings preview PNG ending")
                break
        if not chunks or chunks[-1] != b"IEND" or b"IDAT" not in chunks:
            raise ExportError("Incomplete settings preview PNG")
        return
    if b"\0" in data:
        raise ExportError(f"Binary content is not allowed: {relative}")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportError(f"Non-UTF-8 content is not allowed: {relative}") from exc
    for description, pattern in PRIVATE_PATTERNS:
        if pattern.search(value):
            raise ExportError(f"Review required: {description} in {relative}")
    if relative in GUIDE_IMAGES:
        # The documentation diagrams are static shapes and text, with no
        # scripts, external resources, embedded HTML, entities, or CSS URLs.
        if len(data) > 256 * 1024 or "<!" in value:
            raise ExportError("Unreviewed SVG content")
        try:
            root = ET.fromstring(value)
        except ET.ParseError as exc:
            raise ExportError("Invalid guide SVG") from exc
        namespace = "{http://www.w3.org/2000/svg}"
        tags = {namespace + tag for tag in ["svg", "g", "title", "desc", "text", "rect", "line", "path", "circle", "polyline", "polygon"]}
        attributes = {"width", "height", "viewBox", "role", "aria-labelledby", "id", "font-family", "font-size", "font-weight", "text-anchor", "fill", "stroke", "stroke-width", "stroke-dasharray", "x", "y", "x1", "y1", "x2", "y2", "rx", "ry", "cx", "cy", "r", "d", "points", "opacity"}
        if root.tag != namespace + "svg":
            raise ExportError("Invalid guide SVG root")
        for node in root.iter():
            if node.tag not in tags or set(node.attrib) - attributes or any("url(" in attr.lower() for attr in node.attrib.values()):
                raise ExportError("Unreviewed active SVG content or resource")
    if relative.endswith(".json"):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ExportError(f"Invalid JSON: {relative}") from exc

        def inspect(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    # Legitimate test fixtures use explicit synthetic identities.
                    if key in {"serial", "device_path", "authToken", "access_token", "private_key"}:
                        if child not in (None, "", "TEST", "EXAMPLE", "SYNTHETIC"):
                            raise ExportError(f"Review required: identity/secret field {key} in {relative}")
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)
        inspect(parsed)


def source_files(source: Path) -> dict[str, tuple[bytes, int]]:
    candidates = set()
    for relative in EXACT_FILES:
        path = source / relative
        if path.is_symlink():
            raise ExportError(f"Symlinks are not exported: {relative}")
        if path.is_file():
            candidates.add(relative)
    for prefix in SOURCE_TREES:
        folder = source / prefix
        if folder.is_symlink():
            raise ExportError(f"Symlinks are not exported: {prefix}")
        if not folder.exists():
            continue
        for root, dirs, names in os.walk(folder, followlinks=False):
            dirs[:] = [name for name in dirs if name not in SKIP_PARTS]
            for name in [*dirs, *names]:
                path = Path(root) / name
                if path.is_symlink():
                    raise ExportError(f"Symlinks are not exported: {path.relative_to(source)}")
            for name in names:
                relative = (Path(root) / name).relative_to(source).as_posix()
                if allowed(relative):
                    candidates.add(relative)
    missing = REQUIRED_FILES - candidates
    if missing:
        raise ExportError("Required source files missing: " + ", ".join(sorted(missing)))
    result = {}
    for relative in sorted(candidates):
        path = source / relative
        # Check every parent so an exact-file allowlist cannot cross a symlink.
        if any(p.is_symlink() for p in [path, *path.parents] if p != source.parent):
            raise ExportError(f"Symlinks are not exported: {relative}")
        data = path.read_bytes()
        validate_text(relative, data)
        result[relative] = (data, 0o755 if path.stat().st_mode & 0o111 else 0o644)
    return result


def validate_replace(destination: Path) -> None:
    marker = destination / MANIFEST
    try:
        metadata = json.loads(marker.read_text())
        records = metadata["files"]
        if metadata["schema"] != SCHEMA or not isinstance(records, dict):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ExportError("Existing destination is not a managed public export; choose an empty path") from exc
    actual = set()
    for root, dirs, names in os.walk(destination, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(root) / name
            if path.is_symlink():
                raise ExportError("Destination contains a symlink; preserve it and choose a new output path")
        for name in names:
            actual.add((Path(root) / name).relative_to(destination).as_posix())
    if actual != set(records) | {MANIFEST}:
        raise ExportError("Destination has added or removed files; preserve it and choose a new output path")
    for relative, record in records.items():
        if not isinstance(relative, str) or not allowed(relative):
            raise ExportError("Destination manifest contains an unapproved path")
        if not isinstance(record, dict) or record.get("sha256") != digest((destination / relative).read_bytes()):
            raise ExportError(f"Destination was edited: {relative}; choose a new output path")


def export_public(source: Path, destination: Path, replace: bool = False) -> dict:
    if destination.is_symlink():
        raise ExportError("Destination must not be a symlink")
    source, destination = source.resolve(), destination.resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ExportError("Output must be separate from the source project and its parent folders")
    payload = source_files(source)
    if destination.exists():
        if not replace:
            raise ExportError("Destination exists; --replace is only available for an unchanged managed export")
        validate_replace(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent))
    previous = None
    try:
        records = {}
        for relative, (data, mode) in payload.items():
            path = temporary / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
            records[relative] = {"sha256": digest(data), "bytes": len(data)}
        manifest = {"schema": SCHEMA, "kind": "source-only", "files": records,
                    "excludes": ["device backups", "runtime data", "compiled apps and firmware",
                                 "downloaded dependencies", "private installation records"]}
        (temporary / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        if destination.exists():
            # Recheck just before replacing so normal edits during export are kept.
            validate_replace(destination)
            previous = Path(tempfile.mkdtemp(prefix=f".{destination.name}.previous-", dir=destination.parent))
            previous.rmdir()
            destination.rename(previous)
        try:
            temporary.rename(destination)
        except OSError:
            if previous is not None:
                previous.rename(destination)
                previous = None
            raise
        if previous is not None:
            shutil.rmtree(previous)
        return {"files": len(records), "bytes": sum(x["bytes"] for x in records.values()),
                "destination": str(destination), "source_unchanged": True}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true", help="Replace an unchanged export created by this tool")
    args = parser.parse_args()
    try:
        result = export_public(Path(__file__).resolve().parents[1], args.output, args.replace)
    except (ExportError, OSError) as exc:
        parser.exit(1, f"Public export: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
