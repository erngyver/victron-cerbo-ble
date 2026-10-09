#!/usr/bin/env python3
"""Tests for the charge-current control law.  Run: python3 test_controller.py"""

import unittest

import controller as c


def pack(cells=(3.32,) * 4, temp=14, block=(), disabled=False):
    return {"cells": list(cells), "cell_temp": temp, "charge_block": list(block),
            "charge_disabled": disabled}


def both(p1=None, p2=None, age=5):
    return {"pack1": (p1 or pack(), age), "pack2": (p2 or pack(), age)}


class TaperTest(unittest.TestCase):
    def test_absorption_does_not_engage(self):
        # 14.2 V absorption, cells reading 10-15 mV high.
        self.assertIsNone(c.taper(3.57))
        self.assertIsNone(c.taper(3.59))

    def test_taper_is_monotonic_and_quantised(self):
        prev = c.TAPER_MAX_A
        v = 3.591
        while v < 3.66:
            a = c.taper(v)
            self.assertLessEqual(a, prev)
            self.assertEqual(a % c.QUANTUM_A, 0)
            self.assertGreaterEqual(a, c.TAPER_MIN_A)
            prev, v = a, v + 0.001
        self.assertEqual(c.taper(3.63), c.TAPER_MIN_A)
        self.assertEqual(c.taper(3.70), c.TAPER_MIN_A)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.k = c.Controller()
        self.t = 1000.0

    def step(self, packs, dt=10):
        self.t += dt
        return self.k.step(packs, self.t)[0]

    def test_normal_is_no_limit(self):
        self.assertIsNone(self.step(both()))

    def test_absorption_never_latches(self):
        # A whole absorption at 3.57 V after a brief 3.605 V excursion must
        # return to no limit, not park the bank at a reduced current.
        self.assertEqual(self.step(both(pack((3.605, 3.57, 3.57, 3.57)))), 35)
        for _ in range(60):
            a = self.step(both(pack((3.57,) * 4)))
        self.assertIsNone(a)

    def test_tightens_at_once_loosens_slowly(self):
        self.assertEqual(self.step(both(pack((3.64, 3.5, 3.5, 3.5)))), c.TAPER_MIN_A)
        # Cell relaxes: limit rises by LOOSEN_STEP_A per LOOSEN_EVERY_S, no bounce.
        seen = [self.step(both(), dt=10) for _ in range(30)]
        steps = [a for i, a in enumerate(seen) if i == 0 or a != seen[i - 1]]
        self.assertEqual(steps, [5, 25, 45, 60, None])

    def test_release_passes_through_taper_max(self):
        # 2026-10-01: 45 A + 20 jumped straight to no limit and the cell spiked.
        self.step(both(pack((3.60, 3.5, 3.5, 3.5))))
        self.assertEqual(self.k.applied, 45)
        self.step(both(), dt=c.LOOSEN_EVERY_S)
        self.assertEqual(self.k.applied, c.TAPER_MAX_A)
        self.step(both(), dt=c.LOOSEN_EVERY_S)
        self.assertIsNone(self.k.applied)

    def test_reason_names_pack_and_cell(self):
        _, reason = self.k.step(both(p2=pack((3.5, 3.5, 3.62, 3.5))), self.t)
        self.assertTrue(reason.startswith("pack2 cell3 3.620V"), reason)

    def test_runaway_cell_does_not_oscillate(self):
        # Cell alternates between high while charging and relaxed when capped.
        writes = set()
        prev = None
        for i in range(120):
            v = 3.64 if (prev is None or prev > 20) else 3.58
            a = self.step(both(pack((v, 3.5, 3.5, 3.5))))
            if a != prev:
                writes.add(i)
            prev = a
        self.assertLess(len(writes), 12)   # loosen back-off bounds the cycle

    def test_missing_pack_does_not_cancel_other_packs_limit(self):
        self.assertEqual(self.step(both(pack((3.64, 3.5, 3.5, 3.5)))), c.TAPER_MIN_A)
        packs = {"pack1": (pack((3.64, 3.5, 3.5, 3.5)), 5), "pack2": (None, 0)}
        self.assertEqual(self.step(packs), c.TAPER_MIN_A)
        packs = {"pack1": (pack((3.64, 3.5, 3.5, 3.5)), 5), "pack2": (pack(), 500)}
        self.assertEqual(self.step(packs), c.TAPER_MIN_A)

    def test_no_valid_data_eases_to_no_limit(self):
        self.step(both(pack((3.64, 3.5, 3.5, 3.5))))
        for _ in range(40):
            a = self.step({"pack1": (None, 0), "pack2": (None, 0)})
        self.assertIsNone(a)

    def test_malformed_status_is_ignored_not_trusted(self):
        for bad in ({}, {"cells": []}, pack((3.3, 3.3, 3.3)), pack((0.0, 3.3, 3.3, 3.3)),
                    {**pack(), "cell_temp": None}, {**pack(), "charge_block": None},
                    {**pack(), "charge_disabled": None}, "x"):
            k = c.Controller()
            limit, reason = k.target({"pack1": (bad, 5), "pack2": (None, 0)}, 0)
            self.assertIsNone(limit, bad)
            self.assertIn("invalid", reason)
        # A malformed pack must not hide the other pack's high cell.
        limit, _ = c.Controller().target(
            {"pack1": ({"cells": []}, 5), "pack2": (pack((3.64, 3.5, 3.5, 3.5)), 5)}, 0)
        self.assertEqual(limit, c.TAPER_MIN_A)

    def test_temperature_hysteresis(self):
        self.assertEqual(self.step(both(pack(temp=45))), c.TEMP_TAPER_A)
        self.assertEqual(self.step(both(pack(temp=44))), c.TEMP_TAPER_A)   # held
        self.assertEqual(self.step(both(pack(temp=45))), c.TEMP_TAPER_A)
        self.assertEqual(self.step(both(pack(temp=50))), 0)
        self.assertEqual(self.step(both(pack(temp=48))), 0)                # held
        self.assertEqual(self.k.target(both(pack(temp=46)), self.t)[0], c.TEMP_TAPER_A)
        self.assertIsNone(self.k.target(both(pack(temp=41)), self.t)[0])

    def test_confirmed_protection_blocks_and_holds(self):
        self.assertEqual(self.step(both(p2=pack(block=["overcharge"], disabled=True))), 0)
        self.assertEqual(self.step(both(), dt=c.PROTECT_HOLD_S - 20), 0)
        self.assertIsNone(self.k.target(both(), self.t + 30)[0])

    def test_unconfirmed_protection_caps_at_taper_minimum(self):
        # A reverse-engineered bit alone must not stop charging.
        limit, reason = self.k.target(both(p2=pack(block=["overcharge"])), 0)
        self.assertEqual(limit, c.TAPER_MIN_A)
        self.assertIn("unconfirmed", reason)
        # Corroborated by a high cell or a high temperature: 0 A.
        self.assertEqual(c.Controller().target(
            both(p2=pack((3.6, 3.5, 3.5, 3.5), block=["overcharge"])), 0)[0], 0)
        self.assertEqual(c.Controller().target(
            both(p2=pack(temp=46, block=["high temp charge"])), 0)[0], 0)

    def test_above_range_readings_hold_not_release(self):
        # Review 2026-10-02: a pack reading 101 C or 4.01 V used to be
        # discarded as garbage, releasing its limit at the worst moment.
        self.assertEqual(self.step(both(pack(temp=52))), 0)
        for _ in range(60):
            a = self.step(both(pack(temp=101)))
        self.assertEqual(a, c.TAPER_MIN_A)
        k = c.Controller()
        self.assertEqual(k.target(both(pack((4.01, 3.5, 3.5, 3.5))), 0)[0], c.TAPER_MIN_A)
        self.assertEqual(k.target({"pack1": (pack(temp=101), 5), "pack2": (None, 0)}, 0)[0],
                         c.TAPER_MIN_A)
        # Low-side nonsense is still garbage (contributes nothing).
        self.assertIsNone(k.target({"pack1": (pack((0.5, 3.3, 3.3, 3.3)), 5),
                                    "pack2": (None, 0)}, 0)[0])

    def test_losing_all_packs_releases_at_normal_pace(self):
        # Build the loosen back-off up to its maximum with a cycling cell...
        prev = None
        for _ in range(400):
            v = 3.64 if (prev is None or prev > 20) else 3.58
            prev = self.step(both(pack((v, 3.5, 3.5, 3.5))))
        self.assertGreater(self.k.loosen_every, c.LOOSEN_EVERY_S)
        # ...then every link drops: no hour-long hold at 5 A.
        seen = [self.step({"pack1": (None, 0), "pack2": (None, 0)}) for _ in range(40)]
        self.assertIsNone(seen[-1])

    def test_seed_resumes_from_leftover(self):
        self.k.seed(5, self.t)
        self.assertEqual(self.step({"pack1": (None, 0), "pack2": (None, 0)}, dt=10), 5)

    def test_cold_is_not_a_limit(self):
        self.assertIsNone(self.step(both(pack(temp=-5))))

    def test_one_pack_charge_disabled_caps(self):
        self.assertEqual(self.step(both(pack(disabled=True))), c.PER_PACK_A)
        self.assertIsNone(c.Controller().target(both(pack(disabled=True),
                                                     pack(disabled=True)), 0)[0])

    def test_charge_disabled_scales_with_accepting_packs(self):
        three = {"p1": (pack(disabled=True), 5), "p2": (pack(), 5), "p3": (pack(), 5)}
        self.assertEqual(c.Controller(per_pack_a=100).target(three, 0)[0], 200)
        # A silent pack is not counted as accepting.
        three["p3"] = (None, 0)
        self.assertEqual(c.Controller(per_pack_a=100).target(three, 0)[0], 100)

    def test_overrides(self):
        k = c.Controller(taper_start_v=3.62, taper_end_v=3.66, cells_per_pack=4)
        self.assertIsNone(k.target(both(pack((3.61, 3.5, 3.5, 3.5))), 0)[0])
        self.assertEqual(k.target(both(pack((3.67, 3.5, 3.5, 3.5))), 0)[0], c.TAPER_MIN_A)
        with self.assertRaises(ValueError):
            c.Controller(no_such_setting=1)


if __name__ == "__main__":
    unittest.main()
