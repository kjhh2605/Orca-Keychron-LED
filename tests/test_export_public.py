"""Source-only export privacy and preservation checks; no hardware access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import struct
import zlib

spec = importlib.util.spec_from_file_location("public_export", Path(__file__).resolve().parents[1] / "scripts/export-public.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "project"
        self.output = self.root / "public"
        for name in export.REQUIRED_FILES:
            self.put(name, "{}" if name.endswith(".json") else "public source\n")

    def put(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_exports_only_reviewed_sources_and_keeps_backups(self):
        self.put("native/settings/main.swift", "import SwiftUI\n")
        script = self.put("scripts/build-settings.sh", "#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        private = {}
        for name in ["firmware/backups/receipt.json", "runtime/status.json", "build/Orca LED.app/binary",
                     "firmware/vendor/source.c", "firmware/build/image.bin", "firmware/tools/bin/dfu-util",
                     "TEST-RESULTS.md", "docs/firmware-install.md", "native/temporary.log",
                     "tests/__pycache__/cache.pyc", "scripts/private-notes.py"]:
            private[name] = self.put(name, "private information")
        result = export.export_public(self.source, self.output)
        self.assertTrue(result["source_unchanged"])
        self.assertTrue((self.output / "native/settings/main.swift").exists())
        self.assertTrue((self.output / "scripts/build-settings.sh").stat().st_mode & 0o111)
        for name, source in private.items():
            self.assertFalse((self.output / name).exists(), name)
            self.assertEqual(source.read_text(), "private information")
        manifest = json.loads((self.output / export.MANIFEST).read_text())
        self.assertEqual(set(manifest["files"]), set(export.REQUIRED_FILES) | {"native/settings/main.swift", "scripts/build-settings.sh"})

    def test_refuses_private_home_path_before_output_is_created(self):
        self.put("README.md", "/" + "Users/" + "private-person/workspace")
        with self.assertRaisesRegex(export.ExportError, "user-home"):
            export.export_public(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_device_identifiers_in_json(self):
        self.put("examples/theme.json", json.dumps({"serial": "private-device-identity"}))
        with self.assertRaisesRegex(export.ExportError, "identity/secret"):
            export.export_public(self.source, self.output)

    def test_accepts_explicitly_synthetic_identity(self):
        self.put("tests/fixtures/device.json", '{"serial":"SYNTHETIC"}')
        export.export_public(self.source, self.output)
        self.assertTrue((self.output / "tests/fixtures/device.json").exists())

    def test_refuses_source_symlink(self):
        (self.source / "native").symlink_to(self.root)
        with self.assertRaisesRegex(export.ExportError, "Symlinks"):
            export.export_public(self.source, self.output)

    def test_refuses_binary_in_allowed_source_file(self):
        self.put("native/main.swift", "\0binary")
        with self.assertRaisesRegex(export.ExportError, "Binary"):
            export.export_public(self.source, self.output)

    def test_only_generated_preview_png_is_exported(self):
        def chunk(kind, content):
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b"")
        image = self.source / export.PREVIEW_IMAGE
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(png)
        image.with_name("private-screenshot.png").write_bytes(png)
        export.export_public(self.source, self.output)
        self.assertEqual((self.output / export.PREVIEW_IMAGE).read_bytes(), png)
        self.assertFalse((self.output / "docs/public/private-screenshot.png").exists())
        metadata = chunk(b"tEXt", b"Private\0personal capture metadata")
        image.write_bytes(png[:-12] + metadata + png[-12:])
        with self.assertRaisesRegex(export.ExportError, "metadata"):
            export.export_public(self.source, self.root / "unsafe")

    def test_cannot_export_inside_source_or_over_its_parent(self):
        for destination in [self.source, self.source / "public", self.root]:
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(export.ExportError, "separate"):
                    export.export_public(self.source, destination)

    def test_only_reviewed_static_svg_guides_are_exported(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text x="2" y="20">USB → BT</text></svg>'
        for path in export.GUIDE_IMAGES:
            self.put(path, svg)
        self.put("docs/public/private-diagram.svg", svg)
        export.export_public(self.source, self.output)
        for path in export.GUIDE_IMAGES:
            self.assertEqual((self.output / path).read_text(), svg)
        self.assertFalse((self.output / "docs/public/private-diagram.svg").exists())

    def test_svg_active_or_external_content_is_rejected(self):
        for body in ['<script>alert(1)</script>', '<image href="https://example.com/private"/>',
                     '<rect onload="alert(1)"/>', '<rect fill="url(https://example.com/image)"/>',
                     '<foreignObject/>']:
            with self.subTest(body=body):
                svg = '<svg xmlns="http://www.w3.org/2000/svg">' + body + '</svg>'
                with self.assertRaisesRegex(export.ExportError, "SVG"):
                    export.validate_text(next(iter(export.GUIDE_IMAGES)), svg.encode())

    def test_will_not_replace_unmanaged_destination(self):
        self.output.mkdir()
        keep = self.output / "keep.txt"
        keep.write_text("keep me")
        with self.assertRaises(export.ExportError):
            export.export_public(self.source, self.output, replace=True)
        self.assertEqual(keep.read_text(), "keep me")

    def test_can_replace_unchanged_managed_export(self):
        export.export_public(self.source, self.output)
        self.put("README.md", "new source\n")
        export.export_public(self.source, self.output, replace=True)
        self.assertEqual((self.output / "README.md").read_text(), "new source\n")

    def test_preserves_user_changes_in_existing_export(self):
        export.export_public(self.source, self.output)
        (self.output / "README.md").write_text("user changes")
        with self.assertRaisesRegex(export.ExportError, "edited"):
            export.export_public(self.source, self.output, replace=True)
        self.assertEqual((self.output / "README.md").read_text(), "user changes")

    def test_preserves_added_files_in_existing_export(self):
        export.export_public(self.source, self.output)
        (self.output / "new.txt").write_text("user addition")
        with self.assertRaisesRegex(export.ExportError, "added or removed"):
            export.export_public(self.source, self.output, replace=True)
        self.assertEqual((self.output / "new.txt").read_text(), "user addition")

    def test_refuses_manifest_path_traversal(self):
        export.export_public(self.source, self.output)
        marker = self.output / export.MANIFEST
        manifest = json.loads(marker.read_text())
        manifest["files"]["../outside"] = {"sha256": "invalid"}
        marker.write_text(json.dumps(manifest))
        with self.assertRaises(export.ExportError):
            export.export_public(self.source, self.output, replace=True)


if __name__ == "__main__":
    unittest.main()
