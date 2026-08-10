the app for the Hackthon_interdisciplinary_ai :)
Used ai :)
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
