#!/usr/bin/env python3
"""Read one status from a pack over BLE, then disconnect.

    svc -d /service/softbms                  # a pack takes one central at a time
    python3 probe.py pack1                    # a pack from /data/softbms/config.json
    python3 probe.py roypow AA:BB:CC:DD:EE:01 public
    python3 probe.py redodo AA:BB:CC:DD:EE:02 public
    svc -u /service/softbms

Uses the bridge's own Pack connect logic, so it exercises the same code the
service runs (ConnectDevice only to create the object, then Device1.Connect,
never StartDiscovery). Read-only: one status cycle, then the link is released.
Close the phone app first.
"""

import importlib
import json
import logging
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

import bridge
import controller
from venus_ble import restore_ble_scanning

TIMEOUT = 180                        # enough for a full ATTEMPT_TIMEOUT plus a poll

log = logging.getLogger("softbms")


def target(argv):
    """(protocol, mac, address_type, protocol_options, cells_per_pack)"""
    if len(argv) == 2:
        cfg = bridge.load_config()
        p = cfg["packs"][argv[1]]
        return (cfg["protocol"], p["mac"], p.get("address_type", "public"),
                cfg.get("protocol_options", {}),
                controller.Controller(**cfg.get("controller", {})).cells_per_pack)
    if len(argv) in (3, 4):
        return (argv[1], argv[2].upper(), argv[3] if len(argv) == 4 else "public", {},
                controller.CELLS_PER_PACK)
    raise SystemExit(__doc__)


def main():
    protocol, mac, address_type, options, cells = target(sys.argv)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()
    result = {}

    def on_status(name, status):
        result["status"] = status
        loop.quit()

    session = importlib.import_module(protocol).Session(**options)
    pack = bridge.Pack(bus, "probe", mac, session, on_status, lambda name, up: None,
                       address_type, stale=60)
    pack.start()

    @bridge.guarded(True)
    def supervise():
        pack.supervise(time.monotonic())
        if pack.state == bridge.DOWN:
            result["error"] = "connect attempt failed (see warnings above)"
            loop.quit()

    GLib.timeout_add_seconds(1, supervise)
    GLib.timeout_add_seconds(TIMEOUT, lambda: (loop.quit(), False)[1])
    loop.run()
    name = pack.get("Name")
    pack.close()
    restore_ble_scanning()

    status = result.get("status")
    if status is None:
        log.error("RESULT: %s", result.get("error", f"no status within {TIMEOUT}s"))
        return 1
    log.info("device name: %r", str(name) if name is not None else None)
    log.info("status:\n%s", json.dumps(status, indent=2))
    bad = controller.validate(status, cells)
    if bad:
        log.error("RESULT: decoded, but the controller would reject it: %s", bad)
        return 1
    log.info("RESULT: status decoded and valid")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    sys.exit(main())
