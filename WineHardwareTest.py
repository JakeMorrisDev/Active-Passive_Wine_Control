#!/home/jakem/WineCellarManager/bin/python

import board
import busio
import adafruit_tca9548a
import adafruit_sht31d
import RPi.GPIO as GPIO
import time

# ── GPIO setup ──────────────────────────────────────────
GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.OUT)  # IN1 - fan 1 (K1)
GPIO.setup(27, GPIO.OUT)  # IN2 - fan 2 (K2)

# Songle relay is active LOW, so HIGH = off to start
GPIO.output(17, GPIO.HIGH)
GPIO.output(27, GPIO.HIGH)

# ── I2C and sensor setup ─────────────────────────────────
i2c = busio.I2C(board.SCL, board.SDA)
tca = adafruit_tca9548a.TCA9548A(i2c)
sensor_inside  = adafruit_sht31d.SHT31D(tca[0])  # channel 0
sensor_outside = adafruit_sht31d.SHT31D(tca[1])  # channel 1

# ── Optional bottle probe (DS18B20 via 1-Wire) ───────────
# Same detection approach as RunWineCooling.py - never raises if the
# probe isn't wired up or w1thermsensor isn't installed, so this test
# script still runs fine without it (bottle readings just show as
# "not detected").
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


def read_bottle_probe():
    """Read the bottle probe if connected. Returns None on any
    failure so the test loop keeps running even if it's unplugged or
    flaky mid-test."""
    if bottle_sensor is None:
        return None
    try:
        return bottle_sensor.get_temperature()
    except Exception as e:
        print(f"Bottle probe read failed: {e}")
        return None


# ── Test 1: Read both cellar sensors + bottle probe, repeatedly ──
TEST_DURATION_SECONDS = 1 * 30   # in minutes
READ_INTERVAL_SECONDS = 15

print("=" * 40)
print("SENSOR TEST")
print(f"Running for {TEST_DURATION_SECONDS // 60} minutes, "
      f"reading every {READ_INTERVAL_SECONDS}s")
print("=" * 40)

start_time = time.monotonic()
reading_num = 0

while time.monotonic() - start_time < TEST_DURATION_SECONDS:
    reading_num += 1
    elapsed = time.monotonic() - start_time

    inside_temp = sensor_inside.temperature
    inside_humidity = sensor_inside.relative_humidity
    outside_temp = sensor_outside.temperature
    outside_humidity = sensor_outside.relative_humidity
    bottle_temp = read_bottle_probe()

    bottle_str = f"{bottle_temp:.1f}C" if bottle_temp is not None else "N/A"

    print(f"[{elapsed:5.0f}s] #{reading_num:>2} "
          f"Inside: {inside_temp:.1f}C {inside_humidity:.1f}%  |  "
          f"Outside: {outside_temp:.1f}C {outside_humidity:.1f}%  |  "
          f"Bottle: {bottle_str}")

    time.sleep(READ_INTERVAL_SECONDS)

print("=" * 40)
print("SENSOR TEST COMPLETE")
print("=" * 40)


# ── Test 2: Fan 1 (K1/IN1) ───────────────────────────────
print()
print("=" * 40)
print("FAN TEST — K1 (IN1, GPIO17)")
print("=" * 40)
print("Turning fan 1 ON (relay should click)...")
GPIO.output(17, GPIO.LOW)   # active LOW = ON
time.sleep(20)

print("Turning fan 1 OFF...")
GPIO.output(17, GPIO.HIGH)
time.sleep(2)

print("Fan 1 test complete")

# ── Test 3: Fan 2 (K2/IN2) ───────────────────────────────
print()
print("=" * 40)
print("FAN TEST — K2 (IN2, GPIO27)")
print("=" * 40)
print("Turning fan 2 ON (relay should click)...")
GPIO.output(27, GPIO.LOW)
time.sleep(20)

print("Turning fan 2 OFF...")
GPIO.output(27, GPIO.HIGH)
time.sleep(2)

print("Fan 2 test complete")

# ── Test 4: Both fans together ───────────────────────────
print()
print("=" * 40)
print("BOTH FANS ON TOGETHER")
print("=" * 40)
print("Turning both fans ON...")
GPIO.output(17, GPIO.LOW)
GPIO.output(27, GPIO.LOW)
time.sleep(10)

print("Turning both fans OFF...")
GPIO.output(17, GPIO.HIGH)
GPIO.output(27, GPIO.HIGH)
time.sleep(2)

# ── Cleanup ──────────────────────────────────────────────
GPIO.cleanup()
print()
print("=" * 40)
print("ALL TESTS COMPLETE")
print("=" * 40)