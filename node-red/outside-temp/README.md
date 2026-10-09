# Outside Temp

Re-exposes the Mopeka propane sensor's temperature as a Venus virtual temperature
sensor typed **Outdoor**, so it shows on the GX display and in VRM as an outside
temperature rather than being buried in the tank data.

The Mopeka sits under the propane tank outside. Venus reads it through
`dbus-ble-sensors` as `com.victronenergy.tank.mopeka_<mac>`, which carries a
`/Temperature` path the tank UI never shows.

## Flow

    victron-input-custom (com.victronenergy.tank/24, /Temperature)
        |-> function: Mopeka -> virtual sensor   {Temperature, Status: 0}
        |-> trigger: no data 30 min              {Status: 1}
                    \-> victron-virtual (temperature, Outdoor)

The `victron-input-custom` node is used rather than `victron-input-tank` because
the tank node's editor does not list `/Temperature`; both run the same code.

The trigger node marks the sensor Disconnected if the Mopeka goes quiet for 30
minutes, so a dead sensor does not leave a plausible-looking outside temperature
on screen.

## Moving it between installations

`build-flow.py` has the service (`com.victronenergy.tank/<instance>`) at the top.
Set it to the tank instance on the target Cerbo, run the script, and import
`flow.json` (Node-RED: Menu -> Import). It needs the `victron-client` config node
that every Venus Node-RED already has.

## Files

| file | purpose |
| --- | --- |
| `mopeka-to-virtual.js` | source of the flow's function node |
| `flow.json` | importable Node-RED flow |
| `build-flow.py` | regenerates `flow.json` after editing the `.js` |

Replaces an earlier `mopeka-dbus-parser.js`, which parsed the output of a
shelled-out `dbus -y ... GetValue` call.
