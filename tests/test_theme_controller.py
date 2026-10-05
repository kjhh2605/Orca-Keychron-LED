"""Simulated wire time, failures, and concurrent configuration changes (no devices)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from orca_led import protocol
from orca_led.controller import Controller
from orca_led.theme import default_theme


class ThemeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 100.0
        self.frames = []
        self.calls = []
        owner = self
        class Native:
            def call(self, op, **args):
                owner.calls.append((op, args))
                return {}
        self.controller = Controller(None, Native(), self.root / 'status.json')
        self.controller._snapshot = {'supported': True, 'app_running': True,
            'slots': [{'index': 1, 'id': 'one', 'state': 2}]}
        self.controller._snapshot_at = self.now
        self.config = default_theme().as_dict()
        self.config['global_brightness'] = 91
        self.after_send = None
        self.after_wait = None
        self.waits = 0
        self.finish = lambda: self.controller.themes.status()['phase'] == 'sent'

    def save(self):
        self.config['revision'] = str(uuid.uuid4())
        (self.root / 'theme.json').write_text(json.dumps(self.config))

    def run_sender(self):
        def send(frame, abort=None):
            start = self.now
            self.now += len(protocol.symbols(frame)) * .25 + .1
            self.controller._snapshot_at = self.now  # Independent Orca polling stays fresh.
            self.frames.append((frame, start, self.now))
            if self.after_send: self.after_send(frame)
        def wait(delay):
            self.now += delay
            self.waits += 1
            if self.after_wait: self.after_wait()
            if self.finish() or self.waits >= 30:
                self.controller.stop.set()
                return True
            return False
        with patch('orca_led.controller.time.monotonic', side_effect=lambda: self.now), \
             patch('orca_led.controller.time.time', side_effect=lambda: self.now + 1000), \
             patch.object(self.controller.writer, 'send', side_effect=send), \
             patch.object(self.controller.stop, 'wait', side_effect=wait):
            self.controller._send()

    def test_state_change_preempts_periodic_refresh_and_goes_out_as_delta(self):
        from orca_led.native import FramePreempted
        events = []
        def send(frame, abort=None):
            if abort is not None and frame[0] == 0x11:
                self.controller._snapshot = {'supported': True, 'app_running': True,
                    'slots': [{'index': 1, 'id': 'one', 'state': 4}]}
                self.assertTrue(abort())
                events.append('preempted')
                raise FramePreempted()
            events.append(frame[0])
            self.now += len(protocol.symbols(frame)) * .2 + .1
            self.controller._snapshot_at = self.now
        waits = []
        def wait(delay):
            self.now += delay
            self.controller._snapshot_at = self.now  # Orca polling stays fresh.
            waits.append(delay)
            if 0x12 in events or len(waits) > 5000:
                self.controller.stop.set()
                return True
            return False
        with patch('orca_led.controller.time.monotonic', side_effect=lambda: self.now), \
             patch('orca_led.controller.time.time', side_effect=lambda: self.now + 1000), \
             patch.object(self.controller.writer, 'send', side_effect=send), \
             patch.object(self.controller.stop, 'wait', side_effect=wait):
            self.controller._send()
        self.assertEqual(events[0], 0x11)
        self.assertEqual(events[-2:], ['preempted', 0x12])
        self.assertEqual(events[1:-2], [0x13] * (len(events) - 3))

    def test_legacy_sends_no_styles(self):
        self.finish = lambda: True
        self.run_sender()
        self.assertEqual([row[0][0] for row in self.frames], [0x11])
        self.assertEqual(self.controller._theme_status['phase'], 'legacy')

    def test_six_styles_have_both_heartbeats_and_do_not_starve_lease(self):
        self.save()
        self.run_sender()
        opcodes = [row[0][0] for row in self.frames]
        self.assertEqual(opcodes, ([0x11] + [0x13, 0x16, 0x13] * 2) * 3)
        self.assertEqual(self.frames[0][0][1], 91)
        renewed = None
        for frame, _, end in self.frames:
            if frame[0] in (0x11, 0x12, 0x13):
                if renewed is not None: self.assertLess(end - renewed, 15)
                renewed = end
        # Three 8.6 s snapshots + six (2.6 + 8.6 + 2.6) s sets + six waits.
        self.assertAlmostEqual(self.now - 100, 109.2)
        status = json.loads((self.root / 'status.json').read_text())['theme']
        self.assertEqual(status['sent_count'], 6)
        self.assertEqual(status['total'], 6)
        self.assertEqual(status['phase'], 'sent')
        self.assertFalse(status['device_acknowledged'])
        listeners = [args['enabled'] for op, args in self.calls if op == 'listen']
        self.assertEqual(listeners, [False, True])  # Navigation stays responsive during styles.

    def test_invalid_theme_does_not_send_styles_or_stop_normal_bridge(self):
        (self.root / 'theme.json').write_text('{broken')
        self.finish = lambda: True
        self.run_sender()
        self.assertEqual([row[0][0] for row in self.frames], [0x11])
        self.assertEqual(self.controller._theme_status['phase'], 'error')
        self.assertEqual(self.frames[0][0][1], 72)

    def test_revision_change_during_wire_discards_only_unsent_old_styles(self):
        self.save()
        changed = False
        def after(frame):
            nonlocal changed
            if frame[0] == 0x16 and not changed:
                changed = True
                self.config['states'][1]['color'] = '#FEDCBA'
                self.save()
        self.after_send = after
        self.run_sender()
        styles = [row[0] for row in self.frames if row[0][0] == 0x16]
        self.assertEqual([frame[1] for frame in styles], [1, 2, 3, 4, 5, 6])
        self.assertEqual(list(styles[1][2:5]), [254, 220, 186])
        self.assertEqual(self.controller._theme_status['total'], 5)
        self.assertEqual(self.controller._theme_status['revision'], self.config['revision'])

    def test_ui_loss_during_preheartbeat_sends_no_style_or_postheartbeat(self):
        self.save()
        def after(frame):
            if frame[0] == 0x13:
                self.controller._snapshot_at = 0
        self.after_send = after
        self.finish = lambda: True
        self.run_sender()
        self.assertEqual([row[0][0] for row in self.frames], [0x11, 0x13])
        self.assertEqual(self.controller._mapping_activated_at, float('inf'))
        self.assertEqual(self.controller._theme_status['phase'], 'pending')

    def test_ui_loss_during_style_stops_lease_renewal_and_navigation(self):
        self.save()
        def after(frame):
            if frame[0] == 0x16: self.controller._snapshot_at = 0
        self.after_send = after
        self.finish = lambda: True
        self.run_sender()
        self.assertEqual([row[0][0] for row in self.frames], [0x11, 0x13, 0x16])
        self.assertEqual(self.controller._mapping_activated_at, float('inf'))
        self.assertEqual(self.controller._theme_status['sent_count'], 1)
        self.assertIn(('listen', {'enabled': False}), self.calls)

    def test_remapping_during_style_does_not_renew_old_mapping(self):
        self.save()
        def after(frame):
            if frame[0] == 0x16:
                self.controller._snapshot['slots'][0]['id'] = 'other'
                self.controller.stop.set()
        self.after_send = after
        self.run_sender()
        self.assertEqual([row[0][0] for row in self.frames], [0x11, 0x13, 0x16])
        self.assertEqual(self.controller._delivered_ids, ('one',))
        self.assertEqual(self.controller._mapping_activated_at, float('inf'))

    def test_unsafe_slow_style_timing_is_rejected(self):
        self.save()
        self.controller.writer.high = .4
        self.finish = lambda: True
        self.run_sender()
        self.assertEqual([row[0][0] for row in self.frames], [0x11])
        self.assertEqual(self.controller._theme_status['phase'], 'error')
        self.assertIn('lease', self.controller._theme_status['error'])

    def test_disconnect_after_style_resends_complete_theme_on_reconnect(self):
        self.save()
        failed = False
        def after(frame):
            nonlocal failed
            if frame[0] == 0x16 and not failed:
                failed = True
                raise RuntimeError('disconnected')
        self.after_send = after
        self.run_sender()
        styles = [frame[1] for frame, _, _ in self.frames if frame[0] == 0x16]
        self.assertEqual(styles, [1, 1, 2, 3, 4, 5, 6])
        self.assertEqual(sum(op == 'open' for op, _ in self.calls), 2)
        self.assertEqual(self.controller._theme_status['phase'], 'sent')

    def test_continuous_revisions_cannot_starve_full_resynchronization(self):
        self.save()
        revisions = 0
        def after(frame):
            nonlocal revisions
            if frame[0] == 0x16:
                revisions += 1
                self.config['states'][0]['color'] = '#010101' if revisions % 2 else '#020202'
                self.save()
        self.after_send = after
        self.finish = lambda: revisions >= 9
        self.run_sender()
        full_ends = [end for frame, _, end in self.frames if frame[0] == 0x11]
        self.assertGreaterEqual(len(full_ends), 4)
        self.assertLess(max(b-a for a, b in zip(full_ends, full_ends[1:])), 40)
        self.assertEqual(self.controller._theme_status['phase'], 'pending')

    def test_later_one_color_edit_sends_only_that_style(self):
        self.save()
        changed = False
        def on_wait():
            nonlocal changed
            if not changed and self.controller.themes.status()['phase'] == 'sent':
                changed = True
                self.config['states'][2]['color'] = '#112233'
                self.save()
        self.after_wait = on_wait
        self.finish = lambda: (changed and self.controller.themes.status()['phase'] == 'sent'
                               and self.controller.themes.status()['revision'] == self.config['revision'])
        self.run_sender()
        styles = [frame for frame, _, _ in self.frames if frame[0] == 0x16]
        self.assertEqual([frame[1] for frame in styles], [1, 2, 3, 4, 5, 6, 3])
        self.assertEqual(list(styles[-1][2:5]), [17, 34, 51])
        self.assertEqual(self.controller._theme_status['total'], 1)
        self.assertEqual(self.controller._theme_status['sent_count'], 1)

    def test_later_global_brightness_edit_uses_only_a_snapshot(self):
        self.save()
        changed = False
        def on_wait():
            nonlocal changed
            if not changed and self.controller.themes.status()['phase'] == 'sent':
                changed = True
                self.config['global_brightness'] = 150
                self.save()
        self.after_wait = on_wait
        self.finish = lambda: (changed and self.controller.themes.status()['phase'] == 'sent'
                               and self.controller.themes.status()['revision'] == self.config['revision'])
        self.run_sender()
        self.assertEqual(sum(frame[0] == 0x16 for frame, _, _ in self.frames), 6)
        self.assertEqual(self.frames[-1][0], protocol.snapshot([2] + [0] * 11, 150))
        self.assertEqual(self.controller._theme_status['total'], 0)
        self.assertEqual(self.controller._theme_status['sent_count'], 0)
