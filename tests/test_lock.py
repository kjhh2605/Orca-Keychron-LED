import tempfile
import unittest
from pathlib import Path
from orca_led.lock import DeviceLock


class DeviceLockTests(unittest.TestCase):
    def test_competing_writer_rejected_and_clean_exit_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keyboard.lock"
            with DeviceLock(path):
                with self.assertRaisesRegex(RuntimeError, "already controlling"):
                    with DeviceLock(path):
                        self.fail("Two writers obtained the same device lock")
            with DeviceLock(path):
                pass

    def test_symlink_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "other"
            target.write_text("keep")
            link = Path(directory) / "keyboard.lock"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                with DeviceLock(link):
                    pass
            self.assertEqual(target.read_text(), "keep")
