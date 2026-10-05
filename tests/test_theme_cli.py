import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orca_led.__main__ import main


class VerifyThemeCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.receipt = Path(self.directory.name) / 'theme-firmware.json'
        self.devices = [dict(vendor_id=0x3434, product_id=0x02A0, transport='USB',
                             usage_page=0xFF60, registry_id=123)]
        self.reply = dict(firmware_verified=True, version=1, transport='USB', theme_protocol=1)
        self.calls = []
        owner = self
        class Native:
            def call(self, op, **args):
                owner.calls.append((op, args))
                if op == 'devices': return {'devices': owner.devices}
                if op == 'query': return owner.reply
                return {}
            def close(self): pass
        self.native = Native()

    def run_cli(self, helper=True):
        argv = ['orca_led']
        if helper: argv += ['--helper', '/unused/fresh-test-helper']
        argv += ['verify-theme', '--receipt-file', str(self.receipt)]
        with patch('sys.argv', argv), patch('orca_led.__main__.NativeClient', return_value=self.native), \
             patch('orca_led.__main__.DeviceLock', return_value=contextlib.nullcontext()), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main()

    def test_exact_device_and_verified_extension_write_receipt_without_mutations(self):
        self.assertEqual(self.run_cli(), 0)
        value = json.loads(self.receipt.read_text())
        self.assertEqual(value['theme_protocol'], 1)
        self.assertEqual(value['verified_via'], 'usb_query')
        self.assertEqual(value['vendor_id'], 0x3434)
        self.assertEqual(value['product_id'], 0x02A0)
        self.assertEqual([op for op, _ in self.calls], ['devices', 'open', 'query'])
        self.assertEqual(self.calls[1][1]['registry_id'], 123)

    def test_wrong_vendor_or_multiple_targets_cannot_be_queried(self):
        self.devices[0]['vendor_id'] = 1
        with self.assertRaisesRegex(RuntimeError, 'exactly one'): self.run_cli()
        self.assertEqual([op for op, _ in self.calls], ['devices'])
        self.calls.clear()
        self.devices[0]['vendor_id'] = 0x3434
        self.devices.append(dict(self.devices[0], registry_id=124))
        with self.assertRaisesRegex(RuntimeError, 'exactly one'): self.run_cli()
        self.assertEqual([op for op, _ in self.calls], ['devices'])
        self.assertFalse(self.receipt.exists())

    def test_legacy_or_future_extension_invalidates_earlier_success_receipt(self):
        self.run_cli()
        for unsupported in [0, 3, True, None]:
            self.reply['theme_protocol'] = unsupported
            with self.subTest(protocol=unsupported), self.assertRaisesRegex(RuntimeError, 'did not confirm'):
                self.run_cli()
            self.assertEqual(json.loads(self.receipt.read_text())['theme_protocol'], 0)

    def test_unverified_firmware_cannot_create_receipt(self):
        self.reply['firmware_verified'] = False
        with self.assertRaisesRegex(RuntimeError, 'OL v1'): self.run_cli()
        self.assertFalse(self.receipt.exists())

    def test_explicit_new_helper_is_required(self):
        with self.assertRaises(SystemExit) as raised: self.run_cli(helper=False)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.calls, [])
