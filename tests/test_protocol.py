import random
import unittest
from orca_led.protocol import crc8, snapshot, delta, symbols, choose_update, raw_report


class ProtocolTests(unittest.TestCase):
    def test_standard_crc_vector(self):
        self.assertEqual(crc8(b"123456789"), 0xF4)

    def test_all_slot_states_survive_packing(self):
        rng = random.Random(31)
        for _ in range(200):
            states = [rng.randrange(7) for _ in range(12)]
            data = snapshot(states)
            packed = int.from_bytes(data[2:7], "little")
            self.assertEqual([(packed >> (3*i)) & 7 for i in range(12)], states)
            self.assertEqual(packed >> 36, 0)
            self.assertEqual(crc8(data), 0)

    def test_symbol_byte_order(self):
        data = bytes(range(256))
        encoded = symbols(data)
        self.assertEqual((encoded[0], encoded[-1]), (7, 6))
        decoded = bytes(sum((encoded[1+i*4+j]-1) << (6-j*2) for j in range(4)) for i in range(256))
        self.assertEqual(decoded, data)

    def test_invalid_inputs_rejected(self):
        for states in ([0]*11, [0]*13, [8]*12, [-1]*12, [True]*12):
            with self.assertRaises(ValueError): snapshot(states)
        for slot, state in [(-1, 1), (12, 2), (1, 8), (True, 1)]:
            with self.assertRaises(ValueError): delta(slot, state)

    def test_coalesce_and_no_mutation(self):
        previous = (0,)*12
        desired = [0]*12
        desired[11] = 3
        self.assertEqual(choose_update(previous, desired), [delta(11, 3)])
        self.assertEqual(previous, (0,)*12)
        desired[0] = desired[1] = 2
        self.assertEqual(choose_update(previous, desired), [snapshot(desired)])
        self.assertEqual(choose_update(desired, desired), [])

    def test_usb_frame(self):
        p = snapshot([1]*12)
        raw = raw_report(p)
        self.assertEqual(len(raw), 32)
        self.assertEqual(raw[:2], [0xC7, 8])
        self.assertEqual(bytes(raw[2:10]), p)


if __name__ == "__main__": unittest.main()


class WriterPreemptionTests(unittest.TestCase):
    def writer(self):
        from orca_led.native import BluetoothWriter
        sent = []
        class Native:
            def call(self, op, **args):
                sent.append(args["value"])
                return {}
        writer = BluetoothWriter(Native(), 120, 80)
        writer._wait = lambda seconds: None
        return writer, sent

    def test_abort_mid_frame_sends_abort_symbol_and_clears_field(self):
        from orca_led.native import FramePreempted
        from orca_led.protocol import heartbeat, symbols
        writer, sent = self.writer()
        calls = iter([False, False, True])
        with self.assertRaises(FramePreempted):
            writer.send(heartbeat(), abort=lambda: next(calls))
        first = symbols(heartbeat())[:2]
        self.assertEqual(sent, [0, first[0], 0, first[1], 0, 5, 0, 0])

    def test_abort_before_start_sends_nothing_but_idle(self):
        from orca_led.native import FramePreempted
        from orca_led.protocol import heartbeat
        writer, sent = self.writer()
        with self.assertRaises(FramePreempted):
            writer.send(heartbeat(), abort=lambda: True)
        self.assertEqual(sent, [0, 0])

