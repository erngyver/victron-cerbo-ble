#!/usr/bin/env python3
"""Tests for chargelimit.py against a fake settings service.

Run: python3 test_chargelimit.py   (Venus's python has no unittest; run locally)
"""

import os
import sys
import tempfile
import types
import unittest

try:
    import dbus  # noqa: F401
except ImportError:
    # Minimal stand-in so the tests run without dbus-python.
    dbus = types.ModuleType("dbus")
    dbus.DBusException = type("DBusException", (Exception,), {})
    dbus.Int32 = int

    class Interface:
        def __init__(self, obj, iface):
            self._obj, self._iface = obj, iface

        def __getattr__(self, member):
            return self._obj.get_dbus_method(member, self._iface)

    dbus.Interface = Interface
    sys.modules["dbus"] = dbus

import chargelimit


class FakeItem:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def get_dbus_method(self, member, iface=None):
        def method(*args, **kwargs):
            if member == "GetValue":
                if self.store.get("fail_reads", 0) > 0:
                    self.store["fail_reads"] -= 1
                    raise dbus.DBusException("org.freedesktop.DBus.Error.NoReply")
                return self.store[self.path]
            if member == "SetValue":
                assert kwargs.get("signature") == "v", "BusItem SetValue needs signature='v'"
                if self.store.get("crash_after_set"):
                    self.store[self.path] = int(args[0])
                    raise SystemExit("crash between SetValue and saving")
                if self.store.get("drop_writes"):
                    return 0
                self.store[self.path] = int(args[0])
                return 0
            raise AttributeError(member)
        return method


class FakeBus:
    def __init__(self, value, dvcc=1):
        self.store = {chargelimit.PATH: value, chargelimit.DVCC_PATH: dvcc}

    def get_object(self, name, path, introspect=True, follow_name_owner_changes=False):
        # Without following the name, dbus-python pins the proxy to the
        # service's current process: a localsettings restart kills it for good.
        assert follow_name_owner_changes, f"proxy for {name}{path} must follow name owner changes"
        return FakeItem(self.store, path)


class ChargeLimitTest(unittest.TestCase):
    def setUp(self):
        fd, self.state = tempfile.mkstemp()
        os.close(fd)
        os.unlink(self.state)

    def tearDown(self):
        if os.path.exists(self.state):
            os.unlink(self.state)

    def value(self, bus):
        return bus.store[chargelimit.PATH]

    def test_combine(self):
        self.assertEqual(chargelimit.combine(-1, None), -1)
        self.assertEqual(chargelimit.combine(-1, 5), 5)
        self.assertEqual(chargelimit.combine(50, None), 50)
        self.assertEqual(chargelimit.combine(50, 60), 50)
        self.assertEqual(chargelimit.combine(50, 0), 0)

    def test_limits_and_restores_user_baseline(self):
        bus = FakeBus(50)
        cl = chargelimit.ChargeLimit(bus, self.state)
        self.assertEqual(cl.baseline, 50)
        cl.apply(5)
        self.assertEqual(self.value(bus), 5)
        cl.apply(None)
        self.assertEqual(self.value(bus), 50)
        cl.apply(5)
        cl.restore()
        self.assertEqual(self.value(bus), 50)

    def test_restart_recognises_its_own_leftover(self):
        bus = FakeBus(-1)
        chargelimit.ChargeLimit(bus, self.state).apply(5)     # then "crash"
        cl = chargelimit.ChargeLimit(bus, self.state)
        self.assertEqual(cl.baseline, -1)                     # not 5
        cl.apply(None)
        self.assertEqual(self.value(bus), -1)

    def test_user_change_becomes_baseline(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        bus.store[chargelimit.PATH] = 70                      # user sets 70 A in the GUI
        cl.apply(None)
        self.assertEqual((cl.baseline, self.value(bus)), (70, 70))
        cl.apply(5)
        self.assertEqual(self.value(bus), 5)
        bus.store[chargelimit.PATH] = -1                      # user clears it while we limit
        cl.apply(5)
        self.assertEqual((cl.baseline, self.value(bus)), (-1, 5))

    def test_lost_write_is_retried_not_adopted(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        bus.store["drop_writes"] = True
        cl.apply(5)
        self.assertEqual(self.value(bus), -1)
        bus.store["drop_writes"] = False
        cl.apply(5)
        self.assertEqual((cl.baseline, self.value(bus)), (-1, 5))

    def test_no_write_when_unchanged(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        writes = []
        orig = cl._write
        cl._write = lambda v: (writes.append(v), orig(v))
        for _ in range(5):
            cl.apply(None)
        cl.apply(5)
        cl.apply(5)
        self.assertEqual(writes, [5])


class SafetyTest(unittest.TestCase):
    """The ways the soft BMS could leave charging blocked (review 2026-10-02)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state.json")

    def value(self, bus):
        return bus.store[chargelimit.PATH]

    def test_failed_readback_never_adopts_our_own_write(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        bus.store["fail_reads"] = 1
        orig_read = cl.read
        calls = []

        def read():
            calls.append(1)
            if len(calls) == 2:          # the read-back right after SetValue(0)
                raise dbus.DBusException("NoReply")
            return orig_read()
        bus.store["fail_reads"] = 0
        cl.read = read
        with self.assertRaises(dbus.DBusException):
            cl.apply(0)
        cl.read = orig_read
        self.assertEqual(self.value(bus), 0)
        cl.apply(None)                   # packs gone: must go back to the user's -1
        self.assertEqual((cl.baseline, self.value(bus)), (-1, -1))
        cl.restore()
        self.assertEqual(self.value(bus), -1)

    def test_crash_between_write_and_save_is_recognised_on_restart(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        bus.store["crash_after_set"] = True
        with self.assertRaises(SystemExit):
            cl.apply(0)
        bus.store["crash_after_set"] = False
        cl2 = chargelimit.ChargeLimit(bus, self.state)
        self.assertEqual(cl2.baseline, -1)           # not 0
        self.assertEqual(cl2.leftover, 0)            # held until data, see bridge
        cl2.restore()
        self.assertEqual(self.value(bus), -1)

    def test_clean_stop_has_no_leftover(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        cl.apply(5)
        cl.restore()
        self.assertIsNone(chargelimit.ChargeLimit(bus, self.state).leftover)

    def test_corrupt_state_file_is_ignored(self):
        for junk in ("[1]", '{"written": 45}', '"x"', "42", "{", '{"baseline": "a", "written": 1}'):
            with open(self.state, "w") as f:
                f.write(junk)
            bus = FakeBus(-1)
            cl = chargelimit.ChargeLimit(bus, self.state)
            self.assertEqual(cl.baseline, -1, junk)
            cl.apply(5)
            self.assertEqual(self.value(bus), 5, junk)

    def test_unwritable_state_file_does_not_stop_control(self):
        os.chmod(self.dir, 0o500)
        try:
            bus = FakeBus(50)
            cl = chargelimit.ChargeLimit(bus, self.state)
            self.assertEqual(cl.apply(5), 5)
            cl.restore()                             # must not raise
            self.assertEqual(self.value(bus), 50)
        finally:
            os.chmod(self.dir, 0o700)

    def test_restore_never_raises(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        cl.apply(5)
        bus.store["fail_reads"] = 5
        cl.restore()                                 # logs, does not raise

    def test_another_writer_triggers_conflict_hold(self):
        bus = FakeBus(-1)
        t = [0.0]
        cl = chargelimit.ChargeLimit(bus, self.state, clock=lambda: t[0])
        writes = []
        orig = cl.item.get_dbus_method

        def counting(member, iface=None):
            m = orig(member, iface)
            if member == "SetValue":
                return lambda *a, **k: (writes.append(a[0]), m(*a, **k))[1]
            return m
        cl.item.get_dbus_method = counting
        for i in range(30):
            t[0] += 10
            bus.store[chargelimit.PATH] = 70 if i % 2 else 100   # e.g. a Node-RED flow
            cl.apply(45)
        self.assertTrue(cl.in_conflict)
        self.assertLessEqual(len(writes), chargelimit.CONFLICT_CHANGES)
        t[0] += chargelimit.CONFLICT_HOLD_S + 1
        bus.store[chargelimit.PATH] = 70
        cl.apply(45)
        self.assertFalse(cl.in_conflict)

    def test_apply_reports_what_is_in_force(self):
        bus = FakeBus(-1)
        cl = chargelimit.ChargeLimit(bus, self.state)
        bus.store["drop_writes"] = True
        self.assertEqual(cl.apply(5), -1)            # the write did not land


if __name__ == "__main__":
    unittest.main()
