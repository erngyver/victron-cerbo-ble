"""RoyPow BMS BLE protocol (PowerUrus, Epoch, RoyPow packs; the "RoyPow Fish" app).

Framing and field positions follow aiobmsble's roypow_bms.py and its test
frames (test_roypow.py re-checks them). Multi-byte fields are big-endian.

    request   EA D1 01 | len | FF cmd | xor | F5      len = 4, xor over len..cmd
    response  EA D1 01 | len | FF cmd | payload | xor | F5
              total length len + 4, xor over bytes 3 .. -3

One status takes three exchanges: 0x02 cells, 0x03 current/alarms/temperatures/
FETs, 0x04 SOC/voltage/capacity. The BLE module sometimes injects "AT+STAT\\r\\n"
into the notification stream; it is stripped.
"""

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_UUID   = "0000ffe1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID  = "0000ffe1-0000-1000-8000-00805f9b34fb"

HEAD = b"\xea\xd1\x01"
TAIL = 0xF5
AT_MSG = b"AT+STAT\r\n"
CMDS = (0x02, 0x03, 0x04)


def xor(data):
    x = 0
    for b in data:
        x ^= b
    return x


def request(cmd):
    body = bytes([4, 0xFF, cmd])
    return HEAD + body + bytes([xor(body), TAIL])


def valid(frame):
    return (len(frame) >= 8 and frame.startswith(HEAD) and len(frame) == frame[3] + 4
            and frame[-1] == TAIL and xor(frame[3:-2]) == frame[-2])


def be(frame, pos, size):
    return int.from_bytes(frame[pos:pos + size], "big")


# Shortest frames decode() can read: the last field read plus xor and tail.
MIN_LEN = {0x02: 13, 0x03: 27, 0x04: 51}


def decode(msgs, cell_temp_sensors=None):
    """msgs: {cmd: frame}. Returns the status dict (raw fields plus the
    normalised ones the controller reads: cells, cell_temp, charge_block,
    charge_disabled)."""
    f2, f3, f4 = msgs[0x02], msgs[0x03], msgs[0x04]
    for cmd, frame in msgs.items():
        if len(frame) < MIN_LEN[cmd]:
            raise ValueError(f"reply 0x{cmd:02x} too short: {len(frame)} bytes")

    n = max(0, (len(f2) - 11) // 2)
    cells = [be(f2, 9 + 2 * i, 2) / 1000 for i in range(n)]

    raw_current = be(f3, 6, 3)
    current = (raw_current & 0xFFFF) / 100 * (-1 if (raw_current >> 16) & 1 else 1)
    problem = be(f3, 9, 3)
    nsens = f3[13]
    temps = [f3[14 + i] - 40 for i in range(nsens) if 14 + i < len(f3) - 2]
    fets = f3[24]

    sensors = temps if cell_temp_sensors is None else [temps[i] for i in cell_temp_sensors
                                                        if i < len(temps)]
    charge_fet = bool(fets & 0x04)
    return {
        "cells":           cells,
        "cell_temp":       max(sensors) if sensors else None,
        "temps":           temps,
        "current":         current,
        "voltage":         be(f4, 47, 2) / 100,
        "soc":             f4[7],
        "cycles":          be(f4, 9, 2),
        "problem_code":    problem,
        "charge_fet":      charge_fet,
        "discharge_fet":   bool(fets & 0x02),
        "fet_flags":       fets,
        # The problem-code bits are not mapped anywhere public, so nothing here
        # is known to mean "stop charging". The pack's own decision is visible
        # as the charge FET, which the controller treats as charge disabled.
        "charge_block":    [],
        "charge_disabled": not charge_fet,
    }


class Session:
    """One pack's request/response cycle. begin() returns the first request;
    feed() returns ("send", bytes) for the next request, ("done", status),
    ("error", reason) to log, or None while a reply is still arriving."""

    SERVICE_UUID, WRITE_UUID, NOTIFY_UUID = SERVICE_UUID, WRITE_UUID, NOTIFY_UUID

    def __init__(self, cell_temp_sensors=None):
        self.cell_temp_sensors = cell_temp_sensors
        self.buf = bytearray()
        self.msgs = {}
        self.pending = []
        self.awaiting = None

    def begin(self):
        self.buf = bytearray()
        self.msgs = {}
        self.pending = list(CMDS[1:])
        self.awaiting = CMDS[0]
        return request(CMDS[0])

    def feed(self, chunk):
        chunk = bytes(chunk).replace(AT_MSG, b"")
        if not chunk:
            return None
        if chunk.startswith(HEAD) and not self.buf.startswith(HEAD):
            self.buf = bytearray()
        self.buf += chunk
        if not self.buf.startswith(HEAD):
            self.buf = bytearray()
            return None
        if len(self.buf) < 4 or len(self.buf) < self.buf[3] + 4:
            return None
        frame, self.buf = bytes(self.buf[:self.buf[3] + 4]), bytearray()
        if not valid(frame):
            return ("error", f"bad frame {frame.hex()}")
        cmd = frame[5]
        if cmd != self.awaiting:
            # A late reply from an earlier cycle (or an unrelated frame): using
            # it would mix two cycles' values and send an extra request.
            return None
        if len(frame) < MIN_LEN[cmd]:
            self.awaiting = None
            return ("error", f"reply 0x{cmd:02x} too short: {frame.hex()}")
        self.msgs[cmd] = frame
        if self.pending:
            self.awaiting = self.pending.pop(0)
            return ("send", request(self.awaiting))
        self.awaiting = None
        try:
            return ("done", decode(self.msgs, self.cell_temp_sensors))
        except (ValueError, IndexError) as e:
            return ("error", f"undecodable status: {e}")
