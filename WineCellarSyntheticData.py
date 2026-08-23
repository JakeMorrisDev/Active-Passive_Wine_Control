#!/home/jakem/WineCellarManager/bin/python
"""
Generates a synthetic wine_cellar_log.csv with a few days of fake
data, for building/testing plotting logic before real hardware data
is available. Mimics daily temp/humidity swings and occasional fan
activity, in the same format as the real log.
"""

import csv
import math
import random
from datetime import datetime, timedelta

OUTPUT_FILE = "/home/jakem/WineCellarManagerCode/wine_cellar_log_test.csv"
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

# Set to False to simulate the bottle probe not being attached yet -
# matches how RunWineCooling.py leaves the column blank when the
# DS18B20 probe isn't connected.
SIMULATE_BOTTLE_PROBE = True

DAYS = 600
INTERVAL_MINUTES = 15
POLL_INTERVAL_MINUTES = 5  # must match POLL_INTERVAL_SECONDS/60 in RunWineCooling.py
POLLS_PER_INTERVAL = INTERVAL_MINUTES // POLL_INTERVAL_MINUTES
START = datetime.now() - timedelta(days=DAYS)

# Roughly the middle of the real target bands - inside temp/humidity
# drift gently towards this (plus a small randomised daily offset)
# whenever the fans aren't actively exchanging air with outside.
BASELINE_INSIDE_TEMP = 14.0       # °C
BASELINE_INSIDE_HUMIDITY = 72.5   # %

# Max size of the randomised daily wobble applied to the baseline -
# keeps the "resting" inside conditions realistically stable
# (a degree or two of natural day-to-day drift) rather than swinging
# around with the outside temperature every step.
MAX_DAILY_TEMP_OFFSET = 1.5       # °C either way
MAX_DAILY_HUMIDITY_OFFSET = 3.0   # % either way

# Night hours (inclusive start, exclusive end) - fans are more likely
# to run overnight since that's usually when outside conditions are
# most favourable for passive cooling and dehumidifying.
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6


def is_summer_month(month):
    return month in (6, 7, 8, 9)  # Jun-Sep


def is_winter_month(month):
    return month in (11, 12, 1, 2)  # Nov-Feb


def calculate_dew_point(temp_c, rh_pct):
    a, b = 17.62, 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh_pct / 100.0)
    return (b * gamma) / (a - gamma)


def fake_outside_temp(hours_elapsed):
    """Simple day/night sine wave: cool at night, warm in afternoon."""
    day_fraction = (hours_elapsed % 24) / 24.0
    return 15 + 8 * math.sin(2 * math.pi * (day_fraction - 0.3)) + random.uniform(-0.5, 0.5)


def fake_outside_humidity(hours_elapsed):
    day_fraction = (hours_elapsed % 24) / 24.0
    # Higher RH at night, lower in afternoon (inverse of temp roughly)
    return 70 - 20 * math.sin(2 * math.pi * (day_fraction - 0.3)) + random.uniform(-3, 3)


def main():
    rows = []
    inside_temp = 20.0
    inside_humidity = 75.0
    bottle_temp = inside_temp  # bottle starts at the same temp as air
    fan_state = "off"

    # Randomised daily "resting point" wobble - recomputed once per
    # calendar day so inside conditions stay stable within a day but
    # still drift a little from one day to the next.
    day_temp_offset = random.uniform(-MAX_DAILY_TEMP_OFFSET, MAX_DAILY_TEMP_OFFSET)
    day_humidity_offset = random.uniform(-MAX_DAILY_HUMIDITY_OFFSET, MAX_DAILY_HUMIDITY_OFFSET)
    current_day_index = None

    total_steps = int(DAYS * 24 * 60 / INTERVAL_MINUTES)

    for step in range(total_steps):
        timestamp = START + timedelta(minutes=step * INTERVAL_MINUTES)
        hours_elapsed = step * INTERVAL_MINUTES / 60.0

        # Re-roll the daily offset whenever we cross into a new
        # calendar day.
        day_index = timestamp.date().toordinal()
        if day_index != current_day_index:
            current_day_index = day_index
            day_temp_offset = random.uniform(-MAX_DAILY_TEMP_OFFSET, MAX_DAILY_TEMP_OFFSET)
            day_humidity_offset = random.uniform(-MAX_DAILY_HUMIDITY_OFFSET, MAX_DAILY_HUMIDITY_OFFSET)

        outside_temp = fake_outside_temp(hours_elapsed)
        outside_humidity = max(30, min(99, fake_outside_humidity(hours_elapsed)))

        month = timestamp.month
        hour = timestamp.hour
        is_night = hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
        summer = is_summer_month(month)
        winter = is_winter_month(month)

        # Simple fake decision logic just for test data variety -
        # combines a seasonal/night-time pattern (fans favoured
        # overnight, both fans in summer, extractor-only in winter)
        # with the original temp/humidity threshold logic, so either
        # can trigger a run.
        night_cooling = False
        night_dehumidify = False
        if is_night:
            if summer:
                # Warm nights - good chance of full cooling (both
                # fans), amount/likelihood randomised night to night.
                night_cooling = random.random() < random.uniform(0.5, 0.9)
                if not night_cooling:
                    night_dehumidify = random.random() < random.uniform(0.2, 0.5)
            elif winter:
                # Cold nights - just the extractor, dehumidifying
                # only, randomised likelihood.
                night_dehumidify = random.random() < random.uniform(0.4, 0.8)
            else:
                # Shoulder seasons - occasional modest activity.
                night_dehumidify = random.random() < random.uniform(0.1, 0.3)
                night_cooling = random.random() < random.uniform(0.05, 0.2)

        threshold_cooling = inside_temp > 17.5 and outside_temp < inside_temp - 2
        threshold_dehumidify = inside_humidity > 80 and outside_humidity < inside_humidity - 7.5

        if night_cooling or threshold_cooling:
            fan_state = "cooling"
        elif night_dehumidify or threshold_dehumidify:
            fan_state = "dehumidify"
        else:
            fan_state = "off"

        extractor_ran = fan_state in ("cooling", "dehumidify")
        intake_ran = fan_state == "cooling"

        # Very rough simulation of the cellar drifting slowly towards
        # its randomised daily resting point when the fans are idle,
        # but pulled more strongly towards outside conditions whenever
        # a fan is actively exchanging air.
        target_inside_temp = BASELINE_INSIDE_TEMP + day_temp_offset
        target_inside_humidity = BASELINE_INSIDE_HUMIDITY + day_humidity_offset

        if extractor_ran or intake_ran:
            inside_temp += (outside_temp - inside_temp) * 0.05
            inside_humidity += (outside_humidity - inside_humidity) * 0.03
        else:
            inside_temp += (target_inside_temp - inside_temp) * 0.01
            inside_humidity += (target_inside_humidity - inside_humidity) * 0.01

        # Bottle temp lags well behind air temp - glass + liquid has
        # much higher thermal mass, so it responds far more slowly
        # than the surrounding cellar air.
        bottle_temp += (inside_temp - bottle_temp) * 0.005

        # Fabricate a plausible poll count rather than a flat
        # all-or-nothing value - if the fan is meant to be running,
        # randomly vary how many of the interval's polls it was
        # actually on for (simulating it switching on partway through,
        # or being held off briefly by MIN_RUN_SECONDS in reality).
        def fake_polls_on(is_running):
            if not is_running:
                return random.randint(0, 1)  # occasional brief blip
            # Randomise "how much" it ran this interval, rather than
            # always being nearly fully on.
            return random.randint(1, POLLS_PER_INTERVAL)

        extractor_polls_on = fake_polls_on(extractor_ran)
        intake_polls_on = fake_polls_on(intake_ran)

        inside_dewpoint = calculate_dew_point(inside_temp, inside_humidity)
        outside_dewpoint = calculate_dew_point(outside_temp, outside_humidity)

        bottle_temp_str = f"{bottle_temp:.1f}" if SIMULATE_BOTTLE_PROBE else ""

        rows.append([
            timestamp.isoformat(timespec="seconds"),
            f"{inside_temp:.1f}",
            f"{inside_humidity:.1f}",
            f"{inside_dewpoint:.1f}",
            f"{outside_temp:.1f}",
            f"{outside_humidity:.1f}",
            f"{outside_dewpoint:.1f}",
            bottle_temp_str,
            fan_state,
            extractor_polls_on,
            intake_polls_on,
            POLLS_PER_INTERVAL,
        ])

    with open(OUTPUT_FILE, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(LOG_HEADER)
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()