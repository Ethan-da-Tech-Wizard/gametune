# Program: gametune - mangohud.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Parses MangoHud frame-time CSV logs into a FrameLog object (frame times, timestamps, temperatures) and trims the warm-up period; summary files are refused.

"""Parser for MangoHud frame-time logs (the CSV files MangoHud writes with logging on).

Layout (MangoHud 0.6 to 0.8):
  line 1: system header names   os,cpu,gpu,ram,kernel,driver,cpuscheduler
  line 2: system header values  Linux...,Intel...,NVIDIA...,15978868,6.x,...,powersave
  line 3: column names          fps,frametime,cpu_load,...,elapsed
  line 4+: one row per frame    frametime is milliseconds, elapsed is NANOSECONDS since logging began.
Older logs have no system header and start at the column-name line. Extra or missing columns are fine;
only `frametime` (or `fps`) is required. Summary files (*_summary.csv) are not frame logs and are refused.
"""
# os is used for file names and paths.
import os


class LogError(ValueError):
    """The file is not a usable MangoHud frame log (message is a plain sentence)."""


class FrameLog(object):
    """Holds one parsed log: file path, system header, frame times (ms), timestamps (s), temperatures and a count of skipped rows."""
    def __init__(self, path, header, frametimes_ms, times_s, gpu_temps, cpu_temps, skipped_rows):
        """Store the parsed values on the object."""
        # Store everything the parser found, so report.py can use it.
        self.path = path
        self.header = header            # dict of system header fields (may be empty)
        self.frametimes_ms = frametimes_ms
        self.times_s = times_s          # elapsed seconds per frame (cumulative frametime if no `elapsed` column)
        self.gpu_temps = gpu_temps
        self.cpu_temps = cpu_temps
        self.skipped_rows = skipped_rows


def _floats(fields):
    """Convert a list of text fields to floats, using None for any field that is not a number."""
    # Convert text fields to numbers; anything that is not a number becomes None.
    out = []
    for f in fields:
        try:
            out.append(float(f))
        except ValueError:
            out.append(None)
    return out


def parse_text(text, path="<text>"):
    """Parse the text of a MangoHud log and return a FrameLog; raises LogError if it is not a usable frame log."""
    # Drop blank lines and surrounding spaces.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise LogError("The log file is empty.")
    # The column-name line is within the first few lines (after the optional 2 system-header lines).
    col_idx = None
    for i, ln in enumerate(lines[:6]):
        # Normalise the names so matching is not case sensitive.
        cols = [c.strip().lower() for c in ln.split(",")]
        # Accept a line that names frametime, or an fps column along with others.
        if "frametime" in cols or ("fps" in cols and len(cols) > 1):
            col_idx = i
            break
    # No column line means this is not a MangoHud frame log.
    if col_idx is None:
        raise LogError("No MangoHud column line (with 'frametime' or 'fps') was found. This does not look like a MangoHud frame log.")
    cols = [c.strip().lower() for c in lines[col_idx].split(",")]
    # A summary file has os/cpu columns but no frametime column.
    if "os" in cols and "cpu" in cols and "frametime" not in cols:
        raise LogError("This looks like a summary file, not a frame log.")
    # Optional system header: two lines above the column line (names, then values).
    header = {}
    if col_idx >= 2:
        names = [c.strip() for c in lines[col_idx - 2].split(",")]
        values = [c.strip() for c in lines[col_idx - 1].split(",")]
        header = dict(zip(names, values))
    # Find which column number holds each value we care about.
    ft_i = cols.index("frametime") if "frametime" in cols else None
    fps_i = cols.index("fps") if "fps" in cols else None
    el_i = cols.index("elapsed") if "elapsed" in cols else None
    gt_i = cols.index("gpu_temp") if "gpu_temp" in cols else None
    ct_i = cols.index("cpu_temp") if "cpu_temp" in cols else None
    # Parallel lists: one entry per good frame.
    frametimes, times, gtemps, ctemps = [], [], [], []
    skipped = 0
    # Fallback clock used when there is no elapsed column.
    cumulative = 0.0
    # Go through every data row after the column line.
    for ln in lines[col_idx + 1:]:
        fields = ln.split(",")
        # Too few fields: row cut off, count it as skipped.
        if len(fields) < len(cols):          # a short (cut-off) row, e.g. the log was killed mid-write
            skipped += 1
            continue
        # Convert fields to numbers.
        vals = _floats(fields)
        ft = vals[ft_i] if ft_i is not None else None
        # No frametime column: derive frame time (ms) from fps.
        if ft is None and fps_i is not None and vals[fps_i]:
            ft = 1000.0 / vals[fps_i] if vals[fps_i] > 0 else None
        # Skip rows with no time, a zero/negative time, or NaN (ft != ft is only true for NaN).
        if ft is None or ft <= 0 or ft != ft:
            skipped += 1
            continue
        # Track total seconds from the frame times.
        cumulative += ft / 1000.0
        # elapsed is in nanoseconds; divide by 1e9 for seconds, else use our own running total.
        t = vals[el_i] / 1e9 if el_i is not None and vals[el_i] is not None else cumulative
        frametimes.append(ft)
        times.append(t)
        # Keep GPU and CPU temperatures when present.
        if gt_i is not None and vals[gt_i] is not None:
            gtemps.append(vals[gt_i])
        if ct_i is not None and vals[ct_i] is not None:
            ctemps.append(vals[ct_i])
    # No usable rows at all.
    if not frametimes:
        raise LogError("The log has a header but no usable frame rows.")
    return FrameLog(path, header, frametimes, times, gtemps, ctemps, skipped)


def parse_file(path):
    """Read a MangoHud log from disk and parse it; raises LogError for summary files or unreadable files."""
    # Names ending in _summary.csv are MangoHud summaries, not frame logs.
    if os.path.basename(path).lower().endswith("_summary.csv"):
        raise LogError("%s is a MangoHud summary file, not a frame log." % os.path.basename(path))
    # errors='replace' stops odd bytes from crashing the read.
    try:
        with open(path, "r", errors="replace") as handle:
            text = handle.read()
    # Turn a file error into our own LogError with a clear message.
    except OSError as exc:
        raise LogError("Cannot read %s (%s)." % (path, exc.strerror or exc))
    return parse_text(text, path)


def drop_warmup(log, warmup_s):
    """Return (frametimes, gpu_temps) after throwing away the first warmup_s seconds of the log."""
    if warmup_s <= 0:
        # No warm-up: return all frame times (a copy).
        return list(log.frametimes_ms)
    # Times are measured from the first row.
    start = log.times_s[0] if log.times_s else 0.0
    cut = 0
    # elapsed is measured from when logging began, so use time relative to the first row
    for i, t in enumerate(log.times_s):
        if t - start >= warmup_s:
            cut = i
            break
    # for-else: the loop never hit break, so the whole log is shorter than the warm-up: nothing left.
    else:
        return []
    # Keep frames from the first one past the warm-up onward.
    return list(log.frametimes_ms[cut:])
