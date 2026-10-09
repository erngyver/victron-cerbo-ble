# Victron Multiplus Node-RED — Project Instructions

## Project-Specific Rules

- Victron Cerbo GX (Venus OS) integrations: Python services (`softbms/`, `dometic-cfx3/`, sharing `venus_ble/`) and Node-RED pieces under `node-red/`. See `README.md` for the component map.
- Use `node.warn()` / `node.error()` for logging — never bare `console.log` in Node-RED function nodes. Function-node tests: `node --test node-red/test_function_nodes.js`; CI (`.github/workflows/tests.yml`) runs every suite.
- The Cerbo communicates via D-Bus and MQTT; preserve those integration patterns when making changes.
- The Python services run on the Cerbo under daemontools. Use the `logging` module there, never `print`.
- Anything deployed to the Cerbo must live under `/data` — everything else is wiped by firmware updates. `/service` is tmpfs, so service symlinks are recreated from `/data/rc.local` at boot.
- Never call BlueZ `StartDiscovery` from code running on the Cerbo. `dbus-ble-sensors` scans with raw HCI sockets, which makes discovery fail for every other process (victronenergy/venus#1597). Connect to BLE devices by MAC via `ConnectDevice` instead.
- BLE services share `venus_ble/` (GattLink, ScanRestorer, RetainedMqtt): put BLE link fixes there, not in a service, and read `venus_ble/README.md` before changing BLE behaviour. The rules below are what it encodes.
- Venus runs bluetoothd with `ReverseServiceDiscovery=false`, so a link opened by `ConnectDevice` never gets a GATT client. Use `ConnectDevice` only to create the device object, then drop that link and reconnect with `Device1.Connect()`. Mark unbonded devices `Trusted` after the first good connect, or BlueZ deletes the object 30 s after each disconnect.
- Never block the GLib main loop on the radio (use async D-Bus calls with a deadline), and never let an exception escape a GLib timer callback: PyGObject removes the timer for good.
- With `introspect=False` proxies, pass `signature=` on any D-Bus call whose arguments include a variant (e.g. `a{sv}`, `ssv`, BusItem `SetValue` `v`); dbus-python cannot infer it.
- Nothing may leave a persisted Venus setting (e.g. DVCC `MaxChargeCurrent`) changed when the process that owns it stops: restore the user's value on shutdown and on the next start.
- BlueZ allows one outstanding write per characteristic (others fail "In Progress"); `GattLink.send()` queues for this.
- Python tests run locally, one file at a time (`(cd softbms && for t in test_*.py; do python3 $t; done)`, `python3 venus_ble/test_venus_ble.py`); Venus's python has no `unittest`. One codebase serves every Cerbo. Real per-installation values (hosts, IPs, Bluetooth addresses, pack layouts) live only in `sites/<host>/*.json`, which are git-ignored; code and docs use `config.example.json` and placeholder addresses. Deploy with `<component>/install.sh <host> sites/<host>/<component>.json` from the repo root (installers `cd` to their own directory and share `venus_ble/install-lib.sh`).
- Every BLE `Connect()` leaves the kernel's accept-list scan filter on the controller, and `dbus-ble-sensors` only re-enables scanning, so all Ruuvi/Mopeka sensors go silent. This happens on successful connects too, not only failed ones. After any connect attempt, restart `dbus-ble-sensors` (`svc -t /service/dbus-ble-sensors`; `ScanRestorer` does this). When checking BLE work, count *distinct* advertiser addresses in `btmon`, not reports: an accept-listed device alone can make the count look healthy.
- Use the address type from a device's connectable (ADV_IND) adverts when calling `ConnectDevice`; a wrong type fails silently after ~40 s.
- At boot, BLE services must not start until the network is up (default route), capped so they still run with no network: the RTL8723DU's Wi-Fi has repeatedly failed to rejoin after reboots (firmware updates) when BLE traffic started while it was joining (venus#1618). Both `service/run` scripts implement this; keep it in any new BLE service.
