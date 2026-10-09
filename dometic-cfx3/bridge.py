#!/usr/bin/env python3
"""Dometic CFX3 -> MQTT bridge for Venus OS.

Reads the fridge's DDMP telemetry over BLE and republishes it to the Cerbo's
local broker, where the Node-RED flow in flow.json turns the two zone
temperatures into Venus virtual temperature sensors.

Three things about this device are non-obvious and load-bearing:

  * It must be BONDED. Unbonded, it accepts the LE connection and then sends
    Remote User Terminated Connection (0x13) about 330ms later, having
    exchanged no ATT traffic at all. Run probe.py --pair once, with the fridge
    in Bluetooth pair mode, to establish the bond.
  * It PINGs, and drops the central after roughly 8s if that PING is not
    answered with an ACK. It also retransmits any PUB that is not
    ACKed, every ~2s, forever - so every PUB is ACKed before it is looked at.
  * The BLE link itself (connect by MAC without discovery, the ConnectDevice /
    Connect() workaround, bonding, async connects, backoff, and restarting
    dbus-ble-sensors after connects so BLE sensors keep reporting) is
    venus_ble.GattLink, shared with softbms/.

It holds one connection open. That only works with Settings > Services >
Bluetooth turned OFF on the GX (BLE sensors can stay on): Venus's vesmart-server
- VictronConnect over Bluetooth - runs a keep-alive watchdog that disconnects
every device on the adapter 60s after any connection unless a VE.Smart write
arrives (victronenergy/venus#1587, acknowledged, fix still broken in the v3.80
betas). With it running this link is cut every minute, and the reconnect churn
that caused wedged the Cerbo's RTL8723DU radio for two days in 2026-09.
"""

import json
import logging
import os
import re
import signal
import struct
import sys

# Deployed, venus_ble/ sits next to this file. Only in the repo (no local copy)
# is it one level up; never add the parent when deployed, where it is /data.
_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(os.path.join(_HERE, "venus_ble")):
    sys.path.insert(1, os.path.dirname(_HERE))

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from venus_ble import CONNECTING, UP, GattLink, RetainedMqtt, ScanRestorer, guarded, mono

# ---------------------------------------------------------------- tunables

CONFIG_FILE  = "/data/dometic/config.json"   # {"mac": "..."}; see config.example.json
ADAPTER      = "hci0"

MQTT_CLIENT_ID = "dometic-cfx3-bridge"
BASE         = "dometic/cfx3"

PING_EVERY   = 20                    # our heartbeat: keeps a request outstanding so staleness measures the link
STALE_AFTER  = 90                    # a send unanswered for this long -> force a reconnect
LINK_DOWN_GRACE = 120                # only report Disconnected after this long down
SUPERVISE_EVERY = 5                  # supervisor pass interval
RETRY_MIN    = 5                     # reconnect backoff bounds
RETRY_MAX    = 120
CONNECT_TIMEOUT = 40                 # per D-Bus call
ATTEMPT_TIMEOUT = 120                # whole connect attempt
PAIR_TIMEOUT = 30                    # unattended re-pair if the bond was lost
SCAN_RESTORE_EVERY = 30              # at most one dbus-ble-sensors restart per this long

LOG_LEVEL    = logging.INFO          # DEBUG also logs every decoded value

# ---------------------------------------------------------------- protocol

SVC_UUID     = "537a0300-0995-481f-926c-1604e23fd515"
WRITE_UUID   = "537a0301-0995-481f-926c-1604e23fd515"
NOTIFY_UUID  = "537a0302-0995-481f-926c-1604e23fd515"

PUB, SUB, PING, HELLO, ACK, NAK, NOP = range(7)
ACTION_NAMES = {PUB: "PUB", SUB: "SUB", PING: "PING", HELLO: "HELLO",
                ACK: "ACK", NAK: "NAK", NOP: "NOP"}

# DDMP topic (4 bytes) -> (mqtt suffix, decoder). Zone 2 uses the 16 prefix.
TOPICS = {
    (0,  1, 1, 1): ("fridge/temperature",  "s16d10"),
    (16, 1, 1, 1): ("freezer/temperature", "s16d10"),
    (0,  2, 1, 1): ("fridge/setpoint",     "s16d10"),
    (16, 2, 1, 1): ("freezer/setpoint",    "s16d10"),
    (0,  8, 1, 1): ("fridge/door",         "u8"),
    (16, 8, 1, 1): ("freezer/door",        "u8"),
    (0,  1, 3, 1): ("battery/voltage",     "s16d10"),
    (0,  5, 3, 1): ("power/source",        "source"),
    (0,  2, 3, 1): ("battery/protection",  "u8"),
    # Power state. "Off" on a CFX3 is standby: cooling stops, the electronics
    # and Bluetooth stay up, and it keeps reporting (warming) temperatures.
    # Without these the bridge cannot tell an off fridge from a working one.
    (0,  0, 3, 1): ("power/cooler",        "u8"),
    (0,  0, 1, 1): ("fridge/power",        "u8"),
    (16, 0, 1, 1): ("freezer/power",       "u8"),
    # The fridge keeps its own history: 7 equal buckets over the last hour /
    # day / week, oldest first, as int16 x10 plus a trailing byte of unknown
    # meaning. Published as a JSON list. The doc calls the current "mA"; the
    # values only make sense as 0.1 A (a compressor fridge does not draw 32mA).
    (0,  64, 3, 1): ("dc_current/history/hour", "hist7"),
    (0,  65, 3, 1): ("dc_current/history/day",  "hist7"),
    (0,  66, 3, 1): ("dc_current/history/week", "hist7"),
    (0,  64, 1, 1): ("fridge/history/hour",     "hist7"),
    (0,  65, 1, 1): ("fridge/history/day",      "hist7"),
    (0,  66, 1, 1): ("fridge/history/week",     "hist7"),
    (16, 64, 1, 1): ("freezer/history/hour",    "hist7"),
    (16, 65, 1, 1): ("freezer/history/day",     "hist7"),
    (16, 66, 1, 1): ("freezer/history/week",    "hist7"),
}
SOURCES = {0: "AC", 1: "DC", 2: "Solar"}
NO_DATA = -32768                     # int16 minimum: the fridge's "no value" sentinel

log = logging.getLogger("cfx3")


def decode(kind, payload):
    """Decode a DDMP payload. Temperatures and volts are int16 little-endian
    scaled by 10; door and power source are single bytes."""
    if kind == "s16d10":
        if len(payload) < 2:
            return None
        v = struct.unpack_from("<h", payload)[0]
        return None if v == NO_DATA else v / 10.0
    if kind == "hist7":
        if len(payload) < 14:
            return None
        # Buckets the fridge has not filled yet (it clears history when it
        # restarts) carry the sentinel; publish them as null, not -3276.8.
        return "[" + ", ".join("null" if v == NO_DATA else f"{v / 10.0:.1f}"
                               for v in struct.unpack_from("<7h", payload)) + "]"
    if not payload:
        return None
    if kind == "source":
        return SOURCES.get(payload[0], f"unknown({payload[0]})")
    return payload[0]


def validate(cfg, source="config"):
    if not isinstance(cfg, dict):
        raise ValueError(f"{source}: expected a JSON object")
    mac = str(cfg.get("mac", "")).upper()
    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
        raise ValueError(f"{source}: 'mac' must be the fridge's Bluetooth address")
    if cfg.get("address_type", "public") not in ("public", "random"):
        raise ValueError(f"{source}: address_type must be public or random")
    return dict(cfg, mac=mac)


def load_config(path=CONFIG_FILE):
    with open(path) as f:
        return validate(json.load(f), path)


class Bridge:
    def __init__(self, cfg):
        self.mac = cfg["mac"]
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus  = dbus.SystemBus()
        self.loop = GLib.MainLoop()
        self.down_since = mono()
        self.down_published = False
        self.scan = ScanRestorer(SCAN_RESTORE_EVERY)
        # If this process dies, the broker's last will tells Node-RED, which
        # marks the sensors disconnected rather than leaving a stale
        # temperature on screen.
        self.mqtt = RetainedMqtt(MQTT_CLIENT_ID, BASE)
        self.link = GattLink(
            self.bus, "fridge", self.mac, WRITE_UUID, NOTIFY_UUID,
            on_up=self.on_up, on_down=self.on_down, on_data=self.on_data,
            adapter=ADAPTER, address_type=cfg.get("address_type", "public"),
            stale_after=STALE_AFTER, require_bond=True,
            pair_timeout=PAIR_TIMEOUT, write_type="request",
            retry_min=RETRY_MIN, retry_max=RETRY_MAX,
            connect_timeout=CONNECT_TIMEOUT, attempt_timeout=ATTEMPT_TIMEOUT)

    def publish(self, suffix, value):
        self.mqtt.publish(suffix, value)

    def send(self, label, frame, urgent=False):
        log.debug("-> %s %s", label, bytes(frame).hex())
        return self.link.send(bytes(frame), label, urgent=urgent)

    # ------------------------------------------------------------ link events

    def on_up(self, link):
        self.send("HELLO", [HELLO])
        for topic, (suffix, _) in TOPICS.items():
            self.send(f"SUB {suffix}", [SUB, *topic])
        self.down_since = None
        self.down_published = False
        self.publish("link", "connected")
        log.info("link up, subscribed to %d topics", len(TOPICS))

    def on_down(self, link):
        """Note the time, but do not announce it yet.

        A brief reconnect is not worth surfacing: a fridge temperature does not
        change meaningfully in the seconds a reconnect takes, and flipping the
        sensors to Disconnected each time would fire spurious Venus alarms.
        supervise() announces it if we are still down after LINK_DOWN_GRACE.
        Process death is different - the MQTT last will reports that at once.
        """
        if self.down_since is None:
            self.down_since = mono()

    def on_data(self, frame):
        if not frame:
            return
        # Any frame, including the fridge's ACK of our PING, proves the link.
        self.link.alive()
        action = frame[0]

        if action == PING:
            log.debug("<- PING, answering ACK")
            self.send("ACK", [ACK], urgent=True)
            return

        if action != PUB:
            log.debug("<- %s %s", ACTION_NAMES.get(action, action), frame.hex())
            return

        # ACK before looking at the content. The fridge retransmits an
        # un-ACKed PUB every ~2s indefinitely, whether or not we wanted it.
        self.send("ACK", [ACK], urgent=True)

        if len(frame) < 5:
            log.warning("<- short PUB frame %s", frame.hex())
            return

        topic = tuple(frame[1:5])
        entry = TOPICS.get(topic)
        if not entry:
            log.warning("<- PUB for unsubscribed topic %s payload=%s", topic, frame[5:].hex())
            return

        suffix, kind = entry
        value = decode(kind, frame[5:])
        if value is None:
            log.warning("<- %s undecodable payload %s", suffix, frame[5:].hex())
            return

        log.debug("<- %s = %s", suffix, value)
        self.publish(suffix, value)

    # ------------------------------------------------------------ timers

    @guarded(True)
    def supervise(self):
        now = mono()
        self.link.supervise(now)
        if self.link.state == CONNECTING:
            return
        # Every connect attempt, successful or not, leaves the controller's
        # scan filter behind: restore whenever no attempt is in progress,
        # including while the link is up.
        if self.scan.settle(now) or self.link.state == UP:
            return
        down_for = now - (self.down_since or now)
        if not self.down_published and down_for > LINK_DOWN_GRACE:
            log.warning("link down %.0fs, reporting sensors disconnected", down_for)
            self.publish("link", "disconnected")
            self.down_published = True
        if self.link.due(now):
            self.link.start()
            self.scan.dirty()

    @guarded(True)
    def heartbeat(self):
        """Exercise the link.

        The fridge only publishes on change, so after the initial subscribe
        burst the connection can sit idle for a long time. Our PING and the
        fridge's ACK keep the link's staleness check measuring the link rather
        than the weather in the fridge.
        """
        if self.link.state == UP:
            self.send("PING", [PING])

    @guarded(False)
    def first_pass(self):
        self.supervise()
        return False

    # ------------------------------------------------------------ lifecycle

    def stop(self, *_):
        log.info("signal received, stopping")
        self.loop.quit()
        return True                      # keep the handler: a repeated signal is harmless

    def run(self):
        log.info("bridge starting, target %s via %s", self.mac, ADAPTER)
        GLib.timeout_add_seconds(SUPERVISE_EVERY, self.supervise)
        GLib.timeout_add_seconds(PING_EVERY, self.heartbeat)
        GLib.idle_add(self.first_pass)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.stop)
        try:
            self.loop.run()
        finally:
            log.info("shutting down, releasing the fridge")
            self.link.close()
            self.mqtt.close({"link": "disconnected"})
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=LOG_LEVEL, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    sys.exit(Bridge(load_config(sys.argv[1] if len(sys.argv) > 1 else CONFIG_FILE)).run())
