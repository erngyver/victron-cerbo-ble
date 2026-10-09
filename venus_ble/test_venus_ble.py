#!/usr/bin/env python3
"""Tests for venus_ble against a fake BlueZ.  Run: python3 venus_ble/test_venus_ble.py

dbus, gi and paho are stubbed, so this runs anywhere (Venus's python has no
unittest). The fake models the BlueZ behaviour the link depends on: a link
opened by ConnectDevice has no GATT client, Device1.Connect() resolves services,
bonding, and PropertiesChanged signals.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ---------------------------------------------------------------- stubs

dbus = types.ModuleType("dbus")


class DBusException(Exception):
    def get_dbus_message(self):
        return str(self)

    def get_dbus_name(self):
        return "org.bluez.Error.Failed"


dbus.DBusException = DBusException
dbus.String = str
dbus.Boolean = bool
dbus.Byte = int


class Interface:
    def __init__(self, obj, iface):
        self._obj, self._iface = obj, iface

    def __getattr__(self, member):
        return self._obj.get_dbus_method(member, self._iface)


dbus.Interface = Interface
sys.modules["dbus"] = dbus

gi = types.ModuleType("gi")
repo = types.ModuleType("gi.repository")
TIMERS = []


class GLibStub:
    @staticmethod
    def timeout_add_seconds(seconds, fn):
        TIMERS.append(fn)
        return len(TIMERS)


repo.GLib = GLibStub
gi.repository = repo
sys.modules["gi"] = gi
sys.modules["gi.repository"] = repo

paho = types.ModuleType("paho")
paho_mqtt = types.ModuleType("paho.mqtt")
paho_client = types.ModuleType("paho.mqtt.client")
paho.mqtt = paho_mqtt
paho_mqtt.client = paho_client
paho_client.Client = object
sys.modules.update({"paho": paho, "paho.mqtt": paho_mqtt, "paho.mqtt.client": paho_client})

import venus_ble                                   # noqa: E402
from venus_ble import link as linkmod, scan       # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
W, N = "0000ffe2-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"


class FakeBluez:
    """One device, plus the adapter and ObjectManager."""

    def __init__(self, exists=False, paired=False, connect_fails=False, pair_works=True):
        self.dev = {"exists": exists, "Connected": False, "ServicesResolved": False,
                    "Paired": paired, "Trusted": False}
        self.connect_fails, self.pair_works = connect_fails, pair_works
        self.receivers = []
        self.calls = []
        self.writes = []

    # dbus.SystemBus-like
    def get_object(self, name, path, introspect=True, follow_name_owner_changes=False):
        return FakeObject(self, path)

    def add_signal_receiver(self, handler, **match):
        r = types.SimpleNamespace(handler=handler, match=match, removed=False)
        r.remove = lambda: setattr(r, "removed", True)
        self.receivers.append(r)
        return r

    def emit(self, signal_name, path, *args):
        for r in self.receivers:
            m = r.match
            if (not r.removed and m.get("signal_name") == signal_name
                    and m.get("path", path) == path):
                r.handler(*args)

    def set(self, **props):
        self.dev.update(props)
        self.emit("PropertiesChanged", DEV, "org.bluez.Device1", props, [])


class FakeObject:
    def __init__(self, bluez, path):
        self.b, self.path = bluez, path

    def get_dbus_method(self, member, iface):
        b, dev = self.b, self.b.dev

        def call(*args, reply_handler=None, error_handler=None, **kw):
            b.calls.append(member)
            # introspect=False proxies cannot infer variant signatures: these
            # calls fail on a real Cerbo without an explicit signature=.
            needs = {"ConnectDevice": "a{sv}", "WriteValue": "aya{sv}", "Set": "ssv"}
            if member in needs:
                assert kw.get("signature") == needs[member], \
                    f"{member} needs signature={needs[member]!r}, got {kw.get('signature')!r}"

            def ok(*r):
                return reply_handler(*r) if reply_handler else (r[0] if r else None)

            def err(msg):
                e = DBusException(msg)
                if error_handler:
                    return error_handler(e)
                raise e

            if member == "Get":
                if not dev["exists"]:
                    return err("UnknownObject")
                return {"Address": MAC}.get(args[1], dev.get(args[1]))
            if member == "Set":
                dev[args[1]] = args[2]
                return ok()
            if member == "ConnectDevice":
                dev.update(exists=True, Connected=True, ServicesResolved=False)
                return ok()
            if member == "Disconnect":
                dev.update(Connected=False, ServicesResolved=False)
                return ok()
            if member == "Pair":
                if b.pair_works:
                    dev["Paired"] = True
                return ok()
            if member == "Connect":
                if b.connect_fails:
                    return err("le-connection-abort-by-local")
                dev.update(Connected=True, ServicesResolved=True)
                return ok()
            if member == "GetManagedObjects":
                return ok({f"{DEV}/service1/char1": {"org.bluez.GattCharacteristic1":
                                                 {"UUID": W, "Flags": ["write"]}},
                        f"{DEV}/service1/char2": {"org.bluez.GattCharacteristic1":
                                                 {"UUID": N, "Flags": ["notify"]}}})
            if member == "StartNotify":
                return ok()
            if member == "WriteValue":
                b.writes.append(bytes(args[0]))
                return ok()
            raise AssertionError(f"unexpected call {member}")
        return call


class Clock:
    t = 1000.0


def run_timers():
    while TIMERS:
        TIMERS.pop(0)()


class GattLinkTest(unittest.TestCase):
    def setUp(self):
        TIMERS.clear()
        Clock.t = 1000.0
        linkmod.mono = lambda: Clock.t
        self.events = []

    def make(self, bluez, **kw):
        return venus_ble.GattLink(
            bluez, "dev", MAC, W, N,
            on_up=lambda l: self.events.append("up"),
            on_down=lambda l: self.events.append("down"),
            on_data=lambda d: self.events.append(("data", d)), **kw)

    def test_fresh_device_goes_through_connectdevice_then_connect(self):
        b = FakeBluez(exists=False)
        link = self.make(b)
        link.start()
        run_timers()                     # the 2 s pause after dropping the ConnectDevice link
        self.assertEqual(link.state, venus_ble.UP)
        self.assertEqual([c for c in b.calls if c in ("ConnectDevice", "Disconnect", "Connect")],
                         ["ConnectDevice", "Disconnect", "Connect"])
        self.assertTrue(b.dev["Trusted"])
        self.assertEqual(self.events, ["up"])

    def test_connected_without_services_is_redone(self):
        b = FakeBluez(exists=True)
        b.dev.update(Connected=True, ServicesResolved=False)
        link = self.make(b)
        link.start()
        run_timers()
        self.assertEqual(link.state, venus_ble.UP)
        self.assertIn("Disconnect", b.calls)

    def test_bond_required(self):
        b = FakeBluez(exists=True, paired=False)
        link = self.make(b, require_bond=True)
        link.start()
        self.assertEqual(link.state, venus_ble.UP)
        self.assertLess(b.calls.index("Pair"), b.calls.index("Connect"))

    def test_bond_failure_backs_off(self):
        b = FakeBluez(exists=True, paired=False, pair_works=False)
        link = self.make(b, require_bond=True, retry_min=10)
        link.start()
        self.assertEqual(link.state, venus_ble.DOWN)
        self.assertNotIn("Connect", b.calls)
        self.assertEqual(link.next_attempt, Clock.t + 10)

    def test_backoff_doubles_and_resets_only_on_alive(self):
        b = FakeBluez(exists=True, connect_fails=True)
        link = self.make(b, retry_min=10, retry_max=40, absent_after=10)
        for expected in (10, 20, 40, 40):
            link.start()
            self.assertEqual(link.next_attempt - Clock.t, expected)
        b.connect_fails = False
        link.start()
        self.assertEqual(link.state, venus_ble.UP)
        self.assertEqual(link.backoff, 40)          # a connect alone is not proof
        link.alive()
        self.assertEqual(link.backoff, 10)

    def test_stale_only_after_sending(self):
        b = FakeBluez(exists=True)
        link = self.make(b, stale_after=60)
        link.start()
        Clock.t += 500
        link.supervise(Clock.t)
        self.assertEqual(link.state, venus_ble.UP)   # nothing sent, nothing expected
        link.send(b"\x01")
        Clock.t += 30
        link.supervise(Clock.t)
        self.assertEqual(link.state, venus_ble.UP)   # measured from the send, not last_rx
        link.send(b"\x01")                          # a second send does not reset the clock
        Clock.t += 31
        link.supervise(Clock.t)
        self.assertEqual(link.state, venus_ble.DOWN)
        self.assertEqual(self.events, ["up", "down"])

    def test_alive_clears_unanswered(self):
        b = FakeBluez(exists=True)
        link = self.make(b, stale_after=60)
        link.start()
        for _ in range(10):
            link.send(b"\x01")
            Clock.t += 50
            link.alive()
            link.supervise(Clock.t)
        self.assertEqual(link.state, venus_ble.UP)

    def test_attempt_deadline(self):
        b = FakeBluez(exists=True)
        link = self.make(b, attempt_timeout=150)
        # Connect accepted but services never resolve.
        orig = FakeObject.get_dbus_method

        def slow(self_, member, iface):
            if member == "Connect":
                return lambda *a, reply_handler=None, **k: (
                    b.dev.update(Connected=True), reply_handler())
            return orig(self_, member, iface)
        FakeObject.get_dbus_method = slow
        try:
            link.start()
            self.assertEqual(link.state, venus_ble.CONNECTING)
            Clock.t += 151
            link.supervise(Clock.t)
            self.assertEqual(link.state, venus_ble.DOWN)
        finally:
            FakeObject.get_dbus_method = orig

    def test_notify_reaches_owner_and_write_has_variant_signature(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        link.start()
        b.emit("PropertiesChanged", f"{DEV}/service1/char2", "org.bluez.GattCharacteristic1",
               {"Value": [1, 2]}, [])
        self.assertIn(("data", b"\x01\x02"), self.events)
        self.assertTrue(link.send(b"\x09"))
        self.assertEqual(b.writes, [b"\x09"])

    def test_writes_are_serialised(self):
        # BlueZ fails a second write while one is in flight ("In Progress").
        b = FakeBluez(exists=True)
        link = self.make(b)
        link.start()
        pending = []
        orig = FakeObject.get_dbus_method

        def deferred(self_, member, iface):
            if member == "WriteValue":
                def call(data, opts, reply_handler=None, error_handler=None, **k):
                    if pending:
                        return error_handler(DBusException("In Progress"))
                    pending.append((bytes(data), reply_handler))
                return call
            return orig(self_, member, iface)
        FakeObject.get_dbus_method = deferred
        try:
            for i in range(5):
                self.assertTrue(link.send(bytes([i])))
            done = []
            while pending:
                data, reply = pending.pop(0)
                done.append(data)
                reply()
            self.assertEqual(done, [bytes([i]) for i in range(5)])
        finally:
            FakeObject.get_dbus_method = orig

    def test_absent_device_is_retried_rarely_until_it_answers(self):
        b = FakeBluez(exists=True, connect_fails=True)
        link = self.make(b, retry_min=10, absent_after=3, absent_retry=1800)
        waits = []
        for _ in range(4):
            link.start()
            waits.append(link.next_attempt - Clock.t)
        self.assertEqual(waits, [10, 20, 1800, 1800])
        b.connect_fails = False
        link.start()
        link.alive()
        b.set(Connected=False)               # a later drop: normal, prompt retry
        link.start()
        self.assertEqual(link.state, venus_ble.UP)
        self.assertEqual(link.failures, 0)

    def test_stale_write_completion_does_not_unblock_the_new_link(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        pending = []
        orig = FakeObject.get_dbus_method

        def deferred(self_, member, iface):
            if member == "WriteValue":
                def call(data, opts, reply_handler=None, error_handler=None, **k):
                    pending.append((bytes(data), reply_handler, error_handler))
                return call
            return orig(self_, member, iface)
        FakeObject.get_dbus_method = deferred
        try:
            link.start()
            link.send(b"\x01")               # in flight on attempt 1
            b.set(Connected=False)           # lost
            link.start()                     # relinked
            link.send(b"\x02")               # in flight on the new link
            link.send(b"\x03")               # queued behind it
            old = pending.pop(0)
            old[2](DBusException("NoReply")) # the old write finally times out
            link.send(b"\x04")
            in_flight = [p[0] for p in pending]
            self.assertEqual(in_flight, [b"\x02"])   # still one at a time
        finally:
            FakeObject.get_dbus_method = orig

    def test_urgent_writes_jump_the_queue(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        pending = []
        orig = FakeObject.get_dbus_method

        def deferred(self_, member, iface):
            if member == "WriteValue":
                def call(data, opts, reply_handler=None, error_handler=None, **k):
                    pending.append((bytes(data), reply_handler))
                return call
            return orig(self_, member, iface)
        FakeObject.get_dbus_method = deferred
        try:
            link.start()
            for i in range(5):
                link.send(bytes([0x10 + i]))  # e.g. SUBs
            link.send(b"\x04", urgent=True)  # ACK
            order = []
            while pending:
                data, reply = pending.pop(0)
                order.append(data)
                reply()
            self.assertEqual(order[1], b"\x04")     # right after the write in flight
        finally:
            FakeObject.get_dbus_method = orig

    def test_drop_while_connecting_fails_with_backoff(self):
        b = FakeBluez(exists=True)
        link = self.make(b, retry_min=10)
        orig = FakeObject.get_dbus_method

        def early(self_, member, iface):
            if member == "Connect":
                return lambda *a, reply_handler=None, **k: (
                    b.dev.update(Connected=True), reply_handler())
            return orig(self_, member, iface)
        FakeObject.get_dbus_method = early
        try:
            link.start()
            self.assertEqual(link.state, venus_ble.CONNECTING)
            b.set(Connected=False)
            self.assertEqual(link.state, venus_ble.DOWN)
            self.assertEqual(link.next_attempt - Clock.t, 10)   # backoff, not 0
        finally:
            FakeObject.get_dbus_method = orig

    def test_start_notify_failure_fails_the_attempt(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        orig = FakeObject.get_dbus_method

        def broken(self_, member, iface):
            if member == "StartNotify":
                return lambda *a, error_handler=None, **k: error_handler(DBusException("Failed"))
            return orig(self_, member, iface)
        FakeObject.get_dbus_method = broken
        try:
            link.start()
            self.assertEqual(link.state, venus_ble.DOWN)
            self.assertNotIn("up", self.events)
        finally:
            FakeObject.get_dbus_method = orig

    def test_fake_rejects_missing_signature(self):
        # The guard the real dbus-python enforces at runtime on introspect=False proxies.
        b = FakeBluez(exists=True)
        with self.assertRaises(AssertionError):
            FakeObject(b, DEV).get_dbus_method("WriteValue", None)([1], {})

    def test_late_disconnect_signal_is_ignored(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        link.start()
        b.emit("PropertiesChanged", DEV, "org.bluez.Device1", {"Connected": False}, [])
        self.assertEqual(link.state, venus_ble.UP)  # BlueZ still says connected

    def test_real_link_loss_reconnects_promptly(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        link.start()
        b.set(Connected=False)
        self.assertEqual(link.state, venus_ble.DOWN)
        self.assertEqual(link.next_attempt, Clock.t)
        self.assertEqual(self.events, ["up", "down"])

    def test_close_releases(self):
        b = FakeBluez(exists=True)
        link = self.make(b)
        link.start()
        link.close()
        self.assertFalse(b.dev["Connected"])
        self.assertFalse(link.send(b"\x01"))


class ScanRestorerTest(unittest.TestCase):
    def test_debounced(self):
        calls = []
        scan.restore_ble_scanning = lambda service=None: calls.append(service)
        r = scan.ScanRestorer(min_interval=30, service="/service/x")
        r.settle(100)
        self.assertEqual(calls, [])                  # nothing dirty yet
        r.dirty()
        r.settle(100)
        r.dirty()
        r.settle(110)                                # within the interval: deferred
        self.assertEqual(len(calls), 1)
        r.settle(131)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
