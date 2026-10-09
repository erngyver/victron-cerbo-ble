#!/usr/bin/env python3
"""Regenerate flow.json, embedding mopeka-to-virtual.js as the function node body."""

import json
import pathlib

HERE = pathlib.Path(__file__).parent
TAB = "outside_temp_tab"
CLIENT = "victron-client-id"        # the victron-client config node already on the Cerbo

INPUT_TYPE = "victron-input-custom"   # same runtime class as the tank node; the editor lets it show /Temperature
MOPEKA_SERVICE = "com.victronenergy.tank/24"
MOPEKA_NAME = "Propane"

func_body = (HERE / "mopeka-to-virtual.js").read_text()

flow = [
    {"id": TAB, "type": "tab", "label": "Outside Temp", "disabled": False,
     "info": "Re-exposes the Mopeka propane sensor's temperature as a Venus "
             "virtual Outdoor temperature sensor."},

    {"id": "ot_mopeka_temp", "type": INPUT_TYPE, "z": TAB,
     "service": MOPEKA_SERVICE, "path": "/Temperature",
     "serviceObj": {"service": MOPEKA_SERVICE, "name": MOPEKA_NAME},
     "pathObj": {"path": "/Temperature", "type": "float", "name": "Temperature (C)"},
     "name": "Mopeka temperature", "onlyChanges": False, "outputs": 1,
     "x": 180, "y": 120, "wires": [["ot_wrap", "ot_watchdog"]]},

    {"id": "ot_wrap", "type": "function", "z": TAB, "name": "Mopeka -> virtual sensor",
     "func": func_body, "outputs": 1, "timeout": 0, "noerr": 0,
     "initialize": "", "finalize": "", "libs": [],
     "x": 430, "y": 120, "wires": [["ot_outdoor"]]},

    # Nothing for 30 minutes -> blank the temperature and mark it Disconnected.
    # A Status flag alone changes nothing on the GX display or VRM (the stale
    # value still looks valid); a null Temperature shows as "--".
    {"id": "ot_watchdog", "type": "trigger", "z": TAB, "name": "no data 30 min -> Disconnected",
     "op1": "", "op2": "{\"Temperature\":null,\"Status\":1}", "op1type": "nul", "op2type": "json",
     "duration": "30", "extend": True, "overrideDelay": False, "units": "min",
     "reset": "", "bytopic": "all", "topic": "topic", "outputs": 1,
     "x": 450, "y": 180, "wires": [["ot_outdoor"]]},

    {"id": "ot_outdoor", "type": "victron-virtual", "z": TAB, "name": "Outside",
     "device": "temperature", "default_values": True, "temperature_type": 4,
     "include_humidity": False, "include_pressure": False,
     "include_temp_battery": False, "temp_battery_voltage": 3.3,
     "outputs": 1, "x": 700, "y": 120, "wires": [[]]},
]

out = HERE / "flow.json"
out.write_text(json.dumps(flow, indent=2) + "\n")
print(f"wrote {out} ({len(flow)} nodes, function body {len(func_body)} bytes)")
