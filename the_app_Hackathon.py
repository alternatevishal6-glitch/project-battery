#!/usr/bin/env python3
"""
CELLWATCH DESKTOP
------------------
Live battery telemetry monitor for a USB-connected Android phone, via ADB.

Requirements (on the machine you RUN this on, not this sandbox):
  1. Android SDK Platform Tools installed, with `adb` on your PATH.
     https://developer.android.com/tools/releases/platform-tools
  2. Phone connected via USB with "USB debugging" enabled
     (Settings > About phone > tap "Build number" 7x > Developer options
     > enable USB debugging), and the "Allow USB debugging?" prompt
     accepted on the phone screen.
  3. Python 3.8+ (Tkinter ships with standard Python on Windows/macOS;
     on Linux you may need: sudo apt install python3-tk)

Run:
  python3 cellwatch_desktop.py

What it shows, polled every N seconds:
  - Voltage (V)
  - Current (mA) + direction (charging/discharging), when the device exposes it
  - Temperature (°C)
  - Cycle count, when the device exposes it
  - Status / health
  - Charge or discharge RATE, computed two ways:
        a) directly from current_now, when available
        b) derived from %SOC change over time (always available as fallback)
  - Rolling risk score reusing the same thermal/stress logic from the
    hackathon SoH/RUL problem statement.

This is an MVP diagnostic tool. It issues NO control commands to the
device and never attempts to modify battery/BMS behavior.
"""

import subprocess
import threading
import time
import shutil
import re
import json
import os
from collections import deque
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

# Local file used to persist the estimated-cycle-count accumulator between
# runs, since it's computed from SOC history the app has observed itself.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cellwatch_state.json")

# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

ADB = shutil.which("adb") or "adb"

# Known sysfs locations for fields dumpsys doesn't reliably expose.
# Different OEMs (Samsung, Pixel, Xiaomi/MIUI, etc.) use different paths,
# so we try each candidate in order and use the first one that responds.
CURRENT_NOW_PATHS = [
    "/sys/class/power_supply/battery/current_now",
    "/sys/class/power_supply/bms/current_now",
    "/sys/class/power_supply/max170xx_battery/current_now",
]
CYCLE_COUNT_PATHS = [
    "/sys/class/power_supply/battery/cycle_count",
    "/sys/class/power_supply/bms/cycle_count",
]

STATUS_MAP = {"1": "Unknown", "2": "Charging", "3": "Discharging", "4": "Not charging", "5": "Full"}
HEALTH_MAP = {
    "1": "Unknown", "2": "Good", "3": "Overheat", "4": "Dead",
    "5": "Over voltage", "6": "Unspecified failure", "7": "Cold",
}


def adb(args, timeout=4):
    """Run an adb command, return stdout (str) or None on failure."""
    try:
        result = subprocess.run(
            [ADB] + args, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def get_connected_device():
    """Return a device serial if exactly one device is connected & authorized."""
    out = adb(["devices"])
    if not out:
        return None, "adb not found or not responding. Is it installed and on PATH?"
    lines = [l.strip() for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        return None, "No device detected. Plug in via USB and enable USB debugging."
    devices = [l.split()[0] for l in lines if l.endswith("device")]
    unauthorized = [l for l in lines if "unauthorized" in l]
    if unauthorized:
        return None, "Device connected but unauthorized. Accept the 'Allow USB debugging?' prompt on the phone."
    if not devices:
        return None, "Device found but not ready (still initializing?)."
    return devices[0], None


def shell(serial, cmd, timeout=4):
    out = adb(["-s", serial, "shell"] + cmd.split(), timeout=timeout)
    return out.strip() if out else None


def read_first_working_path(serial, paths):
    for p in paths:
        val = shell(serial, f"cat {p}")
        if val is not None and val != "" and re.match(r"^-?\d+$", val):
            return int(val), p
    return None, None


# ---------------------------------------------------------------------------
# Estimated cycle count (fallback when the OEM hides real cycle_count)
#
# Industry-standard definition: one full cycle = cumulative DISCHARGE equal
# to 100% of rated capacity, however it's split up (e.g. five separate 20%
# discharges = 1 cycle). We accumulate this from observed SOC drops between
# polls and persist it to disk per-device so it survives app restarts.
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


class CycleEstimator:
    """Accumulates observed discharge (in % SOC) per device serial and
    converts it into an estimated cycle count. Persists across restarts.
    Also stores a user-supplied manufacture/purchase date per device to
    compute an age-based cycle RANGE as a second, independent estimate."""

    # Rough industry heuristic: a typical smartphone user puts their battery
    # through roughly 0.7-1.3 full-equivalent charge cycles per day. This is
    # NOT a measured figure for your specific device/usage - it's a coarse
    # sanity-check range, useful mainly when no other data source exists.
    LOW_CYCLES_PER_DAY = 0.7
    HIGH_CYCLES_PER_DAY = 1.3

    def __init__(self):
        self.state = load_state()

    def update(self, serial, level):
        if level is None:
            return self.get(serial)
        dev = self.state.setdefault(serial, {"last_level": None, "cum_discharge_pct": 0.0, "mfg_date": None})
        last = dev["last_level"]
        if last is not None and level < last:
            dev["cum_discharge_pct"] += (last - level)
        dev["last_level"] = level
        save_state(self.state)
        return self.get(serial)

    def get(self, serial):
        dev = self.state.get(serial)
        if not dev:
            return 0.0
        return dev["cum_discharge_pct"] / 100.0

    def set_mfg_date(self, serial, date_str):
        """date_str expected as YYYY-MM-DD. Raises ValueError if malformed."""
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        dev = self.state.setdefault(serial, {"last_level": None, "cum_discharge_pct": 0.0, "mfg_date": None})
        dev["mfg_date"] = date_str
        save_state(self.state)
        return parsed

    def get_mfg_date(self, serial):
        dev = self.state.get(serial)
        if not dev:
            return None
        return dev.get("mfg_date")

    def set_manufacturer(self, serial, name):
        dev = self.state.setdefault(serial, {"last_level": None, "cum_discharge_pct": 0.0, "mfg_date": None})
        dev["manufacturer"] = name
        save_state(self.state)

    def get_manufacturer(self, serial):
        dev = self.state.get(serial)
        if not dev:
            return None
        return dev.get("manufacturer")

    def age_based_range(self, serial):
        """Returns (elapsed_days, low_cycles, high_cycles) or None if no date set."""
        date_str = self.get_mfg_date(serial)
        if not date_str:
            return None
        mfg = datetime.strptime(date_str, "%Y-%m-%d")
        elapsed_days = (datetime.now() - mfg).days
        if elapsed_days < 0:
            return None
        low = elapsed_days * self.LOW_CYCLES_PER_DAY
        high = elapsed_days * self.HIGH_CYCLES_PER_DAY
        return elapsed_days, low, high


# Cycle-count-based health rating. Keyed by the LOW end of each band, checked
# in descending order. Applied against the midpoint of the estimated range.
CYCLE_HEALTH_BANDS = [
    (800, "Significant degradation becomes more likely", "#e0524a"),
    (500, "Noticeable aging possible", "#e08a3a"),
    (300, "Good, depending on battery", "#f0d43a"),
    (100, "Very good", "#3fcf8e"),
    (0,   "Excellent / nearly new", "#3fcf8e"),
]


def cycle_health_rating(cycles):
    for threshold, label, color in CYCLE_HEALTH_BANDS:
        if cycles >= threshold:
            return label, color
    return CYCLE_HEALTH_BANDS[-1][1], CYCLE_HEALTH_BANDS[-1][2]


# ---------------------------------------------------------------------------
# Manufacturer capacity-retention curves
#
# This app has no internet access at runtime, so it can't live-fetch a
# dataset (e.g. Kaggle/NASA Li-ion cycling datasets) each time it runs.
# Instead, each curve below is anchored to a real published figure, sourced
# either from that manufacturer's own support pages, or (where the
# manufacturer doesn't publish one directly) from independent tech press
# reporting on official claims/EU battery-label data (checked August 2026):
#
#   Apple  - "Batteries of iPhone 14 models and earlier are designed to
#             retain 80% of their original capacity at 500 complete charge
#             cycles... iPhone 15 models... at 1000 complete charge cycles."
#             (support.apple.com/en-us/101575) - official, first-party.
#   Google - "Pixel 3 through Pixel 8 Pro and Pixel Fold: ...80% capacity
#             for about 800 charge cycles. Pixel 8a and later: ...1000
#             charge cycles." (support.google.com/pixelphone/answer/15738128)
#             - official, first-party.
#   Samsung- No single official Samsung support page states a figure as
#            plainly as Apple/Google's; ~500 cycles to 80% is the widely
#            cited figure for Galaxy S23-era and earlier (Samsung Community
#            moderator guidance, multiple independent write-ups). Newer
#            Galaxy S25 / Z Fold7-era devices are rated far higher (up to
#            ~2000 cycles) per the EU's mandatory battery energy label,
#            introduced in 2025 - a regulatory disclosure, not a marketing
#            claim, though exact figures vary by exact model.
#   OnePlus- EU battery-label data reported by Android Authority: OnePlus 12
#            rated 1600 cycles to 80%, OnePlus 13 rated 1000 cycles, OnePlus
#            15 claims ~1400 cycles (per OnePlus's own statement to Android
#            Authority). Used ~1200 as a representative mid-range flagship
#            figure across recent models, which vary noticeably generation
#            to generation.
#   Xiaomi - GSMArena reporting on Xiaomi's own claim for 200W-charging
#            models: ~20% capacity loss (i.e. 80% retained) at 800 cycles.
#
# The full curve SHAPE beyond each single anchor point is this app's own
# extrapolation (a smooth, accelerating fade), not manufacturer data -
# only the "80% at N cycles" anchor itself is a real published figure.
# ---------------------------------------------------------------------------

# (fraction_of_anchor_cycles, capacity_pct) - calibrated so fraction=1.0
# lands on 80%, matching how every manufacturer above states its figure.
_CURVE_SHAPE = [
    (0, 100), (0.1, 99), (0.2, 97), (0.4, 94), (0.6, 91),
    (0.8, 87), (1.0, 80), (1.3, 72), (1.6, 63), (2.0, 53), (2.4, 44),
]


def _generate_curve(anchor_cycles):
    return [(round(frac * anchor_cycles), cap) for frac, cap in _CURVE_SHAPE]


MANUFACTURER_CURVES = {
    "Apple (iPhone 14 & earlier)": _generate_curve(500),
    "Apple (iPhone 15 & later)": _generate_curve(1000),
    "Samsung (Galaxy S23 & earlier)": _generate_curve(500),
    "Samsung (Galaxy S25 / Z Fold7 & newer)": _generate_curve(2000),
    "Google Pixel (3 – 8 Pro / Fold)": _generate_curve(800),
    "Google Pixel (8a & later)": _generate_curve(1000),
    "OnePlus (recent flagships)": _generate_curve(1200),
    "Xiaomi (200W charging models)": _generate_curve(800),
    "Generic / other Android": _generate_curve(500),
}
DEFAULT_MANUFACTURER = "Generic / other Android"


def interpolate_reference_capacity(cycles, curve):
    """Linear-interpolate estimated % capacity retained at a given cycle
    count from the given curve. Clamps to the curve's ends."""
    pts = curve
    if cycles <= pts[0][0]:
        return pts[0][1]
    if cycles >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= cycles <= x1:
            frac = (cycles - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return pts[-1][1]


# ---------------------------------------------------------------------------
# Temperature effect on effective wear
#
# Sourced from Battery University BU-502 ("Discharging at High and Low
# Temperatures"): cycle life is reduced ~20% at 30C, ~40% at 40C, and ~50%
# at 45C, relative to a 20C baseline. We use this to inflate the "effective"
# cycle count used against the reference curve when the app's own observed
# average operating temperature runs hot - a battery run hot has worn out
# faster than its raw cycle count alone would suggest.
# ---------------------------------------------------------------------------

TEMP_LIFE_FRACTION_CURVE = [(20, 1.0), (30, 0.8), (40, 0.6), (45, 0.5)]


def temp_life_multiplier(avg_temp_c):
    pts = TEMP_LIFE_FRACTION_CURVE
    if avg_temp_c <= pts[0][0]:
        frac = pts[0][1]
    elif avg_temp_c >= pts[-1][0]:
        frac = pts[-1][1]
    else:
        frac = pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= avg_temp_c <= x1:
                f = (avg_temp_c - x0) / (x1 - x0)
                frac = y0 + f * (y1 - y0)
                break
    return 1.0 / frac if frac > 0 else 2.0


def poll_battery(serial):
    """Return a dict of parsed telemetry, or None fields where unavailable."""
    raw = shell(serial, "dumpsys battery", timeout=5)
    data = {
        "voltage_v": None, "temp_c": None, "level": None,
        "status": "N/A", "health": "N/A", "technology": "N/A",
        "current_ma": None, "current_source": None,
        "cycle_count": None,
    }
    if raw:
        fields = {}
        for line in raw.splitlines():
            if ":" in line:
                k, v = line.strip().split(":", 1)
                fields[k.strip()] = v.strip()

        if "voltage" in fields:
            try:
                data["voltage_v"] = int(fields["voltage"]) / 1000.0
            except ValueError:
                pass
        if "temperature" in fields:
            try:
                data["temp_c"] = int(fields["temperature"]) / 10.0
            except ValueError:
                pass
        if "level" in fields:
            try:
                data["level"] = int(fields["level"])
            except ValueError:
                pass
        data["status"] = STATUS_MAP.get(fields.get("status"), fields.get("status", "N/A"))
        data["health"] = HEALTH_MAP.get(fields.get("health"), fields.get("health", "N/A"))
        data["technology"] = fields.get("technology", "N/A")

    current_raw, path = read_first_working_path(serial, CURRENT_NOW_PATHS)
    if current_raw is not None:
        # Units vary by OEM: usually microamps, sometimes milliamps.
        # Heuristic: anything with magnitude > 100000 is almost certainly µA.
        ma = current_raw / 1000.0 if abs(current_raw) > 100000 else float(current_raw)
        data["current_ma"] = ma
        data["current_source"] = path

    cycles, _ = read_first_working_path(serial, CYCLE_COUNT_PATHS)
    if cycles is not None:
        data["cycle_count"] = cycles

    return data# ---------------------------------------------------------------------------
# Risk scoring
#
# Combines live thermal/current stress (from recent polling) with the
# battery's overall wear level (estimated cycle count), since a battery
# that's already heavily cycled is inherently more at-risk even during an
# otherwise "calm" polling window. Weights: thermal/current stress still
# dominates (it's the acute, immediate-safety signal) but cycle count now
# contributes a meaningful share rather than being ignored.
# ---------------------------------------------------------------------------

def compute_risk(history, cycles=None):
    if len(history) < 2:
        return None
    temps = [h["temp_c"] for h in history if h["temp_c"] is not None]
    socs = [h["level"] for h in history if h["level"] is not None]
    currents = [abs(h["current_ma"]) for h in history if h["current_ma"] is not None]

    if not temps or not socs:
        return None

    time_above_45 = sum(1 for t in temps if t > 45) / len(temps)
    time_above_60 = sum(1 for t in temps if t > 60) / len(temps)
    dod = max(socs) - min(socs)
    peak_current = max(currents) if currents else 0
    cycle_factor = min(cycles / 1000, 1) if cycles is not None else 0

    normed = {
        "Time above 45C": min(time_above_45, 1),
        "Time above 60C": min(time_above_60, 1),
        "Depth of discharge": min(dod / 100, 1),
        "Peak current stress": min(peak_current / 3000, 1),
        "Cycle count / wear": cycle_factor,
    }
    weights = {"Time above 45C": 20, "Time above 60C": 30, "Depth of discharge": 10,
               "Peak current stress": 15, "Cycle count / wear": 25}
    score = sum(normed[k] * weights[k] for k in weights)

    if score < 30:
        band = "HEALTHY"
    elif score < 60:
        band = "MONITOR CLOSELY"
    elif score < 80:
        band = "REPLACE SOON"
    else:
        band = "HIGH RISK"
    return score, band, normed


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG = "#0b0d12"
CARD = "#131720"
CARD_BORDER = "#232a36"
TEXT = "#eef2f6"
MUTED = "#8b96a5"
GOLD = "#f2a93b"
BLUE = "#4fa3e0"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#ef4444"

F_TITLE = ("Segoe UI Semibold", 19)
F_SUB = ("Segoe UI", 10)
F_LABEL = ("Segoe UI", 9, "bold")
F_VALUE = ("Consolas", 22, "bold")
F_VALUE_SM = ("Consolas", 14, "bold")
F_BTN = ("Segoe UI", 10, "bold")


def rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    points = [
        x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
        x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


CARD_HOVER = "#171d29"


def _lighten(hexcolor, amount=10):
    hexcolor = hexcolor.lstrip("#")
    r, g, b = int(hexcolor[0:2], 16), int(hexcolor[2:4], 16), int(hexcolor[4:6], 16)
    r, g, b = min(255, r + amount), min(255, g + amount), min(255, b + amount)
    return f"#{r:02x}{g:02x}{b:02x}"


class Card(tk.Frame):
    """An elevated-looking panel: subtle border + a thin colored accent
    strip along the top edge, standing in for a real drop-shadow/rounded
    card in plain Tkinter. On hover, the border brightens to the accent
    color, the strip thickens, and the body lightens slightly — a cheap
    but convincing "lift" effect."""
    def __init__(self, parent, accent=GOLD, **kw):
        super().__init__(parent, bg=CARD, highlightbackground=CARD_BORDER,
                          highlightcolor=CARD_BORDER, highlightthickness=1, bd=0, **kw)
        self.accent = accent
        self.strip = tk.Frame(self, bg=accent, height=3)
        self.strip.pack(fill="x", side="top")
        self.body = tk.Frame(self, bg=CARD)
        self.body.pack(fill="both", expand=True)
        self._hover = False

    def enable_hover(self):
        """Call once, after all child widgets have been added, so the hover
        zone covers the whole card including its labels/canvases."""
        self._bind_recursive(self)
        return self

    def _bind_recursive(self, widget):
        widget.bind("<Enter>", self._schedule_check, add="+")
        widget.bind("<Leave>", self._schedule_check, add="+")
        for child in widget.winfo_children():
            self._bind_recursive(child)

    def _schedule_check(self, event=None):
        self.after(25, self._check_hover)

    def _check_hover(self):
        try:
            x, y = self.winfo_pointerxy()
            under = self.winfo_containing(x, y)
        except (KeyError, tk.TclError):
            under = None
        inside = False
        w = under
        while w is not None:
            if w == self:
                inside = True
                break
            w = w.master
        if inside != self._hover:
            self._hover = inside
            self._apply_hover_style()

    def _apply_hover_style(self):
        on = self._hover
        self.configure(highlightbackground=self.accent if on else CARD_BORDER,
                        highlightthickness=2 if on else 1)
        self.strip.configure(height=5 if on else 3, bg=_lighten(self.accent, 25) if on else self.accent)
        bg = CARD_HOVER if on else CARD
        self.body.configure(bg=bg)
        self._recolor_children(self.body, bg)

    def _recolor_children(self, widget, bg):
        for child in widget.winfo_children():
            try:
                if str(child.cget("bg")) in (CARD, CARD_HOVER):
                    child.configure(bg=bg)
            except tk.TclError:
                pass
            self._recolor_children(child, bg)


class MetricTile(Card):
    def __init__(self, parent, icon, label, accent=GOLD, **kw):
        super().__init__(parent, accent=accent, **kw)
        top = tk.Frame(self.body, bg=CARD)
        top.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(top, text=icon, bg=CARD, font=("Segoe UI Emoji", 13)).pack(side="left")
        tk.Label(top, text=label, fg=MUTED, bg=CARD, font=F_LABEL).pack(side="left", padx=(6, 0))
        self.value_label = tk.Label(self.body, text="—", fg=TEXT, bg=CARD, font=F_VALUE)
        self.value_label.pack(anchor="w", padx=14, pady=(2, 12))

    def set(self, text, color=None):
        self.value_label.config(text=text, fg=color or TEXT)


class ProgressBar(tk.Canvas):
    def __init__(self, parent, width=220, height=12, **kw):
        super().__init__(parent, width=width, height=height, bg=CARD, highlightthickness=0, **kw)
        self.w, self.h = width, height
        rounded_rect(self, 0, 0, width, height, height/2, fill=CARD_BORDER, outline="")
        self.fg = rounded_rect(self, 0, 0, 2, height, height/2, fill=BLUE, outline="")

    def set(self, pct, color=BLUE):
        pct = max(0, min(100, pct))
        w = max(self.h, self.w * pct / 100)
        self.delete(self.fg)
        self.fg = rounded_rect(self, 0, 0, w, self.h, self.h/2, fill=color, outline="")
        self.tag_lower(self.fg)
        # keep bg track under fg by redrawing fg last, but ensure ordering:
        self.tag_raise(self.fg)


class RadialGauge(tk.Canvas):
    def __init__(self, parent, size=150, **kw):
        super().__init__(parent, width=size, height=size, bg=CARD, highlightthickness=0, **kw)
        self.size = size
        pad = 12
        self.create_oval(pad, pad, size-pad, size-pad, outline=CARD_BORDER, width=10)
        self.arc = self.create_arc(pad, pad, size-pad, size-pad, start=90, extent=0,
                                    style="arc", width=10, outline=GREEN)
        self.score_text = self.create_text(size/2, size/2 - 8, text="—", fill=TEXT, font=("Consolas", 26, "bold"))
        self.band_text = self.create_text(size/2, size/2 + 22, text="AWAITING DATA", fill=MUTED,
                                           font=("Segoe UI", 9, "bold"))

    def set(self, score, color, band):
        extent = -3.6 * score
        self.itemconfig(self.arc, extent=extent, outline=color)
        self.itemconfig(self.score_text, text=f"{score:.0f}")
        self.itemconfig(self.band_text, text=band, fill=color)



class ReferenceCurveChart(tk.Canvas):
    """Plots a manufacturer capacity-vs-cycles reference curve, with a
    marker showing where the user's own (temperature-adjusted) estimated
    cycle count falls on it. Label placement is dynamic (above or below
    the marker depending on its position) with a solid background behind
    the text so it never gets visually cut by gridlines or the curve."""
    def __init__(self, parent, width=580, height=220, **kw):
        super().__init__(parent, width=width, height=height, bg=CARD, highlightthickness=0, **kw)
        self.w, self.h = width, height

    def plot(self, curve, user_cycles=None, raw_cycles=None):
        self.delete("chart")
        pad_l, pad_r, pad_t, pad_b = 46, 20, 34, 40
        plot_w = self.w - pad_l - pad_r
        plot_h = self.h - pad_t - pad_b

        xs = [p[0] for p in curve]
        xmax = xs[-1]
        ymin, ymax = 30, 100  # capacity % axis range

        def xy(cycles, cap_pct):
            x = pad_l + (min(cycles, xmax) / xmax) * plot_w
            y = pad_t + plot_h - ((cap_pct - ymin) / (ymax - ymin)) * plot_h
            return x, y

        # gridlines + y-axis labels
        for frac in (0, 0.25, 0.5, 0.75, 1):
            gy = pad_t + plot_h - frac * plot_h
            self.create_line(pad_l, gy, self.w - pad_r, gy, fill=CARD_BORDER, tags="chart")
            val = ymin + frac * (ymax - ymin)
            self.create_text(pad_l - 8, gy, text=f"{val:.0f}%", fill=MUTED, font=("Consolas", 8),
                              anchor="e", tags="chart")

        # x-axis labels
        for cycles in (0, xmax // 3, 2 * xmax // 3, xmax):
            gx = pad_l + (cycles / xmax) * plot_w
            self.create_text(gx, pad_t + plot_h + 12, text=str(int(cycles)), fill=MUTED,
                              font=("Consolas", 8), tags="chart")

        # reference curve
        points = [xy(c, cap) for c, cap in curve]
        flat = [v for pt in points for v in pt]
        self.create_line(*flat, fill=MUTED, width=2, smooth=True, dash=(4, 3), tags="chart")

        # compact legend, bottom edge, out of the way of any data
        self.create_line(pad_l, self.h - 8, pad_l + 18, self.h - 8, fill=MUTED, width=2,
                          dash=(4, 3), tags="chart")
        self.create_text(pad_l + 24, self.h - 8, text="reference curve", fill=MUTED,
                          font=("Segoe UI", 8), anchor="w", tags="chart")
        self.create_oval(pad_l + 130, self.h - 11, pad_l + 136, self.h - 5, fill=TEXT, outline="", tags="chart")
        self.create_text(pad_l + 142, self.h - 8, text="your estimate", fill=MUTED,
                          font=("Segoe UI", 8), anchor="w", tags="chart")

        # user's marker (plotted at temperature-adjusted "effective" cycles)
        if user_cycles is not None:
            cap = interpolate_reference_capacity(user_cycles, curve)
            mx, my = xy(user_cycles, cap)
            color = cycle_health_rating(user_cycles)[1]
            self.create_line(mx, pad_t, mx, pad_t + plot_h, fill=color, dash=(2, 2), tags="chart")
            r = 6
            self.create_oval(mx-r, my-r, mx+r, my+r, fill=color, outline=CARD, width=2, tags="chart")

            raw_note = f" (raw {raw_cycles:.0f})" if raw_cycles is not None and abs(raw_cycles - user_cycles) > 1 else ""
            label = f"You: ~{user_cycles:.0f} cycles{raw_note} → ~{cap:.0f}%"

            # Place the label above the dot if there's room, else below -
            # whichever direction keeps it fully inside the plot area.
            label_above = (my - pad_t) > 24
            label_y = my - 16 if label_above else my + 16
            anchor_v = "s" if label_above else "n"
            label_x = min(max(mx, pad_l + 75), self.w - pad_r - 75)

            text_id = self.create_text(label_x, label_y, text=label, fill=color,
                                        font=("Consolas", 9, "bold"), anchor=anchor_v, tags="chart")
            bbox = self.bbox(text_id)
            if bbox:
                x0, y0, x1, y1 = bbox
                pad = 4
                rect_id = self.create_rectangle(x0 - pad, y0 - pad, x1 + pad, y1 + pad,
                                                 fill=CARD, outline="", tags="chart")
                self.tag_raise(text_id, rect_id)


def make_button(parent, text, command, bg, fg, hover_bg=None):
    btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, font=F_BTN,
                     relief="flat", padx=18, pady=9, bd=0, cursor="hand2",
                     activebackground=hover_bg or bg, activeforeground=fg)
    hb = hover_bg or bg
    btn.bind("<Enter>", lambda e: btn.config(bg=hb))
    btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class CellwatchApp:
    POLL_INTERVAL_SEC = 3

    def __init__(self, root):
        self.root = root
        root.title("CELLWATCH — Battery Intelligence")
        root.geometry("700x700")
        root.configure(bg=BG)
        root.minsize(680, 500)
        root.resizable(True, True)
        self._is_fullscreen = False
        root.bind("<F11>", self._toggle_fullscreen)
        root.bind("<Escape>", self._exit_fullscreen)

        self.serial = None
        self.polling = False
        self.history = deque(maxlen=200)
        self.last_reading_time = None
        self.last_level = None
        self.cycle_estimator = CycleEstimator()

        self._build_ui()
        self._refresh_device_status()

    # -- UI construction --------------------------------------------------
    def _build_ui(self):
        # Wrap everything in a scrollable canvas so the window can be a
        # normal, screen-friendly size while still fitting all the cards -
        # scroll with the mouse wheel or the scrollbar on the right.
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # outer_wrapper sits directly on the canvas; outer is packed inside
        # it purely so we can keep using padx/pady for the page margins.
        outer_wrapper = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=outer_wrapper, anchor="nw")
        outer = tk.Frame(outer_wrapper, bg=BG)
        outer.pack(fill="both", expand=True, padx=22, pady=20)

        def _on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(window_id, width=event.width)

        outer_wrapper.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ---- Header ----
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 4))

        titlewrap = tk.Frame(header, bg=BG)
        titlewrap.pack(side="left")
        tk.Label(titlewrap, text="⚡ CELLWATCH", fg=GOLD, bg=BG, font=F_TITLE).pack(anchor="w")
        tk.Label(titlewrap, text="Battery Intelligence for USB-connected Android devices",
                 fg=MUTED, bg=BG, font=F_SUB).pack(anchor="w")

        self.status_pill = tk.Frame(header, bg=CARD, highlightbackground=CARD_BORDER, highlightthickness=1)
        self.status_pill.pack(side="right", anchor="e", pady=4)
        self.status_dot = tk.Label(self.status_pill, text="●", fg=MUTED, bg=CARD, font=("Segoe UI", 10))
        self.status_dot.pack(side="left", padx=(12, 4), pady=6)
        self.device_label = tk.Label(self.status_pill, text="Checking for device...", fg=TEXT, bg=CARD,
                                      font=("Consolas", 9))
        self.device_label.pack(side="left", padx=(0, 12), pady=6)

        # ---- Buttons ----
        btnrow = tk.Frame(outer, bg=BG)
        btnrow.pack(fill="x", pady=(16, 18))
        self.start_btn = make_button(btnrow, "▶  Start Polling", self.toggle_polling, GOLD, "#1a1204", "#ffbc55")
        self.start_btn.pack(side="left")
        self.rescan_btn = make_button(btnrow, "⟲  Rescan Device", self._refresh_device_status,
                                       CARD, TEXT, "#1c222c")
        self.rescan_btn.pack(side="left", padx=10)

        # ---- Top row: SOC card + Risk gauge card ----
        top_row = tk.Frame(outer, bg=BG)
        top_row.pack(fill="x", pady=(0, 14))

        soc_card = Card(top_row, accent=BLUE)
        soc_card.pack(side="left", fill="both", expand=True, padx=(0, 7))
        soc_inner = soc_card.body
        head = tk.Frame(soc_inner, bg=CARD)
        head.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(head, text="🔋", bg=CARD, font=("Segoe UI Emoji", 14)).pack(side="left")
        tk.Label(head, text="STATE OF CHARGE", fg=MUTED, bg=CARD, font=F_LABEL).pack(side="left", padx=(6, 0))
        self.soc_value = tk.Label(soc_inner, text="—", fg=TEXT, bg=CARD, font=("Consolas", 30, "bold"))
        self.soc_value.pack(anchor="w", padx=16, pady=(2, 6))
        self.soc_bar = ProgressBar(soc_inner, width=260, height=12)
        self.soc_bar.pack(anchor="w", padx=16, pady=(0, 8))
        self.status_value = tk.Label(soc_inner, text="Status: —", fg=MUTED, bg=CARD, font=F_SUB)
        self.status_value.pack(anchor="w", padx=16, pady=(0, 14))

        gauge_card = Card(top_row, accent=GREEN)
        gauge_card.pack(side="left", fill="both", padx=(7, 0))
        g_inner = gauge_card.body
        tk.Label(g_inner, text="🛡  RISK STATUS", fg=MUTED, bg=CARD, font=F_LABEL).pack(anchor="w", padx=16, pady=(14, 0))
        self.risk_gauge = RadialGauge(g_inner, size=150)
        self.risk_gauge.pack(padx=16, pady=(4, 14))

        # ---- Metric tiles row ----
        tiles_row = tk.Frame(outer, bg=BG)
        tiles_row.pack(fill="x", pady=(0, 14))
        self.voltage_tile = MetricTile(tiles_row, "🔌", "VOLTAGE (V)", accent=BLUE)
        self.voltage_tile.pack(side="left", fill="both", expand=True, padx=(0, 7))
        self.temp_tile = MetricTile(tiles_row, "🌡", "TEMPERATURE (°C)", accent=AMBER)
        self.temp_tile.pack(side="left", fill="both", expand=True, padx=7)
        self.cycles_tile = MetricTile(tiles_row, "🔁", "EST. CYCLES", accent=GREEN)
        self.cycles_tile.pack(side="left", fill="both", expand=True, padx=(7, 0))

        # ---- Rate card ----
        rate_card = Card(outer, accent=BLUE)
        rate_card.pack(fill="x", pady=(0, 14))
        tk.Label(rate_card.body, text="⚡ CHARGE / DISCHARGE RATE", fg=MUTED, bg=CARD, font=F_LABEL).pack(
            anchor="w", padx=16, pady=(14, 0))
        self.rate_label = tk.Label(rate_card.body, text="—", fg=BLUE, bg=CARD, font=("Consolas", 16, "bold"))
        self.rate_label.pack(anchor="w", padx=16, pady=(2, 14))

        # ---- Age-based cycle range card ----
        age_card = Card(outer, accent=GOLD)
        age_card.pack(fill="x", pady=(0, 14))
        age_inner = age_card.body
        tk.Label(age_inner, text="📅 ESTIMATED CYCLE RANGE (FROM DEVICE AGE)", fg=MUTED, bg=CARD,
                 font=F_LABEL).pack(anchor="w", padx=16, pady=(14, 8))

        age_input_row = tk.Frame(age_inner, bg=CARD)
        age_input_row.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(age_input_row, text="Manufacture / purchase date:", fg=TEXT, bg=CARD, font=F_SUB).pack(side="left")
        self.date_entry = tk.Entry(age_input_row, font=("Consolas", 10), width=13, bg="#0a0d10", fg=TEXT,
                                    insertbackground=TEXT, relief="flat", highlightthickness=1,
                                    highlightbackground=CARD_BORDER, highlightcolor=BLUE)
        self.date_entry.pack(side="left", padx=8, ipady=4)
        self.date_entry.insert(0, "YYYY-MM-DD")
        make_button(age_input_row, "Set", self.set_mfg_date, BLUE, "#08131c", "#6fb8ea").pack(side="left")

        self.age_range_label = tk.Label(age_inner, text="No date set yet — enter one above.",
                                         fg=TEXT, bg=CARD, font=("Consolas", 15, "bold"))
        self.age_range_label.pack(anchor="w", padx=16)
        self.health_rating_label = tk.Label(age_inner, text="", bg=CARD, font=("Segoe UI", 11, "bold"))
        self.health_rating_label.pack(anchor="w", padx=16, pady=(3, 0))
        tk.Label(age_inner, text="Rough heuristic (~0.7–1.3 cycles/day of typical use) — not a measured value.",
                 fg=MUTED, bg=CARD, font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(4, 14))

        # ---- Cycles vs manufacturer reference capacity curve ----
        ref_card = Card(outer, accent=BLUE)
        ref_card.pack(fill="x", pady=(0, 14))
        ref_inner = ref_card.body
        tk.Label(ref_inner, text="📈 YOUR CYCLES vs MANUFACTURER CAPACITY CURVE", fg=MUTED, bg=CARD,
                 font=F_LABEL).pack(anchor="w", padx=16, pady=(14, 8))

        phone_row = tk.Frame(ref_inner, bg=CARD)
        phone_row.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(phone_row, text="Phone brand:", fg=TEXT, bg=CARD, font=F_SUB).pack(side="left")
        self.manufacturer_var = tk.StringVar(value=DEFAULT_MANUFACTURER)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("CW.TCombobox", fieldbackground="#0a0d10", background=CARD, foreground=TEXT)
        manufacturer_dropdown = ttk.Combobox(phone_row, textvariable=self.manufacturer_var,
                                              values=list(MANUFACTURER_CURVES.keys()), state="readonly",
                                              width=32, style="CW.TCombobox")
        manufacturer_dropdown.pack(side="left", padx=8)
        manufacturer_dropdown.bind("<<ComboboxSelected>>", self.on_manufacturer_change)

        self.ref_chart = ReferenceCurveChart(ref_inner, width=580, height=220)
        self.ref_chart.pack(padx=16, pady=(0, 8))
        self.ref_chart.plot(MANUFACTURER_CURVES[DEFAULT_MANUFACTURER], user_cycles=None)
        self.ref_conclusion_label = tk.Label(ref_inner, text="Set a manufacture/purchase date above to see this.",
                                              fg=MUTED, bg=CARD, font=("Segoe UI", 10, "bold"),
                                              wraplength=560, justify="left")
        self.ref_conclusion_label.pack(anchor="w", padx=16, pady=(0, 6))
        tk.Label(ref_inner, text="Anchor points (the '80% at N cycles' figure) come from each brand's own "
                 "published specs, or independent tech-press reporting on official/EU-label data where a "
                 "brand doesn't publish one directly (Apple, Google: first-party; Samsung, OnePlus, Xiaomi: "
                 "via Android Authority / GSMArena). Curve shape beyond that anchor is this app's estimate. "
                 "Your marker is adjusted for observed operating temperature, per Battery University's "
                 "published heat-vs-cycle-life data (BU-502).",
                 fg=MUTED, bg=CARD, font=("Segoe UI", 8), wraplength=560, justify="left").pack(
            anchor="w", padx=16, pady=(0, 14))

        # ---- Log ----
        tk.Label(outer, text="ACTIVITY LOG", fg=MUTED, bg=BG, font=F_LABEL).pack(anchor="w", pady=(0, 6))
        log_wrap = tk.Frame(outer, bg=CARD, highlightbackground=CARD_BORDER, highlightthickness=1)
        log_wrap.pack(fill="both", expand=True, pady=(0, 10))
        self.log_box = tk.Text(log_wrap, height=6, bg=CARD, fg=MUTED, font=("Consolas", 9),
                                relief="flat", bd=0, padx=10, pady=8)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

        disclaimer = ("Advisory MVP tool. Issues no control commands. Cycle estimates are derived from "
                      "observed usage and device age, not a measured lifetime count. Field verification "
                      "required before any replacement decision.")
        tk.Label(outer, text=disclaimer, fg="#4a5568", bg=BG, font=("Segoe UI", 8),
                 wraplength=620, justify="left").pack(fill="x")

        # Enable the hover "lift" effect now that every card has all its
        # child widgets in place.
        for card in (soc_card, gauge_card, self.voltage_tile, self.temp_tile,
                     self.cycles_tile, rate_card, age_card, ref_card):
            card.enable_hover()

    def _toggle_fullscreen(self, event=None):
        self._is_fullscreen = not self._is_fullscreen
        self.root.attributes("-fullscreen", self._is_fullscreen)

    def _exit_fullscreen(self, event=None):
        if self._is_fullscreen:
            self._is_fullscreen = False
            self.root.attributes("-fullscreen", False)

    def _log(self, msg):
        self.log_box.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # -- Device detection ---------------------------------------------------
    def _refresh_device_status(self):
        serial, err = get_connected_device()
        if err:
            self.serial = None
            self.status_dot.config(fg=RED)
            self.device_label.config(text=err[:44])
            self._log(err)
        else:
            self.serial = serial
            self.status_dot.config(fg=GREEN)
            self.device_label.config(text=f"Connected: {serial}")
            self._log(f"Device ready: {serial}")
            self._prefill_mfg_date()
            existing_mfr = self.cycle_estimator.get_manufacturer(self.serial)
            if existing_mfr and existing_mfr in MANUFACTURER_CURVES:
                self.manufacturer_var.set(existing_mfr)
            self._refresh_age_range()

    def _prefill_mfg_date(self):
        existing = self.cycle_estimator.get_mfg_date(self.serial)
        if existing:
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, existing)

    def set_mfg_date(self):
        if not self.serial:
            messagebox.showwarning("No device", "Connect and detect a device first.")
            return
        date_str = self.date_entry.get().strip()
        try:
            self.cycle_estimator.set_mfg_date(self.serial, date_str)
            self._log(f"Manufacture/purchase date set to {date_str}")
            self._refresh_age_range()
        except ValueError:
            messagebox.showerror("Invalid date", "Please enter the date as YYYY-MM-DD, e.g. 2023-11-15")

    def _refresh_age_range(self):
        if not self.serial:
            return
        manufacturer = self.manufacturer_var.get()
        curve = MANUFACTURER_CURVES.get(manufacturer, MANUFACTURER_CURVES[DEFAULT_MANUFACTURER])
        anchor_cycles = next((c for c, cap in curve if cap == 80), curve[-1][0])

        result = self.cycle_estimator.age_based_range(self.serial)
        if result:
            elapsed_days, low, high = result
            self.age_range_label.config(text=f"{elapsed_days} days old  →  est. {low:.0f}–{high:.0f} cycles")
            self.cycles_tile.set(f"{low:.0f}–{high:.0f}")
            raw_mid = (low + high) / 2

            avg_temp = self._average_observed_temp()
            multiplier = temp_life_multiplier(avg_temp) if avg_temp is not None else 1.0
            effective_mid = raw_mid * multiplier

            label, color = cycle_health_rating(effective_mid)
            self.health_rating_label.config(text=f"●  {label}", fg=color)

            self.ref_chart.plot(curve, user_cycles=effective_mid, raw_cycles=raw_mid)
            ref_cap = interpolate_reference_capacity(effective_mid, curve)

            remaining = anchor_cycles - effective_mid
            if remaining > 0:
                milestone_line = f"~{remaining:.0f} cycles left before typically reaching {manufacturer}'s published 80% mark ({anchor_cycles:.0f} cycles)."
            else:
                milestone_line = f"Already ~{abs(remaining):.0f} cycles past {manufacturer}'s published 80% mark ({anchor_cycles:.0f} cycles) — real-world capacity varies by unit."

            temp_line = ""
            if avg_temp is not None and multiplier > 1.01:
                temp_line = (f"\nRunning ~{multiplier:.2f}× hotter-than-baseline (avg {avg_temp:.0f}°C observed) "
                             f"per Battery University's heat/cycle-life data — effective wear is ahead of raw cycle count.")

            self.ref_conclusion_label.config(
                text=(f"~{effective_mid:.0f} effective cycles ≈ {ref_cap:.0f}% capacity retained · Health band: {label}\n"
                      f"{milestone_line}{temp_line}"),
                fg=color)
        else:
            self.age_range_label.config(text="No date set yet — enter one above.")
            self.health_rating_label.config(text="")
            self.ref_chart.plot(curve, user_cycles=None)
            self.ref_conclusion_label.config(text="Set a manufacture/purchase date above to see this.", fg=MUTED)

    def _average_observed_temp(self):
        temps = [h["temp_c"] for h in self.history if h["temp_c"] is not None]
        if not temps:
            return None
        return sum(temps) / len(temps)


    # -- Polling loop ---------------------------------------------------
    def toggle_polling(self):
        if self.polling:
            self.polling = False
            self.start_btn.config(text="▶  Start Polling")
            self._log("Polling stopped.")
        else:
            if not self.serial:
                messagebox.showwarning("No device", "No authorized device detected. Click 'Rescan Device' after connecting your phone.")
                return
            self.polling = True
            self.start_btn.config(text="■  Stop Polling")
            self._log("Polling started.")
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self):
        while self.polling:
            try:
                data = poll_battery(self.serial)
                self.history.append(data)
                self.root.after(0, self._update_ui, data)
            except Exception as e:
                self.root.after(0, self._log, f"Poll error: {e}")
            time.sleep(self.POLL_INTERVAL_SEC)

    # -- UI update ---------------------------------------------------
    def _update_ui(self, data):
        level = data["level"]
        self.soc_value.config(text=f"{level}%" if level is not None else "N/A")
        if level is not None:
            color = GREEN if level > 40 else (AMBER if level > 15 else RED)
            self.soc_bar.set(level, color)
        self.status_value.config(text=f"Status: {data['status']}")

        self.voltage_tile.set(f'{data["voltage_v"]:.2f}' if data["voltage_v"] else "N/A")
        temp = data["temp_c"]
        self.temp_tile.set(f'{temp:.1f}' if temp is not None else "N/A",
                            RED if (temp is not None and temp > 45) else TEXT)

        est_cycles = self.cycle_estimator.update(self.serial, data["level"])
        # Age-based range (if set) takes priority in the tile; refreshed below.
        self._refresh_age_range()
        if not self.cycle_estimator.get_mfg_date(self.serial):
            self.cycles_tile.set(f"{est_cycles:.2f}")

        if data["current_ma"] is not None:
            direction = "charging" if data["current_ma"] > 0 else "discharging"
            self.rate_label.config(text=f'{abs(data["current_ma"]):.0f} mA ({direction}) — via {data["current_source"]}')
        else:
            self._update_rate_from_soc(data)

        cycles_for_risk = self._current_cycle_estimate()
        risk = compute_risk(list(self.history), cycles=cycles_for_risk)
        if risk:
            score, band, _ = risk
            color = {"HEALTHY": GREEN, "MONITOR CLOSELY": AMBER,
                     "REPLACE SOON": RED, "HIGH RISK": RED}[band]
            self.risk_gauge.set(score, color, band)

    def _current_cycle_estimate(self):
        """Best available cycle estimate for risk scoring: age-based
        midpoint if a date is set, else the usage-based accumulator."""
        if not self.serial:
            return None
        result = self.cycle_estimator.age_based_range(self.serial)
        if result:
            _, low, high = result
            return (low + high) / 2
        return self.cycle_estimator.get(self.serial)

    def on_manufacturer_change(self, event=None):
        if self.serial:
            self.cycle_estimator.set_manufacturer(self.serial, self.manufacturer_var.get())
        self._refresh_age_range()

    def _update_rate_from_soc(self, data):
        now = time.time()
        if self.last_level is not None and data["level"] is not None and self.last_reading_time:
            dt_min = (now - self.last_reading_time) / 60.0
            if dt_min > 0:
                d_soc = data["level"] - self.last_level
                rate_pct_per_min = d_soc / dt_min
                direction = "charging" if rate_pct_per_min > 0 else "discharging" if rate_pct_per_min < 0 else "idle"
                self.rate_label.config(
                    text=f'{abs(rate_pct_per_min):.2f} %/min ({direction}) — estimated from SOC (no current_now on this device)'
                )
        self.last_level = data["level"]
        self.last_reading_time = now


if __name__ == "__main__":
    root = tk.Tk()
    app = CellwatchApp(root)
    root.mainloop()
