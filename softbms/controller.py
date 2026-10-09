"""Charge-current control law ("soft BMS"), independent of the battery brand.

Pure logic, no I/O: feed it each pack's latest status and its age, get back the
DVCC max charge current to apply (None = no limit from us). bridge.py applies
the result through chargelimit.py. test_controller.py exercises it.

A status is the protocol module's normalised dict: cells (volts), cell_temp
(°C), charge_block (list of reasons the pack says charging must stop) and
charge_disabled (the pack's charge FET is off). Any other keys are ignored.
Every tunable below can be overridden per site (Controller(**overrides)).

Why current and not voltage: DVCC only honours /Settings/SystemSetup/
MaxChargeVoltage when a BMS supplies a charge voltage (dvcc.py), and these packs
are deliberately not registered as a BMS. So the one actuator is current.

Rules, each producing a cap; the tightest wins:

  * Highest cell voltage tapers the current linearly from TAPER_MAX_A at
    TAPER_START_V to TAPER_MIN_A at TAPER_END_V. The chargers absorb at 14.2 V,
    3.55 V a cell, and the packs read cells 10-15 mV high, so a normal absorption
    reads ~3.57 V; the taper starts above that and only engages when one cell
    runs ahead. There is no step to 0 A on voltage: the pack's own overcharge
    protection trips at about 3.65 V, and the overcharge bit below stops charging.
  * Cell temperature: TEMP_TAPER_A at TEMP_TAPER_C, 0 A at TEMP_STOP_C, each
    released only TEMP_HYST_C lower.
  * A pack reporting a charge-blocking protection: 0 A, held PROTECT_HOLD_S
    after it clears. Protocol modules leave low-temperature charge protection
    out: self-heating packs warm themselves from charger power, and a 0 A
    limit in the cold would starve the heater.
  * Packs reporting charge disabled while others still charge: cap at
    PER_PACK_A times the number of packs still accepting charge, so the
    remaining packs do not take everything.

A pack whose status is missing, older than MAX_AGE_S or malformed contributes
nothing; it neither limits nor cancels another pack's limit. With no valid
pack at all the result is None and the user's own setting applies.

A reading beyond the plausible range on the high side (a cell at or above
CELL_V_RANGE[1], a temperature above TEMP_C_RANGE[1]) is either a real
emergency or a broken sensor; it is not discarded (that would release the
limit exactly when it matters most) but holds TAPER_MIN_A for that pack.

A protection flag only forces 0 A when something corroborates it: the pack has
switched its own charging off, a cell is above TAPER_START_V, or the
temperature is at TEMP_TAPER_C or above. The bit maps come from reverse
engineering; on their own they cap at TAPER_MIN_A, so a misread bit cannot stop
charging.

The limit tightens at once and loosens by at most LOOSEN_STEP_A every
LOOSEN_EVERY_S (and reaches TAPER_MAX_A before releasing altogether), which
stops a relaxing cell from bouncing the limit and keeps settings (flash) writes
rare. If a loosen has to be undone within two
intervals, the interval doubles (up to LOOSEN_MAX_S), so a cell that relaxes
the moment the current drops cannot drive a limit cycle.
"""

import math

CELLS_PER_PACK  = 4
CELL_V_RANGE    = (2.0, 4.0)       # outside this a reading is garbage, not a cell
TEMP_C_RANGE    = (-40, 100)

TAPER_START_V   = 3.59
TAPER_END_V     = 3.63
TAPER_MAX_A     = 60
TAPER_MIN_A     = 5
QUANTUM_A       = 5                # limits are multiples of this

TEMP_TAPER_C    = 45
TEMP_STOP_C     = 50
TEMP_HYST_C     = 3
TEMP_TAPER_A    = 40

PROTECT_HOLD_S  = 300
PER_PACK_A      = 90               # safe charge current per pack still accepting

MAX_AGE_S       = 120
LOOSEN_EVERY_S  = 60
LOOSEN_MAX_S    = 900              # back-off ceiling when loosening keeps being undone
LOOSEN_STEP_A   = 20


def _amps(limit):
    return math.inf if limit is None else limit


def validate(status, cells_per_pack=CELLS_PER_PACK):
    """Return a reason string if the status is garbage (cannot be used at all),
    else None. High out-of-range values are not garbage: see extreme()."""
    if not isinstance(status, dict):
        return "not a status object"
    cells = status.get("cells")
    if not isinstance(cells, list) or len(cells) != cells_per_pack:
        return f"expected {cells_per_pack} cells, got {cells!r}"
    lo, _ = CELL_V_RANGE
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) and c > lo
               for c in cells):
        return f"implausible cell voltage in {cells}"
    t = status.get("cell_temp")
    if (not isinstance(t, (int, float)) or isinstance(t, bool) or t != t
            or t < TEMP_C_RANGE[0]):
        return f"implausible cell temperature {t!r}"
    if not isinstance(status.get("charge_block"), list):
        return "missing charge_block"
    if not isinstance(status.get("charge_disabled"), bool):
        return "missing charge_disabled"
    return None


def extreme(status):
    """A reason if a (valid) status reads above the plausible range, else None."""
    hi_v = max(status["cells"])
    if hi_v >= CELL_V_RANGE[1]:
        return f"cell {hi_v:.3f}V beyond range"
    if status["cell_temp"] > TEMP_C_RANGE[1]:
        return f"{status['cell_temp']}°C beyond range"
    return None


def taper(max_cell, start=TAPER_START_V, end=TAPER_END_V, hi=TAPER_MAX_A,
          lo=TAPER_MIN_A, quantum=QUANTUM_A):
    if max_cell <= start:
        return None
    frac = min(1.0, (max_cell - start) / (end - start))
    amps = hi - frac * (hi - lo)
    return max(lo, int(amps // quantum) * quantum)


TUNABLES = ("CELLS_PER_PACK", "TAPER_START_V", "TAPER_END_V", "TAPER_MAX_A",
            "TAPER_MIN_A", "QUANTUM_A", "TEMP_TAPER_C", "TEMP_STOP_C", "TEMP_HYST_C",
            "TEMP_TAPER_A", "PROTECT_HOLD_S", "PER_PACK_A", "MAX_AGE_S",
            "LOOSEN_EVERY_S", "LOOSEN_MAX_S", "LOOSEN_STEP_A")


class Controller:
    def __init__(self, **overrides):
        for name in TUNABLES:
            setattr(self, name.lower(), globals()[name])
        for name, value in overrides.items():
            if name.upper() not in TUNABLES:
                raise ValueError(f"unknown controller setting {name!r}")
            setattr(self, name.lower(), value)
        self.applied = None          # what we currently ask for; None = no limit
        self.last_change = -math.inf
        self.last_loosen = -math.inf
        self.loosen_every = self.loosen_every_s
        self.temp_level = 0          # 0 normal, 1 taper, 2 stop
        self.protect_until = -math.inf
        self.protect_amps = 0
        self.no_data = True
        self.reason = "starting"

    def seed(self, amps, now):
        """Resume from a limit left in force (after a crash): ease off it
        through the normal path instead of dropping it at once."""
        self.applied, self.last_change = amps, now

    def _temp_cap(self, max_temp):
        want = 2 if max_temp >= self.temp_stop_c else 1 if max_temp >= self.temp_taper_c else 0
        if want > self.temp_level:
            self.temp_level = want
        elif want < self.temp_level:
            release = (self.temp_stop_c if self.temp_level == 2 else self.temp_taper_c) - self.temp_hyst_c
            if max_temp < release:
                self.temp_level = 1 if self.temp_level == 2 and max_temp >= self.temp_taper_c - self.temp_hyst_c else 0
        return {0: None, 1: self.temp_taper_a, 2: 0}[self.temp_level]

    def target(self, packs, now):
        """packs: {name: (status or None, age_s)}. Returns (limit, reason)."""
        valid, notes, caps = {}, [], []
        for name, (status, age) in packs.items():
            if status is None:
                notes.append(f"{name} no data")
                continue
            if age > self.max_age_s:
                notes.append(f"{name} stale {age:.0f}s")
                continue
            bad = validate(status, self.cells_per_pack)
            if bad:
                notes.append(f"{name} invalid: {bad}")
                continue
            why = extreme(status)
            if why:
                caps.append((self.taper_min_a, f"{name} {why}"))
                continue
            valid[name] = status

        self.no_data = not valid and not caps
        if not valid:
            self.temp_level = 0
            if caps:
                amps, why = min(caps, key=lambda c: c[0])
                return amps, why + " (" + ", ".join(notes) + ")" if notes else why
            return None, "no valid pack data (" + ", ".join(notes) + ")"

        max_cell, high_pack, high_idx = max(
            (v, n, i) for n, s in valid.items() for i, v in enumerate(s["cells"]))
        max_temp = max(s["cell_temp"] for s in valid.values())
        high = f"{high_pack} cell{high_idx + 1} {max_cell:.3f}V"

        c = taper(max_cell, self.taper_start_v, self.taper_end_v, self.taper_max_a,
                  self.taper_min_a, self.quantum_a)
        if c is not None:
            caps.append((c, high))
        c = self._temp_cap(max_temp)
        if c is not None:
            caps.append((c, f"{max_temp}°C"))

        blocked, confirmed = [], False
        for n, s in valid.items():
            if s["charge_block"]:
                blocked += [f"{n} {why}" for why in s["charge_block"]]
                confirmed = confirmed or (s["charge_disabled"]
                                          or max(s["cells"]) > self.taper_start_v
                                          or s["cell_temp"] >= self.temp_taper_c)
        if blocked:
            self.protect_until = now + self.protect_hold_s
            self.protect_amps = 0 if confirmed else self.taper_min_a
        if now < self.protect_until:
            caps.append((self.protect_amps, (", ".join(blocked) or "protection hold")
                         + ("" if self.protect_amps == 0 else " (unconfirmed)")))

        disabled = [n for n, s in valid.items() if s["charge_disabled"]]
        accepting = len(valid) - len(disabled)
        if disabled and accepting > 0:
            caps.append((self.per_pack_a * accepting,
                         " ".join(disabled) + " charge disabled"))

        summary = f"{high} {max_temp}°C"
        if notes:
            summary += " (" + ", ".join(notes) + ")"
        if not caps:
            return None, summary
        amps, why = min(caps, key=lambda c: c[0])
        return amps, why + " - " + summary

    def step(self, packs, now):
        """Advance one evaluation. Returns (limit to apply, reason)."""
        target, self.reason = self.target(packs, now)
        if self.no_data:
            # Losing every pack is not a relaxing cell: release at the normal pace.
            self.loosen_every = self.loosen_every_s
        if _amps(target) < _amps(self.applied):
            if now - self.last_loosen < 2 * self.loosen_every:
                self.loosen_every = min(self.loosen_every * 2, self.loosen_max_s)
            self.applied, self.last_change = target, now
        elif _amps(target) > _amps(self.applied) and now - self.last_change >= self.loosen_every:
            nxt = self.applied + self.loosen_step_a
            if target is None and self.applied >= self.taper_max_a:
                self.applied = None
                self.loosen_every = self.loosen_every_s
            elif target is None:
                # Pass through the top of the taper before letting go: jumping
                # from 45 A straight to unlimited sent a high cell from 3.50 to
                # 3.64 V within a minute on 2026-10-01.
                self.applied = min(nxt, self.taper_max_a)
            else:
                self.applied = min(nxt, _amps(target))
            self.last_change = self.last_loosen = now
        if self.applied != target:
            self.reason += f" (easing toward {'no limit' if target is None else f'{target}A'})"
        return self.applied, self.reason
