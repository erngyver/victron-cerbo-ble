#!/usr/bin/env python3
"""Tests for config.py.  Run: python3 test_config.py"""

import json
import os
import tempfile
import unittest

import config


def write(cfg):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path


GOOD = {"protocol": "roypow",
        "packs": {"pack1": {"mac": "aa:bb:cc:dd:ee:01", "address_type": "public"}},
        "controller": {"cells_per_pack": 4}}


class ConfigTest(unittest.TestCase):
    def test_example_config_is_valid(self):
        config.load_config(os.path.join(os.path.dirname(__file__), "config.example.json"))

    def test_mac_is_upper_cased(self):
        # BlueZ object paths are upper case: a lower-case MAC never connected.
        cfg = config.load_config(write(GOOD))
        self.assertEqual(cfg["packs"]["pack1"]["mac"], "AA:BB:CC:DD:EE:01")

    def test_rejects(self):
        cases = {
            "no packs": {**GOOD, "packs": {}},
            "missing mac": {**GOOD, "packs": {"p": {}}},
            "bad mac": {**GOOD, "packs": {"p": {"mac": "not a mac"}}},
            "short mac": {**GOOD, "packs": {"p": {"mac": "AA:BB:CC:DD:EE"}}},
            "address type": {**GOOD, "packs": {"p": {"mac": "AA:BB:CC:DD:EE:01",
                                                     "address_type": "static"}}},
            "unknown protocol": {**GOOD, "protocol": "os"},
            "bad controller key": {**GOOD, "controller": {"nope": 1}},
            "bad protocol option": {**GOOD, "protocol_options": {"nope": 1}},
        }
        for what, cfg in cases.items():
            with self.assertRaises((ValueError, TypeError), msg=what):
                config.load_config(write(cfg))


if __name__ == "__main__":
    unittest.main()
