#!/usr/bin/env python3
"""Tests for the protocol modules.  Run: python3 test_protocols.py

RoyPow frames are aiobmsble's reference frames (tests/bms/test_roypow_bms.py);
the LiTime frame was captured from pack1 on 2026-09-30.
"""

import unittest

import controller
import redodo
import roypow

AT = b"AT+STAT\r\n"
ROYPOW = {
    0x02: AT + bytes.fromhex("ead1010fff020404040d2f0d2a0d290d2cf6f5"),
    0x03: bytes.fromhex("ead1011aff033200230000000004" "3b3b3b3c0010000000" "0c07000000eff5") + AT,
    0x04: bytes.fromhex(
        "ead10139ff04016002000203000104" "9a2805000106" "9e2407000108" "7f7d09ffff0a04d40b00164a700c25"
        "0317005145" "05440d2e0d290d144e0000006ff5"),
}
LITIME = bytes.fromhex(
    "000065019355aa00ed330000ec330000fb0cfb0cfb0cfb0c000000000000000000000000000000000000000000"
    "0000008cf3ffff0e000f000000000000007d8c40830000000001000000000000000000000000000000000002006300"
    "64000000020000000a03000092")


def chunks(data, n=20):
    return [data[i:i + n] for i in range(0, len(data), n)]


class RoyPowTest(unittest.TestCase):
    def run_cycle(self, session, frames):
        sent = [session.begin()]
        out = None
        for cmd in (0x02, 0x03, 0x04):
            for c in chunks(frames[cmd]):
                r = session.feed(c)
                if r and r[0] == "send":
                    sent.append(r[1])
                elif r:
                    out = r
        return sent, out

    def test_requests(self):
        self.assertEqual(roypow.request(0x02), bytes.fromhex("ead10104ff02f9f5"))
        self.assertEqual(roypow.request(0x03), bytes.fromhex("ead10104ff03f8f5"))
        self.assertEqual(roypow.request(0x04), bytes.fromhex("ead10104ff04fff5"))

    def test_reference_decode(self):
        sent, (kind, s) = self.run_cycle(roypow.Session(), ROYPOW)
        self.assertEqual(kind, "done")
        self.assertEqual(sent, [roypow.request(c) for c in roypow.CMDS])
        self.assertEqual(s["cells"], [3.375, 3.370, 3.369, 3.372])
        self.assertEqual(s["temps"], [19, 19, 19, 20])
        self.assertEqual(s["cell_temp"], 20)
        self.assertEqual((s["current"], s["voltage"], s["soc"], s["cycles"]), (0.35, 13.48, 96, 2))
        self.assertEqual((s["charge_fet"], s["discharge_fet"], s["problem_code"]), (True, True, 0))
        self.assertIsNone(controller.validate(s))

    def test_discharge_sign_and_charge_fet_off(self):
        f3 = bytearray(ROYPOW[0x03].replace(AT, b""))
        f3[6] |= 0x01                       # discharge flag
        f3[24] &= ~0x04                     # charge FET off
        f3[-2] = roypow.xor(f3[3:-2])
        s = self.run_cycle(roypow.Session(), {**ROYPOW, 0x03: bytes(f3)})[1][1]
        self.assertEqual(s["current"], -0.35)
        self.assertTrue(s["charge_disabled"])

    def test_cell_temp_sensors_option(self):
        s = self.run_cycle(roypow.Session(cell_temp_sensors=[0, 1]), ROYPOW)[1][1]
        self.assertEqual(s["cell_temp"], 19)

    def test_short_frames_are_errors_not_crashes(self):
        # Review 2026-10-02: a checksum-valid but short 0x03 raised IndexError
        # on every poll, so the pack never reported and kept reconnecting.
        body = bytes([14, 0xFF, 0x03]) + bytes(11)
        short3 = roypow.HEAD + body + bytes([roypow.xor(body), roypow.TAIL])
        s = roypow.Session()
        s.begin()
        results = [r for c in chunks(ROYPOW[0x02]) for r in [s.feed(c)] if r]
        results += [r for c in chunks(short3) for r in [s.feed(c)] if r]
        self.assertEqual(results[-1][0], "error")

    def test_late_reply_from_previous_cycle_is_ignored(self):
        s = roypow.Session()
        self.run_cycle(s, ROYPOW)
        s.begin()                                    # new cycle: awaiting 0x02
        late = [s.feed(c) for c in chunks(ROYPOW[0x04])]
        self.assertTrue(all(r is None for r in late))   # no extra request, not stored
        sent, (kind, st) = self.run_cycle(s, ROYPOW)
        self.assertEqual(kind, "done")

    def test_bad_checksum_is_an_error(self):
        f2 = bytearray(ROYPOW[0x02].replace(AT, b""))
        f2[-2] ^= 0xFF
        s = roypow.Session()
        s.begin()
        results = [s.feed(c) for c in chunks(bytes(f2))]
        self.assertEqual([r[0] for r in results if r], ["error"])


class RedodoTest(unittest.TestCase):
    def test_captured_frame(self):
        s = redodo.Session()
        self.assertEqual(s.begin(), bytes.fromhex("000004011355aa17"))
        out = [r for r in (s.feed(c) for c in chunks(LITIME)) if r]
        kind, status = out[-1]
        self.assertEqual(kind, "done")
        self.assertEqual(status["cells"], [3.323] * 4)
        self.assertEqual((status["charge_block"], status["charge_disabled"]), ([], False))
        self.assertIsNone(controller.validate(status))

    def test_noauth_is_fatal(self):
        s = redodo.Session()
        s.begin()
        self.assertEqual(s.feed(b"NOAUTH")[0], "fatal")

    def test_dead_cell_is_rejected(self):
        frame = bytearray(LITIME)
        frame[22:24] = b"\x00\x00"           # cell 4 reads 0 V
        frame[-1] = sum(frame[:-1]) & 0xFF
        s = redodo.Session()
        s.begin()
        status = [r for r in (s.feed(c) for c in chunks(bytes(frame))) if r][-1][1]
        self.assertIsNotNone(controller.validate(status))

    def test_charge_block_mapping(self):
        status = {"protection": 0x0004 | 0x0400, "state": 4}
        redodo.normalise(status)
        self.assertEqual(status["charge_block"], ["overcharge"])   # low-temp bit ignored
        self.assertTrue(status["charge_disabled"])


if __name__ == "__main__":
    unittest.main()
