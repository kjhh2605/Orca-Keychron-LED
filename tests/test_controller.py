import unittest
from orca_led.controller import navigation_action, states_from_snapshot


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"supported":True,"app_running":True,"selected_id":"b","active_view":"workspace",
                         "compatibility":"verified",
                         "slots":[{"index":1,"id":"a","state":2},{"index":2,"id":"b","state":3}]}

    def test_background_same_target_activates_not_minimizes(self):
        self.assertEqual(navigation_action(self.snapshot,1,"com.apple.finder")["action"],"select")

    def test_actual_current_selection_minimizes(self):
        self.assertEqual(navigation_action(self.snapshot,1,"com.stablyai.orca")["action"],"minimize")

    def test_different_slot_selects(self):
        self.assertEqual(navigation_action(self.snapshot,0,"com.stablyai.orca")["target_id"],"a")

    def test_other_app_view_does_not_minimize(self):
        self.snapshot["active_view"] = "settings"
        self.assertEqual(navigation_action(self.snapshot,1,"com.stablyai.orca")["action"],"select")

    def test_empty_and_unavailable_have_no_action(self):
        self.assertEqual(navigation_action(self.snapshot,11,"com.stablyai.orca")["action"],"none")
        self.snapshot["supported"] = False
        self.assertEqual(navigation_action(self.snapshot,0,"com.stablyai.orca")["action"],"none")

    def test_unverified_orca_version_never_switches_or_minimizes(self):
        self.snapshot["compatibility"] = "unverified"
        for slot, front in [(0, "com.apple.finder"), (1, "com.stablyai.orca")]:
            self.assertEqual(navigation_action(self.snapshot,slot,front)["action"],"none")
        # LED state stays available for the same snapshot.
        self.assertEqual(states_from_snapshot(self.snapshot)[:2], (2, 3))

    def test_snapshot_duplicate_rejected(self):
        self.snapshot["slots"].append(self.snapshot["slots"][0])
        with self.assertRaises(ValueError): states_from_snapshot(self.snapshot)



class ControllerRegressionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from orca_led.controller import Controller
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 100.0
        self.wall = 1000.0
        self.calls = []
        self.value = {"supported": True, "app_running": True, "selected_id": "b",
                      "active_view": "terminal", "captured_at": self.wall,
                      "runtime_id": "r1", "window_id": 1, "compatibility": "verified",
                      "slots": [{"index": 1, "id": "a", "state": 2},
                                {"index": 2, "id": "b", "state": 3}]}
        owner = self
        class Native:
            foreground = "com.apple.finder"
            minimized = False
            session_active = True
            fail_activate = False
            after_call = None
            def call(self, op, **args):
                owner.calls.append((op, args))
                if self.after_call:
                    self.after_call(op)
                if op == "session": return {"active": self.session_active}
                if op == "frontmost": return {"bundle_id": self.foreground, "is_minimized": self.minimized}
                if op == "activate":
                    if self.fail_activate: raise RuntimeError("multiple_windows_unsupported")
                    self.foreground = "com.stablyai.orca"
                    self.minimized = False
                if op == "minimize": return {"minimized": True}
                return {}
        class Orca:
            after_snapshot = None
            before_select_snapshot = None
            confirm_selection = True
            def snapshot(self):
                owner.calls.append(("snapshot", {}))
                if self.after_snapshot: self.after_snapshot()
                return {**owner.value, "slots": [dict(row) for row in owner.value["slots"]]}
            def select(self, target, before_dispatch):
                if self.before_select_snapshot: self.before_select_snapshot()
                current = self.snapshot()
                before_dispatch(current)
                owner.calls.append(("select", {"target": target}))
                if self.confirm_selection:
                    owner.value["selected_id"] = target
                    owner.value["captured_at"] = owner.wall + 0.001
                return {"accepted": True}
        self.native, self.orca = Native(), Orca()
        self.controller = Controller(self.orca, self.native, Path(self.directory.name) / "status.json")
        self.controller._snapshot = self.value
        self.controller._snapshot_at = self.now
        self.controller._delivered_ids = ("a", "b")
        self.controller._mapping_activated_at = 99
        for name, side_effect in (("monotonic", lambda: self.now), ("time", lambda: self.wall)):
            patched = patch("orca_led.controller.time." + name, side_effect=side_effect)
            patched.start()
            self.addCleanup(patched.stop)

    def operations(self):
        return [op for op, _ in self.calls]

    def test_expired_input_after_slow_snapshot_does_not_move_window(self):
        self.orca.after_snapshot = lambda: setattr(self, "now", 102)
        self.assertFalse(self.controller._navigate(0, 100))
        self.assertEqual(self.operations(), ["snapshot"])

    def test_expired_input_after_frontmost_does_not_move_window(self):
        self.native.after_call = lambda op: setattr(self, "now", 102) if op == "frontmost" else None
        self.assertFalse(self.controller._navigate(0, 100))
        self.assertNotIn("activate", self.operations())
        self.assertNotIn("select", self.operations())

    def test_inactive_session_does_not_minimize_or_select(self):
        self.native.foreground = "com.stablyai.orca"
        self.native.session_active = False
        with self.assertRaisesRegex(RuntimeError, "unlocked"):
            self.controller._navigate(1, 100)
        self.assertNotIn("minimize", self.operations())
        self.assertNotIn("select", self.operations())

    def test_single_window_guard_precedes_workspace_mutation(self):
        self.native.fail_activate = True
        with self.assertRaisesRegex(RuntimeError, "multiple_windows"):
            self.controller._navigate(0, 100)
        self.assertNotIn("select", self.operations())
        activate = next(args for op, args in self.calls if op == "activate")
        self.assertTrue(activate["single_window"])

    def test_successful_selection_activates_then_verifies_fresh_renderer(self):
        self.assertTrue(self.controller._navigate(0, 100))
        operations = self.operations()
        self.assertLess(operations.index("activate"), operations.index("select"))
        self.assertEqual(operations.count("select"), 1)
        self.assertTrue(self.controller._last_navigation["verified"])

    def test_unverified_orca_version_key_press_does_not_touch_windows_or_workspaces(self):
        self.value["compatibility"] = "unverified"
        for slot, front in [(0, "com.apple.finder"), (1, "com.stablyai.orca")]:
            self.native.foreground = front
            self.assertFalse(self.controller._navigate(slot, 100))
        for operation in ("activate", "minimize", "select"):
            self.assertNotIn(operation, self.operations())

    def test_activation_waits_for_delayed_frontmost_before_select(self):
        from unittest.mock import patch
        original = self.native.call
        activated = {"at": None}
        def call(op, **args):
            if op == "activate":
                result = original(op, **args)
                activated["at"] = self.now
                self.native.foreground = "com.apple.finder"
                return {**result, "frontmost": False}
            if (op == "frontmost" and activated["at"] is not None
                    and self.now - activated["at"] >= 0.099):
                self.native.foreground = "com.stablyai.orca"
            return original(op, **args)
        def wait(delay):
            self.now += delay
            self.wall += delay
            return False
        with patch.object(self.native, "call", side_effect=call), patch.object(self.controller.stop, "wait", side_effect=wait):
            self.assertTrue(self.controller._navigate(0, 100))
        self.assertGreaterEqual(self.now, 100.1)
        self.assertEqual(self.operations().count("activate"), 1)
        self.assertEqual(self.operations().count("select"), 1)

    def test_same_selection_waits_for_native_minimization_confirmation(self):
        from unittest.mock import patch
        self.native.foreground = "com.stablyai.orca"
        original = self.native.call
        def call(op, **args):
            if op == "minimize":
                self.now += 0.582
                self.wall += 0.582
            return original(op, **args)
        with patch.object(self.native, "call", side_effect=call):
            self.assertTrue(self.controller._navigate(1, 100))
        self.assertEqual(self.operations().count("minimize"), 1)
        self.assertNotIn("select", self.operations())
        self.assertTrue(self.controller._last_navigation["verified"])

    def test_frontmost_but_minimized_same_slot_restores_instead_of_minimizing(self):
        self.native.foreground = "com.stablyai.orca"
        self.native.minimized = True
        self.assertTrue(self.controller._navigate(1, 100))
        self.assertEqual(self.operations().count("activate"), 1)
        self.assertEqual(self.operations().count("select"), 1)
        self.assertNotIn("minimize", self.operations())

    def test_unknown_frontmost_window_state_does_not_minimize(self):
        self.native.foreground = "com.stablyai.orca"
        self.native.minimized = None
        with self.assertRaisesRegex(RuntimeError, "window state"):
            self.controller._navigate(1, 100)
        self.assertNotIn("minimize", self.operations())
        self.assertNotIn("select", self.operations())

    def test_mouse_selection_after_old_snapshot_controls_minimize(self):
        from unittest.mock import patch
        self.native.foreground = "com.stablyai.orca"
        self.value["captured_at"] = 999.9
        def refresh():
            if self.now >= 100.099:
                self.value["selected_id"] = "a"
                self.value["captured_at"] = self.wall
        self.orca.after_snapshot = refresh
        def wait(delay):
            self.now += delay
            self.wall += delay
            return False
        with patch.object(self.controller.stop, "wait", side_effect=wait):
            self.assertTrue(self.controller._navigate(0, 100))
        self.assertEqual(self.operations().count("minimize"), 1)
        self.assertNotIn("select", self.operations())

    def test_ui_that_never_publishes_after_input_cannot_minimize(self):
        from unittest.mock import patch
        self.native.foreground = "com.stablyai.orca"
        self.value["captured_at"] = 999.9
        def wait(delay):
            self.now += delay
            self.wall += delay
            return False
        with patch.object(self.controller.stop, "wait", side_effect=wait):
            self.assertFalse(self.controller._navigate(1, 100))
        self.assertNotIn("minimize", self.operations())
        self.assertNotIn("activate", self.operations())

    def test_slow_select_revalidation_cannot_dispatch_expired_event(self):
        self.orca.before_select_snapshot = lambda: setattr(self, "now", 102)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.controller._navigate(0, 100)
        self.assertIn("activate", self.operations())
        self.assertNotIn("select", self.operations())

    def test_mapping_epoch_change_during_select_revalidation_rejects_input(self):
        self.orca.before_select_snapshot = lambda: setattr(self.controller, "_mapping_activated_at", float("inf"))
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.controller._navigate(0, 100)
        self.assertNotIn("select", self.operations())

    def test_accepted_but_unobserved_selection_is_not_reported_verified_or_retried(self):
        from unittest.mock import patch
        self.orca.confirm_selection = False
        with patch.object(self.controller.stop, "wait", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                self.controller._navigate(0, 100)
        self.assertEqual(self.operations().count("select"), 1)
        self.assertFalse(self.controller._last_navigation["verified"])
        self.assertLessEqual(self.operations().count("snapshot"), 8)

    def single_send_iteration(self, on_send=None):
        from unittest.mock import patch
        frames = []
        def send(frame, abort=None):
            frames.append(frame)
            if on_send: on_send()
        def wait(_):
            self.controller.stop.set()
            return True
        with patch.object(self.controller.writer, "send", side_effect=send), patch.object(self.controller.stop, "wait", side_effect=wait):
            self.controller._send()
        return frames

    def test_same_mapping_resume_establishes_new_input_epoch(self):
        self.controller._mapping_activated_at = float("inf")
        frames = self.single_send_iteration()
        self.assertEqual(frames[0][0], 0x11)
        self.assertEqual(self.controller._mapping_activated_at, 100)
        listeners = [args["enabled"] for op, args in self.calls if op == "listen"]
        self.assertEqual(listeners, [False, True])

    def test_ui_that_expires_during_frame_is_not_armed(self):
        self.controller._mapping_activated_at = float("inf")
        self.single_send_iteration(lambda: setattr(self, "now", 105))
        self.assertFalse(any(args.get("enabled") for op, args in self.calls if op == "listen"))
        self.assertEqual(self.controller._mapping_activated_at, float("inf"))

    def test_order_changed_during_frame_is_not_armed(self):
        self.controller._mapping_activated_at = float("inf")
        self.single_send_iteration(lambda: self.value["slots"].reverse())
        self.assertFalse(any(args.get("enabled") for op, args in self.calls if op == "listen"))
        self.assertEqual(self.controller._delivered_ids, ("a", "b"))

    def test_transport_failure_invalidates_epoch(self):
        def fail(op):
            if op == "open": raise RuntimeError("device disconnected")
        self.native.after_call = fail
        self.single_send_iteration()
        self.assertEqual(self.controller._mapping_activated_at, float("inf"))
        self.assertEqual(self.controller._transport, "disconnected_or_error")

    def test_successful_poll_does_not_erase_transport_error(self):
        from unittest.mock import patch
        self.controller._transport_error = "device disconnected"
        self.controller._error = "device disconnected"
        def wait(_):
            self.controller.stop.set()
            return True
        with patch.object(self.controller.stop, "wait", side_effect=wait):
            self.controller._poll()
        self.assertEqual(self.controller._error, "device disconnected")

    def test_stale_ui_invalidates_epoch_even_without_previous_send(self):
        self.controller._snapshot_at = 0
        self.assertEqual(self.single_send_iteration(), [])
        self.assertEqual(self.controller._mapping_activated_at, float("inf"))
        self.assertIn(("listen", {"enabled": False}), self.calls)

    def test_status_write_failure_stops_bridge(self):
        from unittest.mock import patch
        with patch("orca_led.controller.atomic_json", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.controller._save()
        self.assertTrue(self.controller.stop.is_set())


class AtomicStatusTests(unittest.TestCase):
    def test_simultaneous_writes_have_distinct_temporary_files(self):
        import json
        import os
        import tempfile
        import threading
        from pathlib import Path
        from unittest.mock import patch
        from orca_led.controller import atomic_json
        barrier = threading.Barrier(2)
        original = os.replace
        sources, errors = [], []
        def replace(source, target):
            sources.append(source)
            barrier.wait(timeout=2)
            original(source, target)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            def write(value):
                try: atomic_json(target, {"value": value})
                except Exception as exc: errors.append(exc)
            with patch("orca_led.controller.os.replace", side_effect=replace):
                threads = [threading.Thread(target=write, args=(value,)) for value in (1, 2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=3)
            self.assertEqual(errors, [])
            self.assertEqual(len(set(sources)), 2)
            self.assertIn(json.loads(target.read_text())["value"], (1, 2))


if __name__ == "__main__": unittest.main()


class SeenCompletionTests(unittest.TestCase):
    def controller(self, theme_protocol):
        import json, tempfile
        from pathlib import Path
        from orca_led.controller import Controller
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        if theme_protocol is not None:
            (root / "theme-firmware.json").write_text(json.dumps({"theme_protocol": theme_protocol}))
        class Native:
            def call(self, op, **args):
                return {"bundle_id": "com.stablyai.orca", "is_minimized": False}
        return Controller(None, Native(), root / "status.json")

    def test_seen_completion_is_sent_only_to_protocol_two_firmware(self):
        states = (7, 4, 2) + (0,) * 9
        self.assertEqual(self.controller(2)._wire_states(states)[:3], (7, 4, 2))
        for level in (None, 1):
            controller = self.controller(level)
            self.assertEqual(controller._wire_states(states)[:3], (4, 4, 2))
            self.assertEqual(controller.themes.max_state, 6)

    def test_focus_requires_unminimized_frontmost_orca(self):
        controller = self.controller(2)
        self.assertTrue(controller._orca_focused())
        controller.native.call = lambda op, **args: {"bundle_id": "com.apple.finder", "is_minimized": False}
        self.assertFalse(controller._orca_focused())
        controller.native.call = lambda op, **args: {"bundle_id": "com.stablyai.orca", "is_minimized": None}
        self.assertFalse(controller._orca_focused())
