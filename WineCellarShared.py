"""
Shared control-logic constants and pure helper functions used by both
RunWineCooling.py (the GPIO-driving control loop) and
WineCellarManagerDash.py (the read-only web dashboard).

This module has NO hardware dependencies (no RPi.GPIO, board, busio,
adafruit_*) and never touches GPIO - it's safe to import from either
script without side effects, unlike importing RunWineCooling.py
directly would be (that module initialises GPIO pins and I2C sensors
as a side effect of import).
"""

import os
import json
import math
import traceback
from datetime import datetime

# ── Control targets ─────────────────────────────────────
TEMP_TARGET_MAX = 17.5      # °C - ideal cellar maximum

# Cooling keeps running anytime outside is usefully cooler than
# inside, all the way down to this floor - not just until back within
# the ideal band. Banking extra cooling whenever it's free (instead
# of stopping the moment it's "good enough") gives more thermal
# margin to burn through once a heatwave takes that opportunity away.
COOLING_TEMP_FLOOR = 11.0    # °C - stop opportunistic cooling here

HUMIDITY_TARGET_MIN = 65.0  # % - stop opportunistic dehumidify here
HUMIDITY_TARGET_MAX = 80.0  # %

# ── Outside temperature safety limits (seasonal, tweak as needed) ──
# Absolute hard floor - never run the intake fan below this outside
# temperature, no matter what else is going on. Protects against
# shocking the cellar with very cold air. Relax this in summer,
# tighten it in winter.
OUTSIDE_ABS_MIN_TEMP = 6.0       # °C

# Extra margin below COOLING_TEMP_FLOOR - we won't run the intake fan
# if doing so risks pulling inside temp below (COOLING_TEMP_FLOOR -
# margin). This lets the "effective" outside minimum track that floor
# rather than being a fixed number you must remember to update
# separately.
OUTSIDE_MIN_TEMP_MARGIN = 5.0    # °C

# ── Manual fan overrides (dashboard control) ──────────────────────
OVERRIDE_FILE = "/home/jakem/WineCellarManagerCode/fan_override.json"

# How long a manual "on" override is allowed to keep violating a
# safety check before it's forced back to Auto. Temperature-direction
# violations (too cold/too hot to bring in) can hurt fastest, so get
# a short, flat leash. Humidity-only violations develop more slowly,
# so instead of a flat leash they SCALE with how much headroom the
# cellar currently has below HUMIDITY_TARGET_MAX - a currently-dry
# cellar earns more benefit of the doubt, up to a 2 hour cap.
MANUAL_TEMP_VIOLATION_TIMEOUT_SECONDS = 5 * 60        # 5 min, flat
MANUAL_HUMIDITY_TIMEOUT_MIN_SECONDS = 30 * 60         # 30 min - little/no headroom
MANUAL_HUMIDITY_TIMEOUT_MAX_SECONDS = 2 * 60 * 60     # 2 hours - full headroom
MANUAL_EXTRACTOR_HOT_TIMEOUT_SECONDS = 30 * 60       # 30 min - risk is slower (infiltration, not direct intake)

# ── Error logging ────────────────────────────────────────────────
# One plain text append log covers two things: sensor READ failures
# specifically (log_sensor_error), and any OTHER unexpected error
# anywhere in the control loop (log_control_error) - a corrupt
# override file, a library hiccup, anything unanticipated. Keeping
# both in one file means there's only one place to check after the
# fact, rather than hunting for a silent process crash in systemd/
# journal logs. Never watching the console live shouldn't mean
# losing the evidence.
SENSOR_ERROR_LOG_FILE = "/home/jakem/WineCellarManagerCode/sensor_errors.log"

# How many attempts (including the first) to make on a sensor read
# before giving up on this poll cycle, and how long to wait between
# them. Most I2C glitches (bus noise, relay-switching EMI, a
# momentary SHT31D clock-stretch timeout) clear within a few seconds,
# so several short-spaced retries recover far more failures than a
# single retry does, while comfortably fitting inside one
# POLL_INTERVAL_SECONDS window (worst case here: 9 * 5s = 45s).
SENSOR_MAX_ATTEMPTS = 10
SENSOR_RETRY_DELAY_SECONDS = 5


def calculate_abs_humidity(temp_c, rh_pct):
    """Absolute humidity in g/m³, via the Magnus approximation for
    saturation vapour pressure. Represents the actual mass of water
    vapour per cubic metre of air - unlike RH, this doesn't change
    just because temperature changes, only when moisture content
    actually changes."""
    saturation_vp = 6.112 * math.exp((17.62 * temp_c) / (temp_c + 243.12))
    return 216.7 * (rh_pct / 100.0 * saturation_vp) / (273.15 + temp_c)


def max_allowable_abs_humidity(inside_temp, target_max_rh=HUMIDITY_TARGET_MAX):
    """Max outside absolute humidity (g/m³) that, once that air reaches
    inside_temp, would still keep inside RH at or below target_max_rh.
    This is what makes our humidity ceiling dynamic/temperature-aware
    rather than a fixed RH% - it tightens as the cellar cools and
    relaxes as the cellar warms, tracking the actual condensation/
    over-humidify risk rather than an arbitrary flat number."""
    saturation_vp = 6.112 * math.exp((17.62 * inside_temp) / (inside_temp + 243.12))
    return 216.7 * (target_max_rh / 100.0 * saturation_vp) / (273.15 + inside_temp)


def manual_humidity_timeout_seconds(inside_humidity):
    """How long a manual intake-on override gets to keep running
    despite a humidity-ceiling violation, scaled by how much headroom
    the cellar currently has below HUMIDITY_TARGET_MAX - a currently
    dry cellar earns more benefit of the doubt (up to 2 hours), a
    cellar already near the ceiling gets only the 30 min minimum."""
    headroom = HUMIDITY_TARGET_MAX - inside_humidity
    span = HUMIDITY_TARGET_MAX - HUMIDITY_TARGET_MIN
    frac = min(1.0, max(0.0, headroom / span))
    return MANUAL_HUMIDITY_TIMEOUT_MIN_SECONDS + frac * (
        MANUAL_HUMIDITY_TIMEOUT_MAX_SECONDS - MANUAL_HUMIDITY_TIMEOUT_MIN_SECONDS
    )


def evaluate_intake_violation(readings):
    """Classify whether forcing the INTAKE fan on right now would
    violate a safety boundary, and which category - "temp" (outside
    too cold or too hot to bring in - can work against the goal or
    shock the cellar quickly, so gets a short flat leash) or
    "humidity" (would push inside RH over the ceiling - a slower-
    developing risk, so gets a leash that scales with current
    headroom instead). None if nothing is violated. Whether the air
    happens to be warmer or cooler than inside doesn't matter here -
    both cooling and warming assistance are legitimate uses of the
    intake fan, and only outright unsafe conditions count. Only needs
    "inside_temp", "outside_temp" and "outside_humidity" from
    `readings`."""
    inside_temp = readings["inside_temp"]
    outside_temp = readings["outside_temp"]
    outside_humidity = readings["outside_humidity"]

    temp_violation = (
        outside_temp < OUTSIDE_ABS_MIN_TEMP
        or outside_temp < (COOLING_TEMP_FLOOR - OUTSIDE_MIN_TEMP_MARGIN)
        or outside_temp > inside_temp
    )
    if temp_violation:
        return "temp"

    outside_abs_humidity = calculate_abs_humidity(outside_temp, outside_humidity)
    if outside_abs_humidity > max_allowable_abs_humidity(inside_temp):
        return "humidity"

    return None


def evaluate_extractor_violation(readings):
    """The one universal risk for the extractor running without the
    intake fan (whether that's auto dehumidify/warm-assist, a manual
    extractor-on override, or a manual intake-off override that
    leaves the extractor running alone) - its air source is ambiguous
    (outside vs. the house), so it can't be bounded with a timeout, we
    just watch for outside becoming warmer than inside. Only needs
    "inside_temp" and "outside_temp" from `readings`."""
    return readings["outside_temp"] > readings["inside_temp"]


def read_override():
    """Load the manual fan-override file written by the dashboard.
    Returns {} if missing/unreadable - treated the same as "no
    overrides" rather than crashing the caller."""
    if not os.path.isfile(OVERRIDE_FILE):
        return {}
    try:
        with open(OVERRIDE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to read override file: {e}")
        return {}


def write_override(overrides):
    """Persist the override dict back to the shared file."""
    try:
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(overrides, f)
    except Exception as e:
        print(f"Failed to write override file: {e}")


def _append_error_log(line):
    """Shared append helper for SENSOR_ERROR_LOG_FILE - timestamps and
    writes one line, never raising (a failure to write this log
    should never itself take down the control loop)."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    try:
        with open(SENSOR_ERROR_LOG_FILE, "a") as f:
            f.write(f"{timestamp} {line}\n")
    except Exception as e:
        print(f"Failed to write error log: {e}")


def log_sensor_error(message):
    """Append a timestamped line to the error log for a SENSOR READ
    failure specifically. Console output via print() is easy to lose
    (systemd journal rotation, not watching the terminal live), so
    these also get written somewhere durable and easy to tail/grep:

        tail -f /home/jakem/WineCellarManagerCode/sensor_errors.log
    """
    _append_error_log(message)


def log_control_error(exc):
    """Append a timestamped line (with full traceback) to the SAME
    error log, for an unexpected exception anywhere ELSE in the
    control loop - a corrupt override file, a library error we didn't
    anticipate, anything not already handled more specifically.

    This matters because, without it, an exception like this would
    previously propagate straight out of the main loop uncaught,
    crash the whole script, and leave NOTHING in sensor_errors.log
    (since it was never a sensor read failure) - which is exactly
    consistent with "I see gaps but the log file is empty". Logging
    it here and letting the loop continue turns a silent full crash
    into one skipped cycle with a clear record of what happened."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _append_error_log(f"CONTROL LOOP ERROR: {type(exc).__name__}: {exc}\n{tb}")