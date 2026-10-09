# venus_ble: BLE plumbing for services on Victron Venus OS

Shared by `softbms/` and `dometic-cfx3/`. Each service ships its own copy
(`install.sh` copies the non-test `*.py` next to the service), so updating one
service can never break another.

| module | what it gives you |
| --- | --- |
| `link.GattLink` | a GATT link to one device by MAC: async connect state machine (DOWN -> CONNECTING -> UP), queued writes, stale detection, backoff, optional bonding |
| `scan.ScanRestorer` | keeps `dbus-ble-sensors` hearing advertisements after our own connects |
| `mqtt.RetainedMqtt` | retained state topics with a last will, republished on every (re)connect |
| `util` | `guarded()` for GLib/D-Bus callbacks, `dbus_error()`, `mono()` |
| `install-lib.sh` | shared deploy steps for the components' `install.sh` (not deployed) |

## What it encodes (all learned the hard way on a Cerbo GX)

- **No `StartDiscovery`.** `dbus-ble-sensors` scans with raw HCI sockets, which
  breaks discovery for every other process (victronenergy/venus#1597).
  `Adapter1.ConnectDevice` creates the device object by MAC instead.
- **`ConnectDevice` links get no GATT client.** Venus runs bluetoothd with
  `ReverseServiceDiscovery=false`, so services never resolve on that link. It
  is dropped and remade with `Device1.Connect()`.
- **Trusted.** Unpaired, untrusted devices become temporary on disconnect and
  BlueZ deletes the object 30 s later; marking them Trusted keeps them.
- **Address type** must match the device's connectable (ADV_IND) adverts, or
  the connect silently fails after ~40 s.
- **Every connect silences `dbus-ble-sensors`, successful or not.** The kernel
  connects by scanning with the controller's accept list as a filter and
  leaves those parameters behind; `dbus-ble-sensors` only re-enables scanning.
  Call `ScanRestorer.dirty()` when an attempt starts and `settle()` on every
  pass in which no attempt is in progress (including while links are up); it
  restarts `dbus-ble-sensors` (debounced) and returns True, in which case start
  no connect in that pass.
- **One write at a time.** BlueZ fails a second write to a characteristic while
  one is in flight ("In Progress"), so `send()` queues; `send(..., urgent=True)`
  jumps the queue (protocol ACKs), and only the current link's completions
  drive it.
- **Absent devices.** After `absent_after` failed attempts in a row a device is
  retried every `absent_retry` seconds (default 30 min): every attempt blinds
  `dbus-ble-sensors`, successful or not.
- **Never block the GLib loop, never leak an exception from a timer.** All radio
  calls are async with a deadline; wrap callbacks in `guarded()`.
- **Start after the network at boot.** Wi-Fi and Bluetooth share the
  RTL8723DU; BLE traffic while Wi-Fi is joining has left Cerbos off the network
  after every firmware update. Each service's `service/run` waits for a default
  route (capped at 3 min, then starts anyway), plus 30 s, but only within 5 min
  of boot.
- **Backoff resets on data, not on connect** (`alive()`), so a device that links
  but never answers backs off instead of churning the radio.
- **`introspect=False` proxies need explicit `signature=`** on calls with variant
  arguments.

## Using GattLink

```python
link = GattLink(bus, "pack1", mac, write_uuid, notify_uuid,
                on_up=..., on_down=..., on_data=...,       # data: bytes from notify
                address_type="public", stale_after=75, require_bond=False)
# owner's GLib timer, e.g. every 5 s:
link.supervise(mono())
if link.due(mono()):          # DOWN and backoff expired; one attempt at a time is your policy
    link.start(); scan.dirty()
link.send(b"...")             # queued
link.alive()                  # call when a valid reply arrives
link.close()                  # on shutdown
```

Tests run anywhere (dbus, GLib and paho are stubbed): `python3 venus_ble/test_venus_ble.py`.
