# ==============================================================================
# Program: thermal.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Hardware thermal monitor reading Linux hwmon inputs with safety abort limits.
# ==============================================================================

"""Thermal guard shared by selftest and apply.

Default limits (degrees Celsius). They can be made STRICTER by the caller, never looser:
  GPU: heavy work may START only at or below 84, PAUSE at 86, ABORT at 91.
  CPU: start at or below 89, PAUSE at 94, ABORT at 96 (always below 97).
gametune never overclocks, never changes fans or voltages, and never allows 97 C.
"""

# File role: safety layer. selftest.py calls evaluate() before and during its test scene; cli.py calls it for `thermal` and `apply`; hardware.py
# reuses sample() and run_text(). Temperatures come only from hwmon/nvidia-smi; gametune never changes fans, clocks or voltages.
# Design decision: fail closed. If no sensor can be read the guard answers UNKNOWN, not OK, because it cannot vouch for the machine.
# Known limit: sensor naming varies between laptops and boards; unrecognised hwmon drivers are ignored rather than guessed.

import os
import re
import subprocess

# Threshold constants in degrees Celsius. START_MAX is the hottest a component
# may be when heavy work begins, PAUSE is where running work must pause,
# ABORT is where it must stop, and RESUME is the cool-down level at which a
# paused run may continue (hysteresis, so it does not flap around the limit).
# CPU ABORT stays at 96 so the hard ceiling of 97 C is never reached.
# Gaps are intentional: start (84) < pause (86) < abort (91) gives a 2 C buffer before a run is paused and room to react before the abort.
GPU_START_MAX = 84.0
GPU_PAUSE = 86.0
GPU_ABORT = 91.0
GPU_RESUME = 80.0

# CPU limits are higher because CPUs are rated for hotter operation than GPUs; abort 96 keeps one degree under the hard 97 C ceiling.
CPU_START_MAX = 89.0
CPU_PAUSE = 94.0
CPU_ABORT = 96.0
CPU_RESUME = 80.0

# Guard states returned by evaluate(); callers branch on these strings.
# Plain strings (not an Enum) so they print directly in --json output and compare cleanly in tests.
OK = "OK"
TOO_HOT_TO_START = "TOO_HOT_TO_START"
PAUSE = "PAUSE"
ABORT = "ABORT"
UNKNOWN = "UNKNOWN"


class Limits(object):
    """Temperature limits for the guard. Callers may tighten but never loosen them."""
    def __init__(self, gpu_start=GPU_START_MAX, gpu_pause=GPU_PAUSE, gpu_abort=GPU_ABORT,
                 cpu_start=CPU_START_MAX, cpu_pause=CPU_PAUSE, cpu_abort=CPU_ABORT):
        # min() with the module defaults enforces the "never looser" safety rule even
        # if a caller passes bigger numbers.
        # Only ever stricter than the defaults.
        # Using min() means a caller bug or a user-supplied bigger number cannot weaken the safety margin; tests check this explicitly.
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

    # Default Limits() is used when the caller passes None, so every caller gets the same baseline protection.
    limits = limits or Limits()
    # Fail closed: with no readable sensor we cannot claim the machine is safe.
    if gpu_c is None and cpu_c is None:
        return UNKNOWN, "No temperature sensor could be read, so the thermal guard cannot confirm it is safe."
    # Order matters: ABORT checks come first, then PAUSE, then the start-only
    # check, so the most severe condition always wins.
    # >= (not >): at the limit itself the run must already stop.
    if gpu_c is not None and gpu_c >= limits.gpu_abort:
        return ABORT, "GPU is %.0f C, at or above the stop limit of %.0f C. Stop now." % (gpu_c, limits.gpu_abort)
    if cpu_c is not None and cpu_c >= limits.cpu_abort:
        return ABORT, "CPU is %.0f C, at or above the stop limit of %.0f C. Stop now." % (cpu_c, limits.cpu_abort)
    if gpu_c is not None and gpu_c >= limits.gpu_pause:
        return PAUSE, "GPU is %.0f C, at or above the pause limit of %.0f C. Pause and let it cool." % (gpu_c, limits.gpu_pause)
    if cpu_c is not None and cpu_c >= limits.cpu_pause:
        return PAUSE, "CPU is %.0f C, at or above the pause limit of %.0f C. Pause and let it cool." % (cpu_c, limits.cpu_pause)
    # The lower "start" limits apply only before work begins; a run already in progress is judged by PAUSE/ABORT alone.
    # Start limits use > (strictly above) because exactly 84 C is still allowed, as documented in the module docstring.
    if starting:
        if gpu_c is not None and gpu_c > limits.gpu_start:
            return TOO_HOT_TO_START, "GPU is %.0f C. Heavy work only starts at %.0f C or lower. Let it cool first." % (gpu_c, limits.gpu_start)
        if cpu_c is not None and cpu_c > limits.cpu_start:
            return TOO_HOT_TO_START, "CPU is %.0f C. Heavy work only starts at %.0f C or lower. Let it cool first." % (cpu_c, limits.cpu_start)
    # One sensor missing is tolerated (laptops often expose only one), but the user is told.
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
    """Return the stripped text of a file, or None if it cannot be read."""
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _milli(path):
    """Read a hwmon millidegree value and return degrees C, or None.

    Values outside -20..150 C are treated as sensor garbage and dropped.
    """
    text = _read(path)
    if text is None:
        return None
    try:
        # hwmon reports temperatures in thousandths of a degree Celsius.
        value = float(text) / 1000.0
    except ValueError:
        return None

    # Real chips report values like -128 or 255 when a sensor is broken; the range keeps those from triggering a false abort or hiding a real one.
    return value if -20.0 < value < 150.0 else None


def run_text(args, timeout=5):
    """Run a read-only helper command, return stdout text or None. Never raises."""
    try:
        # universal_newlines is used instead of text= for older Python 3 compatibility.
        # A timeout stops a hung tool such as nvidia-smi on a wedged driver from freezing the guard; stderr is discarded so errors do not spam the user.
        done = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                              universal_newlines=True)
    except (OSError, subprocess.SubprocessError):
        return None

    # A non-zero exit means the tool reported failure, so its output is not trusted.
    return done.stdout if done.returncode == 0 else None


def read_gpu_temp(root="/", runner=run_text):
    """GPU temperature in C, or None. NVIDIA via nvidia-smi; AMD/Intel via sysfs hwmon."""

    # nvidia-smi is tried first because NVIDIA exposes no hwmon temperature for the GPU on most kernels.
    out = runner(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"])
    if out:
        # Multiple GPUs: report the hottest one, since the guard must act on the worst case.
        temps = []
        for line in out.splitlines():
            try:
                temps.append(float(line.strip()))
            except ValueError:
                pass
        if temps:
            return max(temps)
    temps = []
    # `root` lets tests point at a fake filesystem tree instead of the real /sys.
    drm = os.path.join(root, "sys", "class", "drm")
    try:
        # Only top-level "cardN" nodes; entries such as card0-HDMI-A-1 are connectors, not GPUs.
        cards = [c for c in os.listdir(drm) if re.match(r"^card\d+$", c)]
    except OSError:
        cards = []
    for card in cards:

        # amdgpu and i915 put their sensors under the PCI device's hwmon directory.
        hwmon = os.path.join(drm, card, "device", "hwmon")
        try:
            names = os.listdir(hwmon)
        except OSError:
            continue
        for name in names:
            # temp1_input is the primary edge/junction sensor on amdgpu/i915 hwmon nodes.
            value = _milli(os.path.join(hwmon, name, "temp1_input"))
            if value is not None:
                temps.append(value)
    return max(temps) if temps else None


def read_cpu_temp(root="/"):
    """CPU package temperature in C, or None (coretemp, k10temp, zenpower, cpu_thermal)."""
    base = os.path.join(root, "sys", "class", "hwmon")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    # Track the hottest CPU sensor group found across all hwmon devices.
    best = None
    for entry in entries:
        folder = os.path.join(base, entry)
        # Only known CPU sensor drivers; other hwmon devices (NVMe, wifi, battery) must not be mistaken for the CPU.
        # The hwmon "name" file identifies the driver; coretemp = Intel, k10temp/zenpower = AMD, cpu_thermal = ARM boards.
        if _read(os.path.join(folder, "name")) not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            continue
        package = None
        highest = None
        try:
            files = sorted(os.listdir(folder))
        except OSError:
            continue
        for fname in files:
            # Each sensor is tempN_input with an optional tempN_label describing it.
            match = re.match(r"^temp(\d+)_input$", fname)
            if not match:
                continue
            value = _milli(os.path.join(folder, fname))
            if value is None:
                continue

            # Labels distinguish package sensors from individual cores; a core can be much cooler than the package, so the package value is preferred.
            label = _read(os.path.join(folder, "temp%s_label" % match.group(1))) or ""
            highest = value if highest is None else max(highest, value)
            if label in ("Package id 0", "Tctl", "Tdie"):
                package = value if package is None else max(package, value)
        # Prefer the whole-package reading (Intel "Package id 0", AMD "Tctl"/"Tdie"); fall back to the hottest core.
        # Hottest across devices (not first): multi-socket machines must be judged by the worst CPU.
        chosen = package if package is not None else highest
        if chosen is not None:
            best = chosen if best is None else max(best, chosen)
    return best


def sample(root="/", runner=run_text):
    """Take one reading: {"gpu_c": float|None, "cpu_c": float|None}."""
    return {"gpu_c": read_gpu_temp(root, runner), "cpu_c": read_cpu_temp(root)}
