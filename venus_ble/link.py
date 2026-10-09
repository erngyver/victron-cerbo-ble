"""A BLE GATT link to one device by MAC, for BlueZ on Venus OS.

GattLink is an asynchronous connect state machine (DOWN -> CONNECTING -> UP)
that encodes what this hardware and firmware need:

  * Never StartDiscovery: dbus-ble-sensors scans with raw HCI sockets, which
    breaks discovery for every other process (victronenergy/venus#1597).
    Adapter1.ConnectDevice creates the device object by MAC instead.
  * Venus runs bluetoothd with ReverseServiceDiscovery=false, so a link opened
    by ConnectDevice never gets a GATT client. Its link is dropped and remade
    with Device1.Connect(), which browses services. An already-connected link
    without resolved services is treated the same way.
  * After the first good connect the device is marked Trusted; untrusted,
    unpaired devices are made temporary on disconnect and deleted after 30 s.
  * Optional bonding (require_bond): Pair() before connecting if not paired.
  * Nothing blocks the GLib main loop: D-Bus calls on the radio are async, and
    a whole attempt has a deadline.
  * Backoff runs from retry_min to retry_max and resets only when the owner
    reports real data with alive(), not on connect, so a device that links but
    never answers backs off instead of churning the radio.
  * Writes are queued and issued one at a time: BlueZ allows a single
    outstanding write per characteristic and fails the rest "In Progress".
    Urgent writes (protocol ACKs) go to the front of the queue. Only the
    current attempt's completions drive the queue.
  * A device that fails absent_after attempts in a row is treated as absent and
    retried every absent_retry seconds: each attempt blinds dbus-ble-sensors
    (see scan.py), so retrying an absent device every few minutes kept the
    BLE sensors flapping for as long as it was away.
  * A link that drops while connecting fails the attempt (with backoff).
  * Stale: something was sent, and nothing valid has come back within
    stale_after of the first send still unanswered.

The owner provides callbacks: on_up(link), on_down(link), on_data(bytes). It
drives the link by calling start() when it decides to connect (one attempt on
the radio at a time is the owner's policy), supervise(now) regularly, send(),
alive() and close(). Pair with scan.ScanRestorer: every connect leaves the
controller's scan filter behind.
"""

import logging

import dbus
from gi.repository import GLib

from .util import dbus_error, guarded, mono

DEVICE_IFACE = "org.bluez.Device1"
CHAR_IFACE   = "org.bluez.GattCharacteristic1"
PROPS_IFACE  = "org.freedesktop.DBus.Properties"
DOWN, CONNECTING, UP = "down", "connecting", "up"

# Proxies are built with introspect=False (one D-Bus round trip per call instead
# of three), so dbus-python cannot learn signatures: every call whose arguments
# include a variant passes signature= explicitly.

log = logging.getLogger("venus_ble")


class GattLink:
    def __init__(self, bus, name, mac, write_uuid, notify_uuid, *, on_up, on_down, on_data,
                 adapter="hci0", address_type="public", stale_after=75,
                 require_bond=False, pair_timeout=30, write_type=None,
                 retry_min=10, retry_max=300, connect_timeout=60, attempt_timeout=150,
                 absent_after=3, absent_retry=1800, write_timeout=10):
        self.bus, self.name, self.mac = bus, name, mac
        self.write_uuid, self.notify_uuid = write_uuid.lower(), notify_uuid.lower()
        self.on_up, self.on_down, self.on_data = on_up, on_down, on_data
        self.address_type = address_type
        self.stale_after = stale_after
        self.require_bond, self.pair_timeout = require_bond, pair_timeout
        self.forced_write_type = write_type
        self.retry_min, self.retry_max = retry_min, retry_max
        self.connect_timeout, self.attempt_timeout = connect_timeout, attempt_timeout
        self.absent_after, self.absent_retry = absent_after, absent_retry
        self.write_timeout = write_timeout

        adapter_path = f"/org/bluez/{adapter}"
        self.path = f"{adapter_path}/dev_" + mac.replace(":", "_")
        obj = bus.get_object("org.bluez", self.path, introspect=False,
                             follow_name_owner_changes=True)
        self.props  = dbus.Interface(obj, PROPS_IFACE)
        self.device = dbus.Interface(obj, DEVICE_IFACE)
        self.adapter = dbus.Interface(
            bus.get_object("org.bluez", adapter_path, introspect=False,
                           follow_name_owner_changes=True), "org.bluez.Adapter1")

        self.state    = DOWN
        self.attempt  = 0                # bumped on every teardown; stale callbacks compare it
        self.deadline = 0.0
        self.wchar    = None
        self.wtype    = "request"
        self.notify_signal = None
        self.last_rx  = 0.0
        self.last_tx  = 0.0
        self.unanswered_since = None     # first send after the last valid receive
        self.queue    = []               # (bytes, label) waiting for the write in flight
        self.writing  = False
        self.backoff  = retry_min
        self.next_attempt = 0.0
        self.failures = 0                # consecutive failed attempts since valid data
        self.binding  = None             # attempt whose bind is in progress
        self.connect_issued = None       # attempt for which Device1.Connect() was sent

        bus.add_signal_receiver(self._on_device_props, dbus_interface=PROPS_IFACE,
                                signal_name="PropertiesChanged", path=self.path)
        bus.add_signal_receiver(
            self._on_interfaces_removed, dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesRemoved", path="/")
        bus.add_signal_receiver(
            self._on_bluez_owner, dbus_interface="org.freedesktop.DBus",
            signal_name="NameOwnerChanged", arg0="org.bluez")

    # ------------------------------------------------------------ helpers

    def get(self, prop, default=None):
        try:
            return self.props.Get(DEVICE_IFACE, prop, timeout=5)
        except dbus.DBusException:
            return default

    def current(self, attempt):
        return attempt == self.attempt and self.state == CONNECTING

    def due(self, now):
        return self.state == DOWN and self.next_attempt <= now

    # ------------------------------------------------------------ connect

    def start(self):
        self.binding = self.connect_issued = None
        self.attempt += 1
        a = self.attempt
        self.state = CONNECTING
        self.deadline = mono() + self.attempt_timeout
        if self.get("Address") is None:
            log.info("%s: creating device object via ConnectDevice", self.name)
            self.adapter.ConnectDevice(
                {"Address": dbus.String(self.mac), "AddressType": dbus.String(self.address_type)},
                signature="a{sv}",
                reply_handler=lambda *_: self._after_create(a),
                error_handler=lambda e: self._after_create(a, e),
                timeout=self.connect_timeout)
        elif self.get("Connected"):
            if self.get("ServicesResolved"):
                self._bind(a)
            else:
                # A link we did not open through Connect() (a ConnectDevice link
                # that completed late) has no GATT client; drop it and redo.
                log.info("%s: connected without GATT client; reconnecting", self.name)
                self._reconnect(a)
        else:
            self._bond_then_connect(a)

    @guarded(None)
    def _after_create(self, a, err=None):
        if not self.current(a):
            return
        if err is not None:
            log.warning("%s: ConnectDevice: %s", self.name, dbus_error(err))
        if self.get("Address") is None:
            self.fail("no device object; device out of range or asleep", a)
        elif self.get("Connected"):
            self._reconnect(a)
        else:
            self._bond_then_connect(a)

    def _reconnect(self, a):
        @guarded(False)
        def later():
            self._bond_then_connect(a)
            return False
        self.device.Disconnect(
            reply_handler=lambda: GLib.timeout_add_seconds(2, later),
            error_handler=lambda e: GLib.timeout_add_seconds(2, later),
            timeout=15)

    def _bond_then_connect(self, a):
        if not self.current(a):
            return
        if not self.require_bond or self.get("Paired"):
            self._connect(a)
            return
        # Bonds normally persist: Venus keeps bluetoothd's storage under
        # /data/var/lib/bluetooth. Reaching here means it was lost; re-pairing
        # unattended only works if the device accepts it without pair mode.
        log.warning("%s: not bonded; attempting to pair", self.name)

        @guarded(None)
        def paired(err=None):
            if not self.current(a):
                return
            if err is not None:
                # A NoReply here is normal even when the bond does complete.
                log.debug("%s: Pair(): %s", self.name, dbus_error(err))
            if self.get("Paired"):
                log.info("%s: paired", self.name)
                self._connect(a)
            else:
                self.fail("not bonded - put the device in pair mode and pair it by hand", a)

        self.device.Pair(reply_handler=lambda: paired(), error_handler=lambda e: paired(e),
                         timeout=self.pair_timeout)

    def _connect(self, a):
        if not self.current(a):
            return
        log.info("%s: connecting to %s", self.name, self.mac)
        self.connect_issued = a
        self.device.Connect(
            reply_handler=lambda: self._check_resolved(a),
            error_handler=lambda e: self.fail(f"connect failed: {dbus_error(e)}", a),
            timeout=self.connect_timeout)

    @guarded(None)
    def _check_resolved(self, a):
        if self.current(a) and self.get("ServicesResolved"):
            self._bind(a)
        # otherwise _on_device_props binds when ServicesResolved turns true,
        # or supervise() fails the attempt at the deadline

    def _bind(self, a):
        """Find the characteristics, enable notifications, go UP. Asynchronous:
        StartNotify is a radio round trip (CCCD write) and must not block the loop."""
        if not self.current(a) or self.binding == a:
            return
        self.binding = a
        om = dbus.Interface(self.bus.get_object("org.bluez", "/", introspect=False),
                            "org.freedesktop.DBus.ObjectManager")
        om.GetManagedObjects(
            reply_handler=lambda objs: self._found(a, objs),
            error_handler=lambda e: self.fail(f"listing characteristics failed: {dbus_error(e)}", a),
            timeout=10)

    @guarded(None)
    def _found(self, a, objects):
        if not self.current(a):
            return
        notify_path = write_path = None
        for path, ifaces in objects.items():
            char = ifaces.get(CHAR_IFACE)
            if not char or not str(path).startswith(self.path + "/"):
                continue
            uuid = str(char.get("UUID")).lower()
            # Separate ifs: some devices write and notify on one characteristic.
            if uuid == self.write_uuid:
                write_path = path
                flags = [str(f) for f in char.get("Flags", [])]
                self.wtype = self.forced_write_type or (
                    "request" if "write" in flags else "command")
            if uuid == self.notify_uuid:
                notify_path = path
        if not write_path or not notify_path:
            self.fail("write/notify characteristics not found", a)
            return
        self._remove_notify()
        self.notify_signal = self.bus.add_signal_receiver(
            self._on_notify, dbus_interface=PROPS_IFACE,
            signal_name="PropertiesChanged", path=notify_path)
        wchar = dbus.Interface(self.bus.get_object("org.bluez", write_path, introspect=False),
                               CHAR_IFACE)
        dbus.Interface(self.bus.get_object("org.bluez", notify_path, introspect=False),
                       CHAR_IFACE).StartNotify(
            reply_handler=lambda: self._notifying(a, wchar),
            error_handler=lambda e: self.fail(f"enabling notifications failed: {dbus_error(e)}", a),
            timeout=10)

    @guarded(None)
    def _notifying(self, a, wchar):
        if not self.current(a):
            return
        self.wchar = wchar
        if not self.get("Trusted"):
            try:
                self.props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True),
                               signature="ssv", timeout=5)
                log.info("%s: marked trusted", self.name)
            except dbus.DBusException as e:
                log.warning("%s: could not mark trusted: %s", self.name, dbus_error(e))
        self.state   = UP
        self.last_rx = self.last_tx = mono()
        self.unanswered_since = None
        log.info("%s: link up", self.name)
        self.on_up(self)

    # ------------------------------------------------------------ teardown

    def _remove_notify(self):
        if self.notify_signal:
            self.notify_signal.remove()
            self.notify_signal = None

    def _down(self, retry_in):
        was_up = self.state == UP
        self.attempt += 1
        self.state = DOWN
        self.wchar = None
        self.queue = []
        self.writing = False
        self._remove_notify()
        self.next_attempt = mono() + retry_in
        if was_up:
            self.on_down(self)

    def fail(self, reason, a=None):
        """A connect attempt or a link that is not delivering. Backs off."""
        if a is not None and not self.current(a):
            return
        self.failures += 1
        retry_in = self.backoff
        if self.failures >= self.absent_after:
            retry_in = max(retry_in, self.absent_retry)
        self._down(retry_in)
        if self.failures == self.absent_after:
            log.warning("%s: %s; %d attempts failed in a row, treating as absent: "
                        "retrying every %d min", self.name, reason, self.failures,
                        self.absent_retry // 60)
        else:
            log.warning("%s: %s; retrying in %ds", self.name, reason, retry_in)
        self.backoff = min(self.backoff * 2, self.retry_max)
        if self.get("Connected"):
            self.device.Disconnect(reply_handler=lambda: None,
                                   error_handler=lambda e: None, timeout=15)

    def lost(self, reason):
        """The link went away underneath us. Reconnect promptly, keeping backoff;
        while still connecting it is a failed attempt (with backoff)."""
        if self.state == DOWN:
            return
        if self.state == CONNECTING:
            self.fail(reason, self.attempt)
            return
        self._down(0)
        log.warning("%s: %s", self.name, reason)

    def close(self):
        self._down(self.retry_max)
        if self.get("Connected"):
            try:
                self.device.Disconnect(timeout=5)
            except dbus.DBusException as e:
                log.debug("%s: disconnect on close: %s", self.name, dbus_error(e))

    # ------------------------------------------------------------ periodic

    def supervise(self, now):
        if self.state == CONNECTING and now > self.deadline:
            self.fail(f"connect attempt timed out after {self.attempt_timeout}s", self.attempt)
        elif (self.state == UP and self.unanswered_since is not None
              and now - self.unanswered_since > self.stale_after):
            self.fail(f"no valid reply for {now - self.unanswered_since:.0f}s")

    def send(self, data, label=None, urgent=False):
        """Queue a write (urgent: ahead of everything queued); returns False if
        the link is not up."""
        if self.state != UP or not self.wchar:
            return False
        self.last_tx = mono()
        if self.unanswered_since is None:
            self.unanswered_since = self.last_tx
        item = (bytes(data), label)
        if urgent:
            self.queue.insert(0, item)
        else:
            self.queue.append(item)
        if not self.writing:
            self._write_next(self.attempt)
        return True

    def _write_next(self, a):
        if a != self.attempt:
            return                       # a completion from an earlier link: not ours
        if self.state != UP or not self.queue:
            self.writing = False
            return
        data, label = self.queue.pop(0)
        self.writing = True

        def done(err=None):
            if err is not None:
                log.warning("%s: write %s failed: %s", self.name,
                            label or data.hex(), dbus_error(err))
            self._write_next(a)

        self.wchar.WriteValue([dbus.Byte(b) for b in data], {"type": self.wtype},
                              signature="aya{sv}",
                              reply_handler=lambda: done(),
                              error_handler=lambda e: done(e),
                              timeout=self.write_timeout)

    def alive(self):
        """The owner received valid data: the link is delivering."""
        self.last_rx = mono()
        self.unanswered_since = None
        self.backoff = self.retry_min
        self.failures = 0

    # ------------------------------------------------------------ inbound

    @guarded(None)
    def _on_notify(self, iface, changed, invalidated):
        if "Value" in changed and self.state == UP:
            self.on_data(bytes(changed["Value"]))

    @guarded(None)
    def _on_device_props(self, iface, changed, invalidated):
        if self.state == CONNECTING and changed.get("ServicesResolved"):
            self._bind(self.attempt)
        elif ("Connected" in changed and not changed["Connected"]
              and self.state == CONNECTING and self.connect_issued == self.attempt
              and not self.get("Connected")):
            self.fail("link dropped while connecting", self.attempt)
        elif "Connected" in changed and not changed["Connected"] and self.state == UP:
            # The throwaway ConnectDevice link can report its disconnect after
            # the real link is up; only believe a drop BlueZ still shows.
            if not self.get("Connected"):
                self.lost("link lost")

    @guarded(None)
    def _on_interfaces_removed(self, path, ifaces):
        if path == self.path and DEVICE_IFACE in ifaces:
            self.lost("device object removed")

    @guarded(None)
    def _on_bluez_owner(self, name, old, new):
        self.lost("bluetoothd restarted")
