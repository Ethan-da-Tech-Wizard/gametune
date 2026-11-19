# ==============================================================================
# Program: mangohud.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: MangoHud launch option generator and CSV frame-time log parser.
# ==============================================================================

"""Parser for MangoHud frame-time logs (the CSV files MangoHud writes with logging on).

Layout (MangoHud 0.6 to 0.8):
  line 1: system header names   os,cpu,gpu,ram,kernel,driver,cpuscheduler
  line 2: system header values  Linux...,Intel...,NVIDIA...,15978868,6.x,...,powersave
  line 3: column names          fps,frametime,cpu_load,...,elapsed
  line 4+: one row per frame    frametime is milliseconds, elapsed is NANOSECONDS since logging began.
Older logs have no system header and start at the column-name line. Extra or missing columns are fine;
only `frametime` (or `fps`) is required. Summary files (*_summary.csv) are not frame logs and are refused.
"""
import os


class LogError(ValueError):
    """The file is not a usable MangoHud frame log (message is a plain sentence)."""


class FrameLog(object):
    """Parsed contents of one MangoHud frame log (plain data holder)."""
    def __init__(self, path, header, frametimes_ms, times_s, gpu_temps, cpu_temps, skipped_rows):
        self.path = path
        self.header = header            # dict of system header fields (may be empty)
        self.frametimes_ms = frametimes_ms
        self.times_s = times_s          # elapsed seconds per frame (cumulative frametime if no `elapsed` column)
        self.gpu_temps = gpu_temps
        self.cpu_temps = cpu_temps
        self.skipped_rows = skipped_rows


def _floats(fields):
    """Convert strings to floats, using None for any field that is not a number."""
    out = []
    for f in fields:
        try:
            out.append(float(f))
        except ValueError:
            out.append(None)
    return out


def parse_text(text, path="<text>"):
    """Parse MangoHud CSV text into a FrameLog; raise LogError if unusable.

    Rows that are damaged or cut off are counted in `skipped_rows` instead of
    failing the whole file, because logs are often truncated when a game exits.
    """
    # Blank lines are discarded so row positions are stable.
    # Blank lines are discarded so row positions are stable.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise LogError("The log file is empty.")
    # Locate the column-name line. It is line 1 for old logs or line 3 when a
    # two-line system header precedes it, so scan the first few lines.
    col_idx = None
    for i, ln in enumerate(lines[:6]):
        cols = [c.strip().lower() for c in ln.split(",")]
        if "frametime" in cols or ("fps" in cols and len(cols) > 1):
            col_idx = i
            break
    if col_idx is None:
        raise LogError("No MangoHud column line (with 'frametime' or 'fps') was found. This does not look like a MangoHud frame log.")
    # Re-split the chosen line; names are lower-cased so matching ignores case.
    cols = [c.strip().lower() for c in lines[col_idx].split(",")]
    # Summary CSVs start with a header row like "os,cpu,gpu,..." and have no frame data.
    if "os" in cols and "cpu" in cols and "frametime" not in cols:
        raise LogError("This looks like a summary file, not a frame log.")
    # System header (os/cpu/gpu/driver/kernel) sits in the two lines before the columns; used later to detect unfair comparisons.
    header = {}
    if col_idx >= 2:
        names = [c.strip() for c in lines[col_idx - 2].split(",")]
        values = [c.strip() for c in lines[col_idx - 1].split(",")]
        header = dict(zip(names, values))
    # Column positions are looked up by name because MangoHud versions differ in column order and count.
    ft_i = cols.index("frametime") if "frametime" in cols else None
    fps_i = cols.index("fps") if "fps" in cols else None
    el_i = cols.index("elapsed") if "elapsed" in cols else None
    gt_i = cols.index("gpu_temp") if "gpu_temp" in cols else None
    ct_i = cols.index("cpu_temp") if "cpu_temp" in cols else None
    # Parallel output lists: one entry per accepted frame.
    frametimes, times, gtemps, ctemps = [], [], [], []
    # Count of rows rejected (short, non-numeric or non-positive frame time).
    skipped = 0
    cumulative = 0.0
    for ln in lines[col_idx + 1:]:
        fields = ln.split(",")
        if len(fields) < len(cols):          # a short (cut-off) row, e.g. the log was killed mid-write
            skipped += 1
            continue
        vals = _floats(fields)
        ft = vals[ft_i] if ft_i is not None else None
        # Fallback for logs without a frametime column: derive it from instantaneous FPS.
        if ft is None and fps_i is not None and vals[fps_i]:
            ft = 1000.0 / vals[fps_i] if vals[fps_i] > 0 else None
        # `ft != ft` is the NaN test; zero, negative or NaN frame times are corrupt.
        if ft is None or ft <= 0 or ft != ft:
            skipped += 1
            continue
        # Running clock in seconds, used when there is no usable elapsed column.
        cumulative += ft / 1000.0
        # `elapsed` is in nanoseconds; if absent, the running sum of frame times is used as the clock.
        t = vals[el_i] / 1e9 if el_i is not None and vals[el_i] is not None else cumulative
        frametimes.append(ft)
        times.append(t)
        if gt_i is not None and vals[gt_i] is not None:
            gtemps.append(vals[gt_i])
        if ct_i is not None and vals[ct_i] is not None:
            ctemps.append(vals[ct_i])
    # A header with zero usable rows is as useless as no file at all.
    if not frametimes:
        raise LogError("The log has a header but no usable frame rows.")
    return FrameLog(path, header, frametimes, times, gtemps, ctemps, skipped)


def parse_file(path):
    """Read a MangoHud CSV from disk and parse it. Raises LogError on any problem."""
    if os.path.basename(path).lower().endswith("_summary.csv"):
        raise LogError("%s is a MangoHud summary file, not a frame log." % os.path.basename(path))
    try:
        # errors="replace" keeps a stray non-UTF-8 byte from killing the parse.
        with open(path, "r", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise LogError("Cannot read %s (%s)." % (path, exc.strerror or exc))
    return parse_text(text, path)


def drop_warmup(log, warmup_s):
    """Return (frametimes, gpu_temps) after throwing away the first warmup_s seconds of the log."""
    if warmup_s <= 0:
        return list(log.frametimes_ms)
    start = log.times_s[0] if log.times_s else 0.0
    cut = 0
    # elapsed is measured from when logging began, so use time relative to the first row
    for i, t in enumerate(log.times_s):
        if t - start >= warmup_s:
            cut = i
            break
    else:
        return []
    return list(log.frametimes_ms[cut:])
