#!/home/jakem/WineCellarManager/bin/python

import board
import busio
import adafruit_tca9548a
import adafruit_sht31d
import RPi.GPIO as GPIO
import time
import csv
import os
import json
from datetime import datetime, timedelta
import math

from WineCellarShared import (
    TEMP_TARGET_MAX,
    COOLING_TEMP_FLOOR,
    OUTSIDE_ABS_MIN_TEMP,
    OUTSIDE_MIN_TEMP_MARGIN,
    HUMIDITY_TARGET_MIN,
    HUMIDITY_TARGET_MAX,
    OVERRIDE_FILE,
    MANUAL_TEMP_VIOLATION_TIMEOUT_SECONDS,
    MANUAL_EXTRACTOR_HOT_TIMEOUT_SECONDS,
    calculate_abs_humidity,
    max_allowable_abs_humidity,
    manual_humidity_timeout_seconds,
    evaluate_intake_violation,
    evaluate_extractor_violation,
    read_override,
    write_override,
)

# ── GPIO setup ──────────────────────────────────────────
GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.OUT)  # IN1 - fan 1 (K1) - Extractor
GPIO.setup(27, GPIO.OUT)  # IN2 - fan 2 (K2) - Intake

# Songle relay is active LOW, so HIGH = off to start
GPIO.output(17, GPIO.HIGH)
GPIO.output(27, GPIO.HIGH)

# ── I2C and SHT31 sensor setup ─────────────────────────────────
i2c = busio.I2C(board.SCL, board.SDA)
tca = adafruit_tca9548a.TCA9548A(i2c)
sensor_inside  = adafruit_sht31d.SHT31D(tca[0])  # channel 0
sensor_outside = adafruit_sht31d.SHT31D(tca[1])  # channel 1

# ── Bottle probe (DS18B20, 1-Wire) ──────────────────────────────
# Optional third sensor - a temperature-only probe embedded in/against
# a bottle to track actual wine temperature (which lags ambient air
# and is arguably a more meaningful number than cellar air temp).
# Wrapped so the whole script still works fine if this sensor isn't
# wired up yet, or w1thermsensor isn't installed - it just reads as
# None everywhere and gets skipped in logging/display.
try:
    from w1thermsensor import W1ThermSensor, NoSensorFoundError
    try:
        bottle_sensor = W1ThermSensor()
        print("Bottle probe (DS18B20) detected.")
    except NoSensorFoundError:
        bottle_sensor = None
        print("No bottle probe detected - continuing without it.")
except ImportError:
    bottle_sensor = None
    print("w1thermsensor not installed - continuing without bottle probe.")

# ── Control targets ─────────────────────────────────────
# TEMP_TARGET_MAX, COOLING_TEMP_FLOOR, HUMIDITY_TARGET_MIN/MAX,
# OUTSIDE_ABS_MIN_TEMP and OUTSIDE_MIN_TEMP_MARGIN now live in
# WineCellarShared.py, since the dashboard's manual-override logic
# needs the exact same values.
TEMP_HYSTERESIS = 0.5       # °C - avoid rapid on/off cycling

# Mirror of COOLING_TEMP_FLOOR for the cold side - only bother
# warm-venting/warm-assisting once inside drops below this.
WARMING_TEMP_CEILING = 10.0  # °C - only warm below this

HUMIDITY_HYSTERESIS = 2.0   # %

# Required outside/inside temp edge before we bother venting for temp
# reasons (cooling OR warming) - SCALES with how far inside is from
# target: only marginally off-target requires a bigger edge (not
# worth cycling fans for a tiny gain), badly off-target relaxes down
# to the floor (grab any usable help once conditions are actually bad).
OUTSIDE_ADVANTAGE_TEMP_MAX = 2.0   # °C required edge when just off target
OUTSIDE_ADVANTAGE_TEMP_MIN = 0.5   # °C required edge when far off target (stays above sensor noise)
ADVANTAGE_SCALE_RANGE = 3.0        # °C of off-target-ness over which the requirement fully relaxes

# Outside DEW POINT must be at least this much lower than inside dew
# point before we bother running the extractor. Dew point reflects
# actual moisture content of the air, unlike RH (which rises at night
# purely because temperature drops, even with no change in moisture).
# Using dew point avoids being fooled by that relative-humidity effect.
OUTSIDE_DEWPOINT_ADVANTAGE = 1.0  # °C

# ── Dynamic humidity ceiling (replaces a fixed outside RH%) ──────
# See max_allowable_abs_humidity() in WineCellarShared.py for the
# full rationale - condensation/over-humidify risk depends on the
# ABSOLUTE moisture content of incoming air vs. the cellar's own
# temperature, so the ceiling is dynamic rather than a fixed RH%.

# ── Outside temperature safety limits (seasonal, tweak as needed) ──
# OUTSIDE_ABS_MIN_TEMP and OUTSIDE_MIN_TEMP_MARGIN now live in
# WineCellarShared.py (see above import).

POLL_INTERVAL_SECONDS = 300      # how often we read sensors & make decisions
LOG_INTERVAL_SECONDS = 900       # how often we write a row to the CSV log
OVERRIDE_CHECK_SECONDS = 10      # how often to check for new override requests between polls

#These were for tetsing
#POLL_INTERVAL_SECONDS = 15
#LOG_INTERVAL_SECONDS = 30
#MIN_RUN_SECONDS = 20

# Minimum time a fan state must run before we allow switching away
# from it. Protects the fan motors/relay from short-cycling if
# conditions hover right around a threshold. Does not apply to the
# very first transition out of FANS_OFF (there's nothing to protect
# there).

MIN_RUN_SECONDS = 600

# ── CSV logging ───────────────────────────────────────────
LOG_FILE = "/home/jakem/WineCellarManagerCode/wine_cellar_log.csv"
LOG_HEADER = [
    "timestamp",
    "inside_temp_c",
    "inside_humidity_pct",
    "inside_dewpoint_c",
    "outside_temp_c",
    "outside_humidity_pct",
    "outside_dewpoint_c",
    "bottle_temp_c",
    "fan_state",
    "extractor_polls_on",
    "intake_polls_on",
    "polls_this_interval",
]


# ── Fan state enum-ish constants ─────────────────────────
FANS_OFF = "off"
FANS_COOLING = "cooling"          # both fans - push/pull venting with outside air
FANS_WARM_VENT = "warm_vent"      # both fans - bring in outside air because it's now warmer than the cellar
FANS_DEHUMIDIFY = "dehumidify"    # extractor only - vent humidity without pulling in cold air via the intake fan
FANS_WARM_ASSIST = "warm_assist"  # extractor only - gamble that infiltration draws warmer air from the adjoining house


def calculate_dew_point(temp_c, rh_pct):
    """Calculate dew point (°C) from temperature and relative humidity
    using the Magnus-Tetens approximation. Valid for typical ambient
    ranges (0-60°C, RH 1-100%), which comfortably covers a cellar/
    outdoor use case."""
    a, b = 17.62, 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh_pct / 100.0)
    return (b * gamma) / (a - gamma)


def required_temp_advantage(temp_error):
    """How far outside temp must beat inside temp before we bother
    venting, scaled by temp_error (how far inside already is from its
    target). Requires a bigger edge for a marginal excursion, relaxes
    toward OUTSIDE_ADVANTAGE_TEMP_MIN as the excursion gets severe, so
    we grab any usable help once conditions are actually bad. Used for
    both cooling (outside cooler) and warm-venting (outside warmer)."""
    frac = min(1.0, max(0.0, temp_error) / ADVANTAGE_SCALE_RANGE)
    return OUTSIDE_ADVANTAGE_TEMP_MAX - frac * (OUTSIDE_ADVANTAGE_TEMP_MAX - OUTSIDE_ADVANTAGE_TEMP_MIN)


def read_bottle_probe():
    """Read the bottle probe if it's connected. Returns None if the
    sensor isn't attached/installed, or if the read fails for any
    reason - never raises, so this never breaks the main control
    loop even if the probe is unplugged or flaky."""
    if bottle_sensor is None:
        return None
    try:
        return bottle_sensor.get_temperature()
    except Exception as e:
        print(f"Bottle probe read failed: {e}")
        return None


def read_sensors():
    """Read both cellar sensors plus the optional bottle probe,
    returning a dict (including derived dew points). Returns None on
    a CELLAR sensor read failure so the caller can skip this cycle
    safely rather than crash. The bottle probe is independent - if
    it's missing/fails, "bottle_temp" is just None and everything
    else continues as normal."""
    try:
        inside_temp = sensor_inside.temperature
        inside_humidity = sensor_inside.relative_humidity
        outside_temp = sensor_outside.temperature
        outside_humidity = sensor_outside.relative_humidity

        return {
            "inside_temp": inside_temp,
            "inside_humidity": inside_humidity,
            "inside_dewpoint": calculate_dew_point(inside_temp, inside_humidity),
            "outside_temp": outside_temp,
            "outside_humidity": outside_humidity,
            "outside_dewpoint": calculate_dew_point(outside_temp, outside_humidity),
            "bottle_temp": read_bottle_probe(),
        }
    except Exception as e:
        print(f"Sensor read failed: {e}")
        return None


def decide_fan_state(readings, current_state):
    """
    Decide what the fans should be doing this cycle.

    PRIORITY ORDER (highest first):
      1. Safety     - never run intake fan if outside air is too cold;
                      never gamble on extractor-only infiltration for
                      warmth if outside is at our hard cold floor.
                      For extractor-only modes the hot-outside check is
                      per-mode: dehumidify blocks when outside > inside
                      (with hysteresis); warm-assist caps at TEMP_TARGET_MAX.
                      Cooling/warm-vent need no hot-outside check because
                      outside_cool/warm_enough already gates them.
      2. Cooling    - bring in outside air to cool the cellar. This is
                      the primary goal of a passive/assisted cellar, so
                      it always beats humidity/warmth control.
      3. Warm-vent  - mirror image of cooling: bring in outside air
                      (both fans) to warm the cellar when it's too cold
                      and outside is now the warmer side.
      4. Dehumidify-only - if we can't (or don't need to) cool or
                      warm-vent, but inside humidity is too high, run
                      the extractor alone. This creates negative
                      pressure so air infiltrates naturally through
                      gaps rather than blasting in cold outside air.
      5. Warm-assist - last resort for a too-cold cellar: extractor
                      alone, gambling that infiltration draws warmer
                      air from the adjoining house rather than outside
                      (we have no sensor to tell which).
      6. Off        - conditions are within target range.

    HYSTERESIS: we use wider "trigger" thresholds to turn ON, and
    tighter "release" thresholds (back to the plain target) to turn
    OFF. This stops the fans short-cycling on/off right at the
    boundary. current_state tells us whether we're already running,
    so we know which threshold to apply.
    """
    inside_temp = readings["inside_temp"]
    inside_humidity = readings["inside_humidity"]
    outside_temp = readings["outside_temp"]
    outside_humidity = readings["outside_humidity"]

    # ── 1. Safety check: is outside air cold enough to be a risk? ──
    # Hard absolute floor.
    outside_too_cold_abs = outside_temp < OUTSIDE_ABS_MIN_TEMP
    # Seasonal/relative floor - don't undercut our cooling floor by
    # more than the configured margin.
    outside_too_cold_relative = outside_temp < (COOLING_TEMP_FLOOR - OUTSIDE_MIN_TEMP_MARGIN)
    intake_blocked_by_cold = outside_too_cold_abs or outside_too_cold_relative

    # ── 1b. Safety checks for extractor-only modes ───────────────────────────
    # Both dehumidify and warm-assist run the extractor alone, so their makeup
    # air source is ambiguous (outside vs. house). The hot-outside check is
    # deliberately different for each:
    #
    # Dehumidify: block when outside is warmer than inside - the relative check
    # matches the manual-extractor logic and is right for a mode whose only
    # goal is removing moisture (any warming side-effect is unwanted).
    # Hysteresis stops short-cycling right at the boundary.
    if current_state == FANS_DEHUMIDIFY:
        dehumidify_blocked_by_hot_outside = outside_temp > (inside_temp + TEMP_HYSTERESIS)
    else:
        dehumidify_blocked_by_hot_outside = outside_temp > inside_temp

    # Warm-assist: use an absolute cap at TEMP_TARGET_MAX rather than a
    # relative inside-temp check. Warm-assist is *trying* to raise the cellar
    # temp, so outside being warmer than inside is normal and desirable; we
    # only need to prevent infiltration from overshooting the target ceiling.
    warm_assist_blocked_by_hot_outside = outside_temp > TEMP_TARGET_MAX

    # ── 1c. Safety check: is outside air too cold to gamble the
    # extractor-only warm-assist on? We can't tell whether its makeup
    # air actually comes from the (presumably warmer) adjoining house
    # or straight from outside, so if outside is already at our hard
    # cold floor, skip the gamble entirely rather than risk it.
    warm_assist_blocked_by_cold_outside = outside_too_cold_abs

    # ── 2. Cooling check ─────────────────────────────────
    # Trigger cooling if inside is above the cooling floor (+
    # hysteresis if not already cooling) - deliberately keeps going
    # well past TEMP_TARGET_MAX so we bank extra cooling whenever it's
    # free - outside is usefully cooler than inside, and the
    # cold-safety check above doesn't block intake air.
    if current_state == FANS_COOLING:
        cooling_temp_trigger = inside_temp > COOLING_TEMP_FLOOR
    else:
        cooling_temp_trigger = inside_temp > (COOLING_TEMP_FLOOR + TEMP_HYSTERESIS)

    outside_cool_enough = outside_temp < (
        inside_temp - required_temp_advantage(inside_temp - COOLING_TEMP_FLOOR)
    )

    # Moisture check: cooling mode actively pushes outside air in via
    # the intake fan, so we must not do this if that air would push
    # inside RH above our target ceiling once it reaches cellar temp -
    # otherwise we'd be solving a heat problem while creating a damp/
    # condensation one. Uses the same absolute-humidity ceiling as the
    # dehumidify check, since the physics (and the risk) are identical
    # whenever outside air is being introduced.
    outside_abs_humidity = calculate_abs_humidity(outside_temp, outside_humidity)
    max_abs_humidity_for_cooling = max_allowable_abs_humidity(inside_temp)
    cooling_wont_overhumidify = outside_abs_humidity <= max_abs_humidity_for_cooling

    if (
        cooling_temp_trigger
        and outside_cool_enough
        and cooling_wont_overhumidify
        and not intake_blocked_by_cold
    ):
        return FANS_COOLING

    # ── 3. Warm-vent check ────────────────────────────────
    # Mirror image of cooling: inside is too COLD and outside air is
    # now the warmer side, so bring it in on purpose via both fans -
    # same mechanism as cooling, just the opposite direction. The
    # cold-intake safety check doesn't apply here since we're pulling
    # in warmer air, not colder. Still gated by the same moisture
    # ceiling so we don't fix the cold at the cost of over-humidifying.
    if current_state == FANS_WARM_VENT:
        warming_temp_trigger = inside_temp < WARMING_TEMP_CEILING
    else:
        warming_temp_trigger = inside_temp < (WARMING_TEMP_CEILING - TEMP_HYSTERESIS)

    outside_warm_enough = outside_temp > (
        inside_temp + required_temp_advantage(WARMING_TEMP_CEILING - inside_temp)
    )

    if (
        warming_temp_trigger
        and outside_warm_enough
        and cooling_wont_overhumidify
    ):
        return FANS_WARM_VENT

    # ── 4. Dehumidify-only check ─────────────────────────
    # Only considered if cooling/warm-venting isn't happening this
    # cycle. Runs the extractor alone - no intake fan, so the
    # cold-safety check does NOT apply here (we're not pulling in
    # outside air directly).
    #
    # But we DON'T know where the replacement air the extractor draws
    # in actually comes from - the negative pressure it creates pulls
    # air in through whatever gaps are easiest, which could be from
    # outside OR from the house interior. That's fine if inside temp
    # is already OK, but if inside is ALSO too hot (and cooling isn't
    # viable, e.g. outside isn't cool enough), running the extractor
    # alone risks pulling in warm/humid house air instead of outside
    # air, making the exact problem we're trying to fix worse. So we
    # block dehumidify-only mode whenever inside is too hot - in that
    # state we'd rather actively cool (if outside allows) or do
    # nothing, rather than vent via an unknown infiltration path.
    dehumidify_blocked_by_hot_inside = inside_temp > 15.0  # headroom against warm house infiltration

    # Trigger dehumidify if inside humidity is above the floor (+
    # hysteresis if not already dehumidifying) - deliberately keeps
    # going well past HUMIDITY_TARGET_MAX so we bank extra drying
    # whenever it's free, same rationale as the cooling floor above.
    if current_state == FANS_DEHUMIDIFY:
        humidity_trigger = inside_humidity > HUMIDITY_TARGET_MIN
    else:
        humidity_trigger = inside_humidity > (HUMIDITY_TARGET_MIN + HUMIDITY_HYSTERESIS)

    # Don't bother extracting unless outside air is meaningfully drier
    # in absolute terms.
    outside_drier_than_inside = (
        readings["outside_dewpoint"] < (readings["inside_dewpoint"] - OUTSIDE_DEWPOINT_ADVANTAGE)
    )

    # Precise condensation/over-humidify check: would this outside air,
    # once it reaches cellar temperature, push inside RH above our
    # target ceiling? Uses absolute humidity rather than a flat dew
    # point margin, so it's exact rather than approximated. (Reuses
    # the same calculation as the cooling branch above, since the
    # physics are identical.)
    wont_overhumidify = outside_abs_humidity <= max_abs_humidity_for_cooling

    outside_dewpoint_ok = outside_drier_than_inside and wont_overhumidify

    if (
        humidity_trigger
        and outside_dewpoint_ok
        and not dehumidify_blocked_by_hot_outside
        and not dehumidify_blocked_by_hot_inside
    ):
        return FANS_DEHUMIDIFY

    # ── 5. Warm-assist check ──────────────────────────────
    # Last resort for a too-cold cellar when warm-venting isn't
    # available (outside isn't the warmer side, or not by enough).
    # Runs the extractor alone, gambling that its makeup air is drawn
    # more from the adjoining (heated) house than from outside -
    # skipped if that gamble is blocked by cold outside air, or if
    # outside has already exceeded TEMP_TARGET_MAX (infiltration could
    # then overshoot the ceiling with no way to stop it).
    if current_state == FANS_WARM_ASSIST:
        cold_trigger = inside_temp < WARMING_TEMP_CEILING
    else:
        cold_trigger = inside_temp < (WARMING_TEMP_CEILING - TEMP_HYSTERESIS)

    if (
        cold_trigger
        and not warm_assist_blocked_by_cold_outside
        and not warm_assist_blocked_by_hot_outside
    ):
        return FANS_WARM_ASSIST

    # ── 6. Otherwise, all good - fans off ────────────────
    return FANS_OFF


def drive_relays(extractor_on, intake_on):
    """Directly drive the relay GPIOs. Relay is active LOW: LOW = fan
    on, HIGH = fan off. This is the only place that actually touches
    GPIO - used for both the auto decision and manual overrides."""
    GPIO.output(17, GPIO.LOW if extractor_on else GPIO.HIGH)
    GPIO.output(27, GPIO.LOW if intake_on else GPIO.HIGH)


def fan_flags(state):
    """Return (extractor_running, intake_running) booleans for a given
    named auto fan state."""
    if state in (FANS_COOLING, FANS_WARM_VENT):
        return True, True
    elif state in (FANS_DEHUMIDIFY, FANS_WARM_ASSIST):
        return True, False
    else:  # FANS_OFF
        return False, False


# ── Manual fan overrides ──────────────────────────────────────────
# Lets the dashboard force a fan on/off via a small shared JSON file
# this script polls each cycle - the dashboard is the only writer for
# NEW requests, this script is the only writer for validation/expiry/
# reverts, avoiding any need for sockets or other IPC.


def _override_entry(overrides, fan_key):
    entry = overrides.get(fan_key)
    if not entry or entry.get("state") not in ("on", "off"):
        return None
    return entry


def resolve_extractor_override(readings, overrides, auto_value):
    """Mirrors intake: timed if already hot at validation, untimed but instant-revert if clean."""
    entry = _override_entry(overrides, "extractor")
    if entry is None:
        return auto_value, False

    if entry["state"] == "off":
        return False, True

    now = datetime.now()

    if not entry.get("validated"):
        entry["validated"] = True
        if evaluate_extractor_violation(readings):
            timeout = MANUAL_EXTRACTOR_HOT_TIMEOUT_SECONDS
            outside_temp = readings["outside_temp"]
            inside_temp = readings["inside_temp"]
            entry["expires_at"] = (now + timedelta(seconds=timeout)).isoformat()
            entry["reason"] = "temp"
            print(f"Extractor override: outside {outside_temp:.1f}\u00b0C > inside {inside_temp:.1f}\u00b0C - reverts in {timeout // 60} min")
            overrides["warning"] = f"Extractor forced on: outside {outside_temp:.1f}\u00b0C > inside {inside_temp:.1f}\u00b0C \u2013 reverts in ~{timeout // 60} min"
        return True, True

    expires_at = entry.get("expires_at")
    if expires_at:
        if now >= datetime.fromisoformat(expires_at):
            print("Extractor override: timed out - reverting to auto")
            del overrides["extractor"]
            return auto_value, False
        return True, True

    if evaluate_extractor_violation(readings):
        print("Extractor override: hot-outside check hit - reverting to auto")
        del overrides["extractor"]
        return auto_value, False

    return True, True


def resolve_intake_override(readings, overrides, auto_value, extractor_on_result):
    """Apply a manual intake override on top of the auto decision.

    Forcing OFF is safe UNLESS it leaves the extractor running alone
    (negative pressure / unknown-source infiltration) - in that case
    it's watched exactly like an extractor-alone override and reverts
    instantly (no timer) if outside becomes hotter than our ideal max.

    Forcing ON is checked against evaluate_intake_violation(): if it
    was already violating a check the first time we see it, it's
    accepted but bounded by a timeout (flat 5 min for a temp
    violation, scaled 30 min-2 hr for a humidity violation); if it was
    clean, it runs untimed but reverts to auto instantly the moment a
    violation later appears - no benefit of the doubt for a risk that
    wasn't there when it was turned on.

    Mutates `overrides` in place. Returns (intake_on, is_manual)."""
    entry = _override_entry(overrides, "intake")
    if entry is None:
        return auto_value, False

    now = datetime.now()

    if entry["state"] == "off":
        if extractor_on_result and evaluate_extractor_violation(readings):
            print("Intake-off override: extractor alone, hot-outside check hit - reverting to auto")
            del overrides["intake"]
            return auto_value, False
        return False, True

    if not entry.get("validated"):
        entry["validated"] = True
        violation = evaluate_intake_violation(readings)
        if violation is not None:
            outside_temp = readings["outside_temp"]
            inside_temp = readings["inside_temp"]
            if violation == "temp":
                timeout = MANUAL_TEMP_VIOLATION_TIMEOUT_SECONDS
                if outside_temp < OUTSIDE_ABS_MIN_TEMP:
                    detail = f"outside {outside_temp:.1f}°C (min {OUTSIDE_ABS_MIN_TEMP:.1f}°C)"
                elif outside_temp < (COOLING_TEMP_FLOOR - OUTSIDE_MIN_TEMP_MARGIN):
                    detail = f"outside {outside_temp:.1f}°C (risks undercooling)"
                else:
                    detail = f"outside {outside_temp:.1f}°C > inside {inside_temp:.1f}°C"
            else:
                timeout = manual_humidity_timeout_seconds(readings["inside_humidity"])
                detail = f"outside air too humid at {inside_temp:.1f}°C cellar"
            entry["expires_at"] = (now + timedelta(seconds=timeout)).isoformat()
            entry["reason"] = violation
            entry["timeout_seconds"] = timeout
            print(f"Intake override: {violation} check – {detail} – reverts in {timeout / 60:.0f} min")
            overrides["warning"] = f"Intake forced on: {detail} – reverts in ~{timeout / 60:.0f} min"
        return True, True

    expires_at = entry.get("expires_at")
    if expires_at:
        if now >= datetime.fromisoformat(expires_at):
            print("Intake override: timed out - reverting to auto")
            del overrides["intake"]
            return auto_value, False
        return True, True

    _violation = evaluate_intake_violation(readings)
    if _violation is not None:
        print(f"Intake override: {_violation} check hit - reverting to auto")
        del overrides["intake"]
        return auto_value, False

    return True, True


def log_reading(readings, state, extractor_polls_on, intake_polls_on, polls_this_interval):
    """Append a row of sensor readings + fan poll-counts to the CSV
    log. Creates the file with a header if it doesn't exist yet.

    extractor_polls_on / intake_polls_on record how many of the
    5-minute (POLL_INTERVAL_SECONDS) polls within this logging window
    had that fan running - e.g. "3 of 3" means it ran the whole 15
    minutes, "1 of 3" means it only ran briefly. This is more useful
    than a plain True/False, since it also captures HOW MUCH a fan
    ran during the interval, not just whether it ran at all."""
    file_exists = os.path.isfile(LOG_FILE)
    bottle_temp = readings.get("bottle_temp")
    bottle_temp_str = f"{bottle_temp:.1f}" if bottle_temp is not None else ""
    try:
        with open(LOG_FILE, mode="a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(LOG_HEADER)
            writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                f"{readings['inside_temp']:.1f}",
                f"{readings['inside_humidity']:.1f}",
                f"{readings['inside_dewpoint']:.1f}",
                f"{readings['outside_temp']:.1f}",
                f"{readings['outside_humidity']:.1f}",
                f"{readings['outside_dewpoint']:.1f}",
                bottle_temp_str,
                state,
                extractor_polls_on,
                intake_polls_on,
                polls_this_interval,
            ])
    except Exception as e:
        print(f"Failed to write log: {e}")


def _resolve_and_drive(readings, current_state):
    """Read overrides, resolve against current readings/state, drive relays.
    Returns (extractor_on, intake_on, is_manual)."""
    auto_extractor_on, auto_intake_on = fan_flags(current_state)
    overrides = read_override()
    before = json.dumps(overrides, sort_keys=True)
    if "warning" in overrides and not any(
        overrides.get(k, {}).get("state") in ("on", "off")
        for k in ("extractor", "intake")
    ):
        del overrides["warning"]
    # Extractor resolved first - intake's "off" path needs its final state.
    extractor_on, extractor_manual = resolve_extractor_override(
        readings, overrides, auto_extractor_on
    )
    intake_on, intake_manual = resolve_intake_override(
        readings, overrides, auto_intake_on, extractor_on
    )
    if json.dumps(overrides, sort_keys=True) != before:
        write_override(overrides)
    drive_relays(extractor_on, intake_on)
    return extractor_on, intake_on, extractor_manual or intake_manual


def main():
    current_state = FANS_OFF
    state_started_at = time.monotonic()
    last_log_at = time.monotonic()
    extractor_polls_on = 0
    intake_polls_on = 0
    polls_this_interval = 0
    display_state = FANS_OFF
    last_readings = None

    print("Wine cellar cooling control started.")

    try:
        while True:
            poll_start = time.monotonic()
            readings = read_sensors()

            if readings is not None:
                last_readings = readings
                desired_state = decide_fan_state(readings, current_state)

                # Only switching *away* from an active state is guarded; switching in from OFF is not.
                # Safety-triggered transitions (outside crossed a hard limit) bypass the guard.
                time_in_state = time.monotonic() - state_started_at
                _ot = readings["outside_temp"]
                _it = readings["inside_temp"]
                safety_triggered = (
                    _ot < OUTSIDE_ABS_MIN_TEMP
                    or _ot < (COOLING_TEMP_FLOOR - OUTSIDE_MIN_TEMP_MARGIN)
                    or (_ot > _it and current_state in (FANS_COOLING, FANS_DEHUMIDIFY))
                    or (_ot < _it and current_state == FANS_WARM_VENT)
                    or (_ot > TEMP_TARGET_MAX and current_state == FANS_WARM_ASSIST)
                )
                if (
                    current_state != FANS_OFF
                    and desired_state != current_state
                    and time_in_state < MIN_RUN_SECONDS
                    and not safety_triggered
                ):
                    print(
                        f"Holding {current_state} - min run time not yet reached "
                        f"({time_in_state:.0f}s / {MIN_RUN_SECONDS}s)"
                    )
                    new_state = current_state
                else:
                    new_state = desired_state

                if new_state != current_state:
                    print(
                        f"Auto state change: {current_state} -> {new_state} | "
                        f"inside={readings['inside_temp']:.1f}C/{readings['inside_humidity']:.1f}% "
                        f"outside={readings['outside_temp']:.1f}C/{readings['outside_humidity']:.1f}%"
                    )
                    current_state = new_state
                    state_started_at = time.monotonic()

                extractor_on, intake_on, is_manual = _resolve_and_drive(readings, current_state)

                new_display_state = "manual" if is_manual else current_state
                if new_display_state != display_state:
                    print(f"Fan mode: {display_state} -> {new_display_state}")
                    display_state = new_display_state

                # Poll counts use the actual post-override relay state.
                polls_this_interval += 1
                if extractor_on:
                    extractor_polls_on += 1
                if intake_on:
                    intake_polls_on += 1

                now = time.monotonic()
                if now - last_log_at >= LOG_INTERVAL_SECONDS:
                    log_reading(
                        readings,
                        display_state,
                        extractor_polls_on,
                        intake_polls_on,
                        polls_this_interval,
                    )
                    last_log_at = now
                    extractor_polls_on = 0
                    intake_polls_on = 0
                    polls_this_interval = 0

            # ── Check for new override requests between full sensor polls ──
            while True:
                remaining = POLL_INTERVAL_SECONDS - (time.monotonic() - poll_start)
                if remaining <= 0:
                    break
                time.sleep(min(OVERRIDE_CHECK_SECONDS, remaining))
                if last_readings is None:
                    continue
                _, _, is_manual = _resolve_and_drive(last_readings, current_state)
                new_display_state = "manual" if is_manual else current_state
                if new_display_state != display_state:
                    print(f"Fan mode: {display_state} -> {new_display_state}")
                    display_state = new_display_state

    except KeyboardInterrupt:
        print("Stopping - cleaning up GPIO.")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
