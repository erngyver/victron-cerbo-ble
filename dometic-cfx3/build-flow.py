#!/usr/bin/env python3
"""Regenerate flow.json, embedding cfx3-to-virtual.js as the function node body.

Run this after editing cfx3-to-virtual.js so the importable flow stays in sync
with the reviewable source.
"""

import json
import pathlib

HERE = pathlib.Path(__file__).parent
TAB    = "cfx3_tab"
BROKER = "cfx3_broker"

func_body = (HERE / "cfx3-to-virtual.js").read_text()

flow = [
    {"id": TAB, "type": "tab", "label": "Dometic CFX3", "disabled": False,
     "info": "Bridges the CFX3-75DZ fridge into Venus virtual temperature "
             "sensors. Telemetry arrives from /data/dometic/bridge.py over the "
             "local broker."},

    {"id": BROKER, "type": "mqtt-broker", "name": "Cerbo local",
     "broker": "127.0.0.1", "port": "1883", "clientid": "nodered-cfx3",
     "autoConnect": True, "usetls": False, "protocolVersion": "4",
     "keepalive": "60", "cleansession": True,
     "birthTopic": "", "birthQos": "0", "birthPayload": "",
     "closeTopic": "", "closeQos": "0", "closePayload": "",
     "willTopic": "", "willQos": "0", "willPayload": ""},

    {"id": "cfx3_mqtt_in", "type": "mqtt in", "z": TAB, "name": "CFX3 telemetry",
     "topic": "dometic/cfx3/#", "qos": "0", "datatype": "auto-detect",
     "broker": BROKER, "nl": False, "rap": True, "rh": 0, "inputs": 0,
     "x": 170, "y": 200, "wires": [["cfx3_route"]]},

    {"id": "cfx3_route", "type": "function", "z": TAB,
     "name": "CFX3 -> virtual sensors", "func": func_body, "outputs": 2,
     "timeout": 0, "noerr": 0, "initialize": "", "finalize": "", "libs": [],
     "x": 430, "y": 200, "wires": [["cfx3_fridge"], ["cfx3_freezer"]]},

    {"id": "cfx3_fridge", "type": "victron-virtual", "z": TAB, "name": "Fridge",
     "device": "temperature", "default_values": True, "temperature_type": 1,
     "include_humidity": False, "include_pressure": False,
     "include_temp_battery": False, "temp_battery_voltage": 3.3,
     "outputs": 1, "x": 700, "y": 160, "wires": [[]]},

    {"id": "cfx3_freezer", "type": "victron-virtual", "z": TAB, "name": "Freezer",
     "device": "temperature", "default_values": True, "temperature_type": 6,
     "include_humidity": False, "include_pressure": False,
     "include_temp_battery": False, "temp_battery_voltage": 3.3,
     "outputs": 1, "x": 700, "y": 240, "wires": [[]]},
]

out = HERE / "flow.json"
out.write_text(json.dumps(flow, indent=2) + "\n")
print(f"wrote {out} ({len(flow)} nodes, function body {len(func_body)} bytes)")
