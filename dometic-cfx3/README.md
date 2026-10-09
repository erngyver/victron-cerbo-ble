# Dometic CFX3 -> Venus OS

Brings a CFX3-75DZ's fridge and freezer zones into a Cerbo GX as virtual
temperature sensors, over Bluetooth LE.

`bridge.py` runs on the Cerbo, reads DDMP telemetry from the fridge over BLE and
publishes it to the local broker. `flow.json` is the Node-RED side: it maps the
two zone temperatures onto `victron-virtual` temperature devices, typed Fridge
and Freezer, so they appear on the GX display and in VRM with alarm limits.

## Install

First, on the GX: **Settings → Services → Bluetooth → Off** (leave *BLE sensors*
on). Without this the link is cut every 60 seconds; see below.

    cp config.example.json my-fridge.json      # set "mac" to the fridge's address
    ./install.sh <ssh-host> my-fridge.json

Then pair, once, with the fridge in Bluetooth pair mode and the Dometic phone
app closed:

    ssh <host> 'svc -d /service/dometic-cfx3; python3 /data/dometic/probe.py --pair; svc -u /service/dometic-cfx3'

Finally import `flow.json` in Node-RED (Menu -> Import) and deploy.

## Why it is built this way

The fridge **must be bonded**. Unbonded, it accepts the LE connection and then
sends `Remote User Terminated Connection (0x13)` about 330ms later, having
exchanged no ATT traffic at all.

It also **PINGs, and drops the central after about 8s if the PING is not
answered** with an ACK. The bridge answers every one, and sends its own every 20s.
And it **retransmits any PUB that is not ACKed every ~2s, indefinitely** - so the
bridge ACKs every PUB before deciding whether it cares about the topic.

Connections are made by MAC via BlueZ `ConnectDevice`, which performs no
discovery. `dbus-ble-sensors` scans using raw HCI sockets, which breaks BlueZ
`StartDiscovery` for every other process (victronenergy/venus#1597) — but
connect-by-MAC is unaffected. Nothing here may call `StartDiscovery`.

The BLE link itself (bonding, connect by MAC, async connects, backoff, queued
writes) is `venus_ble.GattLink`, shared with `softbms/`; see `venus_ble/README.md`.

After every connect attempt the bridge restarts `dbus-ble-sensors`: an LE
connect leaves the kernel's accept-list scan filter on the controller, which
otherwise silences every Ruuvi/Mopeka sensor until `dbus-ble-sensors` reopens
hci0 (see `softbms/README.md`, BLE notes). This is the likely cause of the
"BLE scanning deaf for 47h" episode below.

If the bridge dies, the broker publishes its last will and the flow marks both
sensors `Status: 1` (Disconnected), rather than leaving a stale temperature on
screen that looks healthy.

## Why VictronConnect-over-Bluetooth must be off on the Cerbo

The bridge holds one connection open. Venus's `vesmart-server` (the service behind
VictronConnect over Bluetooth) arms a 60-second keep-alive timer whenever *any*
device connects on the adapter, and when it expires without a VE.Smart write it
calls `Disconnect()` on every connected device
(`/opt/victronenergy/vesmart-server/gattserver.py`, `_keep_alive_timer_timeout`).
The fridge never sends one, so with vesmart-server running the link is cut every
minute: `Disconnect Complete / Reason: Connection Terminated By Local Host (0x16)`
about 59s after each connect, with nothing wrong on the air.

Victron acknowledged it in [venus#1587](https://github.com/victronenergy/venus/issues/1587)
(Feb 2026; "there is no way to know via the Bluez dbus API which device is a
connection coming from outside of the GX"). The per-device rewrite in vesmart-server
0.5.16 (v3.80 betas) is itself broken ([#1666](https://github.com/victronenergy/venus/issues/1666),
[#1661](https://github.com/victronenergy/venus/issues/1661)); release v3.7x still
disconnects all. Turning `Settings > Services > Bluetooth` off stops vesmart-server;
`BleSensors` keeps the Ruuvi and Mopeka; bluetoothd is unaffected. The only cost is
VictronConnect *to the Cerbo itself* over Bluetooth - LAN, VRM and the remote console
are untouched. To keep it instead, [TechBlueprints/venus-os-dbus-ble-sensors-py](https://github.com/TechBlueprints/venus-os-dbus-ble-sensors-py)
carries a bind-mount patch for the same timer.

### What the churn did, for the record

Before this was understood the bridge reconnected immediately after every cut -
about 1,440 cycles a day - and was later changed to poll every 5 minutes. Neither
was good for this Cerbo, whose WiFi client, own access point and all Bluetooth share
one Realtek RTL8723DU USB combo chip. Over 2026-09-18..22 the Bluetooth controller
degraded (`Opcode 0x2006 failed: -16`, host/controller desync, BLE scanning deaf for
47h) until bluetoothd hung; the WiFi stayed associated but stopped passing data,
and the Cerbo was off the network and VRM for two days. Two of those signatures are
known bugs on this platform:

- `Opcode 0x2006 failed: -16` is an upstream kernel bug for a host that advertises
  while making outgoing central connections (patches Aug 2026, target 6.12.y), not
  present in Victron's `venus-6.12.90` kernel. It needs the Cerbo to be advertising,
  which vesmart-server did.
- The WiFi data-path wedge matches [venus#1618](https://github.com/victronenergy/venus/issues/1618):
  the `rtl8723du` driver's Bluetooth-coexistence handler fires on vendor event
  `0xfc19` and collides USB transfers until the radio goes silent. A power cycle
  clears it. Ethernet makes it a nuisance rather than an outage.

Cold connects also fail about two times in three (the fridge sends an L2CAP
Connection Parameter Update Request while LE encryption is being set up, and
sometimes the link never recovers). The phone app does not suffer this, so it is
Cerbo-side. Changing the connection interval does not help - the request tracks
encryption start, not time. With a held link it only matters after a genuine drop.

A drop is not reported to Venus straight away: the sensors go `Disconnected` only
after `LINK_DOWN_GRACE` (120s) without a link, so a quick reconnect never fires a
spurious alarm. Process death is still reported instantly through the MQTT last will.

## What the GX display and VRM show when something is wrong

Only the temperature value itself is rendered; a `Status` flag on its own changes
nothing visible, and a stale number looks healthy. So the flow publishes the
temperature as `null` - which `victron-virtual` writes to D-Bus as invalid, exactly
what a real Venus sensor does when it loses contact - and the GX and VRM show `--`.
That happens in two cases:

- the BLE link is down or the bridge is dead (`Status` 1);
- the fridge is switched off (`power/cooler` = 0). A CFX3 "off" is standby: its
  Bluetooth stays up and it keeps reporting the temperature of a warming box, so
  without this VRM showed +17 °C in the freezer as a perfectly valid reading.

The next real temperature restores the sensors. Nothing alarms on its own; if you
want a notification for a warm freezer, that is a VRM alarm rule on the sensor.

## Connection parameters

The adapter default LE supervision timeout on Venus is `42` (420ms) while the
CFX3's own stored preference is `400` (4000ms):

    /sys/kernel/debug/bluetooth/hci0/supervision_timeout   -> 42   (420ms)
    bond info [ConnectionParameters] Timeout=400           -> 4000ms

`install.sh` writes `400` and repeats it from `/data/rc.local` each boot, since
debugfs is volatile. This aligns the link with what the device asks for; it did
**not** fix the 60s teardown above. It only affects outgoing GATT connections -
Ruuvi and Mopeka are advertisement-only and are untouched.

## The bond persists across reboots and firmware updates

Venus compiles bluetoothd with `STORAGEDIR=/data/var/lib/bluetooth` rather than the
upstream `/var/lib/bluetooth`, so bonds land on the ext4 `/data` partition:

    /data/var/lib/bluetooth/<adapter>/<fridge MAC>/info

That file holds the long-term keys and `Trusted=true`, and survives both reboots
and firmware updates. Note that `/var/lib/bluetooth` does not exist on this
platform — do not read its absence as bonds being lost.

If a bond does go missing, the bridge notices `Paired: false` on connect and
attempts `Pair()` unattended before giving up. If that fails, re-pair by hand with
the fridge in Bluetooth pair mode:

    ssh <host> 'svc -d /service/dometic-cfx3; python3 /data/dometic/probe.py --pair; svc -u /service/dometic-cfx3'

## Boot

At boot the bridge waits for the network (a default route, up to 3 min) plus
30 s before touching Bluetooth: Wi-Fi and Bluetooth share the Cerbo's RTL8723DU,
and BLE traffic while Wi-Fi was rejoining kept Cerbos off the network after
firmware updates. Restarts later than 5 min after boot start at once.

## Restarting the service

Use `svc -t` to restart. Do **not** use `svc -dx` — the `-x` tells supervise to
exit once the service stops, after which it ignores `svc -u` and the service
stays down until the stale supervise is killed and svscan respawns it.

    ssh <host> 'svc -t /service/dometic-cfx3'

## MQTT topics

| topic | example |
| --- | --- |
| `dometic/cfx3/bridge` | `online` / `offline` (last will) |
| `dometic/cfx3/link` | `connected` / `disconnected` (the latter only after 120s down) |
| `dometic/cfx3/fridge/temperature` | `2.0` |
| `dometic/cfx3/fridge/setpoint` | `2.0` |
| `dometic/cfx3/fridge/door` | `0` |
| `dometic/cfx3/freezer/temperature` | `-16.0` |
| `dometic/cfx3/freezer/setpoint` | `-17.0` |
| `dometic/cfx3/battery/voltage` | `13.0` |
| `dometic/cfx3/power/source` | `AC` / `DC` / `Solar` |
| `dometic/cfx3/power/cooler` | `1` on / `0` off (standby: still reports temperatures) |
| `dometic/cfx3/fridge/power`, `freezer/power` | `1` / `0` per zone |
| `dometic/cfx3/dc_current/history/{hour,day,week}` | `[1.5, 2.0, 2.2, 2.6, 2.3, 1.5, 1.2]` |
| `dometic/cfx3/fridge/history/{hour,day,week}` | `[1.8, 2.0, 2.2, 2.4, 2.3, 1.7, 1.7]` |
| `dometic/cfx3/freezer/history/{hour,day,week}` | `[-15.4, -15.8, ...]` |

The history topics are the fridge's own records: seven equal buckets over the
period, oldest first, in A or °C; buckets not yet filled since the fridge last
restarted are `null` (the fridge clears its history when it restarts). They answer "did the fridge run last night"
directly, where the Cerbo's DC-system power can only hint. The `day` current
history is the useful one: a compressor that ran all night shows as a high
average in the overnight buckets. The protocol doc calls the current mA; the
values only make sense as 0.1 A, so that is what is published.

Only the two temperatures are wired to virtual sensors. The rest are published
for other flows to consume.

## Files

| file | where it runs |
| --- | --- |
| `bridge.py` | Cerbo, `/data/dometic/`, under daemontools |
| `probe.py` | Cerbo, run by hand to pair (`--pair`) or check the link (same link code as the bridge) |
| `cfx3-to-virtual.js` | source of the flow's function node |
| `flow.json` | importable Node-RED flow |
| `build-flow.py` | regenerates `flow.json` after editing the `.js` |
| `service/` | daemontools run scripts |

## Logs

    ssh <host> 'tail -f /var/log/dometic-cfx3/current | tai64nlocal'

Set `LOG_LEVEL = logging.DEBUG` at the top of `bridge.py` to log every decoded
frame.
