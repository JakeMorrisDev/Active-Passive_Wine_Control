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
import json
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from flask import Flask, request, redirect, url_for, render_template_string
from WineCellarShared import (
    TEMP_TARGET_MAX,
    HUMIDITY_TARGET_MIN,
    HUMIDITY_TARGET_MAX,
    OVERRIDE_FILE,
    read_override,
    write_override,
)

# ── Config ────────────────────────────────────────────────
LOG_FILE = "/home/jakem/WineCellarManagerCode/wine_cellar_log.csv"
# Swap to the test file below while developing against fake data:lets
# LOG_FILE = "/home/jakem/wine_cellar_log_test.csv"

PORT = 5000
AUTO_REFRESH_SECONDS = 300

# ── Target ranges ─────────────────────────────────────────────────
TEMP_TARGET_MIN = 11.5      # °C - display lower bound only; no control equivalent in Shared

# ── Manual "on" duration options ───────────────────────────────────
# Shown via the single slide-through duration control on each fan
# card, rather than a grid of individual buttons. Whatever's
# currently selected here is what gets applied whenever the
# auto/manual toggle next cycles a fan into Manual On.
DURATION_OPTIONS = [
    (5, "5m"), (15, "15m"), (30, "30m"),
    (60, "1h"), (120, "2h"), (180, "3h"),
    (240, "4h"), (300, "5h"), (360, "6h"),
]
DURATION_MINUTES_LIST = [minutes for minutes, _ in DURATION_OPTIONS]
DURATION_LABELS = dict(DURATION_OPTIONS)
DURATION_OPTIONS_JSON = json.dumps(DURATION_OPTIONS)
DEFAULT_DURATION_MINUTES = 5

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
    MAX_BUCKET_SECONDS = 3 * 60 * 60

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


# ── Manual fan overrides (dashboard side) ──────────────────────────
def get_fan_duration_minutes(overrides, fan_key):
    """The duration (minutes) currently selected for this fan's
    Manual On - shown on the duration pill and applied whenever the
    toggle next cycles this fan into Manual On. Stored independently
    of any currently-active override entry, so it persists as a
    standing preference across auto/manual/auto cycles. Falls back to
    the 5-minute default until the user picks something else via the
    duration slider."""
    return overrides.get("durations", {}).get(fan_key, DEFAULT_DURATION_MINUTES)


def fan_override_status(overrides, fan_key, fan_currently_on):
    """Return display state for one fan control card, including the
    single next-tap action (for the auto/manual toggle button) and
    the currently selected Manual On duration (for the duration
    pill/slider)."""
    entry = overrides.get(fan_key, {})
    state = entry.get("state")
    # step 1 = the first manual state after leaving Auto (always the
    # OPPOSITE of whatever was actually running, so the first tap has
    # a visible effect); step 2 = the second manual state (a tap here
    # returns to Auto). Missing on old/malformed entries -> treat as
    # step 1, which just means "flip to the other manual value" next,
    # a safe fallback rather than jumping straight to Auto.
    step = entry.get("step", 1)

    duration_minutes = get_fan_duration_minutes(overrides, fan_key)
    duration_label = DURATION_LABELS.get(duration_minutes, f"{duration_minutes}m")
    duration_index = (
        DURATION_MINUTES_LIST.index(duration_minutes)
        if duration_minutes in DURATION_MINUTES_LIST
        else 0
    )
    common = {
        "duration_minutes": duration_minutes,
        "duration_label": duration_label,
        "duration_index": duration_index,
    }

    if state is None:
        common.update({
            "mode": "auto_on" if fan_currently_on else "auto_off",
            "label": "Auto On" if fan_currently_on else "Auto Off",
            "note": None,
            "is_manual": False,
            # First tap out of Auto always forces the opposite of what's
            # actually running right now.
            "next_action_label": "Force Off" if fan_currently_on else "Force On",
        })
        return common

    note = None
    if entry.get("expires_at"):
        try:
            remaining = datetime.fromisoformat(entry["expires_at"]) - datetime.now()
            secs = max(0, int(remaining.total_seconds()))
            h, m = divmod(secs // 60, 60)
            note = f"{h}h {m}m remaining" if h else f"{m}m remaining"
        except Exception:
            pass

    if state == "off":
        common.update({
            "mode": "manual_off",
            "label": "Manual Off",
            "note": note,
            "is_manual": True,
            "next_action_label": "Force On" if step == 1 else "Back to Auto",
        })
        return common

    # state == "on" - not yet applied by the control loop overrides its countdown note
    if not entry.get("validated"):
        note = "applying..."

    common.update({
        "mode": "manual_on",
        "label": "Manual On",
        "note": note,
        "is_manual": True,
        "next_action_label": "Force Off" if step == 1 else "Back to Auto",
    })
    return common


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


# ── Page template ──────────────────────────────────
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
        .fan-controls {
            display: flex;
            justify-content: center;
            gap: 16px;
            margin: 20px 0;
            flex-wrap: wrap;
        }
        .fan-control-card {
            border-radius: 16px;
            padding: 16px 20px;
            background: #1e1e1e;
            border: 2px solid var(--status-colour);
            min-width: 190px;
        }
        .fan-control-card .label {
            color: #aaa;
            font-size: 0.8em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .fan-control-card .value {
            font-size: 1.4em;
            font-weight: bold;
            color: var(--status-colour);
            margin: 4px 0;
        }
        .fan-control-card .manual-note {
            color: #e6a700;
            font-size: 0.75em;
            margin-bottom: 8px;
        }

        /* ── Toggle button + duration pill, side by side, same size ── */
        .fan-actions {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 10px;
        }
        .toggle-form {
            margin: 0;
        }
        .toggle-btn {
            background: #292929;
            border: 1px solid #555;
            border-radius: 8px;
            padding: 6px 14px;
            color: #eee;
            font-size: 0.85em;
            cursor: pointer;
        }
        .toggle-btn:hover {
            border-color: #6cf;
        }
        .duration-toggle-btn {
            background: #1a1a1a;
            border: 1px solid #444;
            border-radius: 20px;
            padding: 6px 14px;
            color: #aaa;
            font-size: 0.85em;
            cursor: pointer;
        }
        .duration-toggle-btn:hover {
            border-color: #6cf;
            color: #6cf;
        }

        /* ── Slide-out duration picker, full width below the row ── */
        .duration-slider-panel {
            display: none;
            margin-top: 10px;
            padding: 4px 6px 0;
        }
        .duration-slider-panel.open {
            display: block;
        }
        .duration-slider-panel input[type="range"] {
            width: 100%;
        }
        .duration-slider-label {
            color: #6cf;
            font-size: 0.85em;
            margin-top: 4px;
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
    {% if has_bottle_temp %}
    <div class="bottle-stats">
        <div class="stat-card compact" style="--status-colour: {{ bottle_colour }};">
            <div class="label">Bottle Temp</div>
            <div class="value">{{ bottle_temp }}°C</div>
            <div class="target">Target: {{ temp_target_min }}–{{ temp_target_max }}°C</div>
        </div>
    </div>
    {% endif %}

    {% if has_fan_status %}
    <div class="fan-controls">
        {% for fan_name, status, colour in [('intake', intake_status, intake_colour), ('extractor', extractor_status, extractor_colour)] %}
        <div class="fan-control-card" style="--status-colour: {{ colour }};">
            <div class="label">{{ fan_name | title }}</div>
            <div class="value">{{ status.label }}</div>
            {% if status.note %}<div class="manual-note">{{ status.note }}</div>{% endif %}

            <div class="fan-actions">
                <form method="post" action="/fan/{{ fan_name }}/toggle" class="toggle-form">
                    <button type="submit" class="toggle-btn">{{ status.next_action_label }}</button>
                </form>

                {% if status.mode != 'auto_on' %}
                <button type="button" class="duration-toggle-btn" onclick="toggleDurationSlider('{{ fan_name }}')">
                    ⏱ {{ status.duration_label }}
                </button>
                {% endif %}
            </div>

            {% if status.mode != 'auto_on' %}
            <div class="duration-slider-panel" id="duration-panel-{{ fan_name }}">
                <input type="range" min="0" max="{{ duration_options|length - 1 }}" step="1"
                       value="{{ status.duration_index }}"
                       oninput="updateDurationPreview('{{ fan_name }}', this.value)"
                       onchange="applyDuration('{{ fan_name }}', this.value)">
                <div class="duration-slider-label" id="duration-label-{{ fan_name }}">{{ status.duration_label }}</div>
            </div>
            {% endif %}
        </div>
        {% endfor %}
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

    {% if active_warning %}
    <div class="warning-banner">⚠ {{ active_warning }}</div>
    {% endif %}

    <div class="nav-buttons">
        <a href="/graphs">📈 View Graphs</a>
    </div>
    {% else %}
        <p class="no-data">No log data found yet at {{ log_file }}.</p>
    {% endif %}

    <script>
        const DURATION_OPTIONS = {{ duration_options_json | safe }};

        function toggleDurationSlider(fan) {
            const panel = document.getElementById('duration-panel-' + fan);
            panel.classList.toggle('open');
        }

        function updateDurationPreview(fan, index) {
            document.getElementById('duration-label-' + fan).textContent = DURATION_OPTIONS[index][1];
        }

        function applyDuration(fan, index) {
            const minutes = DURATION_OPTIONS[index][0];
            fetch('/fan/' + fan + '/duration/' + minutes, { method: 'POST' })
                .then(() => location.reload());
        }
    </script>
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
            active_warning=None,
            duration_options=DURATION_OPTIONS,
            duration_options_json=DURATION_OPTIONS_JSON,
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

    overrides = read_override()
    # Use live relay state written by RunWineCooling.py each poll - accurate within 10 s.
    extractor_on = bool(overrides.get("relay_extractor", False))
    intake_on = bool(overrides.get("relay_intake", False))
    has_fan_status = "relay_extractor" in overrides
    intake_status = fan_override_status(overrides, "intake", intake_on)
    extractor_status = fan_override_status(overrides, "extractor", extractor_on)
    active_warning = overrides.get("warning")

    intake_colour = STATUS_COLOURS["warn"] if intake_status["is_manual"] else FAN_STATUS_COLOURS[intake_on]
    extractor_colour = STATUS_COLOURS["warn"] if extractor_status["is_manual"] else FAN_STATUS_COLOURS[extractor_on]


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
        intake_colour=intake_colour,
        extractor_colour=extractor_colour,
        intake_status=intake_status,
        extractor_status=extractor_status,
        duration_options=DURATION_OPTIONS,
        duration_options_json=DURATION_OPTIONS_JSON,
        temp_colour=STATUS_COLOURS[temp_status(inside_temp)],
        humidity_colour=STATUS_COLOURS[humidity_status(inside_humidity)],
        bottle_colour=STATUS_COLOURS[bottle_temp_status(bottle_temp)] if has_bottle_temp else None,
        active_warning=active_warning,
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


# ── Manual fan override routes ─────────────────────────────────────
VALID_DURATIONS = set(DURATION_MINUTES_LIST)


def _fan_entry_state_and_step(overrides, fan_key):
    """The current override (state, step) for a fan.

    state is "on", "off", or None (meaning Auto: no override entry
    present, or a malformed one). step is 1 for the first manual
    state entered after leaving Auto, 2 for the second - missing/old
    entries default to step 1, a safe fallback (next tap just flips
    to the other manual value rather than jumping straight to Auto)."""
    entry = overrides.get(fan_key)
    if not entry or entry.get("state") not in ("on", "off"):
        return None, None
    return entry["state"], entry.get("step", 1)


def _manual_entry(state, overrides, fan_key, step):
    """Build a manual override entry - shared by both Manual On and
    Manual Off, which now both run for the selected duration before
    reverting to Auto."""
    minutes = get_fan_duration_minutes(overrides, fan_key)
    now = datetime.now()
    return {
        "state": state,
        "step": step,
        "set_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
        "duration_minutes": minutes,
        "validated": False,
    }


@app.route("/fan/<fan_name>/toggle", methods=["POST"])
def fan_toggle(fan_name):
    """Single tap-to-cycle control, in an order that depends on which
    side Auto is actually running on right now:

        Auto (running)     -> Manual Off -> Manual On  -> Auto
        Auto (not running) -> Manual On  -> Manual Off -> Auto

    i.e. the first tap out of Auto always forces the OPPOSITE of
    whatever's actually happening, so it has a visible effect rather
    than just pinning the current behaviour. The second manual tap
    flips to the other value. The third returns to Auto - which then
    falls back to whatever Auto decides live, possibly different from
    what it was showing when the override started.

    Manual On always picks up whatever duration is currently selected
    on that fan's duration pill (get_fan_duration_minutes), defaulting
    to 5 minutes until the user chooses otherwise via the slider."""
    if fan_name not in ("intake", "extractor"):
        return ("Invalid request", 400)

    overrides = read_override()
    state, step = _fan_entry_state_and_step(overrides, fan_name)

    if state is None:
        fan_currently_on = bool(overrides.get(f"relay_{fan_name}", False))
        overrides[fan_name] = _manual_entry(
            "off" if fan_currently_on else "on", overrides, fan_name, step=1
        )
    elif step == 1:
        # First manual state -> flip to the other manual value.
        overrides[fan_name] = _manual_entry(
            "on" if state == "off" else "off", overrides, fan_name, step=2
        )
    else:
        # Second manual state -> back to Auto.
        overrides.pop(fan_name, None)

    write_override(overrides)
    return redirect(url_for("dashboard"))


@app.route("/fan/<fan_name>/duration/<int:minutes>", methods=["POST"])
def fan_duration(fan_name, minutes):
    """Set the duration used whenever this fan's toggle next moves
    into Manual On. Persisted independently of any active override so
    it's remembered as a standing preference across auto/manual
    cycles. If the fan is ALREADY Manual On, the live timer is updated
    immediately too - recomputed from the override's original set_at
    rather than from now, so time already spent manual isn't lost or
    double-counted (shortening below the elapsed time just ends the
    override promptly, which is the expected behaviour)."""
    if fan_name not in ("intake", "extractor") or minutes not in VALID_DURATIONS:
        return ("Invalid request", 400)

    overrides = read_override()
    overrides.setdefault("durations", {})[fan_name] = minutes

    entry = overrides.get(fan_name)
    if entry and entry.get("state") in ("on", "off"):
        try:
            set_at = datetime.fromisoformat(entry.get("set_at"))
        except Exception:
            set_at = datetime.now()
        entry["duration_minutes"] = minutes
        entry["expires_at"] = (set_at + timedelta(minutes=minutes)).isoformat()

    write_override(overrides)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    print(f"Wine cellar dashboard running at http://<pi-ip>:{PORT}")
    # 0.0.0.0 binds to all interfaces so it's reachable from other
    # devices on the local network (phone, laptop).
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)