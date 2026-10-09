"""LiTime / Redodo BMS BLE protocol (LiTime, Redodo, Power Queen packs).

Offsets follow aiobmsble's redodo_bms.py and dbus-serialbattery's
litime_ble.py, which agree; all fields are little-endian. One request, one
105-byte reply split across notifications. With the LiTime app's Bluetooth
password on, every request is answered with the ASCII string NOAUTH.
"""

import struct

SVC_UUID    = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE_UUID  = "0000ffe2-0000-1000-8000-00805f9b34fb"

OP_STATUS   = 0x13

# Protection bits that mean "stop charging". Low-temperature charge (0x400) is
# deliberately absent: the packs self-heat from charger power.
CHARGE_BLOCK = {0x0004: "overcharge", 0x0040: "charge overcurrent", 0x0100: "high temp charge"}
CHARGE_DISABLED_STATE = 4          # dbus-serialbattery's reading of the state word

# Protection flags at offset 76. Names from the reverse-engineered sources; none
# has yet been confirmed against a real protection event on these packs.
PROTECTION = {
    0x0004: "overcharge",
    0x0020: "over_discharge",
    0x0040: "charge_overcurrent",
    0x0080: "discharge_overcurrent",
    0x0100: "high_temp_charge",
    0x0200: "high_temp_discharge",
    0x0400: "low_temp_charge",
    0x0800: "low_temp_discharge",
    0x4000: "short_circuit",
}


def request(op):
    frame = [0x00, 0x00, 0x04, 0x01, op, 0x55, 0xAA]
    return frame + [sum(frame) & 0xFF]


class Reassembler:
    """Replies arrive split across several notifications. The frame starts
    00 00 <len> and is len+4 bytes long in total."""

    def __init__(self):
        self.buf = bytearray()

    def reset(self):
        """Call before each request. Without it, one lost chunk leaves a partial
        frame behind, and the zero-filled stretches of the next reply look like
        frame headers, costing several replies instead of one."""
        self.buf = bytearray()

    def feed(self, chunk):
        if not self.buf and chunk[:2] != b"\x00\x00":
            return None                         # mid-frame fragment; drop it
        self.buf += chunk
        if len(self.buf) < 3 or len(self.buf) < self.buf[2] + 4:
            return None
        frame, self.buf = bytes(self.buf[:self.buf[2] + 4]), bytearray()
        return frame


def decode_status(f):
    if len(f) < 104:
        raise ValueError(f"status frame too short: {len(f)} bytes")

    u16 = lambda o: struct.unpack_from("<H", f, o)[0]
    s16 = lambda o: struct.unpack_from("<h", f, o)[0]
    u32 = lambda o: struct.unpack_from("<I", f, o)[0]
    s32 = lambda o: struct.unpack_from("<i", f, o)[0]

    # 16 cell slots; unused ones at the end read 0. Trim only the trailing
    # zeros, so a dead cell in the middle still shows (and fails validation).
    cells = [u16(16 + 2 * i) / 1000 for i in range(16)]
    while cells and cells[-1] == 0:
        cells.pop()
    prot = u32(76)
    return {
        "voltage":         u32(8) / 1000,
        "cells":           cells,
        "current":         s32(48) / 1000,     # +charge; ~2A dead band, unreliable
        "cell_temp":       s16(52),
        "mosfet_temp":     s16(54),
        "remaining_ah":    u16(62) / 100,
        "capacity_ah":     u32(64) / 100,
        "fet_flags":       u32(68),
        "protection":      prot,
        "protection_names": [n for b, n in PROTECTION.items() if prot & b],
        "failure":         u32(80),
        "balancing":       u32(84),
        "state":           u16(88),
        "soc":             u16(90),
        "soh":             u32(92),
        "cycles":          u32(96),
        "checksum_ok":     (sum(f[:-1]) & 0xFF) == f[-1],
    }


def normalise(status):
    """Add the brand-neutral fields the controller reads."""
    status["charge_block"] = [label for bit, label in CHARGE_BLOCK.items()
                              if status["protection"] & bit]
    status["charge_disabled"] = status["state"] == CHARGE_DISABLED_STATE
    return status


class Session:
    """One pack's request/response cycle. begin() returns the request; feed()
    returns ("done", status), ("error", reason) to log, ("fatal", reason) to
    drop the link, or None while a reply is still arriving."""

    SERVICE_UUID, WRITE_UUID, NOTIFY_UUID = SVC_UUID, WRITE_UUID, NOTIFY_UUID

    def __init__(self):
        self.rx = Reassembler()

    def begin(self):
        self.rx.reset()
        return bytes(request(OP_STATUS))

    def feed(self, chunk):
        chunk = bytes(chunk)
        if chunk.startswith(b"NOAUTH"):
            return ("fatal", "pack answered NOAUTH - turn off the Bluetooth password in the LiTime app")
        frame = self.rx.feed(chunk)
        if not frame:
            return None
        if len(frame) < 5 or frame[4] != (OP_STATUS | 0x80):
            return None
        try:
            status = decode_status(frame)
        except ValueError as e:
            return ("error", f"{e}: {frame.hex()}")
        if not status["checksum_ok"]:
            return ("error", f"checksum mismatch, dropping {frame.hex()}")
        return ("done", normalise(status))
