"""Keep dbus-ble-sensors hearing advertisements after our own connects.

Every LE Connect() makes the kernel scan with the controller's accept list as a
filter, and it leaves those scan parameters on the controller when the connect
ends. dbus-ble-sensors only re-enables scanning and never re-sets parameters,
so after any connect it hears nothing and every Ruuvi/Mopeka sensor goes
silent. Restarting it makes it reopen hci0 with its own parameters.
"""

import logging
import os
import subprocess

from .util import mono

BLE_SENSORS = "/service/dbus-ble-sensors"

log = logging.getLogger("venus_ble")


def restore_ble_scanning(service=BLE_SENSORS):
    if not os.path.exists(service):
        return False
    try:
        subprocess.run(["svc", "-t", service], timeout=10, check=True)
        log.info("restarted dbus-ble-sensors to restore BLE sensor scanning")
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.error("could not restart dbus-ble-sensors: %s", e)
        return False


class ScanRestorer:
    """Call dirty() whenever a connect attempt starts, and settle() on every
    pass in which no attempt is in progress (including while links are up:
    successful connects blind the sensors too); restores at most once per
    min_interval."""

    def __init__(self, min_interval=30, service=BLE_SENSORS):
        self.min_interval = min_interval
        self.service = service
        self.pending = False
        self.last = -min_interval

    def dirty(self):
        self.pending = True

    def settle(self, now=None):
        """Restore if a connect has dirtied the controller since the last
        restore. Returns True if dbus-ble-sensors was restarted: the caller
        should not start a connect in the same pass, so the restart and the
        kernel's connect scan do not overlap."""
        now = mono() if now is None else now
        if self.pending and now - self.last >= self.min_interval:
            self.pending = False
            self.last = now
            restore_ble_scanning(self.service)
            return True
        return False
