"""Per-reading CSV log, for looking back at a charge cycle.

One row per pack status: the pack's cells and temperature, the SmartShunt's
voltage and current, and the charge limit in force. Rows are buffered and
appended once a minute (flash), one file per local day under DIR, and files
older than KEEP_DAYS are deleted. About 1 MB a day for three packs.
"""

import logging
import os
import time

KEEP_DAYS = 14
COLUMNS = ("time", "pack", "cells", "max_cell", "cell_temp", "temps", "pack_current",
           "pack_voltage", "soc", "charge_disabled", "battery_voltage",
           "battery_current", "limit")

log = logging.getLogger("softbms")


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".")
    return str(v)


class ReadingLog:
    def __init__(self, directory, keep_days=KEEP_DAYS):
        self.dir = directory
        self.keep_days = keep_days
        self.rows = []
        os.makedirs(directory, exist_ok=True)

    def add(self, pack, status, battery_voltage, battery_current, limit, now=None):
        now = time.time() if now is None else now
        cells = status.get("cells") or []
        self.rows.append((now, [
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), pack,
            "/".join(f"{c:.3f}" for c in cells), max(cells) if cells else None,
            status.get("cell_temp"),
            "/".join(str(t) for t in status.get("temps") or []) or None,
            status.get("current"), status.get("voltage"),
            status.get("soc"), status.get("charge_disabled"),
            battery_voltage, battery_current, limit]))

    def flush(self):
        if not self.rows:
            return
        rows, self.rows = self.rows, []
        by_day = {}
        for t, row in rows:
            by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(t)), []).append(row)
        for day, day_rows in by_day.items():
            path = os.path.join(self.dir, f"{day}.csv")
            header = ",".join(COLUMNS)
            if os.path.exists(path):
                with open(path) as f:
                    if f.readline().rstrip("\n") != header:
                        # Columns changed (new version deployed mid-day): keep
                        # the old rows in their own file rather than mixing.
                        os.replace(path, os.path.join(self.dir, f"{day}.old.csv"))
            new = not os.path.exists(path)
            with open(path, "a") as f:
                if new:
                    f.write(header + "\n")
                for row in day_rows:
                    f.write(",".join(fmt(v) for v in row) + "\n")
        self.prune()

    def prune(self, now=None):
        cutoff = (time.time() if now is None else now) - self.keep_days * 86400
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if name.endswith(".csv") and os.path.getmtime(path) < cutoff:
                os.unlink(path)
                log.info("removed old reading log %s", name)
