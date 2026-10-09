#!/usr/bin/env python3
"""Tests for the per-reading CSV log.  Run: python3 test_readings.py"""

import os
import tempfile
import time
import unittest

import readings


class ReadingLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = readings.ReadingLog(self.dir, keep_days=2)

    def files(self):
        return sorted(f for f in os.listdir(self.dir) if f.endswith(".csv"))

    def test_rows_buffer_until_flush_then_one_header(self):
        status = {"cells": [3.341, 3.34, 3.338, 3.341], "cell_temp": 25, "temps": [22, 22, 23, 25],
                  "current": 1.25,
                  "voltage": 13.35, "soc": 100, "charge_disabled": False}
        now = time.time()
        self.log.add("pack1", status, 13.28, -2.0, 250, now)
        self.assertEqual(self.files(), [])
        self.log.flush()
        self.log.add("pack2", status, 13.28, -2.0, 45, now)
        self.log.flush()
        [name] = self.files()
        lines = open(os.path.join(self.dir, name)).read().splitlines()
        self.assertEqual(lines[0], ",".join(readings.COLUMNS))
        self.assertEqual(len(lines), 3)
        row = dict(zip(readings.COLUMNS, lines[1].split(",")))
        self.assertEqual(row["cells"], "3.341/3.340/3.338/3.341")
        self.assertEqual(row["temps"], "22/22/23/25")
        self.assertEqual((row["max_cell"], row["charge_disabled"], row["limit"]), ("3.341", "0", "250"))

    def test_missing_values_are_blank(self):
        self.log.add("pack1", {"cells": []}, None, None, None)
        self.log.flush()
        [name] = self.files()
        row = open(os.path.join(self.dir, name)).read().splitlines()[1].split(",")
        self.assertEqual(row[1], "pack1")
        self.assertTrue(all(v == "" for v in row[2:]))

    def test_header_change_moves_old_rows_aside(self):
        day = time.strftime("%Y-%m-%d")
        with open(os.path.join(self.dir, f"{day}.csv"), "w") as f:
            f.write("time,pack,cells\n2026-01-01T00:00:00,pack1,3.3\n")
        self.log.add("pack1", {"cells": [3.3] * 4}, None, None, None)
        self.log.flush()
        self.assertIn(f"{day}.old.csv", self.files())
        with open(os.path.join(self.dir, f"{day}.csv")) as f:
            self.assertEqual(f.readline().strip(), ",".join(readings.COLUMNS))

    def test_old_files_are_pruned(self):
        old = os.path.join(self.dir, "2000-01-01.csv")
        open(old, "w").close()
        os.utime(old, (0, 0))
        self.log.add("pack1", {"cells": [3.3] * 4}, None, None, None)
        self.log.flush()
        self.assertNotIn("2000-01-01.csv", self.files())
        self.assertEqual(len(self.files()), 1)


if __name__ == "__main__":
    unittest.main()
