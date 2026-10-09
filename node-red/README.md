# Node-RED pieces

For the Node-RED that ships with Venus OS "Large" images
(`node-red-contrib-victron`). Each piece is either an importable `flow.json` or
a function-node body to paste into your own flow.

| piece | what it does |
| --- | --- |
| [`auto-charge/`](#auto-charge) | switches the MultiPlus between Charger Only, On and Inverter Only from SOC |
| [`low-power-charging/`](#low-power-charging) | backs the AC input current limit off when a weak source's voltage sags |
| [`outside-temp/`](outside-temp/README.md) | republishes a Mopeka tank sensor's temperature as an Outdoor temperature sensor |

Tests: `node --test node-red/test_function_nodes.js` (no dependencies).

## auto-charge

Function-node body: `auto-charge/auto-charge.js`. Feed it one joined message
(a `join` node, key/value mode) whose payload has:

| key | from |
| --- | --- |
| `Auto Charge` | an on/off switch (1 / 0); in the original install, `victron-input-relay` `/Relay/1/State` (a GX relay used as the toggle) |
| `Charge State` | `victron-input-vebus` `/VebusChargeState` (3 = Float) |
| `State of Charge` | `victron-input-battery` (or system) `/Soc` |
| `Multiplus Switch Position Input` | `victron-input-vebus` `/Mode` |

and wire its output to a `victron-output-vebus` `/Mode` node. Rules:

| condition | switch position |
| --- | --- |
| SOC < 20 % | Charger Only, and remember it |
| SOC >= 30 % after a < 20 % event | On (clears the memory) |
| 20 % <= SOC < 50 %, no < 20 % event pending | On |
| SOC >= 50 %, Auto Charge on, charger in Float | Inverter Only (stop charging) |
| anything else | unchanged |

Between 20 and 30 % after dropping below 20 % it stays in Charger Only, so the
bank is not cycled at the bottom. A missing or non-numeric SOC holds the current
position instead of being read as 0 %. Thresholds are constants at the top.

## low-power-charging

Function-node body: `low-power-charging/low-power-charging.js`. For charging
from a weak AC source (a small generator, a long cord): it lowers the
MultiPlus's input current limit while the AC input voltage sags below
`MIN_VOLTAGE` and creeps it back up 0.1 A per message when the voltage recovers.
Feed it a joined message with `Auto Charge`, the MultiPlus's
`<product> - Switch Position` and `<product> - Input voltage phase 1`; wire its
output to a `victron-output-vebus` node for the AC input current limit.

The switch-position key includes the inverter's product name. It defaults to a
MultiPlus-II 12/3000/120-50; for another unit set the function node's
environment variable `SWITCH_POSITION_KEY` (Setup tab) instead of editing code.
Limits: 9 A floor (lower settings have no effect on the MultiPlus), 30 A ceiling,
115 V threshold with 2 V hysteresis.
