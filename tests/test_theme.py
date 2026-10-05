import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from orca_led import protocol
from orca_led.theme import ThemeDelivery, default_theme, parse_theme, validate_theme


class ThemeValidationTests(unittest.TestCase):
    def test_defaults_round_trip_and_state_order(self):
        theme = default_theme()
        value = theme.as_dict()
        value['states'].reverse()
        self.assertEqual(validate_theme(value), theme)
        self.assertEqual(parse_theme(json.dumps(value).encode()), theme)

    def test_six_state_theme_gets_solid_seen_completion_from_complete(self):
        value = default_theme().as_dict()
        value['states'] = [row for row in value['states'] if row['state'] != 7]
        value['states'][3].update(color='#123456', brightness=40, effect='solid', period_ms=3000)
        seen = validate_theme(value).states[-1]
        self.assertEqual((seen.state, seen.color, seen.brightness, seen.effect, seen.period_ms),
                         (7, '#123456', 40, 'solid', 3000))
        value['states'] = value['states'][:5]
        with self.assertRaises(ValueError): validate_theme(value)

    def test_style_wire_bytes_crc_and_scaled_color(self):
        value = default_theme().as_dict()
        value['states'][1].update(color='#7fAaff', brightness=50, period_ms=1700, effect='double')
        style = validate_theme(value).states[1]
        frame = style.frame()
        self.assertEqual(list(frame[:7]), [0x16, 2, 64, 85, 128, 3, 17])
        self.assertEqual(protocol.crc8(frame), 0)
        self.assertEqual(len(frame), 8)

    def test_schema_uuid_unknown_fields_and_boolean_numbers_rejected(self):
        for key, bad in [('schema', True), ('schema', 2), ('revision', 'old'),
                         ('revision', 'x'*36), ('global_brightness', True),
                         ('global_brightness', 256), ('extra', 1)]:
            value = default_theme().as_dict()
            value[key] = bad
            with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                validate_theme(value)

    def test_invalid_styles_and_duplicate_states_rejected(self):
        invalid = [('state', 0), ('state', True), ('color', '#FF000'),
                   ('color', '#ZZ0000'), ('color', [1, 2, 3]), ('brightness', 101),
                   ('brightness', -1), ('brightness', False), ('effect', 'sparkle'),
                   ('effect', 1), ('period_ms', 700), ('period_ms', 10001),
                   ('period_ms', 850), ('period_ms', True), ('extra', 'no')]
        for field, bad in invalid:
            value = default_theme().as_dict()
            value['states'][0][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate_theme(value)
        value = default_theme().as_dict()
        value['states'][0]['state'] = 2
        with self.assertRaises(ValueError): validate_theme(value)
        value['states'].pop()
        with self.assertRaises(ValueError): validate_theme(value)

    def test_malformed_oversized_and_duplicate_json_fields_rejected(self):
        for raw in [b'{', b'\xff', b' ' * 65537, b'{"schema":1,"schema":1}']:
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError): parse_theme(raw)

    def test_protocol_refuses_invalid_effect_or_period_without_truncation(self):
        for args in [(0, 0, 0, 0, 0, 800), (1, 256, 0, 0, 0, 800),
                     (1, 0, True, 0, 0, 800), (1, 0, 0, 0, 6, 800),
                     (1, 0, 0, 0, 1, 801), (1, 0, 0, 0, 1, 10100)]:
            with self.subTest(args=args), self.assertRaises(ValueError): protocol.style(*args)


class ThemeQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'theme.json'
        self.queue = ThemeDelivery(self.path)
        self.config = default_theme().as_dict()

    def save(self, value=None, new_revision=True):
        if value is None: value = self.config
        if new_revision: value['revision'] = str(uuid.uuid4())
        self.path.write_text(json.dumps(value))
        self.queue.refresh()

    def submit_all(self):
        self.queue.brightness_accepted(self.queue.brightness(72), 0)
        while (style := self.queue.next_style()) is not None:
            self.queue.sending()
            self.queue.accepted(style, 1)

    def test_absent_file_retains_legacy_without_style_writes(self):
        self.queue.refresh()
        self.assertIsNone(self.queue.next_style())
        self.assertEqual(self.queue.status(), dict(revision=None, phase='legacy', sent_count=0,
                                                 total=0, error=None, device_acknowledged=False))

    def test_initial_seven_then_single_change_and_explicit_same_save_retry(self):
        self.save()
        self.assertEqual(self.queue.status()['total'], 7)
        self.submit_all()
        self.assertEqual(self.queue.status()['phase'], 'sent')
        self.config['states'][2]['color'] = '#AB00EE'
        self.save()
        self.assertEqual(self.queue.status()['total'], 1)
        self.assertEqual(self.queue.next_style().state, 3)
        self.submit_all()
        self.save()
        self.assertEqual(self.queue.status()['total'], 7)
        self.assertFalse(self.queue.status()['device_acknowledged'])

    def test_brightness_only_waits_for_snapshot_and_never_queues_style(self):
        self.save()
        self.submit_all()
        self.config['global_brightness'] = 123
        self.save()
        self.assertIsNone(self.queue.next_style())
        self.assertEqual(self.queue.status()['total'], 0)
        self.assertEqual(self.queue.status()['phase'], 'pending')
        self.queue.brightness_accepted(72, 2)
        self.assertEqual(self.queue.status()['phase'], 'pending')
        self.queue.brightness_accepted(123, 3)
        self.assertEqual(self.queue.status()['phase'], 'sent')

    def test_midflight_revision_compares_actual_submitted_cache(self):
        self.save()
        first = self.queue.next_style()
        self.queue.accepted(first, 1)
        self.config['states'][4]['effect'] = 'off'
        self.save()
        self.assertEqual(self.queue.status()['total'], 6)
        self.assertEqual(self.queue.next_style().state, 2)
        self.config['states'][0]['color'] = '#101010'
        self.save()
        self.assertEqual(self.queue.status()['total'], 7)
        self.assertEqual(self.queue.next_style().state, 1)

    def test_reconnect_resends_all_and_no_timed_resends(self):
        self.save()
        self.submit_all()
        self.assertIsNone(self.queue.next_style(10**12))
        self.queue.restart()
        self.assertEqual(self.queue.status()['total'], 7)
        self.assertEqual(self.queue.status()['sent_count'], 0)

    def test_protocol_one_firmware_never_receives_seen_style(self):
        self.queue.max_state = 6
        self.save()
        self.assertEqual(self.queue.status()['total'], 6)
        self.submit_all()
        self.assertEqual(self.queue.status()['phase'], 'sent')

    def test_bad_revision_blocks_theme_without_overwriting_last_valid(self):
        self.save()
        previous = self.queue.theme
        self.config['states'][0]['color'] = '#ABABAB'
        self.save(new_revision=False)
        self.assertIsNone(self.queue.next_style())
        self.assertIs(self.queue.theme, previous)
        self.assertEqual(self.queue.status()['phase'], 'error')
        self.save()
        self.assertEqual(self.queue.status()['phase'], 'pending')

    def test_unreadable_file_can_recover_without_content_change(self):
        self.save()
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            self.queue.refresh()
        self.assertEqual(self.queue.status()['phase'], 'error')
        self.queue.refresh()
        self.assertEqual(self.queue.status()['phase'], 'pending')

    def test_remove_live_file_restores_defaults_then_legacy(self):
        self.config['states'][0]['color'] = '#123456'
        self.save()
        self.submit_all()
        self.path.unlink()
        self.queue.refresh()
        self.assertEqual(self.queue.status()['total'], 1)
        self.assertIsNone(self.queue.status()['revision'])
        self.assertEqual(self.queue.next_style().color, '#2A2A2A')
        self.submit_all()
        self.assertEqual(self.queue.status()['phase'], 'legacy')
