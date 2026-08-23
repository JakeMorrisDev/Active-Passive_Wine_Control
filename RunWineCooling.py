#!/home/jakem/WineCellarManager/bin/python

import board
import busio
import adafruit_tca9548a
import adafruit_sht31d
import RPi.GPIO as GPIO
import time
import csv
import os
from datetime import datetime
import math

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
TEMP_TARGET_MAX = 17.5      # °C - ideal cellar maximum
TEMP_HYSTERESIS = 0.5       # °C - avoid rapid on/off cycling

# Cooling keeps running anytime outside is usefully cooler than
# inside, all the way down to this floor - not just until back within
# the ideal band. Banking extra cooling whenever it's free (instead
# of stopping the moment it's "good enough") gives more thermal
# margin to burn through once a heatwave takes that opportunity away.
COOLING_TEMP_FLOOR = 12.0    # °C - stop opportunistic cooling here

# Mirror of the floor above for the cold side - only bother
# warm-venting/warm-assisting once inside drops below this.
WARMING_TEMP_CEILING = 10.0  # °C - only warm below this

# Dehumidify keeps running anytime outside air is usefully drier than
# inside, all the way down to this floor - not just until back within
# the ideal band. Same rationale as COOLING_TEMP_FLOOR: bank extra
# drying whenever it's free instead of stopping the moment it's
# "good enough".
HUMIDITY_TARGET_MIN = 65.0  # % - stop opportunistic dehumidify here
HUMIDITY_TARGET_MAX = 80.0  # %
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
# Condensation/over-humidifying risk depends on the ABSOLUTE moisture
# content of incoming air vs. the cellar's own temperature (surfaces
# sit roughly at INSIDE TEMPERATURE). A fixed outside RH% ceiling is
# misleading - e.g. hot muggy air can read >80% RH while still having
# a perfectly safe, low absolute humidity. Instead we calculate the
# exact max outside absolute humidity that, once that air reaches
# inside_temp, would still keep inside RH at or below this target.
#
# This makes the effective "humidity ceiling" DYNAMIC:
#   - Tightens automatically as the cellar cools (less room before
#     condensation risk at low inside temps).
#   - Relaxes automatically as the cellar warms (more headroom, so we
#     can ventilate more freely on warm days - exactly when we most
#     want to vent).
DEHUMIDIFY_TARGET_MAX_RH = 80.0  # % - ceiling we're protecting against

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
OUTSIDE_MIN_TEMP_MARGIN = 2.0    # °C

POLL_INTERVAL_SECONDS = 300      # how often we read sensors & make decisions
LOG_INTERVAL_SECONDS = 900       # how often we write a row to the CSV log

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


def calculate_abs_humidity(temp_c, rh_pct):
    """Absolute humidity in g/m³, via the Magnus approximation for
    saturation vapour pressure. Represents the actual mass of water
    vapour per cubic metre of air - unlike RH, this doesn't change
    just because temperature changes, only when moisture content
    actually changes."""
    saturation_vp = 6.112 * math.exp((17.62 * temp_c) / (temp_c + 243.12))
    return 216.7 * (rh_pct / 100.0 * saturation_vp) / (273.15 + temp_c)


def max_allowable_abs_humidity(inside_temp, target_max_rh=DEHUMIDIFY_TARGET_MAX_RH):
    """Max outside absolute humidity (g/m³) that, once that air reaches
    inside_temp, would still keep inside RH at or below target_max_rh.
    This is what makes our humidity ceiling dynamic/temperature-aware
    rather than a fixed RH% - it tightens as the cellar cools and
    relaxes as the cellar warms, tracking the actual condensation/
    over-humidify risk rather than an arbitrary flat number."""
    saturation_vp = 6.112 * math.exp((17.62 * inside_temp) / (inside_temp + 243.12))
    return 216.7 * (target_max_rh / 100.0 * saturation_vp) / (273.15 + inside_temp)


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
                      warmth if outside is at our hard cold floor; never
                      run the extractor at all if outside is hot+humid.
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

    # ── 1b. Safety check: is outside air hotter than our ideal max?
    # If so, block bringing it in entirely (either fan) - in COOLING
    # mode it pulls that air straight in via the intake fan, and even
    # in DEHUMIDIFY-only mode the negative pressure it creates draws
    # the same hot air in through gaps/infiltration. Either way we'd
    # be working directly against the temperature goal, regardless of
    # how humid that air is.
    outside_too_hot = outside_temp > TEMP_TARGET_MAX

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
        and not outside_too_hot
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
        and not outside_too_hot
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
    dehumidify_blocked_by_hot_inside = inside_temp > TEMP_TARGET_MAX

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
        and not outside_too_hot
        and not dehumidify_blocked_by_hot_inside
    ):
        return FANS_DEHUMIDIFY

    # ── 5. Warm-assist check ──────────────────────────────
    # Last resort for a too-cold cellar when warm-venting isn't
    # available (outside isn't the warmer side, or not by enough).
    # Runs the extractor alone, gambling that its makeup air is drawn
    # more from the adjoining (heated) house than from outside -
    # skipped if that gamble is blocked by cold outside air, or if
    # outside is too hot (same safety flag as everywhere else).
    if current_state == FANS_WARM_ASSIST:
        cold_trigger = inside_temp < WARMING_TEMP_CEILING
    else:
        cold_trigger = inside_temp < (WARMING_TEMP_CEILING - TEMP_HYSTERESIS)

    if (
        cold_trigger
        and not warm_assist_blocked_by_cold_outside
        and not outside_too_hot
    ):
        return FANS_WARM_ASSIST

    # ── 6. Otherwise, all good - fans off ────────────────
    return FANS_OFF


def set_fans(state):
    """Drive the relay GPIOs to match the requested fan state.
    Relay is active LOW: LOW = fan on, HIGH = fan off."""
    if state in (FANS_COOLING, FANS_WARM_VENT):
        GPIO.output(17, GPIO.LOW)   # extractor on
        GPIO.output(27, GPIO.LOW)   # intake on
    elif state in (FANS_DEHUMIDIFY, FANS_WARM_ASSIST):
        GPIO.output(17, GPIO.LOW)   # extractor on
        GPIO.output(27, GPIO.HIGH)  # intake off
    else:  # FANS_OFF
        GPIO.output(17, GPIO.HIGH)  # extractor off
        GPIO.output(27, GPIO.HIGH)  # intake off


def fan_flags(state):
    """Return (extractor_running, intake_running) booleans for a given
    fan state, matching the GPIO logic in set_fans()."""
    if state in (FANS_COOLING, FANS_WARM_VENT):
        return True, True
    elif state in (FANS_DEHUMIDIFY, FANS_WARM_ASSIST):
        return True, False
    else:  # FANS_OFF
        return False, False


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


def main():
    current_state = FANS_OFF
    # Tracks when we entered the current state, so we can enforce
    # MIN_RUN_SECONDS before allowing a switch away from it.
    state_started_at = time.monotonic()

    last_log_at = time.monotonic()
    # Accumulators: set to True if the fan ran at ANY point since the
    # last log write, reset after each log write. Counting polls (not
    # just a True/False "ran at all") tells us HOW MUCH each fan ran
    # within the interval, e.g. 3/3 vs 1/3 polls - much more useful
    # than a boolean when reviewing logs later.
    extractor_polls_on = 0
    intake_polls_on = 0
    polls_this_interval = 0

    print("Wine cellar cooling control started.")

    try:
        while True:
            readings = read_sensors()
            if readings is not None:
                desired_state = decide_fan_state(readings, current_state)

                # ── Minimum run-time guard ───────────────────────
                # If we're currently running a fan state (not OFF) and
                # the decision logic wants to change it, check we've
                # been running long enough first. Switching *into* a
                # running state from OFF is never blocked - only
                # switching *away* from an active state is protected.
                time_in_state = time.monotonic() - state_started_at
                if (
                    current_state != FANS_OFF
                    and desired_state != current_state
                    and time_in_state < MIN_RUN_SECONDS
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
                        f"State change: {current_state} -> {new_state} | "
                        f"inside={readings['inside_temp']:.1f}C/{readings['inside_humidity']:.1f}% "
                        f"outside={readings['outside_temp']:.1f}C/{readings['outside_humidity']:.1f}%"
                    )
                    set_fans(new_state)
                    current_state = new_state
                    state_started_at = time.monotonic()

                # Count this poll toward the running tally - how many
                # polls occurred, and how many of those had each fan on.
                extractor_running, intake_running = fan_flags(current_state)
                polls_this_interval += 1
                if extractor_running:
                    extractor_polls_on += 1
                if intake_running:
                    intake_polls_on += 1

                # Only write to the CSV log every LOG_INTERVAL_SECONDS,
                # logging the poll counts accumulated over that window
                # rather than just the current instantaneous state.
                now = time.monotonic()
                if now - last_log_at >= LOG_INTERVAL_SECONDS:
                    log_reading(
                        readings,
                        current_state,
                        extractor_polls_on,
                        intake_polls_on,
                        polls_this_interval,
                    )
                    last_log_at = now
                    extractor_polls_on = 0
                    intake_polls_on = 0
                    polls_this_interval = 0

            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("Stopping - cleaning up GPIO.")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
