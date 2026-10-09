# Soft BMS: BLE lithium packs -> Venus OS DVCC charge limit

Reads self-heating LiFePO4 packs over Bluetooth LE and uses their cell voltages,
temperatures and protection state to cap the DVCC maximum charge current on a
Cerbo. It is deliberately **not** a `com.victronenergy.battery` BMS, so a lost
BLE link can never stop charging. The SmartShunt stays the battery monitor and
the source of SOC.

Tested with LiTime 12V 320Ah Mini Smart Self-Heating packs (`redodo`
protocol) and PowerUrus 12V 200Ah self-heating packs (`roypow` protocol), two and
three in parallel, on Cerbo GX units running Venus OS v3.71 and v3.79.

`bridge.py` runs on the Cerbo under daemontools. It holds a BLE link to each
pack, polls one pack per 10 s tick (round robin), runs the control law in
`controller.py`, and applies the result to `/Settings/SystemSetup/MaxChargeCurrent`
through `chargelimit.py`. With DVCC on, that caps the MultiPlus and the MPPT
together. Status and the applied limit are published to MQTT for display only.

## Install

1. Close the phone app (a pack takes one central at a time). LiTime: turn **off
   the Bluetooth password** in the LiTime app first, or every request is
   answered `NOAUTH`.
2. On the GX: **Settings -> Services -> Bluetooth -> Off** (leave *BLE sensors*
   on), and **Settings -> DVCC -> On**.
3. Copy `config.example.json`, fill in the protocol and the packs' MACs, and run
   `./install.sh <ssh-host> <config.json>`.

The installer validates the config with the service's own loader first (MAC
format, protocol, controller settings), stops the running bridge and waits for
it to exit (which restores the charge-limit baseline), then uploads.

### Adding a site

Find the packs' MACs without scanning: `dbus-ble-sensors` is already scanning,
so listen to its traffic with `btmon` for 30 s and look for the packs'
advertisements (LiTime: manufacturer ID 0x585A; RoyPow: manufacturer data
`88a0` followed by the MAC). Use the address type of the **connectable**
(ADV_IND) adverts: both brands seen so far use public addresses, and BlueZ
silently never connects if the type is wrong. Then check each with
`probe.py roypow <MAC> public` (it prints voltage and SOC, which tells packs on
the bus from a standalone battery nearby), and write the config from `config.example.json`.

    ssh <host> 'svc -d /service/softbms; python3 /data/softbms/probe.py pack1; svc -u /service/softbms'

`probe.py` uses the bridge's own connect code.

## Your own DVCC limit is kept

If you set a DVCC charge current limit yourself, that value is the **baseline**
and the bridge applies `min(baseline, its own limit)`. Change it in the GX menu
or VRM as usual: the bridge reads the setting back every 10 s and adopts any
value it did not write as the new baseline. While the bridge is actively
limiting, it re-applies its tighter limit on top of your new baseline; to lift a
limit it imposes, stop the service. Stopping the service (or the bridge exiting
for any reason) writes the baseline back. The baseline survives restarts in
`/data/softbms/state.json`.

## The rules

Each rule gives a cap; the tightest wins (`controller.py`; per-site overrides go
under `"controller"` in the config):

| condition | cap |
| --- | --- |
| highest cell 3.59 V -> 3.63 V | tapers 60 A -> 5 A (5 A steps), 5 A above |
| cell temperature >= 45 °C (released below 42 °C) | 40 A |
| cell temperature >= 50 °C (released below 47 °C) | 0 A |
| a pack reports a charge-blocking protection | 0 A, held 5 min after it clears |
| some packs report charging disabled, others still accept | `per_pack_a` x packs still accepting |
| a pack silent > 120 s, or its data malformed | that pack contributes nothing |
| no valid data from any pack | no limit (your baseline) |

The limit tightens immediately and loosens by at most 20 A per minute. If a
loosen has to be undone within two minutes, the wait doubles (up to 15 min), so
a cell that relaxes as soon as the current drops cannot make the limit cycle.
That keeps settings writes (flash) to a handful per charge.

Why these numbers: the tested installations' chargers absorb at 14.2 V (3.55 V a cell). The
LiTime packs read cells 10-15 mV high, so a normal absorption reads ~3.57 V; the
PowerUrus packs read true. The taper starts above that, so it only acts on a
cell running ahead of the others, and releases fully once that cell comes back.
There is no step to 0 A on voltage: the packs' own overcharge protection trips
at ~3.65 V. A site with a higher absorption voltage must raise `taper_start_v`
and `taper_end_v`.

`per_pack_a` is the charge current one pack can take, used when another pack
has switched its charging off. If `per_pack_a` times the packs still accepting
exceeds what the chargers can make, the cap never binds and the packs' own BMSs
are the protection.

**No cold-temperature cut.** These packs heat themselves from charger power; a
0 A limit below freezing would starve the heater. The packs' own BMSs refuse
charge into cold cells; the protocol modules leave low-temperature protection out.

**Voltage is not an option.** DVCC honours the user charge-voltage limit
(`/Settings/SystemSetup/MaxChargeVoltage`) only when a BMS supplies a charge
voltage (`dvcc.py`), so current is the only actuator available.

## Failure behaviour

The design goal is that nothing here can stop charging by failing.

| failure | effect |
| --- | --- |
| a pack's link drops | its last status is used for up to 120 s, then it drops out; the other packs' limits still apply |
| all packs unreachable | eases to your baseline at the normal pace (20 A a minute) |
| a pack is away (3 failed attempts) | retried every 30 min instead of every few minutes, so the BLE sensors are not disturbed |
| a reading beyond range (cell >= 4.0 V, > 100 °C) | holds 5 A for that pack (a broken sensor or a real emergency; never releases the limit) |
| a protection flag the pack does not back up | caps at 5 A; 0 A only with the pack's charging off, a high cell or a high temperature |
| bridge stopped (`svc -d`) or killed by SIGTERM | writes the baseline back, releases the packs |
| bridge crashes | daemontools restarts it within ~11 s; the limit that was in force is kept and eased off until the packs report |
| write read-back fails, or a crash mid-write | the pending write is recorded first, so it is never mistaken for your setting |
| localsettings restarts | the bridge follows it; nothing frozen |
| `state.json` unwritable or corrupt | logged; control carries on without it |
| something else keeps changing MaxChargeCurrent | after 3 changes in 10 min the bridge stops writing for 30 min and says so (`conflict` in `softbms/limit`) |
| bridge hangs | not expected: nothing blocks the main loop; if it does, the last limit stays until restarted |
| MQTT broker down | no effect on control |

The one limit left in place is when the bridge is prevented from running at all
(power cut mid-limit is fine: it restarts at boot). To remove the bridge, run
`svc -d /service/softbms` first so it restores the baseline, then delete the
`/data/rc.local` line.

## Why the control loop is not in Node-RED

The first version computed the limit in a Node-RED function and wrote it through
a `victron-output-custom` node. Review found that design could leave a 0 A limit
in flash whenever Node-RED stopped or the tab was disabled (and a finalize hook
cannot reliably send on shutdown), that the node had no read-back so lost writes
were never retried and the user's own limit was overwritten on every restart,
and that re-importing the flow created a second writer. Owning the setting in
the one process that also owns the data removes all of that.

## Protocols, and what not to trust

Both protocols are wrapped in a `Session` (`begin()` returns the first request,
`feed()` returns the next request, a finished status, or an error) that adds the
brand-neutral fields the controller reads: `cells`, `cell_temp`, `charge_block`,
`charge_disabled`.

**`redodo.py`: LiTime / Redodo / Power Queen.** Service `ffe0`, notify `ffe1`,
write `ffe2`, request `00 00 04 01 13 55 AA 17`, one 105-byte little-endian
reply. Offsets from aiobmsble's `redodo_bms.py` and dbus-serialbattery's
`litime_ble.py`.
- Cell voltages and temperatures agree between packs to the millivolt; current
  agrees with the shunt within ~0.5 A but has a ~2 A dead band; pack voltage
  reads ~50 mV high.
- **SOC and remaining Ah are wrong** (99 % vs the shunt's 87 %; 359 Ah "remaining"
  of 336). Use the shunt.
- Charge block: overcharge, charge-overcurrent and high-temperature-charge bits.
  Charge disabled: state word 4. Both from reverse engineering, not yet seen in
  a real protection event.

**`roypow.py`: PowerUrus / Epoch / RoyPow ("RoyPow Fish" app).** Service `ffe0`,
write and notify both on `ffe1`. Three exchanges per status (0x02 cells, 0x03
current/alarms/temperatures/FETs, 0x04 SOC/voltage/cycles), framed
`EA D1 01 len FF cmd … xor F5`, big-endian. Positions from aiobmsble's
`roypow_bms.py`, checked against its reference frames in `test_protocols.py`.
The BLE module sometimes injects `AT+STAT\r\n`, which is stripped.
- Cells read true (3.268 V average against the shunt's 13.07 V).
- Four temperature sensors per pack, unlabelled; `cell_temp` is the maximum of
  all four until it is clear which are cells (set `protocol_options.cell_temp_sensors`
  then).
- Current read 0.0 at ~0.7 A per pack: a dead band like LiTime's.
- Pack SOCs disagree with each other (61-76 % on the same bus). Use the shunt.
- The problem code has no public bit map, so nothing is mapped to
  `charge_block`; the pack's own decision shows as its charge FET, which is
  `charge_disabled`.

## BLE notes

The link handling below lives in `venus_ble/` (see its README), shared with
the fridge bridge.

- Venus runs bluetoothd with `ReverseServiceDiscovery=false` (`/etc/bluetooth/ble.conf`),
  so a link opened by `Adapter1.ConnectDevice` gets no GATT client. The bridge
  uses `ConnectDevice` only to create the device object, drops that link, and
  reconnects with `Device1.Connect()`. `StartDiscovery` is never called
  (victronenergy/venus#1597).
- **Every LE connect blinds `dbus-ble-sensors`.** The kernel connects by scanning
  with the controller's accept list as a filter and leaves those scan parameters
  behind; `dbus-ble-sensors` only re-enables scanning, so every Ruuvi/Mopeka
  sensor goes silent until it reopens hci0. Found when the sensors on both test
  Cerbos had dropped off D-Bus; probably also the September "BLE scanning deaf
  for 47h" episode in `dometic-cfx3/README.md`. The bridge (and `probe.py`, and
  the fridge bridge) restart `dbus-ble-sensors` after each connect attempt,
  debounced to once per 30 s.
- After the first good connect the bridge sets the device **Trusted**. Untrusted,
  unpaired devices are made temporary on disconnect and deleted after 30 s, which
  forced the two-connection dance on every reconnect.
- All connects are asynchronous with a 150 s deadline, one pack at a time.
  Backoff (10 s -> 5 min) resets only after a valid status frame, so a pack that
  links but never answers backs off instead of churning the radio.
- At boot the service waits for the network (default route, up to 3 min) before
  touching Bluetooth, so it never competes with Wi-Fi rejoining after a reboot or
  firmware update; see `venus_ble/README.md`.
- `install.sh` writes the adapter supervision timeout (4000 ms) to `/data/rc.local`,
  the same line `dometic-cfx3/install.sh` writes.

## Using the phone app

A pack accepts one central at a time and the bridge holds them all. Stop the
service, use the app, close the app, start the service:

    ssh <host> 'svc -d /service/softbms'
    ssh <host> 'svc -u /service/softbms'

Stopping restores your baseline limit and releases the packs within a few
seconds. Use `svc -t` to restart; never `svc -dx` (see `dometic-cfx3/README.md`).

## MQTT topics (display only)

| topic | example |
| --- | --- |
| `softbms/bridge` | `online` / `offline` (last will) |
| `softbms/<pack>/link` | `connected` / `disconnected` (after 60 s down) |
| `softbms/<pack>/status` | JSON: the protocol's decoded fields plus `cells`, `cell_temp`, `charge_block`, `charge_disabled`, `ts` |
| `softbms/limit` | JSON: `amps` in force (-1 none), `controller` limit, `baseline`, `conflict`, `reason` |

All retained, republished whenever the bridge reconnects to the broker.

## Files

| file | purpose |
| --- | --- |
| `bridge.py` | the service: BLE, polling, control loop, MQTT |
| `controller.py` | control law (pure logic) |
| `chargelimit.py` | applies the limit to the DVCC setting; baseline and restore |
| `config.py` | loads and validates the config (also used by install.sh) |
| `readings.py` | per-reading CSV log |
| `redodo.py`, `roypow.py` | protocol framing and decoding |
| `probe.py` | read one pack by hand |
| `config.example.json` | template for the per-installation config |
| `test_*.py` | run locally, one file at a time: `for t in test_*.py; do python3 $t; done` (Venus's python has no unittest) |
| `service/` | daemontools run scripts |

## Reading log

Every pack reading is appended to `/data/softbms/readings/<YYYY-MM-DD>.csv`
(Cerbo local time), buffered and written once a minute, kept 14 days: about
1 MB a day for three packs. Columns: `time, pack, cells` (slash-separated),
`max_cell, cell_temp, temps` (every sensor the pack reports), `pack_current,
pack_voltage, soc, charge_disabled, battery_voltage, battery_current` (the
SmartShunt via systemcalc) and `limit` (the DVCC limit in force). Use it to see
which cell runs ahead at the top of a charge, and which temperature sensor is
which.

## Logs

    ssh <host> 'tail -f /var/log/softbms/current | tai64nlocal'
