"""Renderer module discovery, installed-bundle reading and the version policy."""
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest

from orca_led.orca_compat import (CompatError, OrcaBundle, discover, discover_installed,
                                  is_verified, mark_verified, verified_versions)
from renderer_fixtures import EXPECTED_223, EXPECTED_241, LAYOUT_223, LAYOUT_241, reader


def write_app(root: Path, files: dict, version="1.4.241", package_version=None,
              unpacked=(), integrity=True) -> Path:
    """Build an Orca.app with a real asar archive (header pickle + payload)."""
    app = root / "Orca.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    tree, payload = {"files": {}}, bytearray()
    entries = {"package.json": json.dumps({"version": package_version or version}).encode(),
               **{"out/renderer/" + name: text.encode() for name, text in files.items()}}
    for name, data in entries.items():
        node = tree
        *folders, leaf = name.split("/")
        for folder in folders:
            node = node["files"].setdefault(folder, {"files": {}})
        record = {"size": len(data), "integrity": {"algorithm": "SHA256", "hash": hashlib.sha256(data).hexdigest()}}
        if name in unpacked:
            record["unpacked"] = True
            target = resources / "app.asar.unpacked" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        else:
            record["offset"] = str(len(payload))
            payload.extend(data)
        node["files"][leaf] = record
    header = json.dumps(tree).encode()
    padded = header + b"\0" * (-len(header) % 4)
    pickle = struct.pack("<II", len(padded) + 4, len(header)) + padded
    (resources / "app.asar").write_bytes(struct.pack("<II", 4, len(pickle)) + pickle + payload)
    info = {"CFBundleShortVersionString": version}
    if integrity:
        info["ElectronAsarIntegrity"] = {"Resources/app.asar": {
            "algorithm": "SHA256", "hash": hashlib.sha256(header).hexdigest()}}
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    return app


class DiscoveryTest(unittest.TestCase):
    def test_both_observed_layouts_resolve_the_handlers_order_function_and_store(self):
        for files, expected in [(LAYOUT_223, EXPECTED_223), (LAYOUT_241, EXPECTED_241)]:
            with self.subTest(entry=expected["entry"]):
                self.assertEqual(discover(reader(files)), expected)

    def assert_changed(self, files):
        with self.assertRaises(CompatError) as caught:
            discover(reader(files))
        self.assertEqual(caught.exception.code, "orca_layout_changed")

    def test_missing_or_duplicated_anchor_is_rejected(self):
        app = LAYOUT_223["assets/App-BGnLbgfw.js"]
        self.assert_changed({**LAYOUT_223, "assets/App-BGnLbgfw.js": app.replace("onJumpToWorktreeIndex", "onJumpToWorkspace")})
        # A second handler anywhere in the startup graph is ambiguous.
        store = LAYOUT_241["assets/store-B_rT6dWz.js"]
        self.assert_changed({**LAYOUT_241, "assets/store-B_rT6dWz.js":
                             "function d(){window.api.ui.onJumpToWorktreeIndex(e=>{let t=n.getState();e$()[e]})}" + store})

    def test_handler_found_only_in_a_lazily_loaded_chunk_is_not_used(self):
        # The web chunk is not in the entry's static import graph; a handler
        # there may never have run, and importing it could execute new code.
        self.assertIn(".onJumpToWorktreeIndex(", LAYOUT_241["assets/web-CsCItEI0.js"])
        app = LAYOUT_241["assets/App-Dm3rSCHd.js"]
        self.assert_changed({**LAYOUT_241, "assets/App-Dm3rSCHd.js": app.replace("onJumpToWorktreeIndex", "onJumpToWorkspace")})

    def test_ambiguous_handler_or_unresolvable_export_is_rejected(self):
        app = LAYOUT_241["assets/App-Dm3rSCHd.js"]
        cases = [
            app.replace("let n=e$()[e];", "let n=e$()[e]||vEe()[e];"),           # two order candidates
            app.replace("if(t.activeModal", "if(vEe.getState().activeModal"),    # two stores
            app.replace("export{e$ as br,", "export{"),                         # order not exported
            app.replace("export{e$ as br,", "export{e$ as br,e$ as bs,"),       # exported twice
            app.replace("function vEe(e){}", "function vEe(e){function e$(){}}"),  # shadowing declaration
            app.replace("import{n as U}", "import{n as U,m as U}"),             # doubly bound store
        ]
        for index, text in enumerate(cases):
            with self.subTest(case=index):
                self.assert_changed({**LAYOUT_241, "assets/App-Dm3rSCHd.js": text})

    def test_imported_order_function_must_be_exported_by_its_source(self):
        source = LAYOUT_223["assets/worktree-activation-D0sk4KxD.js"]
        self.assert_changed({**LAYOUT_223, "assets/worktree-activation-D0sk4KxD.js": source.replace("Rs as s,", "")})

    def test_page_must_have_exactly_one_module_entry(self):
        html = LAYOUT_241["index.html"]
        self.assert_changed({**LAYOUT_241, "index.html": html + '<script type="module" src="./assets/other.js"></script>'})
        self.assert_changed({**LAYOUT_241, "index.html": html.replace('type="module" ', "")})


class InstalledBundleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_asar_with_unpacked_page_is_discovered_with_its_version(self):
        app = write_app(self.root, LAYOUT_241, unpacked={"out/renderer/index.html"})
        self.assertEqual(discover_installed(app), {"version": "1.4.241", **EXPECTED_241})

    def test_tampered_file_or_header_is_rejected(self):
        app = write_app(self.root, LAYOUT_241)
        asar = app / "Contents/Resources/app.asar"
        data = asar.read_bytes()
        asar.write_bytes(data.replace(b"getState:()=>", b"getState:()=<"))
        with self.assertRaises(CompatError) as caught:
            discover_installed(app)
        self.assertEqual(caught.exception.code, "orca_bundle_unavailable")
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        info["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"] = "0" * 64
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        with self.assertRaises(CompatError):
            OrcaBundle(app)

    def test_app_and_package_versions_must_agree(self):
        app = write_app(self.root, LAYOUT_241, version="1.4.241", package_version="1.4.240")
        with self.assertRaises(CompatError) as caught:
            OrcaBundle(app)
        self.assertEqual(caught.exception.code, "orca_bundle_unavailable")

    def test_missing_install_is_reported_not_raised_as_os_error(self):
        with self.assertRaises(CompatError) as caught:
            discover_installed(self.root / "Missing.app")
        self.assertEqual(caught.exception.code, "orca_bundle_unavailable")


class PolicyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "orca_compat.json"
        self.path.write_text(json.dumps({"schema": 1, "verified_versions": ["1.4.223"]}))

    def test_only_listed_versions_are_verified(self):
        self.assertTrue(is_verified("1.4.223", self.path))
        self.assertFalse(is_verified("1.4.241", self.path))
        self.assertFalse(is_verified(None, self.path))

    def test_corrupt_policy_verifies_nothing_and_is_never_overwritten(self):
        for content in ["not json", json.dumps({"schema": 2, "verified_versions": ["1.4.223"]}),
                        json.dumps({"schema": 1, "verified_versions": ["1.4.223; rm"]})]:
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertEqual(verified_versions(self.path), frozenset())
                with self.assertRaises(ValueError):
                    mark_verified("1.4.241", self.path)
                self.assertEqual(self.path.read_text(), content)

    def test_mark_verified_adds_once_in_version_order(self):
        self.assertTrue(mark_verified("1.4.241", self.path))
        self.assertTrue(mark_verified("1.4.30", self.path))
        self.assertFalse(mark_verified("1.4.241", self.path))
        self.assertEqual(json.loads(self.path.read_text())["verified_versions"], ["1.4.30", "1.4.223", "1.4.241"])
        with self.assertRaises(ValueError):
            mark_verified("latest", self.path)

    def test_shipped_policy_is_valid(self):
        self.assertTrue(verified_versions())


if __name__ == "__main__":
    unittest.main()
