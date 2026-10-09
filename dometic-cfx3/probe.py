#!/usr/bin/env python3
"""Pair with, and diagnose, the CFX3 over BLE.

Run this once with the fridge in Bluetooth pair mode to establish the bond the
bridge depends on:

    svc -d /service/dometic-cfx3          # the fridge takes one central at a time
    python3 probe.py --pair
    svc -u /service/dometic-cfx3

Without --pair it is a health check: connect, resolve GATT, enable
notifications on the DDMP characteristic and report, without pairing. It uses
the bridge's own link code (venus_ble.GattLink), so it exercises the same path
the service runs: ConnectDevice only to create the device object, then
Device1.Connect(), never StartDiscovery (victronenergy/venus#1597).

The fridge's address and address type come from /data/dometic/config.json (the
bridge's config, validated the same way), or from --mac AA:BB:CC:DD:EE:FF
[--address-type public|random].
"""

import logging
import os
import sys
import time

# Deployed, venus_ble/ sits next to this file; only in the repo is it one level up.
_HERE = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(os.path.join(_HERE, "venus_ble")):
    sys.path.insert(1, os.path.dirname(_HERE))

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

import bridge
from venus_ble import DOWN, UP, GattLink, guarded, restore_ble_scanning

TIMEOUT      = 180                   # a full attempt plus pairing
PAIR_TIMEOUT = 60

log = logging.getLogger("probe")


def target(argv):
    """(mac, address_type) from --mac/--address-type, else the bridge config."""
    def arg(flag):
        if flag not in argv:
            return None
        i = argv.index(flag)
        if i + 1 >= len(argv):
            raise SystemExit(f"{flag} needs a value\n\n{__doc__}")
        return argv[i + 1]

    mac = arg("--mac")
    if mac:
        cfg = bridge.validate({"mac": mac, "address_type": arg("--address-type") or "public"},
                              "--mac")
    else:
        cfg = bridge.load_config()
    return cfg["mac"], cfg.get("address_type", "public")


def main():
    want_pair = "--pair" in sys.argv
    mac, address_type = target(sys.argv)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()
    result = {}

    def up(link):
        result["up"] = True
        loop.quit()

    link = GattLink(bus, "fridge", mac, bridge.WRITE_UUID, bridge.NOTIFY_UUID,
                    on_up=up, on_down=lambda link: None, on_data=lambda data: None,
                    adapter=bridge.ADAPTER, address_type=address_type,
                    require_bond=want_pair, pair_timeout=PAIR_TIMEOUT,
                    write_type="request", absent_after=1)
    log.info("%s %s (%s)", "pairing with" if want_pair else "checking", mac, address_type)
    link.start()

    @guarded(True)
    def supervise():
        link.supervise(time.monotonic())
        if link.state == DOWN:
            loop.quit()

    GLib.timeout_add_seconds(1, supervise)
    GLib.timeout_add_seconds(TIMEOUT, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
        paired = bool(link.get("Paired"))
        uuids = [str(u).lower() for u in link.get("UUIDs", [])]
    finally:
        link.close()
        # Any connect leaves the controller's scan filter behind; without this
        # every BLE sensor stays silent (see venus_ble/scan.py).
        restore_ble_scanning()

    if result.get("up"):
        log.info("RESULT: link up, DDMP service %s, bonded=%s - bridge is good to run",
                 "present" if bridge.SVC_UUID in uuids else "MISSING", paired)
        return 0 if bridge.SVC_UUID in uuids else 1
    log.error("RESULT: could not bring the link up (see warnings above). Close the "
              "Dometic phone app; if the fridge is not bonded, put it in Bluetooth "
              "pair mode and rerun with --pair.")
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    sys.exit(main())
