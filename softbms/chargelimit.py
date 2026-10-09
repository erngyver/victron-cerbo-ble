"""Apply the controller's limit to DVCC /Settings/SystemSetup/MaxChargeCurrent.

The setting is the user's own DVCC limit and is persisted by localsettings, so
this module treats it with care:

  * The user's value is the *baseline*. The applied value is always
    min(baseline, controller limit), with -1 meaning no limit.
  * Every evaluation reads the setting back. A value that is neither our last
    confirmed write nor the write in progress was changed by someone else (the
    GX menu, VRM) and becomes the new baseline.
  * Write-ahead: the value about to be written is saved as "pending" before
    SetValue. A read-back that fails, or a crash between SetValue and saving,
    therefore leaves a value we recognise as our own, never one we mistake for
    the user's. (Without this, a single failed read-back after writing 0 A made
    0 A the user's baseline, restored on every shutdown.)
  * restore() writes the baseline back and records a clean stop; bridge.py
    calls it on shutdown. After a crash (no clean stop recorded), `leftover`
    holds the limit we had in force, so the bridge can keep it until fresh pack
    data arrives instead of dropping to the baseline.
  * The proxies follow the service's well-known name, so a localsettings
    restart does not leave them talking to a dead process.
  * The state file is a convenience: if /data is full or the file is corrupt,
    the bridge logs it and keeps controlling the setting.
  * Another writer: if the setting keeps changing under us (CONFLICT_CHANGES
    times within CONFLICT_WINDOW_S), the bridge stops writing for
    CONFLICT_HOLD_S and says so, rather than fighting it with a flash write
    every evaluation.
"""

import json
import logging
import os
import time

import dbus

SETTINGS  = "com.victronenergy.settings"
PATH      = "/Settings/SystemSetup/MaxChargeCurrent"
DVCC_PATH = "/Settings/Services/Bol"
NO_LIMIT  = -1

CONFLICT_CHANGES  = 3
CONFLICT_WINDOW_S = 600
CONFLICT_HOLD_S   = 1800

log = logging.getLogger("softbms")


def combine(baseline, limit):
    """min() where NO_LIMIT / None mean unlimited."""
    vals = [v for v in (baseline if baseline >= 0 else None, limit) if v is not None]
    return min(vals) if vals else NO_LIMIT


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class ChargeLimit:
    def __init__(self, bus, state_file, clock=time.monotonic):
        self.state_file = state_file
        self.clock = clock
        self.item = dbus.Interface(
            bus.get_object(SETTINGS, PATH, introspect=False, follow_name_owner_changes=True),
            "com.victronenergy.BusItem")
        self.dvcc = dbus.Interface(
            bus.get_object(SETTINGS, DVCC_PATH, introspect=False, follow_name_owner_changes=True),
            "com.victronenergy.BusItem")
        self.save_error = None
        self.foreign = []                # times of recent changes made by someone else
        self.conflict_until = None

        actual = self.read()
        saved = self._load()
        self.leftover = None
        if saved and actual in (saved["written"], saved["pending"]):
            # What is there now is our own write; the user's value is the saved one.
            self.baseline = saved["baseline"]
            if not saved["clean_stop"] and actual != self.baseline:
                self.leftover = actual
        else:
            self.baseline = actual
        self.written = actual
        self.pending = None
        self.clean_stop = False
        log.info("charge limit: setting %s, baseline %s%s", actual, self.baseline,
                 "" if self.leftover is None else f", leftover {actual} from an unclean stop")
        self._save()

    def read(self):
        return int(self.item.GetValue(timeout=5))

    def dvcc_on(self):
        try:
            return bool(self.dvcc.GetValue(timeout=5))
        except dbus.DBusException:
            return None

    @property
    def in_conflict(self):
        return self.conflict_until is not None and self.clock() < self.conflict_until

    def apply(self, limit):
        """Make the setting min(baseline, limit). Returns the value actually in force."""
        actual = self.read()
        if self.pending is not None and actual == self.pending:
            # A write whose confirmation we missed has landed.
            self.written, self.pending = actual, None
            self._save()
        elif actual == self.written:
            self.pending = None          # an attempted write that never landed
        else:
            self._foreign_change(actual)
        if self.in_conflict:
            return actual
        want = combine(self.baseline, limit)
        if actual != want:
            return self._write(want)
        return actual

    def _foreign_change(self, actual):
        (log.debug if self.in_conflict else log.warning)(
            "charge limit changed outside the bridge (%s -> %s); "
            "adopting %s as the new baseline", self.written, actual, actual)
        self.baseline, self.written, self.pending = actual, actual, None
        self._save()
        now = self.clock()
        self.foreign = [t for t in self.foreign if now - t < CONFLICT_WINDOW_S] + [now]
        if len(self.foreign) >= CONFLICT_CHANGES and not self.in_conflict:
            self.conflict_until = now + CONFLICT_HOLD_S
            log.error("MaxChargeCurrent changed by something else %d times in %d min: "
                      "another process is writing it. Not writing for %d min.",
                      len(self.foreign), CONFLICT_WINDOW_S // 60, CONFLICT_HOLD_S // 60)

    def restore(self):
        """Write the baseline back and record a clean stop. Never raises."""
        try:
            if self.read() != self.baseline:
                self._write(self.baseline)
                log.info("charge limit restored to baseline %s", self.baseline)
            self.clean_stop = True
            self._save()
        except Exception as e:
            log.error("could not restore charge limit baseline %s: %s", self.baseline, e)

    def _write(self, value):
        self.pending = value
        self._save()                     # write-ahead: see the module docstring
        rc = self.item.SetValue(dbus.Int32(value), signature="v", timeout=5)
        confirmed = self.read()
        if confirmed != value:
            log.error("write of %s did not land (rc=%s, reads %s)", value, rc, confirmed)
            return confirmed
        self.written, self.pending = value, None
        self._save()
        log.info("charge limit -> %s", "no limit" if value < 0 else f"{value}A")
        return value

    def _load(self):
        try:
            with open(self.state_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        baseline, written = _int_or_none(data.get("baseline")), _int_or_none(data.get("written"))
        if baseline is None or written is None:
            return None
        return {"baseline": baseline, "written": written,
                "pending": _int_or_none(data.get("pending")),
                "clean_stop": data.get("clean_stop") is True}

    def _save(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"baseline": self.baseline, "written": self.written,
                           "pending": self.pending, "clean_stop": self.clean_stop}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
            if self.save_error:
                log.info("state file writable again")
            self.save_error = None
        except OSError as e:
            if str(e) != self.save_error:
                log.error("could not save %s (%s); control continues without it",
                          self.state_file, e)
            self.save_error = str(e)
