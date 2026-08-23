#!/home/jakem/WineCellarManager/bin/python
"""
Wine Cellar Dashboard (read-only)

Reads wine_cellar_log.csv (written by RunWineCooling.py) and serves a
web dashboard viewable from any device on the local network - phone,
laptop, etc - without needing SSH.

This script ONLY reads the log file. It never touches GPIO and never
controls the fans - that's entirely handled by RunWineCooling.py,
which should keep running as its own separate process.
"""

import os
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from flask import Flask, request, render_template_string

# ── Config ────────────────────────────────────────────────
LOG_FILE = "/home/jakem/WineCellarManagerCode/wine_cellar_log.csv"
# Swap to the test file below while developing against fake data:lets
# LOG_FILE = "/home/jakem/wine_cellar_log_test.csv"

PORT = 5000
DEFAULT_HOURS_TO_SHOW = 24
AUTO_REFRESH_SECONDS = 300

# How long each poll represents, in minutes - must match
# POLL_INTERVAL_SECONDS in RunWineCooling.py, since extractor_polls_on
# / intake_polls_on are counts of polls, not minutes directly.
POLL_INTERVAL_MINUTES = 5

# When rounding a fan's poll-count to a simple on/off for plotting, a
# fan is considered "on" for the interval if it ran for at least this
# many minutes out of the log interval (e.g. 10+ of 15 minutes = on).
FAN_ON_THRESHOLD_MINUTES = 10

# ── Target ranges (mirror RunWineCooling.py so colours reflect the
# same thresholds the control logic actually uses) ──────────────
TEMP_TARGET_MIN = 11.5      # °C
TEMP_TARGET_MAX = 17.5      # °C
HUMIDITY_TARGET_MIN = 65.0  # %
HUMIDITY_TARGET_MAX = 80.0  # %

app = Flask(__name__)


# ── Data loading ──────────────────────────────────────────
def load_log():
    """Load the CSV log into a DataFrame. Returns None if the file
    doesn't exist yet or has no rows - lets the page show a friendly
    'no data yet' message instead of crashing."""
    if not os.path.isfile(LOG_FILE):
        return None
    try:
        df = pd.read_csv(LOG_FILE, parse_dates=["timestamp"])
        if df.empty:
            return None
        return df
    except Exception as e:
        print(f"Failed to read log: {e}")
        return None


def filter_recent(df, hours):
    cutoff = datetime.now() - timedelta(hours=hours)
    return df[df["timestamp"] >= cutoff]


def filter_by_period(df, period):
    """Filter the log to a named period: '7d', 'month', 'year', or
    'all'. 'month'/'year' are calendar-based (from the 1st of the
    current month/year to now), not just a rolling N days."""
    now = datetime.now()
    if period == "7d":
        cutoff = now - timedelta(days=7)
    elif period == "month":
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        cutoff = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # "all"
        return df
    return df[df["timestamp"] >= cutoff]


def downsample_for_period(df, period):
    """Reduce the number of rows we actually plot for longer periods,
    so the graphs page stays fast even after months/years of 15-min
    logging has piled up. '7d' is left as raw 15-min data (only ~670
    rows anyway).

    For longer periods we resample, but crucially the bucket size is
    capped well below 24 hours (see MAX_BUCKET_SECONDS below) - both
    inside and outside temp/humidity swing on a daily day/night cycle,
    so resampling to whole-day buckets (mean of a full sine cycle)
    was flattening that swing out to a near-constant line. Using a
    sub-day bucket size means each point still averages over only
    part of a day/night cycle, so the daily wobble stays visible even
    when zoomed out to a year or more.
    """
    if period == "7d" or len(df) < 500:
        return df

    target_points = 900
    # Never bucket by more than this many seconds (6 hours) - keeps
    # the daily temperature/humidity swing visible no matter how long
    # the selected period is.
    MAX_BUCKET_SECONDS = 6 * 60 * 60

    span_seconds = (df["timestamp"].max() - df["timestamp"].min()).total_seconds()
    if span_seconds <= 0:
        return df

    bucket_seconds = max(60, min(span_seconds / target_points, MAX_BUCKET_SECONDS))
    freq = f"{int(bucket_seconds)}s"

    mean_cols = [c for c in (
        "inside_temp_c", "outside_temp_c",
        "inside_humidity_pct", "outside_humidity_pct",
        "inside_dewpoint_c", "outside_dewpoint_c",
        "bottle_temp_c",
    ) if c in df.columns]
    sum_cols = [c for c in (
        "extractor_polls_on", "intake_polls_on", "polls_this_interval",
    ) if c in df.columns]

    agg = {c: "mean" for c in mean_cols}
    agg.update({c: "sum" for c in sum_cols})

    resampled = (
        df.set_index("timestamp")
        .resample(freq)
        .agg(agg)
        .dropna(how="all")
        .reset_index()
    )
    return resampled


PERIOD_LABELS = {
    "7d": "Last 7 Days",
    "month": "This Month",
    "year": "This Year",
    "all": "All Time",
}


def compute_period_stats(df):
    """Max/min for each metric over the given period-filtered
    dataframe, for the summary boxes shown under the graphs. Computed
    on the raw (pre-downsample) rows so a brief spike/dip isn't
    averaged away by downsample_for_period()'s resampling."""
    stats = {}
    for key, col in (
        ("inside_temp", "inside_temp_c"),
        ("outside_temp", "outside_temp_c"),
        ("bottle_temp", "bottle_temp_c"),
        ("inside_humidity", "inside_humidity_pct"),
        ("outside_humidity", "outside_humidity_pct"),
    ):
        if col in df.columns and df[col].notna().any():
            stats[key] = {
                "max": f"{df[col].max():.1f}",
                "min": f"{df[col].min():.1f}",
            }
        else:
            stats[key] = None
    return stats


# ── Status colouring ──────────────────────────────────────
# Simple traffic-light colouring against our target ranges. "ok" is
# comfortably within target, "warn" is outside target but only
# slightly (within one hysteresis-ish step), "bad" is well outside.
STATUS_COLOURS = {
    "ok": "#4caf50",     # green
    "warn": "#e6a700",   # amber
    "bad": "#d9483d",    # red
}


def temp_status(temp_c):
    """Classify a temperature reading against TEMP_TARGET_MIN/MAX."""
    if temp_c is None:
        return "bad"
    if TEMP_TARGET_MIN <= temp_c <= TEMP_TARGET_MAX:
        return "ok"
    # Within 2°C of the target band counts as a mild warning rather
    # than a hard problem - purely a display nicety, doesn't affect
    # any control logic.
    if (TEMP_TARGET_MIN - 2.0) <= temp_c <= (TEMP_TARGET_MAX + 2.0):
        return "warn"
    return "bad"


def humidity_status(rh_pct):
    """Classify a humidity reading against HUMIDITY_TARGET_MIN/MAX."""
    if rh_pct is None:
        return "bad"
    if HUMIDITY_TARGET_MIN <= rh_pct <= HUMIDITY_TARGET_MAX:
        return "ok"
    if (HUMIDITY_TARGET_MIN - 5.0) <= rh_pct <= (HUMIDITY_TARGET_MAX + 5.0):
        return "warn"
    return "bad"


def bottle_temp_status(temp_c):
    """Classify the bottle probe reading. Bottle temp should track
    close to the cellar's own target band, so we reuse the same temp
    thresholds - bottles just respond more slowly than ambient air."""
    return temp_status(temp_c)


# Colours for the small fan on/off badges - green when running, grey
# when off. Off isn't a "problem" so no amber/red here, unlike the
# temp/humidity traffic-light colouring above.
FAN_STATUS_COLOURS = {
    True: "#4caf50",   # on - green
    False: "#555",     # off - grey
}


# ── Deriving on/off from poll counts ──────────────────────
def add_fan_on_off_columns(df):
    """RunWineCooling.py logs extractor_polls_on / intake_polls_on as
    COUNTS of 5-minute polls the fan was running within each ~15-
    minute log interval, not a plain True/False. For plotting we just
    want a simple on/off per interval, so we round: a fan counts as
    "on" for that interval if it ran for at least FAN_ON_THRESHOLD_
    MINUTES out of the interval, otherwise "off"."""
    df = df.copy()
    for prefix in ("extractor", "intake"):
        polls_col = f"{prefix}_polls_on"
        if polls_col in df.columns:
            minutes_on = df[polls_col] * POLL_INTERVAL_MINUTES
            df[f"{prefix}_on"] = minutes_on >= FAN_ON_THRESHOLD_MINUTES
        else:
            df[f"{prefix}_on"] = False
    return df


def add_fan_fraction_columns(df):
    """Derive, per row, what fraction (0-1) of that row's interval
    each fan was running for - polls_on / polls_this_interval. Unlike
    add_fan_on_off_columns' fixed-minutes threshold, this stays
    meaningful after downsample_for_period() resamples rows to
    hourly/daily (where polls_this_interval can be much bigger than a
    single 15-minute window's worth), so the fan bar chart doesn't
    flatten out to "always on" over longer time ranges."""
    df = df.copy()
    if "polls_this_interval" in df.columns:
        total = df["polls_this_interval"].replace(0, pd.NA)
    else:
        total = None

    for prefix in ("extractor", "intake"):
        polls_col = f"{prefix}_polls_on"
        if polls_col in df.columns and total is not None:
            df[f"{prefix}_frac"] = (df[polls_col] / total).fillna(0.0).clip(0, 1)
        else:
            df[f"{prefix}_frac"] = 0.0
    return df


# ── Plot builders ─────────────────────────────────────────
def build_combined_figure(df):
    """Build a single figure with three stacked, x-axis-linked
    subplots (temperature / humidity & dew point / fan activity).
    Using one figure with shared_xaxes=True is what lets zooming or
    panning on any one subplot stay in sync across all three -
    separate figures embedded independently can't do that."""
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.42, 0.36, 0.22],
        vertical_spacing=0.05,
        subplot_titles=("Temperature", "Humidity & Dew Point", "Fan Activity"),
    )

    # ── Row 1: temperature ──
    fig.add_trace(go.Scatter(x=df["timestamp"], y=df["inside_temp_c"],
                              name="Inside Temp", line=dict(color="#c0392b")),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=df["timestamp"], y=df["outside_temp_c"],
                              name="Outside Temp", line=dict(color="#2980b9")),
                  row=1, col=1)
    if "bottle_temp_c" in df.columns and df["bottle_temp_c"].notna().any():
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["bottle_temp_c"],
                                  name="Bottle Temp", line=dict(color="#8e44ad")),
                      row=1, col=1)

    # ── Row 2: humidity & dew point ──
    fig.add_trace(go.Scatter(x=df["timestamp"], y=df["inside_humidity_pct"],
                              name="Inside RH", line=dict(color="#c0392b")),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=df["timestamp"], y=df["outside_humidity_pct"],
                              name="Outside RH", line=dict(color="#2980b9")),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=df["timestamp"], y=df["inside_dewpoint_c"],
                              name="Inside Dew Pt", line=dict(color="#e67e22")),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=df["timestamp"], y=df["outside_dewpoint_c"],
                              name="Outside Dew Pt", line=dict(color="#27ae60")),
                  row=2, col=1)

    # ── Row 3: fan activity, as a % of each interval running ──
    fan_df = add_fan_fraction_columns(df)
    fig.add_trace(go.Bar(x=fan_df["timestamp"], y=fan_df["extractor_frac"] * 100,
                          name="Extractor", marker=dict(color="#8e44ad")),
                  row=3, col=1)
    fig.add_trace(go.Bar(x=fan_df["timestamp"], y=fan_df["intake_frac"] * 100,
                          name="Intake", marker=dict(color="#16a085")),
                  row=3, col=1)

    fig.update_yaxes(title_text="°C", range=[-5, 30], row=1, col=1)
    fig.update_yaxes(title_text="% RH / °C", row=2, col=1)
    fig.update_yaxes(title_text="% running", range=[0, 100], row=3, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#121212",
        plot_bgcolor="#121212",
        height=780,
        barmode="group",
        bargap=0.15,
        margin=dict(l=40, r=20, t=40, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.03, xanchor="right", x=1),
    )
    return fig


# ── Page template (placeholder - filled in next) ─────────
PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Wine Cellar Manager - Active/Passive </title>
    <meta http-equiv="refresh" content="{{ refresh_seconds }}">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, sans-serif;
            background: #121212;
            color: #eee;
            margin: 0;
            padding: 20px;
            text-align: center;
        }
        h1 { font-size: 1.3em; margin-bottom: 0; }
        .subtitle { color: #888; font-size: 0.85em; margin-top: 4px; }

        .bottle-stats, .inside-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 200px));
            justify-content: center;
            gap: 24px;
            margin: 30px 0;
        }
        .fan-badges {
            display: flex;
            flex-direction: column;
            gap: 10px;
            justify-content: center;
        }
        .fan-badge {
            border-radius: 12px;
            padding: 10px 20px;
            flex: 1;
            background: #1e1e1e;
            border: 2px solid var(--status-colour);
            text-align: center;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .fan-badge .label {
            color: #aaa;
            font-size: 0.8em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .fan-badge .value {
            font-size: 1.4em;
            font-weight: bold;
            color: var(--status-colour);
        }
        .stat-card.compact {
            padding: 12px 24px;
            display: flex;
            flex-direction: column;
            justify-content: center;   /* ← overrides the auto-fit rule above */
        }

        .stat-card {
            border-radius: 16px;
            padding: 24px 24px;
            background: #1e1e1e;
            border: 3px solid var(--status-colour);
            box-sizing: border-box;
        }
        .stat-card .label {
            color: #aaa;
            font-size: 1em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .stat-card .value {
            font-size: 3em;
            font-weight: bold;
            color: var(--status-colour);
            line-height: 1.1;
        }
        .stat-card .target {
            color: #777;
            font-size: 0.8em;
            margin-top: 4px;
        }

        .outside-stats {
            display: flex;
            justify-content: center;
            gap: 16px;
            margin: 10px 0 30px;
            flex-wrap: wrap;
        }
        .stat-card.secondary {
            padding: 12px 20px;
            min-width: 120px;
            border-width: 2px;
        }
        .stat-card.secondary .label {
            font-size: 0.8em;
        }
        .stat-card.secondary .value {
            font-size: 1.6em;
        }

        .range-links {
            margin: 10px 0 20px;
            color: #999;
            font-size: 0.9em;
        }
        .range-links a {
            color: #6cf;
            text-decoration: none;
        }
        .nav-buttons {
            margin: 30px 0 10px;
        }
        .nav-buttons a {
            display: inline-block;
            background: #1e1e1e;
            border: 2px solid #444;
            border-radius: 10px;
            padding: 12px 28px;
            color: #eee;
            text-decoration: none;
            font-size: 1em;
        }
        .nav-buttons a:hover {
            border-color: #6cf;
        }
        .charts {
            max-width: 900px;
            margin: 0 auto;
            text-align: left;
        }
        .charts > div {
            margin-bottom: 10px;
        }

        .no-data { color: #f66; margin-top: 40px; }
    </style>
</head>
<body>
    <h1>🍷 Wine Cellar Dashboard</h1>
    <p class="subtitle">Last updated: {{ last_updated }}</p>

    {% if has_data %}
    {% if has_bottle_temp or has_fan_status %}
    <div class="bottle-stats">
        {% if has_bottle_temp %}
        <div class="stat-card compact" style="--status-colour: {{ bottle_colour }};">
            <div class="label">Bottle Temp</div>
            <div class="value">{{ bottle_temp }}°C</div>
            <div class="target">Target: {{ temp_target_min }}–{{ temp_target_max }}°C</div>
        </div>
        {% endif %}
        {% if has_fan_status %}
        <div class="fan-badges">
            <div class="fan-badge" style="--status-colour: {{ intake_colour }};">
                <div class="label">Intake</div>
                <div class="value">{{ intake_text }}</div>
            </div>
            <div class="fan-badge" style="--status-colour: {{ extractor_colour }};">
                <div class="label">Extractor</div>
                <div class="value">{{ extractor_text }}</div>
            </div>
        </div>
        {% endif %}
    </div>
    {% endif %}
    <div class="inside-stats">
        <div class="stat-card" style="--status-colour: {{ temp_colour }};">
            <div class="label">Inside Temp</div>
            <div class="value">{{ inside_temp }}°C</div>
            <div class="target">Target: {{ temp_target_min }}–{{ temp_target_max }}°C</div>
        </div>
        <div class="stat-card" style="--status-colour: {{ humidity_colour }};">
            <div class="label">Inside Humidity</div>
            <div class="value">{{ inside_humidity }}%</div>
            <div class="target">Target: {{ humidity_target_min }}–{{ humidity_target_max }}%</div>
        </div>
    </div>

    <div class="outside-stats">
        <div class="stat-card secondary" style="--status-colour: #555;">
            <div class="label">Outside Temp</div>
            <div class="value">{{ outside_temp }}°C</div>
        </div>
        <div class="stat-card secondary" style="--status-colour: #555;">
            <div class="label">Outside Humidity</div>
            <div class="value">{{ outside_humidity }}%</div>
        </div>
    </div>

    <div class="nav-buttons">
        <a href="/graphs">📈 View Graphs</a>
    </div>
    {% else %}
        <p class="no-data">No log data found yet at {{ log_file }}.</p>
    {% endif %}
</body>
</html>
"""


# ── Graphs page template ──────────────────────────────────
GRAPHS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Wine Cellar Graphs</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, sans-serif;
            background: #121212;
            color: #eee;
            margin: 0;
            padding: 20px;
            text-align: center;
        }
        h1 { font-size: 1.3em; margin-bottom: 0; }
        .subtitle { color: #888; font-size: 0.85em; margin-top: 4px; }

        .nav-buttons {
            margin: 20px 0;
        }
        .nav-buttons a, .period-links a {
            display: inline-block;
            background: #1e1e1e;
            border: 2px solid #444;
            border-radius: 10px;
            padding: 10px 20px;
            margin: 4px;
            color: #eee;
            text-decoration: none;
            font-size: 0.95em;
        }
        .nav-buttons a:hover, .period-links a:hover {
            border-color: #6cf;
        }
        .period-links a.active {
            border-color: #6cf;
            color: #6cf;
        }
        .period-links {
            margin: 10px 0 30px;
        }

        .charts {
            max-width: 900px;
            margin: 0 auto;
            text-align: left;
        }
        .charts > div {
            margin-bottom: 10px;
        }

        .summary-stats {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 16px;
            max-width: 900px;
            margin: 20px auto 10px;
        }
        .summary-row {
            display: flex;
            justify-content: center;
            flex-wrap: wrap;
            gap: 16px;
            width: 100%;
        }
        .summary-card {
            background: #1e1e1e;
            border: 2px solid #444;
            border-radius: 12px;
            padding: 14px 18px;
            min-width: 150px;
        }
        .summary-card .label {
            color: #aaa;
            font-size: 0.8em;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 6px;
        }
        .summary-card .minmax {
            font-size: 1.1em;
            font-weight: bold;
        }
        .summary-card .minmax .max { color: #e6a700; }
        .summary-card .minmax .min { color: #2980b9; }
        .summary-card .minmax .sep { color: #666; font-weight: normal; }

        .no-data { color: #f66; margin-top: 40px; }
    </style>
</head>
<body>
    <h1>📈 Wine Cellar Graphs</h1>
    <p class="subtitle">Last updated: {{ last_updated }}</p>

    <div class="period-links">
        <a href="/graphs?period=7d" class="{{ 'active' if period == '7d' else '' }}">Last 7 Days</a>
        <a href="/graphs?period=month" class="{{ 'active' if period == 'month' else '' }}">This Month</a>
        <a href="/graphs?period=year" class="{{ 'active' if period == 'year' else '' }}">This Year</a>
        <a href="/graphs?period=all" class="{{ 'active' if period == 'all' else '' }}">All Time</a>
    </div>

    {% if has_data %}
    <div class="charts">
        {{ combined_chart | safe }}
    </div>

    <p class="subtitle">Max / Min — {{ period_label }}</p>
    <div class="summary-stats">
        <div class="summary-row">
            {% if stats['bottle_temp'] %}
            <div class="summary-card">
                <div class="label">Bottle Temp</div>
                <div class="minmax"><span class="max">{{ stats['bottle_temp']['max'] }}°C</span><span class="sep"> / </span><span class="min">{{ stats['bottle_temp']['min'] }}°C</span></div>
            </div>
            {% endif %}
            {% if stats['inside_temp'] %}
            <div class="summary-card">
                <div class="label">Inside Temp</div>
                <div class="minmax"><span class="max">{{ stats['inside_temp']['max'] }}°C</span><span class="sep"> / </span><span class="min">{{ stats['inside_temp']['min'] }}°C</span></div>
            </div>
            {% endif %}
            {% if stats['outside_temp'] %}
            <div class="summary-card">
                <div class="label">Outside Temp</div>
                <div class="minmax"><span class="max">{{ stats['outside_temp']['max'] }}°C</span><span class="sep"> / </span><span class="min">{{ stats['outside_temp']['min'] }}°C</span></div>
            </div>
            {% endif %}
        </div>
        <div class="summary-row">
            {% if stats['inside_humidity'] %}
            <div class="summary-card">
                <div class="label">Inside Humidity</div>
                <div class="minmax"><span class="max">{{ stats['inside_humidity']['max'] }}%</span><span class="sep"> / </span><span class="min">{{ stats['inside_humidity']['min'] }}%</span></div>
            </div>
            {% endif %}
            {% if stats['outside_humidity'] %}
            <div class="summary-card">
                <div class="label">Outside Humidity</div>
                <div class="minmax"><span class="max">{{ stats['outside_humidity']['max'] }}%</span><span class="sep"> / </span><span class="min">{{ stats['outside_humidity']['min'] }}%</span></div>
            </div>
            {% endif %}
        </div>
    </div>
    {% else %}
        <p class="no-data">No log data found yet at {{ log_file }}.</p>
    {% endif %}

    <div class="nav-buttons">
        <a href="/">⬅ Back to Dashboard</a>
    </div>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────
@app.route("/")
def dashboard():
    df = load_log()

    if df is None:
        return render_template_string(
            PAGE_TEMPLATE,
            has_data=False,
            log_file=LOG_FILE,
            last_updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            refresh_seconds=AUTO_REFRESH_SECONDS,
        )

    latest = df.iloc[-1]
    inside_temp = latest["inside_temp_c"]
    inside_humidity = latest["inside_humidity_pct"]
    outside_temp = latest["outside_temp_c"]
    outside_humidity = latest["outside_humidity_pct"]

    # Bottle probe: column may not exist yet until that sensor is
    # wired in and RunWineCooling.py is updated to log it. Handle
    # gracefully so the dashboard still works either way.
    has_bottle_temp = "bottle_temp_c" in df.columns and pd.notna(latest.get("bottle_temp_c"))
    bottle_temp = latest["bottle_temp_c"] if has_bottle_temp else None

    # Fan on/off badges: derive from the latest row's poll counts,
    # same rounding rule used for the fan activity chart.
    has_fan_status = "extractor_polls_on" in df.columns and "intake_polls_on" in df.columns
    if has_fan_status:
        latest_with_fans = add_fan_on_off_columns(df.iloc[[-1]]).iloc[-1]
        extractor_on = bool(latest_with_fans["extractor_on"])
        intake_on = bool(latest_with_fans["intake_on"])
    else:
        extractor_on = intake_on = False

    return render_template_string(
        PAGE_TEMPLATE,
        has_data=True,
        log_file=LOG_FILE,
        last_updated=latest["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
        refresh_seconds=AUTO_REFRESH_SECONDS,
        inside_temp=f"{inside_temp:.1f}",
        inside_humidity=f"{inside_humidity:.1f}",
        outside_temp=f"{outside_temp:.1f}",
        outside_humidity=f"{outside_humidity:.1f}",
        has_bottle_temp=has_bottle_temp,
        bottle_temp=f"{bottle_temp:.1f}" if has_bottle_temp else None,
        has_fan_status=has_fan_status,
        intake_text="On" if intake_on else "Off",
        extractor_text="On" if extractor_on else "Off",
        intake_colour=FAN_STATUS_COLOURS[intake_on],
        extractor_colour=FAN_STATUS_COLOURS[extractor_on],
        temp_colour=STATUS_COLOURS[temp_status(inside_temp)],
        humidity_colour=STATUS_COLOURS[humidity_status(inside_humidity)],
        bottle_colour=STATUS_COLOURS[bottle_temp_status(bottle_temp)] if has_bottle_temp else None,
        temp_target_min=TEMP_TARGET_MIN,
        temp_target_max=TEMP_TARGET_MAX,
        humidity_target_min=HUMIDITY_TARGET_MIN,
        humidity_target_max=HUMIDITY_TARGET_MAX,
    )


@app.route("/graphs")
def graphs():
    period = request.args.get("period", "7d")
    if period not in ("7d", "month", "year", "all"):
        period = "7d"

    df = load_log()

    if df is None:
        return render_template_string(
            GRAPHS_TEMPLATE,
            has_data=False,
            log_file=LOG_FILE,
            period=period,
            last_updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    filtered = filter_by_period(df, period)
    stats = compute_period_stats(filtered)
    filtered = downsample_for_period(filtered, period)

    chart_config = {"displayModeBar": False, "responsive": True}
    combined_chart = build_combined_figure(filtered).to_html(
        full_html=False, include_plotlyjs="cdn", config=chart_config)

    return render_template_string(
        GRAPHS_TEMPLATE,
        has_data=True,
        log_file=LOG_FILE,
        period=period,
        period_label=PERIOD_LABELS.get(period, ""),
        stats=stats,
        last_updated=df["timestamp"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
        combined_chart=combined_chart,
    )


if __name__ == "__main__":
    print(f"Wine cellar dashboard running at http://<pi-ip>:{PORT}")
    # 0.0.0.0 binds to all interfaces so it's reachable from other
    # devices on the local network (phone, laptop).
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

