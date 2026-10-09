"""Load and validate the per-installation config (config.example.json).

Kept free of D-Bus so it can be tested anywhere, and so install.sh can check a
config before deploying it: python3 -c "import config; config.load_config(path)".
"""

import importlib
import json
import re

import controller

CONFIG_FILE = "/data/softbms/config.json"
PROTOCOLS   = ("redodo", "roypow")       # protocol modules next to this file
MAC_RE      = re.compile(r"([0-9A-F]{2}:){5}[0-9A-F]{2}")


def load_config(path=CONFIG_FILE):
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg.get("packs"), dict) or not cfg["packs"]:
        raise ValueError(f"{path}: no packs configured")
    for name, pack in cfg["packs"].items():
        mac = str(pack.get("mac", "")).upper()
        if not MAC_RE.fullmatch(mac):
            raise ValueError(f"{path}: {name}: 'mac' must be a Bluetooth address like AA:BB:CC:DD:EE:FF")
        pack["mac"] = mac                          # BlueZ object paths are upper case
        if pack.get("address_type", "public") not in ("public", "random"):
            raise ValueError(f"{path}: {name}: address_type must be public or random")
    if cfg.get("protocol") not in PROTOCOLS:
        raise ValueError(f"{path}: protocol must be one of {', '.join(PROTOCOLS)}")
    importlib.import_module(cfg["protocol"]).Session(**cfg.get("protocol_options", {}))
    controller.Controller(**cfg.get("controller", {}))
    return cfg
