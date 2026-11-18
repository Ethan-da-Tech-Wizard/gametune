# Program: gametune - thermal.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Reads GPU and CPU temperatures from nvidia-smi and Linux sysfs, and provides the thermal guard (evaluate) that says whether heavy work may start, must pause or must stop.

"""Thermal guard shared by selftest and apply.

Default limits (degrees Celsius). They can be made STRICTER by the caller, never looser:
  GPU: heavy work may START only at or below 84, PAUSE at 86, ABORT at 91.
  CPU: start at or below 89, PAUSE at 94, ABORT at 96 (always below 97).
gametune never overclocks, never changes fans or voltages, and never allows 97 C.
"""
# os/re for files and patterns; subprocess to run read-only helper commands.
import os
import re
import subprocess

# Temperature limits in degrees Celsius. A START limit gates beginning; PAUSE and ABORT guard running work.
GPU_START_MAX = 84.0
GPU_PAUSE = 86.0
GPU_ABORT = 91.0
# RESUME values are defined for cooling-down checks.
GPU_RESUME = 80.0
CPU_START_MAX = 89.0
CPU_PAUSE = 94.0
CPU_ABORT = 96.0
CPU_RESUME = 80.0

# Guard states returned by evaluate().
OK = "OK"
TOO_HOT_TO_START = "TOO_HOT_TO_START"
PAUSE = "PAUSE"
ABORT = "ABORT"
UNKNOWN = "UNKNOWN"


class Limits(object):
    """The temperature limits in use. Values passed in can only make a limit stricter, never looser."""
    def __init__(self, gpu_start=GPU_START_MAX, gpu_pause=GPU_PAUSE, gpu_abort=GPU_ABORT,
                 cpu_start=CPU_START_MAX, cpu_pause=CPU_PAUSE, cpu_abort=CPU_ABORT):
        """Store each limit, clamping it to the built-in default so it can never be raised."""
        # Only ever stricter than the defaults.
        self.gpu_start = min(gpu_start, GPU_START_MAX)
        self.gpu_pause = min(gpu_pause, GPU_PAUSE)
        self.gpu_abort = min(gpu_abort, GPU_ABORT)
        self.cpu_start = min(cpu_start, CPU_START_MAX)
        self.cpu_pause = min(cpu_pause, CPU_PAUSE)
        self.cpu_abort = min(cpu_abort, CPU_ABORT)


def evaluate(gpu_c, cpu_c, starting, limits=None):
    """Return (state, plain sentence). gpu_c / cpu_c may be None when unreadable.

    starting=True judges whether heavy work may begin; False judges work already running.
    Unreadable sensors give UNKNOWN: the guard cannot vouch for a machine it cannot read.
    """
    limits = limits or Limits()
    # If neither sensor can be read, the guard cannot vouch for safety.
    if gpu_c is None and cpu_c is None:
        return UNKNOWN, "No temperature sensor could be read, so the thermal guard cannot confirm it is safe."
    # Check the most serious state first: ABORT, then PAUSE, then (when starting) too hot to start.
    if gpu_c is not None and gpu_c >= limits.gpu_abort:
        return ABORT, "GPU is %.0f C, at or above the stop limit of %.0f C. Stop now." % (gpu_c, limits.gpu_abort)
    if cpu_c is not None and cpu_c >= limits.cpu_abort:
        return ABORT, "CPU is %.0f C, at or above the stop limit of %.0f C. Stop now." % (cpu_c, limits.cpu_abort)
    if gpu_c is not None and gpu_c >= limits.gpu_pause:
        return PAUSE, "GPU is %.0f C, at or above the pause limit of %.0f C. Pause and let it cool." % (gpu_c, limits.gpu_pause)
    if cpu_c is not None and cpu_c >= limits.cpu_pause:
        return PAUSE, "CPU is %.0f C, at or above the pause limit of %.0f C. Pause and let it cool." % (cpu_c, limits.cpu_pause)
    # Only when beginning heavy work do we also enforce the lower start limits.
    if starting:
        if gpu_c is not None and gpu_c > limits.gpu_start:
            return TOO_HOT_TO_START, "GPU is %.0f C. Heavy work only starts at %.0f C or lower. Let it cool first." % (gpu_c, limits.gpu_start)
        if cpu_c is not None and cpu_c > limits.cpu_start:
            return TOO_HOT_TO_START, "CPU is %.0f C. Heavy work only starts at %.0f C or lower. Let it cool first." % (cpu_c, limits.cpu_start)
    # Say which sensor was missing but still pass if the other was fine.
    missing = []
    if gpu_c is None:
        missing.append("GPU")
    if cpu_c is None:
        missing.append("CPU")
    note = ""
    if missing:
        note = " (%s temperature could not be read; only the readable one was checked.)" % " and ".join(missing)
    return OK, "Temperatures are within the limits." + note


def _read(path):
    """Read a text file, returning None on any error."""
    # Open the file, return its text; None when unreadable.
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _milli(path):
    """Read a sysfs file holding millidegrees and return degrees C, or None if unreadable or implausible."""
    # sysfs temperatures are in thousandths of a degree.
    text = _read(path)
    if text is None:
        return None
    try:
        # Convert millidegrees to degrees.
        value = float(text) / 1000.0
    except ValueError:
        return None
    # Ignore impossible readings (outside -20 to 150 C).
    return value if -20.0 < value < 150.0 else None


def run_text(args, timeout=5):
    """Run a read-only helper command, return stdout text or None. Never raises."""
    # subprocess.run with a timeout; stderr is thrown away.
    try:
        done = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                              universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        return None
    # Only trust the output when the command succeeded.
    return done.stdout if done.returncode == 0 else None


def read_gpu_temp(root="/", runner=run_text):
    """GPU temperature in C, or None. NVIDIA via nvidia-smi; AMD/Intel via sysfs hwmon."""
    # NVIDIA first: nvidia-smi prints one temperature per line.
    out = runner(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"])
    if out:
        # Collect every GPU's number.
        temps = []
        for line in out.splitlines():
            try:
                temps.append(float(line.strip()))
            except ValueError:
                pass
        if temps:
            # Report the hottest GPU.
            return max(temps)
    # Otherwise look at AMD/Intel GPUs through sysfs.
    temps = []
    drm = os.path.join(root, "sys", "class", "drm")
    try:
        # Pick folders named cardN under /sys/class/drm.
        cards = [c for c in os.listdir(drm) if re.match(r"^card\d+$", c)]
    except OSError:
        cards = []
    for card in cards:
        # Temperature files sit under device/hwmon/<name>/temp1_input.
        hwmon = os.path.join(drm, card, "device", "hwmon")
        try:
            names = os.listdir(hwmon)
        except OSError:
            continue
        for name in names:
            value = _milli(os.path.join(hwmon, name, "temp1_input"))
            if value is not None:
                temps.append(value)
    return max(temps) if temps else None


def read_cpu_temp(root="/"):
    """CPU package temperature in C, or None (coretemp, k10temp, zenpower, cpu_thermal)."""
    # CPU sensors live under /sys/class/hwmon/hwmonN.
    base = os.path.join(root, "sys", "class", "hwmon")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    # `best` is the hottest chip found so far.
    best = None
    for entry in entries:
        folder = os.path.join(base, entry)
        # Only look at known CPU sensor drivers by name.
        if _read(os.path.join(folder, "name")) not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            continue
        # `package` is the package-wide reading, `highest` is the hottest of any core.
        package = None
        highest = None
        try:
            files = sorted(os.listdir(folder))
        except OSError:
            continue
        # Look at files named tempN_input.
        for fname in files:
            match = re.match(r"^temp(\d+)_input$", fname)
            if not match:
                continue
            value = _milli(os.path.join(folder, fname))
            if value is None:
                continue
            # Each tempN_input may have a matching tempN_label.
            label = _read(os.path.join(folder, "temp%s_label" % match.group(1))) or ""
            highest = value if highest is None else max(highest, value)
            # Labels that mean the whole package temperature.
            if label in ("Package id 0", "Tctl", "Tdie"):
                package = value if package is None else max(package, value)
        # Prefer the package value, else the hottest reading.
        chosen = package if package is not None else highest
        if chosen is not None:
            best = chosen if best is None else max(best, chosen)
    return best


def sample(root="/", runner=run_text):
    """Take one temperature sample and return {'gpu_c': ..., 'cpu_c': ...} (None where not readable)."""
    # Return both readings as a dictionary (None means unreadable).
    return {"gpu_c": read_gpu_temp(root, runner), "cpu_c": read_cpu_temp(root)}
