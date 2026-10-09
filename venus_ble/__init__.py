"""Shared plumbing for BLE services on Victron Venus OS (Cerbo GX).

  link.GattLink        a BLE GATT link by MAC: async connect, Trusted, backoff
  scan.ScanRestorer    keep dbus-ble-sensors hearing after our connects
  mqtt.RetainedMqtt    retained state topics, republished on reconnect
  util                 guarded(), dbus_error(), mono()

Each service ships its own copy alongside its code (install.sh copies it), so
updating one service can never break another.
"""

from .link import CONNECTING, DOWN, UP, GattLink
from .mqtt import RetainedMqtt
from .scan import ScanRestorer, restore_ble_scanning
from .util import dbus_error, guarded, mono

__all__ = ["GattLink", "DOWN", "CONNECTING", "UP", "RetainedMqtt", "ScanRestorer",
           "restore_ble_scanning", "guarded", "dbus_error", "mono"]
