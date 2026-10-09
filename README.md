# Victron Venus OS integrations

Services and Node-RED pieces for a Victron Cerbo GX (Venus OS) in an RV / trailer
setup: Bluetooth LE lithium batteries as a "soft BMS", a Dometic CFX3 fridge as
Venus temperature sensors, and Node-RED automation for MultiPlus charging.

| component | runs as | what it does |
| --- | --- | --- |
| [`softbms/`](softbms/README.md) | Python service on the Cerbo | reads LiTime (Redodo protocol) and PowerUrus/Epoch/RoyPow packs over BLE and caps the DVCC charge current from their cell voltages, temperatures and protection state |
| [`dometic-cfx3/`](dometic-cfx3/README.md) | Python service + Node-RED flow | reads a Dometic CFX3 fridge over BLE and shows its zones as Venus temperature sensors |
| [`venus_ble/`](venus_ble/README.md) | library, shipped with each service | the BLE plumbing the services share, and the Venus/BlueZ pitfalls it works around |
| [`node-red/`](node-red/README.md) | Node-RED flows and function nodes | MultiPlus auto-charge switching, weak-source input current limiting, Mopeka outside temperature |

## Requirements

- A Cerbo GX (or other GX device) on Venus OS v3.7x, with root SSH access
  (Settings > General > Access level: Superuser, SSH on LAN).
- **Settings > Services > Bluetooth: Off** for the BLE services (leave *BLE
  sensors* on). VictronConnect-over-Bluetooth disconnects every BLE device on the
  adapter 60 s after any connection (victronenergy/venus#1587).
- The Python services use only what Venus OS ships (python3, dbus-python,
  PyGObject, paho-mqtt). Everything is installed under `/data`, which survives
  firmware updates.

## Install

Each component has its own `install.sh <ssh-host> <config.json>`; start from its
`config.example.json`. See the component READMEs.

## Read this before using `softbms/`

It is a personal project, not a certified battery management system. It lowers
the DVCC maximum charge current; it never registers as a BMS, so a lost BLE link
cannot stop charging, and the packs' own BMSs remain the protection. It writes
`/Settings/SystemSetup/MaxChargeCurrent`, keeps your own value as a baseline and
restores it on shutdown. Protocol details come from reverse engineering
(credited below) and have not been verified against every protection event. Use
at your own risk.

## Tests

    python3 venus_ble/test_venus_ble.py
    (cd softbms && for t in test_*.py; do python3 $t; done)
    node --test node-red/test_function_nodes.js

No hardware or Venus OS needed: D-Bus, GLib and MQTT are stubbed. GitHub Actions
runs them on every push.

## Credits

The battery protocols in `softbms/` were implemented from the reverse-engineering
work in [patman15/aiobmsble](https://github.com/patman15/aiobmsble) (`redodo_bms.py`,
`roypow_bms.py`, Apache-2.0) and
[mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)
(`litime_ble.py`). No code was copied; the field layouts were checked against
their published test frames and live packs.

## Licence

MIT, see [LICENSE](LICENSE).
