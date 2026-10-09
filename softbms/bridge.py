#!/usr/bin/env python3
"""BLE lithium packs -> DVCC charge limit (soft BMS) for Venus OS.

Holds a BLE link to each pack, polls it for status through a protocol module
(redodo.py for LiTime, roypow.py for PowerUrus/Epoch/RoyPow), runs the control
law in controller.py and applies the result to DVCC MaxChargeCurrent through
chargelimit.py. What to talk to and how is per site, in config.json (see
sites/). Status and the applied limit are also published to the local MQTT
broker for display; nothing depends on MQTT for safety.

The control loop lives here, not in Node-RED, so that one process owns the
setting: it restores the user's baseline on shutdown, and a crash is followed
by a daemontools restart whose first pass restores it too.

The BLE link handling (connect by MAC without discovery, the ConnectDevice /
Connect() workaround, Trusted, async connects, backoff, keeping dbus-ble-sensors
alive after connects) lives in venus_ble/, shared with dometic-cfx3. Only one
pack connects at a time. A pack accepts one central at a time: close the phone
app. (LiTime: its Bluetooth password must be OFF, or every request is answered
NOAUTH.)
"""

import importlib
import json
import logging
import os
import signal
import sys
import time

# Deployed, venus_ble/ sits next to this file. Only in the repo (no local copy)
# is it one level up; never add the parent when deployed, where it is /data and
# any stray /data/<name>.py would shadow a module.
_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(os.path.join(_HERE, "venus_ble")):
    sys.path.insert(1, os.path.dirname(_HERE))

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

import chargelimit
import controller
import readings
from config import CONFIG_FILE, load_config
from venus_ble import (CONNECTING, DOWN, UP, GattLink, RetainedMqtt, ScanRestorer,
                       dbus_error, guarded, mono)

__all__ = ["Pack", "Bridge", "load_config", "guarded", "DOWN", "CONNECTING", "UP"]

# ---------------------------------------------------------------- tunables

STATE_FILE   = "/data/softbms/state.json"
READINGS_DIR = "/data/softbms/readings"
ADAPTER      = "hci0"

MQTT_CLIENT_ID = "softbms-bridge"
BASE         = "softbms"

POLL_EVERY   = 10                    # one pack per tick, round robin
SUPERVISE_EVERY = 5
EVALUATE_EVERY = 10
FLUSH_EVERY  = 60                    # reading log is appended to flash this often
RETRY_MIN    = 10
RETRY_MAX    = 300
CONNECT_TIMEOUT = 60                 # per D-Bus call; the first connect to a pack has taken 44 s
ATTEMPT_TIMEOUT = 150                # whole connect attempt, create -> resolved
LINK_DOWN_GRACE = 60                 # only report a link down after this long
SCAN_RESTORE_EVERY = 30              # at most one dbus-ble-sensors restart per this long

LOG_LEVEL    = logging.INFO          # DEBUG also logs every decoded frame

log = logging.getLogger("softbms")


def stale_after(npacks):
    """Polled but no valid status for this long -> reconnect: three missed
    turns of the round robin, plus slack."""
    return POLL_EVERY * npacks * 3 + 15


class Pack(GattLink):
    """One pack: a GattLink driven by a protocol Session."""

    def __init__(self, bus, name, mac, session, on_status, on_link,
                 address_type="public", stale=75):
        self.session = session
        self.on_status, self.on_link = on_status, on_link
        super().__init__(bus, name, mac, session.WRITE_UUID, session.NOTIFY_UUID,
                         on_up=self._up, on_down=lambda link: on_link(name, False),
                         on_data=self._data, adapter=ADAPTER, address_type=address_type,
                         stale_after=stale, retry_min=RETRY_MIN, retry_max=RETRY_MAX,
                         connect_timeout=CONNECT_TIMEOUT, attempt_timeout=ATTEMPT_TIMEOUT)

    def _up(self, link):
        self.on_link(self.name, True)
        self.poll()

    def poll(self):
        if self.state == UP:
            self.send(self.session.begin(), "status request")

    def _data(self, chunk):
        result = self.session.feed(chunk)
        if result is None:
            return
        kind, value = result
        if kind == "send":
            self.send(value)
        elif kind == "fatal":
            self.fail(value)
        elif kind == "error":
            log.warning("%s: %s", self.name, value)
        else:
            self.alive()
            log.debug("%s: %s", self.name, value)
            self.on_status(self.name, value)


class Bridge:
    def __init__(self, cfg):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus  = dbus.SystemBus()
        self.loop = GLib.MainLoop()
        names = list(cfg["packs"])
        self.statuses = {name: (None, 0.0) for name in names}
        self.down_since = {name: mono() for name in names}
        self.link_reported = {name: None for name in names}
        self.control = controller.Controller(**cfg.get("controller", {}))
        self.protocol_name = cfg["protocol"]
        self.limit = None                # ChargeLimit, created once settings is reachable
        self.published_limit = None
        self.logged_limit = None
        self.dvcc_warned = False
        self.poll_index = 0
        self.readings = readings.ReadingLog(READINGS_DIR)
        self.battery_items = [
            dbus.Interface(self.bus.get_object("com.victronenergy.system", path,
                                               introspect=False, follow_name_owner_changes=True),
                           "com.victronenergy.BusItem")
            for path in ("/Dc/Battery/Voltage", "/Dc/Battery/Current")]
        self.scan = ScanRestorer(SCAN_RESTORE_EVERY)
        self.mqtt = RetainedMqtt(MQTT_CLIENT_ID, BASE)

        protocol = importlib.import_module(cfg["protocol"])
        options = cfg.get("protocol_options", {})
        stale = stale_after(len(cfg["packs"]))
        self.packs = [Pack(self.bus, name, p["mac"], protocol.Session(**options),
                           self.on_status, self.on_link,
                           p.get("address_type", "public"), stale)
                      for name, p in cfg["packs"].items()]

    # ------------------------------------------------------------ pack events

    def on_status(self, name, status):
        self.statuses[name] = (status, mono())
        self.mqtt.publish(f"{name}/status", json.dumps(dict(status, ts=int(time.time()))))
        self.evaluate()
        self.log_reading(name, status)

    def battery(self):
        """SmartShunt voltage and current as systemcalc reports them."""
        values = []
        for item in self.battery_items:
            try:
                v = item.GetValue(timeout=5)
                values.append(float(v) if isinstance(v, (int, float)) else None)
            except dbus.DBusException:
                values.append(None)
        return values

    @guarded(None)
    def log_reading(self, name, status):
        voltage, current = self.battery()
        amps = (self.published_limit or {}).get("amps")
        self.readings.add(name, status, voltage, current, amps)

    def on_link(self, name, up):
        if up:
            self.down_since[name] = None
            self.report_link(name, "connected")
        else:
            self.down_since[name] = mono()

    def report_link(self, name, state):
        if self.link_reported[name] != state:
            self.link_reported[name] = state
            self.mqtt.publish(f"{name}/link", state)

    # ------------------------------------------------------------ timers

    @guarded(True)
    def supervise(self):
        now = mono()
        for p in self.packs:
            p.supervise(now)
            since = self.down_since[p.name]
            if since is not None and now - since > LINK_DOWN_GRACE:
                self.report_link(p.name, "disconnected")
        # One connect attempt on the radio at a time.
        if not any(p.state == CONNECTING for p in self.packs):
            if self.scan.settle(now):
                return                   # connect on the next pass, not during the restart
            for p in self.packs:
                if p.due(now):
                    p.start()
                    self.scan.dirty()
                    break

    @guarded(True)
    def poll(self):
        up = [p for p in self.packs if p.state == UP]
        if up:
            up[self.poll_index % len(up)].poll()
            self.poll_index += 1

    @guarded(True)
    def evaluate(self):
        now = mono()
        if self.limit is None:
            try:
                self.limit = chargelimit.ChargeLimit(self.bus, STATE_FILE)
            except dbus.DBusException as e:
                log.warning("settings not reachable yet: %s", dbus_error(e))
                return
            if self.limit.leftover is not None:
                # Restarted after a crash with a limit in force: keep it and
                # ease off through the controller rather than dropping it
                # before the packs have reconnected.
                self.control.seed(self.limit.leftover, now)
        if not self.dvcc_warned and self.limit.dvcc_on() is False:
            log.warning("DVCC is off: the charge limit has no effect until "
                        "Settings > DVCC is turned on")
            self.dvcc_warned = True
        packs = {n: (s, now - t) for n, (s, t) in self.statuses.items()}
        limit, reason = self.control.step(packs, now)
        applied = self.limit.apply(limit)
        state = {"amps": applied, "controller": limit, "baseline": self.limit.baseline,
                 "conflict": self.limit.in_conflict, "reason": reason}
        logged = (applied, limit, self.limit.baseline, self.limit.in_conflict)
        if logged != self.logged_limit:
            self.logged_limit = logged
            log.info("limit %s (%s)%s", "none" if applied < 0 else f"{applied}A", reason,
                     " - NOT WRITING: another process is changing the setting"
                     if self.limit.in_conflict else "")
        if state != self.published_limit:
            self.published_limit = state
            self.mqtt.publish("limit", json.dumps(state))

    @guarded(False)
    def first_pass(self):
        self.supervise()
        self.evaluate()
        return False

    # ------------------------------------------------------------ lifecycle

    def stop(self, *_):
        log.info("signal received, stopping")
        self.loop.quit()
        return True                      # keep the handler: a repeated signal is harmless

    def run(self):
        log.info("bridge starting (%s), packs %s via %s", self.protocol_name,
                 ", ".join(f"{p.name}={p.mac}" for p in self.packs), ADAPTER)
        GLib.timeout_add_seconds(SUPERVISE_EVERY, self.supervise)
        GLib.timeout_add_seconds(POLL_EVERY, self.poll)
        GLib.timeout_add_seconds(EVALUATE_EVERY, self.evaluate)
        GLib.timeout_add_seconds(FLUSH_EVERY, guarded(True)(self.readings.flush))
        GLib.idle_add(self.first_pass)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.stop)
        try:
            self.loop.run()
        finally:
            log.info("shutting down: restoring charge limit, releasing packs")
            # Each step on its own: one failing must not skip the others.
            steps = [("restore the charge limit", self.limit.restore if self.limit else None),
                     ("flush the reading log", self.readings.flush)]
            steps += [(f"release {p.name}", p.close) for p in self.packs]
            steps.append(("close MQTT", lambda: self.mqtt.close(
                {f"{p.name}/link": "disconnected" for p in self.packs})))
            for what, fn in steps:
                if fn is None:
                    continue
                try:
                    fn()
                except Exception:
                    log.exception("could not %s", what)
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=LOG_LEVEL, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    sys.exit(Bridge(load_config(sys.argv[1] if len(sys.argv) > 1 else CONFIG_FILE)).run())
